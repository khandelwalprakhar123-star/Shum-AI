"""Model fallback, the silent-failure bug, and the never-trust-a-model-phone rule."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pipeline
from harness import (FakeNet, Suite, gemini_ok, gemini_truncated, http_error,
                     json_response, openrouter_ok, timeout_error, unescaped,
                     url_error)

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
    s = Suite("pipeline", expect_at_least=92)
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

    # --- MAX_TOKENS: the bug that reported success and served worse output --
    # gemini-3.5-flash spent 1,962 of 2,048 tokens thinking and had 71 left for
    # the answer. The truncated JSON failed to parse, the pipeline fell silently
    # into keyword extraction, and the log said "gemini ok".
    with FakeNet([gemini_truncated(), gemini_ok(GOOD)]) as net:
        out = pipeline.extract_constraints(CHAT)
    s.eq("a truncated MAX_TOKENS response moves to the next model", len(net.requests), 2)
    s.eq("and the next model's answer is used", out["party_size"], 6)
    s.eq("a truncated response is never treated as a success", out["source"], "gemini")

    with FakeNet([gemini_truncated()] * 4, strict=False) as net:
        out = pipeline.extract_constraints(CHAT)
    s.eq("every model truncating degrades honestly to keywords", out["source"], "keyword")

    # The config that prevents it.
    with FakeNet([gemini_ok(GOOD)]) as net:
        pipeline.extract_constraints(CHAT)
    body = json.loads(net.last()["body"])
    s.eq("thinking is disabled so the budget goes to the answer",
         body["generationConfig"]["thinkingConfig"]["thinkingBudget"], 0)
    s.check("the output budget has real headroom",
            body["generationConfig"]["maxOutputTokens"] >= 4096,
            f"got {body['generationConfig']['maxOutputTokens']}")

    # A model that rejects thinkingConfig must not abort the chain over a
    # config key. Same model, retried without it.
    with FakeNet([http_error(400), gemini_ok(GOOD)]) as net:
        out = pipeline.extract_constraints(CHAT)
    s.eq("a 400 on thinkingConfig retries the SAME model without it", len(net.requests), 2)
    s.contains("the retry really is the same model", net.urls()[1], pipeline.GEMINI_MODELS[0])
    s.check("the retry drops thinkingConfig",
            "thinkingConfig" not in json.loads(net.requests[1]["body"])["generationConfig"])
    s.eq("and the retry's answer is used", out["party_size"], 6)

    # But a genuine 400 on both attempts is a real fault, not a config quirk.
    with FakeNet([http_error(400), http_error(400)], strict=False) as net:
        out = pipeline.extract_constraints(CHAT)
    s.eq("a 400 on both attempts aborts rather than trying every model",
         len(net.requests), 2)
    s.eq("and degrades honestly", out["source"], "keyword")

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

    # --- str.format() on a prompt containing literal JSON ------------------
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

    # The fallback vendor's model ids must be real ones. The original defaults
    # here were plausible-looking names that all 404'd, making the second
    # vendor decorative.
    s.check("openrouter models are namespaced ids",
            all("/" in m for m in pipeline.OPENROUTER_MODELS))
    s.check("openrouter models are free-tier tagged",
            all(m.endswith(":free") for m in pipeline.OPENROUTER_MODELS),
            f"got {pipeline.OPENROUTER_MODELS}")
    s.check("no music or domain-specific models in the text chain",
            not any(bad in m for m in pipeline.OPENROUTER_MODELS
                    for bad in ("lyria", "-sante", "-fin", "-vl")))

    # --- JSON salvage, against the shapes models actually emit -------------
    # The expensive one: gemini-3.5-flash returns a complete, correct object
    # and then appends a STRAY EXTRA CLOSING BRACE. json.loads rejects the lot
    # with "Extra data", and a greedy brace regex matches THROUGH the stray
    # brace to the last one, so both salvage paths failed. Extraction silently
    # fell through to the slower vendor and the only symptom was a 53s /decide.
    salvage = {
        "clean object": ('{"a": 1}', {"a": 1}),
        "stray trailing brace": ('{"a": 1, "b": {"c": 2}}\n}', {"a": 1, "b": {"c": 2}}),
        "two stray braces": ('{"a": 1}\n}\n}', {"a": 1}),
        "fenced json": ('```json\n{"a": 1}\n```', {"a": 1}),
        "fenced with a stray brace inside": ('```json\n{"a": 1}\n}\n```', {"a": 1}),
        "prose either side": ('Sure!\n{"a": 1}\nHope that helps.', {"a": 1}),
        "a brace inside a string value": ('{"a": "has } brace"}\n}', {"a": "has } brace"}),
        "leading fragment then a good object": ('"oops": 1}\n{"a": 1}', {"a": 1}),
        "trailing punctuation": ('{"a": 1},,,', {"a": 1}),
    }
    for label, (raw, want) in salvage.items():
        s.eq(f"salvage: {label}", pipeline._parse_json(raw), want)

    for label, raw in {"no braces": "nothing here", "empty string": "",
                       "only closing braces": "}}}"}.items():
        s.raises(f"salvage refuses {label}", ValueError, pipeline._parse_json, raw)

    # End to end through the real code path, not just the parser.
    with FakeNet([json_response({"candidates": [{"content": {"parts": [
            {"text": json.dumps(GOOD) + "\n}"}]}, "finishReason": "STOP"}]})]) as net:
        out = pipeline.extract_constraints(CHAT)
    s.eq("a stray brace no longer costs us the model", out["source"], "gemini")
    s.eq("and the constraints survive intact", out["party_size"], 6)
    s.eq("one call, no fallback to a second vendor", len(net.requests), 1)

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

    # --- dedupe and phrasing ---------------------------------------------
    # Seen live: "prefers Wants pizza" printed TWICE in one constraint list.
    # Two bugs in one line -- the model restated the same preference from two
    # messages, and the renderer blindly prefixed "prefers" to whatever text it
    # was given. A duplicated, ungrammatical constraint list reads as an agent
    # that cannot read, which is the one impression this project cannot afford.
    messy = {
        "party_size": None, "when_text": None, "budget_per_head_hkd": 100,
        "hard": [{"constraint": "Does not eat pork", "who": "Prakhar", "quote": "q"},
                 {"constraint": "Does not eat pork", "who": "Prakhar", "quote": "dup"}],
        "soft": [{"constraint": "Wants pizza", "who": "", "quote": ""},
                 {"constraint": "Wants pizza", "who": "", "quote": ""},
                 {"constraint": "would like Thai", "who": "", "quote": ""},
                 {"constraint": "prefers somewhere quiet", "who": "", "quote": ""}],
        "vetoed": [{"thing": "hotpot", "times_rejected": 2, "quote": "q"},
                   {"thing": "Hotpot", "times_rejected": 1, "quote": ""}],
        "coming_from": [], "prefer_cuisines": ["thai", "thai"], "avoid_cuisines": [],
        "open_questions": ["What time?", "What time?"], "summary_line": "x",
    }
    tidy = pipeline._normalise_constraints(messy, "gemini")
    s.eq("a repeated hard constraint appears once", len(tidy["hard"]), 1)
    s.eq("a repeated soft constraint appears once",
         [x["constraint"] for x in tidy["soft"]].count("pizza"), 1)
    s.eq("the same veto twice is one entry", len(tidy["vetoed"]), 1)
    s.eq("and keeps the higher rejection count", tidy["vetoed"][0]["times_rejected"], 2)
    s.eq("duplicate cuisines collapse", tidy["prefer_cuisines"], ["thai"])
    s.eq("duplicate open questions collapse", tidy["open_questions"], ["What time?"])

    softs = [x["constraint"] for x in tidy["soft"]]
    s.contains("'Wants pizza' becomes 'pizza'", softs, "pizza")
    s.check("so the renderer never prints 'prefers Wants'",
            "prefers Wants" not in pipeline.render_constraints(tidy))
    s.contains("'would like Thai' keeps the proper noun's capital", softs, "Thai")
    s.contains("'prefers somewhere quiet' loses only the verb", softs, "somewhere quiet")
    s.eq("a hard constraint is NOT reworded", tidy["hard"][0]["constraint"], "Does not eat pork")
    s.eq("whitespace is collapsed", pipeline._tidy_constraint("wants   big   table", "soft"), "big table")

    # --- poll ordering ----------------------------------------------------
    # Observed live: the model returned three picks, the FIRST of which openly
    # declared fails=["vegetarian"] while the other two satisfied everything --
    # and the group voted for the first one 2-0 because it was at the top. A
    # poll is read top-down, so its order is part of the recommendation.
    live_constraints = {
        "hard": [{"constraint": "No pork"}, {"constraint": "No beef"},
                 {"constraint": "vegetarian"}],
        "vetoed": [{"thing": "hotpot"}],
    }
    live_picks = [
        {"name": "Coffee Shop", "phone": "+85221111111",
         "satisfies": ["No pork", "No beef"], "fails": ["vegetarian"]},
        {"name": "Man Mo Dim Sum", "phone": "+85222222222",
         "satisfies": ["No pork", "No beef", "vegetarian"], "fails": []},
        {"name": "La Creperie", "phone": "+85223333333",
         "satisfies": ["No pork", "No beef", "vegetarian"], "fails": []},
    ]
    ordered = pipeline._order_picks(live_picks, live_constraints)
    s.eq("a pick that fails a hard constraint sinks to last",
         ordered[-1]["name"], "Coffee Shop")
    s.check("the viable options come first",
            {ordered[0]["name"], ordered[1]["name"]} == {"Man Mo Dim Sum", "La Creperie"})
    s.eq("no pick is dropped - three imperfect options beat two and a gap",
         len(ordered), 3)

    s.eq("a pick failing a VETO also sinks",
         pipeline._order_picks([
             {"name": "Hotpot Place", "phone": "+85221111111", "satisfies": [], "fails": ["hotpot"]},
             {"name": "Fine", "phone": "+85222222222", "satisfies": ["No pork"], "fails": []},
         ], live_constraints)[-1]["name"], "Hotpot Place")

    s.eq("failing something NOT stated as hard does not sink a pick",
         pipeline._order_picks([
             {"name": "Nearly", "phone": "+85221111111", "satisfies": ["No pork"], "fails": ["outdoor seating"]},
             {"name": "NoPhone", "phone": None, "satisfies": [], "fails": []},
         ], live_constraints)[0]["name"], "Nearly")

    s.eq("at equal quality, callable wins",
         pipeline._order_picks([
             {"name": "Uncallable", "phone": None, "satisfies": ["No pork"], "fails": []},
             {"name": "Callable", "phone": "+85221111111", "satisfies": ["No pork"], "fails": []},
         ], live_constraints)[0]["name"], "Callable")

    s.eq("ordering an empty list is safe", pipeline._order_picks([], live_constraints), [])
    s.eq("ordering with no constraints is safe",
         len(pipeline._order_picks(live_picks, {})), 3)

    # propose() must apply the ordering, not just expose the helper.
    with FakeNet([gemini_ok({"picks": [
            {"name": "Coffee Shop", "why": "x", "satisfies": ["No pork"], "fails": ["vegetarian"]},
            {"name": "Man Mo Dim Sum", "why": "y", "satisfies": ["No pork", "vegetarian"], "fails": []},
        ], "tradeoff_line": ""})]):
        result = pipeline.propose(live_constraints, [
            {"name": "Coffee Shop", "phone": "+85221111111", "area": "Central", "cuisine": "cafe"},
            {"name": "Man Mo Dim Sum", "phone": "+85222222222", "area": "Sheung Wan", "cuisine": "dim sum"},
        ])
    s.eq("propose() puts the viable pick first", result["picks"][0]["name"], "Man Mo Dim Sum")

    # The root cause: a stated diet was filed as a soft preference, which is
    # what let a pick that fails it through at all.
    s.check("the prompt states a diet is ALWAYS hard, never soft",
            "ALWAYS hard" in pipeline.EXTRACT_PROMPT)
    s.contains("and gives the exact phrasings that fooled it",
               pipeline.EXTRACT_PROMPT, "im vegetarian")
    s.contains("and says what SOFT is actually for", pipeline.EXTRACT_PROMPT, "genuine wants")

    # --- a booking time has to be a real time ----------------------------
    # "this evening" is TRUTHY, so it sailed through the `if not when_text`
    # guard and would have had the agent asking a restaurant to hold a table
    # at an hour it never named. Refusing to invent a time was the first fix;
    # refusing to accept a vague one is this fix.
    for good in ["today at 2 pm", "friday at 8pm", "8:30pm", "20:30", "sat 7pm",
                 "eight o'clock", "8 o'clock", "noon", "tomorrow at 19:45"]:
        s.check(f"bookable: {good!r}", pipeline.time_is_bookable(good)[0])
    for bad in ["this evening", "tonight", "later", "lunchtime", "after work",
                "friday", "tomorrow", "soon", "", "dinner time"]:
        ok, why = pipeline.time_is_bookable(bad)
        s.check(f"NOT bookable: {bad!r}", not ok)
        s.check(f"and it says why for {bad!r}", bool(why))

    s.contains("a vague word is called out as not-a-time",
               pipeline.time_is_bookable("this evening")[1], "hold a table")
    s.contains("a bare day is called out as having no clock time",
               pipeline.time_is_bookable("friday")[1], "no clock time")

    # The answer parser must drop a vague reply rather than bank it.
    s.eq("a vague reply is not accepted as the time",
         pipeline.keyword_answer("this evening", ["when_text"])["when_text"], None)
    s.eq("nor is a bare day",
         pipeline.keyword_answer("friday", ["when_text"])["when_text"], None)
    s.eq("an exact reply still lands",
         pipeline.keyword_answer("friday at 8pm", ["when_text"])["when_text"], "friday at 8pm")

    # And the extractor is told not to volunteer one.
    s.contains("the prompt forbids a vague when_text", pipeline.EXTRACT_PROMPT, "CLOCK TIME")
    s.contains("naming the exact words that fooled it", pipeline.EXTRACT_PROMPT, "this evening")

    # --- HTML escaping --------------------------------------------------
    # render_constraints is the single most exposed message in the project
    # BECAUSE it quotes the chat verbatim -- that is the whole point of it. One
    # "&" in what somebody typed and Telegram rejects the entire message with
    # 400 can't parse entities, Telegram.call returns None, and the constraint
    # list never arrives. Nothing raises.
    nasty = pipeline._normalise_constraints({
        "party_size": 2, "when_text": "2pm & later", "budget_per_head_hkd": 100,
        "hard": [{"constraint": "no pork & no beef", "who": "A<b>",
                  "quote": "2 < 3 of us & i can't eat pork"}],
        "soft": [{"constraint": "Fish & Chips", "who": "", "quote": ""}],
        "vetoed": [{"thing": "R&B Tea", "times_rejected": 2, "quote": "no & never"}],
        "coming_from": [{"who": "D&D", "place": "Sha Tin & around"}],
        "prefer_cuisines": [], "avoid_cuisines": [],
        "open_questions": ["what time & where?"], "summary_line": "x & y",
    }, "gemini")
    rendered = pipeline.render_constraints(nasty)
    s.eq("the constraint list survives ampersands and angle brackets",
         unescaped(rendered), [])
    s.contains("the ampersand is escaped, not dropped", rendered, "&amp;")
    s.contains("and so is the angle bracket", rendered, "&lt;")
    s.contains("our own bold tags still work", rendered, "<b>")
    s.check("the quote is still shown", "can't eat pork" in rendered)

    for label, constraints in [
        ("an empty extraction", pipeline._empty_constraints("nothing")),
        ("a keyword pass", pipeline.keyword_constraints("A & B: not hotpot & no pork")),
    ]:
        s.eq(f"{label} renders cleanly too",
             unescaped(pipeline.render_constraints(constraints)), [])

    # --- render_constraints shows its evidence ---------------------------
    rendered = pipeline.render_constraints(GOOD)
    s.contains("rendered output quotes the source line", rendered, "i can't eat pork")
    s.contains("rendered output names who said it", rendered, "Priya")
    s.contains("rendered output shows the repeat-rejection count", rendered, "2×")
    s.contains("rendered output states the travel origin", rendered, "Sha Tin")

    os.environ.pop("GEMINI_API_KEY", None)
    return s
