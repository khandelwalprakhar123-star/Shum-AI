"""Exa: an enhancement that must never become a dependency.

Two invariants, and the whole suite exists to hold them:
  1. every failure path returns []
  2. it never, under any response shape, supplies a phone number
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import exa_search
from harness import (FakeNet, Suite, http_error, json_response, timeout_error,
                     url_error)

CONSTRAINTS = {
    "party_size": 6, "when_text": "Friday 8pm", "budget_per_head_hkd": 300,
    "hard": [{"constraint": "no pork", "who": "Priya", "quote": "q"}],
    "soft": [], "vetoed": [{"thing": "hotpot", "times_rejected": 2, "quote": "q"}],
    "coming_from": [{"who": "Dan", "place": "Sha Tin"}],
    "prefer_cuisines": ["thai"], "avoid_cuisines": ["hotpot"], "open_questions": [],
}
OSM = [
    {"name": "Samsen Wanchai", "phone": "+85228033960", "area": "Wan Chai", "cuisine": "Thai", "source": "osm"},
    {"name": "Chom Chom", "phone": "+85228519969", "area": "", "cuisine": "", "source": "osm"},
]


def run() -> Suite:
    s = Suite("exa", expect_at_least=34)

    # --- no key means no attempt ------------------------------------------
    os.environ.pop("EXA_API_KEY", None)
    with FakeNet([], strict=True) as net:
        s.eq("no key returns []", exa_search.search(CONSTRAINTS), [])
    s.eq("no key makes no network call", len(net.requests), 0)

    os.environ["EXA_API_KEY"] = "exa-test-key"

    # --- every failure path returns [] -----------------------------------
    for label, response in [
        ("401 unauthorised", http_error(401)),
        ("429 rate limited", http_error(429)),
        ("500 server error", http_error(500)),
        ("network unreachable", url_error()),
        ("timeout", timeout_error()),
        ("HTML instead of JSON", json_response(b"<html>nope</html>")),
        ("empty body", json_response(b"")),
        ("null results", json_response({"results": None})),
        ("results is a string", json_response({"results": "oops"})),
        ("no results key at all", json_response({"ok": True})),
    ]:
        with FakeNet([response], strict=False):
            got = exa_search.search(CONSTRAINTS)
        s.eq(f"{label} returns []", got, [])

    # --- all four real response shapes parse -----------------------------
    shape_a = {"results": [{"url": "https://x.com/1", "title": "Samsen | Thai",
               "summary": {"restaurant_name": "Samsen Wanchai", "neighbourhood": "Wan Chai",
                           "cuisine": "Thai", "price_band": "mid", "why_relevant": "no pork options"}}]}
    shape_b = {"results": [{"url": "https://x.com/2", "title": "t",
               "summary": json.dumps({"restaurant_name": "Chom Chom", "cuisine": "Vietnamese"})}]}
    shape_c = {"results": [{"url": "https://x.com/3", "title": "t",
               "structuredOutput": {"restaurant_name": "Ho Lee Fook", "neighbourhood": "Central"}}]}
    shape_d = {"results": [{"url": "https://x.com/4",
               "title": "The 12 Best Thai Restaurants in Hong Kong: Nahm Thai | Tatler"}]}

    for label, payload, expected in [
        ("structured dict", shape_a, "Samsen Wanchai"),
        ("structured JSON string", shape_b, "Chom Chom"),
        ("structuredOutput key", shape_c, "Ho Lee Fook"),
        ("title only, SEO stripped", shape_d, "Nahm Thai"),
    ]:
        with FakeNet([json_response(payload)]):
            rows = exa_search.search(CONSTRAINTS)
        s.eq(f"{label} parses", rows[0]["name"] if rows else None, expected)
        s.eq(f"{label} carries no phone number", rows[0]["phone"] if rows else "x", None)

    with FakeNet([json_response({"results": [{"title": "", "url": "u"}, {"title": "A", "url": "u"}]})]):
        rows = exa_search.search(CONSTRAINTS)
    s.eq("unusable rows are dropped, not emitted blank", len(rows), 0)

    # --- the request itself ----------------------------------------------
    with FakeNet([json_response(shape_a)]) as net:
        exa_search.search(CONSTRAINTS)
    body = json.loads(net.last()["body"])
    s.eq("Bearer auth is used", net.header("authorization"), "Bearer exa-test-key")
    s.eq("type is fast, not auto or deep", body["type"], "fast")
    s.check("an output schema is requested", "schema" in body.get("summary", {}))
    s.contains("the endpoint is Exa's search API", net.last()["url"], "api.exa.ai/search")

    # --- query phrasing: the load-bearing detail --------------------------
    # "coming from Sha Tin" means REACHABLE FROM Sha Tin. Prefixing "near"
    # searches restaurants IN Sha Tin, which is the opposite constraint.
    query = exa_search.build_query(CONSTRAINTS)
    s.contains("origins are phrased as 'easy to reach from'", query, "easy to reach from Sha Tin")
    s.check("the query never says 'near' an origin", "near Sha Tin" not in query)
    s.contains("the veto is expressed", query, "not hotpot")
    s.contains("the party size is expressed", query, "group of 6")
    s.contains("the budget is expressed", query, "300")
    s.contains("the dietary constraint is expressed", query, "no pork")
    s.check("the query is bounded in length", len(query) <= 380)
    s.check("an empty constraint set still produces a usable query",
            "Hong Kong restaurant" in exa_search.build_query({}))

    # --- merge ------------------------------------------------------------
    exa_rows = [
        {"name": "Chom Chom", "phone": None, "area": "Central", "cuisine": "Vietnamese", "why": "great", "source": "exa"},
        {"name": "Brand New Place", "phone": None, "area": "Sai Ying Pun", "cuisine": "Thai", "why": "new", "source": "exa"},
    ]
    merged = exa_search.merge(OSM, exa_rows)
    s.eq("merging adds only the genuinely new place", len(merged), 3)
    chom = next(r for r in merged if r["name"] == "Chom Chom")
    s.eq("the OSM phone survives the merge", chom["phone"], "+85228519969")
    s.eq("Exa fills in the cuisine OSM lacked", chom["cuisine"], "Vietnamese")
    s.eq("Exa fills in the area OSM lacked", chom["area"], "Central")
    s.eq("the match is recorded as provenance", chom.get("also_found_by"), "exa")
    new = next(r for r in merged if r["name"] == "Brand New Place")
    s.eq("an Exa-only place has no phone number", new["phone"], None)
    s.check("callable rows still sort first after merging", bool(merged[0]["phone"]))

    samsen = next(r for r in merged if r["name"] == "Samsen Wanchai")
    s.eq("OSM cuisine is not overwritten by Exa", samsen["cuisine"], "Thai")

    s.eq("merging an empty Exa result leaves OSM untouched", exa_search.merge(OSM, []), OSM)
    s.eq("merging into an empty OSM list still works", len(exa_search.merge([], exa_rows)), 2)
    s.eq("two empty lists merge to empty", exa_search.merge([], []), [])
    s.check("no phone number appears anywhere in a merge of Exa-only rows",
            all(r["phone"] is None for r in exa_search.merge([], exa_rows)))

    os.environ.pop("EXA_API_KEY", None)
    # --- this module's contract is that it never raises --------------------
    #
    # The extractor emits dicts, but callers and older saved state pass bare
    # strings, and build_query({"vetoed": ["hotpot"]}) raised AttributeError.
    # bot.py normalises first, so only direct callers hit it -- which is how a
    # dryrun or a demo script blows up while the main flow looks fine.
    for shape in ({"vetoed": ["hotpot"]},
                  {"hard": ["no pork"]},
                  {"coming_from": ["Sha Tin"]},
                  {"vetoed": ["hotpot"], "hard": ["no pork"], "coming_from": ["Sha Tin"]}):
        try:
            query = exa_search.build_query(shape)
            ok = isinstance(query, str) and "Hong Kong restaurant" in query
        except Exception as exc:            # noqa: BLE001 - that is the bug
            ok = False
            query = f"{type(exc).__name__}: {exc}"
        s.check(f"bare strings in {list(shape)} do not raise", ok, str(query))

    s.contains("a bare origin still reads as 'reachable from'",
               exa_search.build_query({"coming_from": ["Sha Tin"]}),
               "easy to reach from Sha Tin")
    s.contains("a bare veto still reads as a veto",
               exa_search.build_query({"vetoed": ["hotpot"]}), "not hotpot")

    return s
