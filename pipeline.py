#!/usr/bin/env python3
"""pipeline.py — read the argument, name the constraints, pick three places.

This module is the product. The phone call is the spectacle, but the reason a
coordinator beats a recommender is that it has read the last two hundred
messages and a form never will. Priya's "I can't do pork" from three weeks ago,
Marcus commuting in from Sha Tin, the hotpot the group vetoed twice — none of
that would ever be typed into a search box. It exists only in the chat.

So extract_constraints() returns evidence, not just values. Every constraint
carries the line it came from and who said it, because a coordinator that
cannot show its work is just a recommender with extra steps, and because the
one thing a human must be able to do is correct it.

Model strategy: a chain, never a pin. Gemini availability moved twice in
twenty-four hours during the build (2.0-flash retired and 404ing, 2.5-flash
closed to new users, flash-latest throwing "high demand" 429s). A pinned model
is a demo that works in rehearsal and dies on stage.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from envlite import env, env_list

# ---------------------------------------------------------------------------
# Model chain. Order is verified-working-first, 11 Sep 2026.
# ---------------------------------------------------------------------------
GEMINI_MODELS = env_list("GEMINI_MODELS") or [
    "gemini-3.5-flash",
    "gemini-flash-latest",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
]
# Verified present on OpenRouter's free tier, 12 Sep 2026, by querying
# /api/v1/models and filtering to zero prompt AND completion pricing. The
# previous defaults here were plausible-looking model names that 404'd, which
# made the second vendor decorative -- exactly the failure this chain exists to
# prevent. Override with OPENROUTER_MODELS if these age out too.
OPENROUTER_MODELS = env_list("OPENROUTER_MODELS") or [
    "nvidia/nemotron-3.5-lightning:free",
    "nex-agi/nex-n2.5-mini:free",
    "thinkingmachines/inkling-small:free",
]
GEMINI_HOST = "https://generativelanguage.googleapis.com/v1beta/models"

# 2048 was not enough and failed in the worst possible way. On the real
# extraction prompt, gemini-3.5-flash spent 1,962 of 2,048 tokens THINKING and
# had 71 left for the answer: finishReason MAX_TOKENS, truncated JSON, and a
# silent fall through to keyword extraction. The pipeline reported success
# ("gemini ok via gemini-3.5-flash") and then served visibly worse output --
# it missed the party size, the budget and the cuisine preference.
#
# Measured on the real prompt:
#   2048, thinking auto -> MAX_TOKENS, 1962 thought tokens, invalid JSON
#   8192, thinking auto -> STOP, 3243 thought tokens, valid but slow
#   2048, thinkingBudget 0 -> STOP, 366 answer tokens, valid, ~4x faster
#
# So: thinking off, and a budget with real headroom for a 200-message history.
# Extraction is a reading task, not a reasoning task; the thinking bought
# nothing here except the bug.
MAX_OUTPUT_TOKENS = 4096
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT = 25

# Retry the NEXT model on these. 404 = retired, 429 = quota/"high demand",
# 5xx = their problem. 400/401/403 mean our request or key is wrong and the
# next model will fail identically, so those abort the chain.
RETRY_STATUSES = {404, 408, 429, 500, 502, 503, 504}

# Prompts contain literal JSON. NEVER str.format() them: Python reads
# {"picks": ...} as a format placeholder and raises KeyError: '"picks"'. The
# call then fails SILENTLY into the fallback and you serve generic output
# forever with no error in the log. This was the most expensive bug of the
# build, and the defence is structural — sentinels and .replace(), everywhere.
CHAT_SENTINEL = "@@CHAT_HISTORY@@"
CANDIDATES_SENTINEL = "@@CANDIDATES@@"
CONSTRAINTS_SENTINEL = "@@CONSTRAINTS@@"
TRANSCRIPT_SENTINEL = "@@TRANSCRIPT@@"


class ModelUnavailable(RuntimeError):
    """Every provider in the chain refused."""


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _post_json(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gemini_payload(prompt: str, disable_thinking: bool) -> dict:
    config = {
        # Ask for JSON at the API level rather than begging in the prompt.
        # Removes the whole class of "model wrapped it in ```json" failures.
        "responseMimeType": "application/json",
        "temperature": 0.2,
        "maxOutputTokens": MAX_OUTPUT_TOKENS,
    }
    if disable_thinking:
        config["thinkingConfig"] = {"thinkingBudget": 0}
    return {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": config}


def _call_gemini(prompt: str) -> str:
    """Try each Gemini model in turn. Returns raw JSON text.

    Each model gets up to two attempts: once with thinking disabled, and if the
    API rejects that config with a 400, once without it. Older and lighter
    models in the chain may not accept thinkingConfig at all, and a 400 would
    otherwise abort the entire chain over a config key rather than a real fault.
    """
    key = env("GEMINI_API_KEY")
    if not key:
        raise ModelUnavailable("no GEMINI_API_KEY")

    headers = {"x-goog-api-key": key}  # header, never ?key= in the URL
    last = "no models attempted"

    for model in GEMINI_MODELS:
        url = f"{GEMINI_HOST}/{model}:generateContent"

        for disable_thinking in (True, False):
            try:
                data = _post_json(url, _gemini_payload(prompt, disable_thinking), headers)
            except urllib.error.HTTPError as exc:
                if exc.code == 400 and disable_thinking:
                    last = f"{model}: rejected thinkingConfig, retrying without it"
                    print(f"[pipeline] {last}")
                    continue
                last = f"{model}: HTTP {exc.code}"
                print(f"[pipeline] {last}")
                if exc.code not in RETRY_STATUSES:
                    # Our request or key is wrong; the next model fails the
                    # same way, so stop rather than burn three more calls.
                    raise ModelUnavailable(f"gemini chain aborted ({last})") from exc
                break
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last = f"{model}: {type(exc).__name__}"
                print(f"[pipeline] {last}")
                break

            candidate = (data.get("candidates") or [{}])[0]
            finish = candidate.get("finishReason")
            parts = (candidate.get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()

            if finish == "MAX_TOKENS":
                # Name it precisely. "no JSON found in model output" sent us
                # looking at the parser for something that was a budget problem.
                thoughts = (data.get("usageMetadata") or {}).get("thoughtsTokenCount", 0)
                last = (f"{model}: truncated at MAX_TOKENS "
                        f"({thoughts} tokens went to thinking) \u2014 raise MAX_OUTPUT_TOKENS")
                print(f"[pipeline] {last}")
                break

            if text:
                print(f"[pipeline] gemini ok via {model}"
                      f"{'' if disable_thinking else ' (thinking enabled)'}")
                return text

            last = f"{model}: empty response (finishReason={finish})"
            print(f"[pipeline] {last}")
            break

    raise ModelUnavailable(f"gemini chain exhausted ({last})")


def _call_openrouter(prompt: str) -> str:
    """A different vendor, so a different outage. Deliberately not a Google model."""
    key = env("OPENROUTER_API_KEY")
    if not key:
        raise ModelUnavailable("no OPENROUTER_API_KEY")

    last = "no models attempted"
    for model in OPENROUTER_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        try:
            data = _post_json(
                OPENROUTER_URL,
                payload,
                {"Authorization": f"Bearer {key}", "X-Title": "Shum-AI"},
            )
            text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
            if text:
                print(f"[pipeline] openrouter ok via {model}")
                return text
            last = f"{model}: empty response"
        except urllib.error.HTTPError as exc:
            last = f"{model}: HTTP {exc.code}"
            if exc.code not in RETRY_STATUSES:
                break
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last = f"{model}: {type(exc).__name__}"
    raise ModelUnavailable(f"openrouter chain exhausted ({last})")


def _parse_json(text: str) -> dict:
    """Get a dict out of a model response, tolerating the real ways they break.

    The one that cost us a live demo: gemini-3.5-flash returns a complete,
    correct JSON object and then appends a STRAY EXTRA CLOSING BRACE.
    json.loads() rejects the whole thing with "Extra data: line 42 column 1",
    and a greedy `\{.*\}` regex is no help either because it happily matches
    through the stray brace to the last one in the string. Both salvage paths
    failed, extraction fell through to the slower fallback vendor, and the only
    symptom was a 53-second /decide.

    raw_decode is the right tool: it parses the first complete JSON value and
    simply stops, ignoring whatever trails it.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty model output")

    decoder = json.JSONDecoder()

    # 1. Clean JSON, the common case.
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        pass

    # 2. Valid JSON with trailing junk — a stray brace, a closing fence, prose.
    start = text.find("{")
    if start >= 0:
        try:
            parsed, _ = decoder.raw_decode(text[start:])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # 3. A ```json fence, with the same trailing-junk tolerance inside it.
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        body = fenced.group(1).strip()
        inner = body.find("{")
        if inner >= 0:
            try:
                parsed, _ = decoder.raw_decode(body[inner:])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

    # 4. Last resort: walk braces to find a balanced object. Handles a leading
    #    fragment followed by a good object, which raw_decode from the FIRST
    #    brace would choke on.
    depth = 0
    opened = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                opened = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and opened >= 0:
                try:
                    parsed = json.loads(text[opened:index + 1])
                    if isinstance(parsed, dict):
                        return parsed
                except json.JSONDecodeError:
                    pass
                opened = -1
            if depth < 0:
                depth = 0

    raise ValueError("no JSON found in model output")


def _generate(prompt: str) -> tuple[dict, str]:
    """Gemini chain, then OpenRouter. Returns (parsed, provider_label)."""
    for label, fn in (("gemini", _call_gemini), ("openrouter", _call_openrouter)):
        try:
            return _parse_json(fn(prompt)), label
        except (ModelUnavailable, ValueError) as exc:
            print(f"[pipeline] {label} unusable: {exc}")
    raise ModelUnavailable("all providers exhausted")


# ---------------------------------------------------------------------------
# 1. Constraint extraction
# ---------------------------------------------------------------------------

EXTRACT_PROMPT = """You are reading a real group chat between friends trying to decide where to eat in Hong Kong.
Your job is NOT to recommend anywhere. It is to surface the constraints the group has already stated but never
collected in one place, including ones stated weeks ago and never repeated.

Rules:
- Only record what someone actually said. Never infer a dietary rule from an ethnicity, a name, or a guess.
- Every constraint must carry the quote it came from. If you cannot quote it, do not record it.
- Distinguish HARD (someone cannot or will not) from SOFT (someone would prefer).
- A dish rejected two or more separate times is a veto, even if nobody used the word.
- "coming from X" is a travel constraint, not a location preference. Record the origin, not a district to search.
- If the group never said how many people or when, leave those null. Do not invent them.

Return ONLY this JSON shape:
{
  "party_size": null,
  "when_text": null,
  "budget_per_head_hkd": null,
  "hard": [{"constraint": "", "who": "", "quote": ""}],
  "soft": [{"constraint": "", "who": "", "quote": ""}],
  "vetoed": [{"thing": "", "times_rejected": 0, "quote": ""}],
  "coming_from": [{"who": "", "place": ""}],
  "prefer_cuisines": [],
  "avoid_cuisines": [],
  "open_questions": [],
  "summary_line": ""
}

CHAT:
@@CHAT_HISTORY@@
"""


def extract_constraints(chat_text: str) -> dict:
    """Read the conversation. Return constraints with the evidence attached."""
    chat_text = (chat_text or "").strip()
    if not chat_text:
        return _empty_constraints("no chat history yet")

    prompt = EXTRACT_PROMPT.replace(CHAT_SENTINEL, chat_text[-14000:])
    try:
        parsed, provider = _generate(prompt)
    except ModelUnavailable:
        print("[pipeline] falling back to keyword extraction (no model reachable)")
        return keyword_constraints(chat_text)

    return _normalise_constraints(parsed, provider)


def _empty_constraints(note: str) -> dict:
    return {
        "party_size": None, "when_text": None, "budget_per_head_hkd": None,
        "hard": [], "soft": [], "vetoed": [], "coming_from": [],
        "prefer_cuisines": [], "avoid_cuisines": [], "open_questions": [],
        "summary_line": note, "source": "empty",
    }


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if value in (None, "", {}):
        return []
    return [value]


def _clean_int(value, lo: int = 1, hi: int = 199):
    """Parse an int and reject anything outside a plausible range.

    The range is a parameter and not a constant on purpose: a party of 300 is a
    misread, a budget of 300 is a Tuesday. Sharing one cap between them meant
    every real budget was thrown away with no error at all.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str):
        found = re.search(r"\d+", value.replace(",", ""))
        if not found:
            return None
        number = int(found.group())
    else:
        return None
    return number if lo <= number <= hi else None


def _clean_money(value):
    """HKD per head. Below 40 is not a meal; above 5000 is a misparse."""
    return _clean_int(value, 40, 5000)


def _normalise_constraints(raw: dict, provider: str) -> dict:
    out = _empty_constraints("")
    out["source"] = provider
    out["party_size"] = _clean_int(raw.get("party_size"))
    out["budget_per_head_hkd"] = _clean_money(raw.get("budget_per_head_hkd"))
    when = raw.get("when_text")
    out["when_text"] = when.strip() if isinstance(when, str) and when.strip() else None

    for bucket in ("hard", "soft"):
        for item in _as_list(raw.get(bucket)):
            if isinstance(item, str) and item.strip():
                out[bucket].append({"constraint": item.strip(), "who": "", "quote": ""})
            elif isinstance(item, dict) and str(item.get("constraint", "")).strip():
                out[bucket].append({
                    "constraint": str(item["constraint"]).strip(),
                    "who": str(item.get("who") or "").strip(),
                    "quote": str(item.get("quote") or "").strip(),
                })

    for item in _as_list(raw.get("vetoed")):
        if isinstance(item, str) and item.strip():
            out["vetoed"].append({"thing": item.strip(), "times_rejected": 1, "quote": ""})
        elif isinstance(item, dict) and str(item.get("thing", "")).strip():
            out["vetoed"].append({
                "thing": str(item["thing"]).strip(),
                "times_rejected": _clean_int(item.get("times_rejected")) or 1,
                "quote": str(item.get("quote") or "").strip(),
            })

    for item in _as_list(raw.get("coming_from")):
        if isinstance(item, dict) and str(item.get("place", "")).strip():
            out["coming_from"].append({
                "who": str(item.get("who") or "").strip(),
                "place": str(item["place"]).strip(),
            })
        elif isinstance(item, str) and item.strip():
            out["coming_from"].append({"who": "", "place": item.strip()})

    for key in ("prefer_cuisines", "avoid_cuisines", "open_questions"):
        out[key] = [str(v).strip() for v in _as_list(raw.get(key)) if str(v).strip()]

    summary = raw.get("summary_line")
    out["summary_line"] = (
        summary.strip() if isinstance(summary, str) and summary.strip()
        else f"{len(out['hard'])} hard, {len(out['soft'])} soft, {len(out['vetoed'])} vetoed"
    )
    return out


# ---------------------------------------------------------------------------
# Keyless fallback. Worse, but it never 429s, and a demo that degrades is a
# demo that finishes.
# ---------------------------------------------------------------------------

HK_PLACES = [
    "Central", "Sheung Wan", "Wan Chai", "Causeway Bay", "Admiralty", "North Point",
    "Quarry Bay", "Tai Koo", "Sai Ying Pun", "Kennedy Town", "Aberdeen", "Stanley",
    "Tsim Sha Tsui", "Jordan", "Mong Kok", "Yau Ma Tei", "Prince Edward", "Sham Shui Po",
    "Kowloon Tong", "Kwun Tong", "Hung Hom", "Sha Tin", "Tai Wai", "Tai Po", "Fo Tan",
    "Tseung Kwan O", "Tsuen Wan", "Yuen Long", "Tuen Mun", "Discovery Bay", "Lantau",
]
DIET_PATTERNS = [
    (r"\b(no|can'?t eat|don'?t eat|avoid|allergic to)\s+(pork|beef|shellfish|prawn\w*|shrimp|peanut\w*|dairy|gluten|nuts?)\b", "no {2}"),
    (r"\b(vegetarian|vegan|pescatarian|halal|kosher)\b", "{1}"),
    (r"\b(lactose|gluten)[- ]?(intolerant|free)\b", "{1}-free"),
]
VETO_PATTERNS = r"\b(?:not|no|no more|nope|anything but|anywhere but|nothing but|please no|never|sick of|bored of|over)\s+(?:the\s+|any\s+|more\s+)?(hotpot|hot pot|sushi|ramen|pizza|indian|thai|korean|bbq|dim sum|italian|burgers?|steak|japanese|mexican|vietnamese)\b"


def keyword_constraints(chat_text: str) -> dict:
    """Regex-only extraction. No key, no network, no quota."""
    out = _empty_constraints("")
    out["source"] = "keyword"
    lines = [ln for ln in chat_text.splitlines() if ln.strip()]
    lower = chat_text.lower()

    def speaker(line: str) -> str:
        return line.split(":", 1)[0].strip() if ":" in line else ""

    party = re.search(r"\b(?:we(?:'re| are)|there(?:'s| are)|table for|party of|book for)\s+(\d{1,2})\b", lower)
    if party:
        out["party_size"] = _clean_int(party.group(1))

    when = re.search(r"\b((?:to(?:night|morrow)|fri(?:day)?|sat(?:urday)?|sun(?:day)?|mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?)(?:\s+at)?\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", lower)
    if when:
        out["when_text"] = when.group(1).strip()

    budget = re.search(r"(?:under|below|max|up to|around|about|\$|hkd?\s*)\s*(\d{2,4})\s*(?:/|per\s*)?(?:head|pax|person|each)?", lower)
    if budget:
        out["budget_per_head_hkd"] = _clean_money(budget.group(1))

    for line in lines:
        low = line.lower()
        for pattern, template in DIET_PATTERNS:
            for match in re.finditer(pattern, low):
                label = template
                for idx in range(1, (match.re.groups or 0) + 1):
                    label = label.replace("{%d}" % idx, (match.group(idx) or "").strip())
                label = label.strip()
                if label and not any(h["constraint"] == label for h in out["hard"]):
                    out["hard"].append({"constraint": label, "who": speaker(line), "quote": line.strip()})

    counts: dict[str, list[str]] = {}
    for line in lines:
        for match in re.finditer(VETO_PATTERNS, line.lower()):
            counts.setdefault(match.group(1).replace("hot pot", "hotpot"), []).append(line.strip())
    for thing, quotes in counts.items():
        out["vetoed"].append({"thing": thing, "times_rejected": len(quotes), "quote": quotes[0]})
        out["avoid_cuisines"].append(thing)
    # Rejected twice or more without anyone using the word "veto" is still a
    # veto. Groups do not announce their vetoes; they just keep saying no.
    out["vetoed"].sort(key=lambda v: v["times_rejected"], reverse=True)

    for line in lines:
        for place in HK_PLACES:
            if re.search(r"\b(?:from|coming from|leaving)\s+" + re.escape(place.lower()), line.lower()):
                out["coming_from"].append({"who": speaker(line), "place": place})

    if out["party_size"] is None:
        out["open_questions"].append("How many people?")
    if out["when_text"] is None:
        out["open_questions"].append("What time?")

    bits = []
    if out["party_size"]:
        bits.append(f"{out['party_size']} people")
    if out["when_text"]:
        bits.append(out["when_text"])
    if out["hard"]:
        bits.append(f"{len(out['hard'])} hard constraints")
    if out["vetoed"]:
        bits.append(f"vetoed {', '.join(v['thing'] for v in out['vetoed'])}")
    out["summary_line"] = ", ".join(bits) or "keyword pass found nothing definite"
    return out


# ---------------------------------------------------------------------------
# 2. Proposal
# ---------------------------------------------------------------------------

PROPOSE_PROMPT = """You are choosing exactly 3 restaurants for a group of friends in Hong Kong from the CANDIDATES list below.

Absolute rules:
- Pick ONLY from CANDIDATES. Never introduce a place that is not in that list.
- NEVER write a phone number. Not even one that appears in the candidate data. Leave phone out entirely.
- Prefer candidates that have a phone number available, because the group has to be able to book.
- A hard constraint is disqualifying. A vetoed dish is disqualifying.
- Say plainly which constraint each pick satisfies, and name any it fails.
- "coming_from" means the venue should be easy to REACH from that place, not located in it.
- One line of "why" per pick, in the group's own terms. No marketing language.

Return ONLY this JSON shape:
{
  "picks": [{"name": "", "why": "", "satisfies": [], "fails": [], "area": ""}],
  "tradeoff_line": ""
}

CONSTRAINTS:
@@CONSTRAINTS@@

CANDIDATES:
@@CANDIDATES@@
"""


def propose(constraints: dict, candidates: list[dict]) -> dict:
    """Choose 3 from the candidate list. Phone numbers come from data, never the model."""
    if not candidates:
        return {"picks": [], "tradeoff_line": "no candidates found", "source": "empty"}

    trimmed = [
        {
            "name": c.get("name"),
            "area": c.get("area") or c.get("district") or "",
            "cuisine": c.get("cuisine") or "",
            "has_phone": bool(c.get("phone")),
        }
        for c in candidates[:40]
    ]
    prompt = (
        PROPOSE_PROMPT
        .replace(CONSTRAINTS_SENTINEL, json.dumps(constraints, ensure_ascii=False, indent=2))
        .replace(CANDIDATES_SENTINEL, json.dumps(trimmed, ensure_ascii=False, indent=2))
    )

    try:
        parsed, provider = _generate(prompt)
        picks = _rehydrate(parsed.get("picks"), candidates)
        if picks:
            return {
                "picks": picks[:3],
                "tradeoff_line": str(parsed.get("tradeoff_line") or "").strip(),
                "source": provider,
            }
        print("[pipeline] model returned no usable picks; using heuristic")
    except ModelUnavailable:
        print("[pipeline] no model reachable for propose(); using heuristic")

    return heuristic_picks(constraints, candidates)


def _rehydrate(model_picks, candidates: list[dict]) -> list[dict]:
    """Match names back to real candidate rows and attach the REAL phone number.

    The model is never trusted with a phone number. Brief §9.6: never invent a
    phone number. The structural guarantee is that the digits we dial only ever
    come from OpenStreetMap, and this function is where that is enforced — any
    phone field the model emitted is dropped on the floor.
    """
    by_name = {_norm(c.get("name", "")): c for c in candidates}
    out: list[dict] = []
    seen: set[str] = set()

    for pick in _as_list(model_picks):
        if not isinstance(pick, dict):
            continue
        name = str(pick.get("name") or "").strip()
        key = _norm(name)
        if not key or key in seen:
            continue
        match = by_name.get(key) or next(
            (c for k, c in by_name.items() if key and (key in k or k in key)), None
        )
        if not match:
            continue  # hallucinated venue — drop it
        seen.add(key)
        out.append({
            "name": match.get("name") or name,
            "phone": match.get("phone"),          # from OSM, never from the model
            "area": match.get("area") or str(pick.get("area") or ""),
            "cuisine": match.get("cuisine") or "",
            "why": str(pick.get("why") or "").strip(),
            "satisfies": [str(s) for s in _as_list(pick.get("satisfies")) if str(s).strip()],
            "fails": [str(s) for s in _as_list(pick.get("fails")) if str(s).strip()],
            "source": match.get("source") or "osm",
        })
    return out


def heuristic_picks(constraints: dict, candidates: list[dict]) -> dict:
    """Score without a model: callable first, then not-vetoed, then cuisine fit."""
    banned = {_norm(v.get("thing", "")) for v in constraints.get("vetoed", [])}
    banned |= {_norm(c) for c in constraints.get("avoid_cuisines", [])}
    wanted = {_norm(c) for c in constraints.get("prefer_cuisines", [])}

    def score(c: dict) -> tuple:
        cuisine = _norm(c.get("cuisine", ""))
        name = _norm(c.get("name", ""))
        vetoed = any(b and (b in cuisine or b in name) for b in banned)
        preferred = any(w and w in cuisine for w in wanted)
        return (bool(c.get("phone")), not vetoed, preferred, bool(c.get("cuisine")))

    ranked = sorted(candidates, key=score, reverse=True)[:3]
    return {
        "picks": [
            {
                "name": c.get("name"),
                "phone": c.get("phone"),
                "area": c.get("area") or "",
                "cuisine": c.get("cuisine") or "",
                "why": "callable and clears the group's vetoes" if c.get("phone")
                       else "clears the group's vetoes",
                "satisfies": [], "fails": [],
                "source": c.get("source") or "osm",
            }
            for c in ranked
        ],
        "tradeoff_line": "Ranked without a model: phone-first, then vetoes, then cuisine fit.",
        "source": "heuristic",
    }


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def render_constraints(constraints: dict) -> str:
    """Human-readable constraint list for the group chat.

    Quotes are included on purpose. The group has to be able to see the agent's
    reasoning and say "no, that's wrong" — which is the correction beat of the
    demo and the only reason anyone would trust it with a phone.
    """
    lines = ["<b>What I read in the chat</b>"]
    if constraints.get("party_size"):
        lines.append(f"• Party of <b>{constraints['party_size']}</b>")
    if constraints.get("when_text"):
        lines.append(f"• When: <b>{constraints['when_text']}</b>")
    if constraints.get("budget_per_head_hkd"):
        lines.append(f"• Budget: ~HK${constraints['budget_per_head_hkd']}/head")

    for item in constraints.get("hard", []):
        who = f" ({item['who']})" if item.get("who") else ""
        quote = f"\n     “{item['quote']}”" if item.get("quote") else ""
        lines.append(f"• <b>{item['constraint']}</b>{who}{quote}")
    for item in constraints.get("vetoed", []):
        times = item.get("times_rejected") or 1
        suffix = f" — rejected {times}×" if times > 1 else ""
        lines.append(f"• Vetoed: <b>{item['thing']}</b>{suffix}")
    for item in constraints.get("coming_from", []):
        who = item.get("who") or "someone"
        lines.append(f"• {who} is coming from <b>{item['place']}</b>")
    for item in constraints.get("soft", [])[:3]:
        lines.append(f"• <i>prefers</i> {item['constraint']}")

    if constraints.get("open_questions"):
        lines.append("\n<i>Still unknown: " + "; ".join(constraints["open_questions"]) + "</i>")
    if len(lines) == 1:
        lines.append("<i>Nothing concrete yet — keep talking and run /decide again.</i>")
    lines.append(f"\n<i>via {constraints.get('source', '?')}</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. Reading the call outcome
# ---------------------------------------------------------------------------

OUTCOME_PROMPT = """You are reading a transcript of a phone call an AI assistant just made to a
restaurant to book a table. Report only what the RESTAURANT STAFF actually confirmed.

Rules:
- If staff did not confirm a time, leave confirmed_time null. Do not copy the time that was requested.
- status must be exactly one of: confirmed, waitlist, declined, no_answer, unclear
- "declined" means they said no or are fully booked. "unclear" means the call ended ambiguously.
- wait_estimate_minutes only if a wait was actually quoted.
- Never invent a detail to make the call look successful.

Return ONLY this JSON shape:
{
  "status": "unclear",
  "confirmed_time": null,
  "confirmed_party_size": null,
  "wait_estimate_minutes": null,
  "booking_name": null,
  "staff_notes": ""
}

TRANSCRIPT:
@@TRANSCRIPT@@
"""

_STATUSES = {"confirmed", "waitlist", "declined", "no_answer", "unclear"}


def extract_call_outcome(transcript: list[dict]) -> dict:
    """Turn a call transcript into the same structured shape the agent's
    data-collection schema produces.

    This exists because the ElevenLabs data-collection schema is evaluated
    server-side AFTER the call and is not handed to the browser session. So the
    browser can report what was said, but not what it meant. Rather than ship a
    field that is always empty, the bridge asks for the schema result over the
    API when an ElevenLabs key is present, and otherwise derives the same
    fields here from the words that were actually spoken.

    Deriving it is strictly second-best and labelled as such wherever it is
    shown, because the agent's own evaluation saw the audio and this only sees
    text. But a demo whose loop closes is worth more than a demo with an
    architecturally purer empty dict.
    """
    turns = [t for t in (transcript or []) if str(t.get("message", "")).strip()]
    if not turns:
        return {}

    lines = []
    for turn in turns[-40:]:
        who = "RESTAURANT" if turn.get("source") == "user" else "AGENT"
        lines.append(f"{who}: {str(turn.get('message')).strip()}")

    prompt = OUTCOME_PROMPT.replace(TRANSCRIPT_SENTINEL, "\n".join(lines)[-8000:])
    try:
        parsed, _ = _generate(prompt)
    except ModelUnavailable:
        return {}

    status = str(parsed.get("status") or "unclear").strip().lower()
    return {
        "status": status if status in _STATUSES else "unclear",
        "confirmed_time": (str(parsed["confirmed_time"]).strip()
                           if parsed.get("confirmed_time") else None),
        "confirmed_party_size": _clean_int(parsed.get("confirmed_party_size")),
        "wait_estimate_minutes": _clean_int(parsed.get("wait_estimate_minutes"), 1, 600),
        "booking_name": (str(parsed["booking_name"]).strip()
                         if parsed.get("booking_name") else None),
        "staff_notes": str(parsed.get("staff_notes") or "").strip()[:300],
    }
