"""Model fallback, the silent-failure bug, and the never-trust-a-model-phone rule."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline
from harness import (FakeNet, Suite, gemini_ok, http_error, json_response,
                     openrouter_ok, timeout_error, url_error)

CHAT = """Marcus: where friday
Priya: anywhere but hotpot please
Priya: not hotpot again, we did it twice already
Priya: i can't eat pork
Dan: coming from sha tin
Dan: table for 6, under 300 a head
Marcus: friday 8pm"""

GOOD = {
    "party_size": 6, "when_text": "Friday 8pm", "budget_per_head_hkd": 300,
    "hard": [{"constraint": "no pork", "who": "Priya", "quote": "i can't eat pork"}],
    "soft": [], "vetoed": [{"thing": "hotpot", "times_rejected": 2, "quote": "not hotpot"}],
    "coming_from": [{"who": "Dan", "place": "Sha Tin"}],
    "prefer_cuisines": [], "avoid_cuisines": ["hotpot"], "open_questions": [],
    "summary_line": "6 people, Friday 8pm",
}


def run() -> Suite:
    s = Suite("pipeline", expect_at_least=26)
    os.environ["GEMINI_API_KEY"] = "AQ.test-key-not-real"
    os.environ.pop("OPENROUTER_API_KEY", None)

    # --- the fallback chain -------------------------------------------------
    with FakeNet([http_error(404), http_error(503), gemini_ok(GOOD)]) as net:
        out = pipeline.extract_constraints(CHAT)
    s.eq("404 then 503 then success walks the chain", len(net.requests), 3)
    s.eq("third model's answer is used", out["party_size"], 6)
    s.eq("provider recorded", out["source"], "gemini")
    s.contains("model 1 tried first", net.urls()[0], pipeline.GEMINI_MODELS[0])
    s.contains("model 2 tried second", net.urls()[1], pipeline.GEMINI_MODELS[1])

    with FakeNet([http_error(429), http_error(429), http_error(429), gemini_ok(GOOD)]) as net:
        out = pipeline.extract_constraints(CHAT)
    s.eq("429 'high demand' is retried, not fatal", out["party_size"], 6)
    s.eq("all four models available to the chain", len(net.requests), 4)

    # 400/401/403 mean our request or key is wrong. The next model fails the
    # same way, so the chain must abort rather than burn three more calls.
    with FakeNet([http_error(403), json_response({})], strict=False) as net:
        out = pipeline.extract_constraints(CHAT)
    s.eq("403 aborts the chain instead of retrying", len(net.requests), 1)
    s.eq("403 degrades to keyword extraction", out["source"], "keyword")

    with FakeNet([url_error(), url_error(), timeout_error(), url_error()]) as net:
        out = pipeline.extract_constraints(CHAT)
    s.eq("total network failure falls back to keywords", out["source"], "keyword")
    s.eq("keyword fallback still finds the pork constraint",
         [h["constraint"] for h in out["hard"]], ["no pork"])

    # --- the key goes in a header, never the URL ---------------------------
    with FakeNet([gemini_ok(GOOD)]) as net:
        pipeline.extract_constraints(CHAT)
    s.eq("key sent as x-goog-api-key header", net.header("x-goog-api-key"), "AQ.test-key-not-real")
    s.check("key absent from the URL", "AQ.test-key-not-real" not in net.urls()[0],
            f"leaked into {net.urls()[0]}")
    s.check("no ?key= in the URL", "key=" not in net.urls()[0])
    s.contains("responseMimeType asks for JSON at the API level",
               net.last()["body"], "application/json")

    # An "AQ."-prefixed key is the newer format and still valid. Rejecting one
    # for not looking like AIzaSy would be a self-inflicted outage.
    s.check("AQ. prefixed keys are accepted", net.header("x-goog-api-key").startswith("AQ."))

    # --- the keyless path --------------------------------------------------
    os.environ.pop("GEMINI_API_KEY", None)
    with FakeNet([], strict=True) as net:
        out = pipeline.extract_constraints(CHAT)
    s.eq("no key means no network call at all", len(net.requests), 0)
    s.eq("no key still returns constraints", out["source"], "keyword")
    s.eq("keyword path reads the budget", out["budget_per_head_hkd"], 300)
    s.eq("keyword path counts the veto twice", out["vetoed"][0]["times_rejected"], 2)
    s.eq("keyword path finds the travel origin", out["coming_from"][0]["place"], "Sha Tin")

    # Regression: money and party size must not share one sanity range, or
    # every real HK$300 budget is silently discarded.
    s.eq("budget of 300 survives (party-size cap would have eaten it)",
         pipeline._clean_money(300), 300)
    s.eq("party size of 300 is rejected as a misread", pipeline._clean_int(300), None)
    s.eq("budget of 5 is rejected as not-a-meal", pipeline._clean_money(5), None)

    # --- gotcha 11: str.format() on a prompt containing literal JSON -------
    # This failed SILENTLY into the fallback and served generic output forever.
    # The defence is structural, so it is tested structurally.
    s.check("extract prompt carries literal JSON braces", '{"' in pipeline.EXTRACT_PROMPT)
    s.raises("str.format() on that prompt really does raise KeyError",
             KeyError, pipeline.EXTRACT_PROMPT.format, chat="x")
    s.check("sentinel substitution is used instead",
            pipeline.CHAT_SENTINEL in pipeline.EXTRACT_PROMPT)
    s.contains("sentinel replacement injects the chat",
               pipeline.EXTRACT_PROMPT.replace(pipeline.CHAT_SENTINEL, "MARKER-XYZ"), "MARKER-XYZ")
    s.check("propose prompt also uses sentinels",
            pipeline.CANDIDATES_SENTINEL in pipeline.PROPOSE_PROMPT
            and pipeline.CONSTRAINTS_SENTINEL in pipeline.PROPOSE_PROMPT)

    # --- JSON salvage ------------------------------------------------------
    os.environ["GEMINI_API_KEY"] = "AQ.test-key-not-real"
    fenced = {"candidates": [{"content": {"parts": [
        {"text": "```json\n" + json.dumps(GOOD) + "\n```"}]}}]}
    with FakeNet([json_response(fenced)]):
        out = pipeline.extract_constraints(CHAT)
    s.eq("fenced ```json is salvaged", out["party_size"], 6)

    chatty = {"candidates": [{"content": {"parts": [
        {"text": "Sure! Here you go:\n" + json.dumps(GOOD) + "\nHope that helps."}]}}]}
    with FakeNet([json_response(chatty)]):
        out = pipeline.extract_constraints(CHAT)
    s.eq("prose-wrapped JSON is salvaged", out["party_size"], 6)

    with FakeNet([json_response({"candidates": [{"content": {"parts": [{"text": "nope"}]}}]})], strict=False):
        out = pipeline.extract_constraints(CHAT)
    s.eq("unsalvageable output degrades rather than crashing", out["source"], "keyword")

    # --- OpenRouter is a real second vendor, not decoration ---------------
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    with FakeNet([http_error(404), http_error(404), http_error(404), http_error(404),
                  openrouter_ok(GOOD)]) as net:
        out = pipeline.extract_constraints(CHAT)
    s.eq("gemini exhausted hands off to openrouter", out["source"], "openrouter")
    s.contains("openrouter really was called", net.urls()[-1], "openrouter.ai")
    s.eq("openrouter uses Bearer auth", net.requests[-1]["headers"].get("authorization"), "Bearer sk-or-test")
    os.environ.pop("OPENROUTER_API_KEY", None)

    # --- propose() must actually call the model ---------------------------
    candidates = [
        {"name": "Samsen Wanchai", "phone": "+85228033960", "area": "Wan Chai", "cuisine": "Thai"},
        {"name": "Chom Chom", "phone": "+85228519969", "area": "Central", "cuisine": "Vietnamese"},
        {"name": "No Phone Place", "phone": None, "area": "Central", "cuisine": "Thai"},
    ]
    picks_payload = {"picks": [
        {"name": "Samsen Wanchai", "why": "thai, no pork on the set menu", "satisfies": ["no pork"], "fails": [], "area": "Wan Chai"},
        {"name": "Chom Chom", "why": "easy from sha tin", "satisfies": [], "fails": [], "area": "Central"},
    ], "tradeoff_line": "both clear the hotpot veto"}

    with FakeNet([gemini_ok(picks_payload)]) as net:
        result = pipeline.propose(GOOD, candidates)
    s.eq("propose() really called the model, not the heuristic", result["source"], "gemini")
    s.eq("propose() returned the model's picks", len(result["picks"]), 2)
    s.eq("tradeoff line preserved", result["tradeoff_line"], "both clear the hotpot veto")

    # --- the model is never trusted with a phone number -------------------
    poisoned = {"picks": [
        {"name": "Samsen Wanchai", "phone": "+85299999999", "why": "x", "satisfies": [], "fails": []},
        {"name": "Totally Invented Bistro", "phone": "+85211112222", "why": "y", "satisfies": [], "fails": []},
    ], "tradeoff_line": ""}
    with FakeNet([gemini_ok(poisoned)]):
        result = pipeline.propose(GOOD, candidates)
    s.eq("hallucinated venue dropped entirely", len(result["picks"]), 1)
    s.eq("kept pick is the real one", result["picks"][0]["name"], "Samsen Wanchai")
    s.eq("model-supplied phone number discarded in favour of OSM's",
         result["picks"][0]["phone"], "+85228033960")
    s.check("the invented number appears nowhere in the output",
            "+85299999999" not in json.dumps(result) and "+85211112222" not in json.dumps(result))

    with FakeNet([gemini_ok({"picks": [], "tradeoff_line": ""})], strict=False):
        result = pipeline.propose(GOOD, candidates)
    s.eq("empty picks fall through to the heuristic", result["source"], "heuristic")
    s.check("heuristic still ranks callable places first", bool(result["picks"][0]["phone"]))

    s.eq("no candidates means no model call and an honest empty result",
         pipeline.propose(GOOD, [])["picks"], [])

    # --- render_constraints shows its evidence ---------------------------
    rendered = pipeline.render_constraints(GOOD)
    s.contains("rendered output quotes the source line", rendered, "i can't eat pork")
    s.contains("rendered output names who said it", rendered, "Priya")
    s.contains("rendered output shows the repeat-rejection count", rendered, "2×")
    s.contains("rendered output states the travel origin", rendered, "Sha Tin")

    os.environ.pop("GEMINI_API_KEY", None)
    return s
