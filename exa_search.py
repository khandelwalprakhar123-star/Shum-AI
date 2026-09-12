#!/usr/bin/env python3
"""exa_search.py — find places nobody tagged in OpenStreetMap.

Exa searches the web semantically, which means it can be handed the group's own
words — "somewhere easy to get to from Sha Tin that isn't hotpot and won't
bankrupt six people" — instead of a district plus a cuisine enum. That is a
genuinely different retrieval shape from Overpass, and it surfaces places OSM
has as an unnamed node or doesn't have at all.

Two rules govern this whole module.

1. IT IS AN ENHANCEMENT, NEVER A DEPENDENCY. Every failure path returns []. No
   key, dead network, malformed response, changed schema, timeout: []. The
   candidate list must survive this module being completely absent, because on
   conference wifi it sometimes is.

2. IT NEVER CLAIMS A PHONE NUMBER. Exa finds names; OSM makes them callable.
   A number scraped out of page text is exactly the kind of number that gets
   misdialled on stage, so results are merged on normalised name and the phone
   field is only ever populated from OSM data.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from envlite import env

EXA_URL = "https://api.exa.ai/search"
TIMEOUT = 12

# type="fast" is ~450ms. "auto" is ~1s and "deep" is far too slow to sit inside
# a /decide that a human is watching. Latency is a product decision here: the
# group is already annoyed, and a bot that takes eight seconds to answer loses.
SEARCH_TYPE = "fast"

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "restaurant_name": {"type": "string", "description": "Name of one specific restaurant"},
        "neighbourhood": {"type": "string", "description": "Hong Kong district or area"},
        "cuisine": {"type": "string", "description": "Short cuisine description"},
        "price_band": {"type": "string", "description": "cheap, mid or expensive"},
        "why_relevant": {"type": "string", "description": "One clause on why it fits the request"},
    },
    "required": ["restaurant_name"],
}


def build_query(constraints: dict) -> str:
    """Turn constraints into the sentence a local would actually say.

    The location phrasing is load-bearing. "coming from Sha Tin" means
    REACHABLE FROM Sha Tin — someone is commuting in and wants the meeting point
    to be fair. Prefixing "near" sends the search to restaurants IN Sha Tin,
    which is the opposite of the constraint and the single worst wrong answer
    this module can give.
    """
    def _items(key: str) -> list[dict]:
        """Coerce a constraint list into dicts.

        The extractor emits dicts, but callers and older saved state pass bare
        strings, and `{"vetoed": ["hotpot"]}` used to raise AttributeError right
        here. bot.py happens to normalise before calling, so only direct
        callers hit it -- exactly the kind of bug that surfaces in a demo
        script rather than in the main flow. This module's contract is that it
        never raises; it returns [] and lets OpenStreetMap carry the search.
        """
        out: list[dict] = []
        for item in constraints.get(key) or []:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str) and item.strip():
                word = item.strip()
                out.append({"constraint": word, "place": word, "thing": word})
        return out

    bits = ["Hong Kong restaurant"]

    cuisines = [c for c in (constraints.get("prefer_cuisines") or []) if c]
    if cuisines:
        bits.append(" or ".join(cuisines[:2]))

    origins = [c.get("place") for c in _items("coming_from") if c.get("place")]
    if origins:
        bits.append("easy to reach from " + " and ".join(dict.fromkeys(origins))[:60])

    party = constraints.get("party_size")
    if party:
        bits.append(f"good for a group of {party}")

    budget = constraints.get("budget_per_head_hkd")
    if budget:
        bits.append(f"around HK${budget} per person")

    hard = [h.get("constraint") for h in _items("hard") if h.get("constraint")]
    if hard:
        bits.append("suitable for " + ", ".join(hard[:3]))

    avoid = [v.get("thing") for v in _items("vetoed") if v.get("thing")]
    avoid += [c for c in (constraints.get("avoid_cuisines") or []) if c]
    if avoid:
        bits.append("not " + " or not ".join(dict.fromkeys(avoid))[:60])

    bits.append("takes phone bookings")
    return ", ".join(bits)[:380]


def _rows_from_payload(payload: dict) -> list[dict]:
    """Parse whatever Exa actually returned.

    The response shape varies with the request: structured output can arrive
    under `summary`, as a parsed dict, as a JSON string, or not at all, and then
    only title and text are there. All four are handled because guessing one and
    being wrong means silently zero results.
    """
    out: list[dict] = []
    for item in (payload.get("results") or []):
        if not isinstance(item, dict):
            continue

        structured = item.get("summary") or item.get("structuredOutput") or item.get("extract")
        if isinstance(structured, str):
            try:
                structured = json.loads(structured)
            except json.JSONDecodeError:
                structured = None
        if not isinstance(structured, dict):
            structured = {}

        name = (
            structured.get("restaurant_name")
            or structured.get("name")
            or _name_from_title(item.get("title") or "")
        )
        name = re.sub(r"\s+", " ", str(name or "")).strip(" -|–—")[:80]
        if not name or len(name) < 2:
            continue

        out.append({
            "name": name,
            "phone": None,  # never from Exa. OSM is the only source we dial.
            "area": str(structured.get("neighbourhood") or "").strip()[:40],
            "cuisine": str(structured.get("cuisine") or "").strip()[:60],
            "price_band": str(structured.get("price_band") or "").strip()[:20],
            "why": str(structured.get("why_relevant") or "").strip()[:160],
            "url": item.get("url") or "",
            "source": "exa",
        })
    return out


def _name_from_title(title: str) -> str:
    """Strip the SEO furniture off a page title."""
    cut = re.split(r"\s[\|–—·]\s|\s-\s", title)[0]
    cut = re.sub(
        r"^\s*(?:the\s+)?(?:\d+\s+)?best\b.*?(?:in|at)\s+hong\s*kong\s*:?\s*", "", cut, flags=re.I
    )
    return cut.strip()


def search(constraints: dict, limit: int = 12) -> list[dict]:
    """Return candidate rows, or [] on any failure whatsoever."""
    key = env("EXA_API_KEY")
    if not key:
        print("[exa] no EXA_API_KEY — skipping discovery (this is fine)")
        return []

    query = build_query(constraints)
    payload = {
        "query": query,
        "type": SEARCH_TYPE,
        "numResults": max(1, min(int(limit), 25)),
        "category": "company",
        "summary": {"schema": OUTPUT_SCHEMA},
    }

    try:
        req = urllib.request.Request(
            EXA_URL, data=json.dumps(payload).encode("utf-8"), method="POST"
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {key}")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"[exa] HTTP {exc.code} — continuing without discovery")
        return []
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"[exa] {type(exc).__name__} — continuing without discovery")
        return []

    try:
        rows = _rows_from_payload(body)
    except (AttributeError, TypeError, KeyError) as exc:
        print(f"[exa] unexpected response shape ({type(exc).__name__}) — continuing without discovery")
        return []

    print(f"[exa] {len(rows)} names for: {query[:70]}...")
    return rows


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def merge(osm_rows: list[dict], exa_rows: list[dict]) -> list[dict]:
    """Merge on normalised name. Exa contributes context; OSM contributes the phone.

    Order is deliberate: OSM rows are the spine because they are the callable
    ones, and an Exa row that matches an OSM row only ever adds description. An
    Exa row with no OSM match is appended with phone None, so it can still win a
    poll — the group just gets told it has to look the number up.
    """
    merged = [dict(row) for row in osm_rows]
    index = {_norm(row.get("name", "")): row for row in merged}

    for row in exa_rows:
        key = _norm(row.get("name", ""))
        if not key:
            continue
        held = index.get(key)
        if held is None:
            held = next((v for k, v in index.items() if len(key) > 4 and (key in k or k in key)), None)

        if held is not None:
            # Exa found a page for a place OSM already knows. The URL is useful
            # (it may be the booking page) but it is NOT a phone number and is
            # never treated as one.
            if not held.get("website") and row.get("url"):
                held["website"] = row["url"]
            if not held.get("cuisine") and row.get("cuisine"):
                held["cuisine"] = row["cuisine"]
            if not held.get("area") and row.get("area"):
                held["area"] = row["area"]
            if row.get("why") and not held.get("why"):
                held["why"] = row["why"]
            held["also_found_by"] = "exa"   # provenance, shown in the poll footer
        else:
            fresh = dict(row)
            fresh["phone"] = None
            fresh.setdefault("website", row.get("url") or None)
            merged.append(fresh)
            index[key] = fresh

    return sorted(merged, key=lambda r: (bool(r.get("phone")), bool(r.get("cuisine"))), reverse=True)
