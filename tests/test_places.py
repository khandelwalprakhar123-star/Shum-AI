"""Phone normalisation, deduplication, and the three-layer fallback ladder.

normalise_phone() gets the most attention in this file because its return value
gets DIALLED. Every input below is a real shape seen in Hong Kong OSM data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time

import places
from harness import FakeNet, Suite, http_error, overpass_ok, timeout_error, url_error


def run() -> Suite:
    s = Suite("places", expect_at_least=71)

    # --- phone normalisation: every real-world shape ----------------------
    good = {
        "+852 2527 2343": "+85225272343",
        "+85225272343": "+85225272343",
        "25734554": "+85225734554",
        "2573 4554": "+85225734554",
        "852-2857-5511": "+85228575511",
        "+852-2522-1234": "+85225221234",
        "tel:+852 2522 1234": "+85225221234",
        "00852 2522 1234": "+85225221234",
        "2522 1234 (shop)": "+85225221234",
        "+852 2877 3833; +852 2877 3834": "+85228773833",
        "+852 2877 3833, +852 2877 3834": "+85228773833",
        "2877 3833 or 2877 3834": "+85228773833",
        "  +852  2527  2343  ": "+85225272343",
        "(852) 2527 2343": "+85225272343",
    }
    for raw, want in good.items():
        s.eq(f"normalise {raw!r}", places.normalise_phone(raw), want)

    # Anything we cannot be certain about must come back None. A plausible-
    # looking wrong number is worse than no number, because somebody dials it.
    bad = ["1234", "", "   ", None, "no phone", "+1 415 555 1234", "+44 20 7946 0958",
           "852", "123456789012345", "0000 0000", "+852 1234 5678", 12345, [], {}]
    for raw in bad:
        s.eq(f"reject {raw!r} rather than half-parse it", places.normalise_phone(raw), None)

    s.check("every returned number is E.164 +852 plus 8 digits",
            all(places.normalise_phone(v) and len(places.normalise_phone(v)) == 12 for v in good))

    # --- English names ----------------------------------------------------
    s.eq("prefers name:en over the bilingual blob",
         places._name_for({"name": "美心Food² Maxim's Food²", "name:en": "Maxim's Food²"}),
         "Maxim's Food²")
    s.eq("falls back to int_name", places._name_for({"name": "上海婆婆", "int_name": "Shanghai Popo"}), "Shanghai Popo")
    s.eq("falls back to name", places._name_for({"name": "Kau Kee"}), "Kau Kee")
    s.eq("no name at all yields empty", places._name_for({}), "")

    # --- districts --------------------------------------------------------
    s.eq("Central coordinates map to Central", places._district_for(22.2820, 114.1580), "Central")
    s.eq("Sheung Wan coordinates map to Sheung Wan", places._district_for(22.2860, 114.1500), "Sheung Wan")
    s.eq("Mong Kok coordinates map to Mong Kok", places._district_for(22.3190, 114.1700), "Mong Kok")
    s.eq("Sha Tin coordinates map to Sha Tin", places._district_for(22.3800, 114.1900), "Sha Tin")
    s.eq("London is not a Hong Kong district", places._district_for(51.5, -0.12), "")
    s.eq("missing coordinates yield empty, not a crash", places._district_for(None, None), "")

    # --- cuisine ----------------------------------------------------------
    s.eq("cuisine tag is humanised", places._cuisine_for({"cuisine": "dim_sum"}), "dim sum")
    s.eq("multi-value cuisine is split", places._cuisine_for({"cuisine": "chinese;cantonese"}), "Chinese, Cantonese")
    s.eq("fast_food with no cuisine is labelled", places._cuisine_for({"amenity": "fast_food"}), "fast food")
    s.eq("no cuisine and no amenity yields empty", places._cuisine_for({}), "")

    # --- dedupe and rank --------------------------------------------------
    rows = [
        {"name": "Tsui Wah Restaurant", "phone": None, "cuisine": "", "area": "Central"},
        {"name": "tsui wah restaurant", "phone": "+85225252468", "cuisine": "Cantonese", "area": "Central"},
        {"name": "Tsui  Wah   Restaurant", "phone": None, "cuisine": "", "area": ""},
        {"name": "Kau Kee", "phone": None, "cuisine": "noodles", "area": "Sheung Wan"},
    ]
    ranked = places.dedupe_and_rank(rows)
    s.eq("three spellings of one chain collapse to one row", len(ranked), 2)
    s.eq("the collision keeps the copy that has a phone",
         next(r for r in ranked if "tsui" in r["name"].lower())["phone"], "+85225252468")
    s.eq("callable rows sort first", bool(ranked[0]["phone"]), True)

    only_cuisine = places.dedupe_and_rank([
        {"name": "X", "phone": None, "cuisine": "", "area": ""},
        {"name": "x", "phone": None, "cuisine": "Thai", "area": ""},
    ])
    s.eq("with no phone either way, the richer row wins", only_cuisine[0]["cuisine"], "Thai")
    s.eq("rows with no name are dropped",
         len(places.dedupe_and_rank([{"name": "", "phone": "+85225252468"}])), 0)

    # --- element parsing --------------------------------------------------
    s.eq("a place with no coordinates is dropped",
         places._row_from_element({"type": "node", "id": 1, "tags": {"name": "Nowhere"}}), None)
    s.eq("a place with no name is dropped",
         places._row_from_element({"type": "node", "id": 1, "lat": 22.28, "lon": 114.15, "tags": {}}), None)
    # One junk tag used to hide a good one: `tags["phone"] or tags["contact:phone"]`
    # short-circuits on "n/a", which normalises to None, so a perfectly
    # callable restaurant came back uncallable. OSM is full of this.
    def phone_of(tags):
        el = {"type": "node", "id": 99, "lat": 22.28, "lon": 114.17,
              "tags": dict(tags, name=tags.get("name", "Somewhere"))}
        got = places._row_from_element(el)
        return got and got.get("phone")

    s.eq("a junk phone tag does not mask a good contact:phone",
         phone_of({"phone": "n/a", "contact:phone": "+852 2123 4567"}), "+85221234567")
    s.eq("...nor does 'see website'",
         phone_of({"phone": "see website", "phone:HK": "+852 2522 1234"}), "+85225221234")
    s.eq("a good phone tag still wins when it is first",
         phone_of({"phone": "+852 2522 1234", "contact:phone": "n/a"}), "+85225221234")
    s.eq("junk everywhere is still no number",
         phone_of({"phone": "n/a", "contact:phone": "-"}), None)
    s.check("and a masked number is never half-parsed",
            phone_of({"phone": "2522"}) is None,
            "a partial number presented as a phone number is the worst outcome here")

    row = places._row_from_element({
        "type": "way", "id": 42, "center": {"lat": 22.2820, "lon": 114.1580},
        "tags": {"name": "Test Place", "contact:phone": "2527 2343", "cuisine": "thai"},
    })
    s.eq("way centre coordinates are accepted", row["area"], "Central")
    s.eq("contact:phone is read as well as phone", row["phone"], "+85225272343")
    s.eq("osm id is recorded for provenance", row["osm_id"], "way/42")

    # --- the three-layer ladder ------------------------------------------
    elements = [
        {"type": "node", "id": 1, "lat": 22.2820, "lon": 114.1580,
         "tags": {"name": "Live Place", "phone": "+852 2527 2343", "cuisine": "thai"}},
        {"type": "node", "id": 2, "lat": 22.2860, "lon": 114.1500,
         "tags": {"name": "Another Live", "cuisine": "japanese"}},
    ]
    original_cache = places.CACHE_PATH
    places.CACHE_PATH = Path(__file__).resolve().parent / "_test_cache.json"
    try:
        with FakeNet([overpass_ok(elements), overpass_ok([]), overpass_ok([])]):
            live = places.search_places()
        s.eq("layer 1: live Overpass is used", live[0]["name"], "Live Place")
        s.check("layer 1 writes the cache for later", places.CACHE_PATH.exists())

        # A FRESH cache short-circuits before any network call at all. This is
        # the demo path: /decide should not spend 18 seconds on Overpass while
        # six people watch a typing indicator.
        with FakeNet([], strict=True) as net:
            hot = places.search_places()
        s.eq("a fresh cache is served with ZERO network calls", len(net.requests), 0)
        s.eq("and returns the cached places", hot[0]["name"], "Live Place")
        s.check("cache age is reported", places.cache_age_seconds() is not None)
        s.check("a just-written cache is seconds old", places.cache_age_seconds() < 60)

        # force_live must be able to ignore a fresh cache.
        with FakeNet([overpass_ok(elements), overpass_ok([]), overpass_ok([])]) as net:
            places.search_places(force_live=True)
        s.check("force_live bypasses a fresh cache", len(net.requests) > 0)

        # A STALE cache must not be trusted over live data.
        raw = json.loads(places.CACHE_PATH.read_text())
        raw["written_at"] = time.time() - (places.CACHE_MAX_AGE_SECONDS + 600)
        places.CACHE_PATH.write_text(json.dumps(raw))
        s.check("a stale cache is detected",
                places.cache_age_seconds() > places.CACHE_MAX_AGE_SECONDS)
        with FakeNet([overpass_ok(elements), overpass_ok([]), overpass_ok([])]) as net:
            refreshed = places.search_places()
        s.check("a stale cache triggers a live query", len(net.requests) > 0)
        s.eq("and the live result is used", refreshed[0]["name"], "Live Place")

        # Overpass tries two endpoints per box and three boxes: six failures.
        raw = json.loads(places.CACHE_PATH.read_text())
        raw["written_at"] = time.time() - (places.CACHE_MAX_AGE_SECONDS + 600)
        places.CACHE_PATH.write_text(json.dumps(raw))
        with FakeNet([url_error()] * 6, strict=False):
            cached = places.search_places()
        s.eq("layer 2: Overpass down falls back to even a stale cache",
             cached[0]["name"], "Live Place")
        s.check("layer 2 rows are still callable", bool(cached[0]["phone"]))

        places.CACHE_PATH.unlink()
        with FakeNet([timeout_error()] * 6, strict=False):
            seeded = places.search_places()
        s.check("layer 3: no network and no cache still returns names", len(seeded) > 0)
        s.eq("layer 3 invents zero phone numbers",
             sum(1 for r in seeded if r.get("phone")), 0)
        s.check("seed rows are labelled as seed", all(r.get("source") == "seed" for r in seeded))

        places.CACHE_PATH.write_text("{ this is not json", encoding="utf-8")
        s.eq("a corrupt cache reads as empty rather than crashing", places.read_cache(), [])
    finally:
        if places.CACHE_PATH.exists():
            places.CACHE_PATH.unlink()
        places.CACHE_PATH = original_cache

    # --- the cache may only ever grow ------------------------------------
    # write_cache used to replace the file wholesale, so a PARTIAL Overpass run
    # -- one box out of three answering, which is what conference wifi does --
    # destroyed everything --refresh-cache had harvested. Measured: 200
    # callable rows down to 1, unrecoverable without network. The cache is the
    # only layer still carrying phone numbers when Overpass is down.
    original_cache = places.CACHE_PATH
    places.CACHE_PATH = Path(__file__).resolve().parent / "_test_merge_cache.json"
    try:
        full = [{"name": f"Place {i}", "phone": f"+8522000000{i}", "area": "Central",
                 "cuisine": "x", "source": "osm"} for i in range(5)]
        places.write_cache(full, replace=True)
        s.eq("a full harvest writes everything", len(places.read_cache()), 5)

        places.write_cache([{"name": "Place 0", "phone": "+85220000000",
                             "area": "Central", "cuisine": "x", "source": "osm"}])
        after = places.read_cache()
        s.eq("a partial run does NOT destroy the rest", len(after), 5)
        s.eq("callable rows survive a partial run",
             sum(1 for r in after if r.get("phone")), 5)

        places.write_cache([{"name": "Brand New", "phone": "+85229999999",
                             "area": "Wan Chai", "cuisine": "y", "source": "osm"}])
        s.eq("a partial run can still ADD", len(places.read_cache()), 6)

        places.write_cache([{"name": "Place 1", "phone": None, "area": "Central",
                             "cuisine": "x", "source": "osm"}])
        kept = next(r for r in places.read_cache() if r["name"] == "Place 1")
        s.check("a phoneless row never overwrites a callable one", bool(kept.get("phone")))

        places.write_cache([{"name": "Only One", "phone": "+85221111111"}], replace=True)
        s.eq("replace=True still rebuilds, because that is what it is for",
             len(places.read_cache()), 1)
    finally:
        if places.CACHE_PATH.exists():
            places.CACHE_PATH.unlink()
        places.CACHE_PATH = original_cache

    # --- helpers ----------------------------------------------------------
    pool = [{"name": "Samsen Wanchai", "phone": "+85228033960"}, {"name": "Chom Chom", "phone": None}]
    s.eq("callable_only filters to dialable rows", len(places.callable_only(pool)), 1)
    s.eq("find_by_name matches exactly", places.find_by_name(pool, "Samsen Wanchai")["phone"], "+85228033960")
    s.eq("find_by_name matches case and spacing insensitively",
         places.find_by_name(pool, "samsen  wanchai")["phone"], "+85228033960")
    s.eq("find_by_name matches a partial", places.find_by_name(pool, "Samsen")["name"], "Samsen Wanchai")
    s.eq("find_by_name returns None for a stranger", places.find_by_name(pool, "Nowhere At All"), None)
    s.eq("find_by_name on empty input is None", places.find_by_name(pool, ""), None)

    # --- the search follows the conversation ------------------------------
    # Nothing about WHERE to search may be hardcoded: it has to come from the
    # districts people actually named. Before this, the candidate pool was the
    # same territory-wide list no matter what the chat said.
    sha_tin = {"coming_from": [{"who": "Dan", "place": "Sha Tin"}], "hard": [], "soft": [],
               "vetoed": [], "open_questions": [], "summary_line": ""}
    s.eq("an origin in coming_from is detected", places.mentioned_districts(sha_tin), ["Sha Tin"])
    areas = places.areas_for(sha_tin)
    s.contains("the origin's own area is searched", areas, "sha_tin")
    s.check("the cross-harbour spine is searched too",
            all(a in areas for a in places.SPINE_AREAS),
            "'coming from X' is a travel constraint, not a request to eat in X")

    free_text = {"coming_from": [], "hard": [{"constraint": "somewhere in Mong Kok"}],
                 "soft": [], "vetoed": [], "open_questions": [], "summary_line": ""}
    s.eq("a district named in free constraint text is detected",
         places.mentioned_districts(free_text), ["Mong Kok"])
    s.contains("and maps to its area", places.areas_for(free_text), "kowloon_south")

    summary = {"coming_from": [], "hard": [], "soft": [], "vetoed": [],
               "open_questions": [], "summary_line": "everyone is near Causeway Bay"}
    s.eq("a district in the summary line is detected too",
         places.mentioned_districts(summary), ["Causeway Bay"])

    silent = {"coming_from": [], "hard": [], "soft": [], "vetoed": [],
              "open_questions": [], "summary_line": ""}
    s.eq("a chat naming no district assumes none", places.mentioned_districts(silent), [])
    s.eq("and falls back to searching everywhere", len(places.areas_for(silent)), len(places.AREAS))
    s.eq("an empty constraint dict does not crash", places.mentioned_districts({}), [])

    s.check("a district nobody mentioned never appears",
            "Tuen Mun" not in places.mentioned_districts(sha_tin))

    pool = [
        {"name": "Far", "phone": "+85221111111", "area": "Tuen Mun", "cuisine": ""},
        {"name": "Origin", "phone": "+85222222222", "area": "Sha Tin", "cuisine": ""},
        {"name": "Spine", "phone": "+85223333333", "area": "Central", "cuisine": ""},
        {"name": "NoPhone", "phone": None, "area": "Sha Tin", "cuisine": "Thai"},
    ]
    ranked = [r["name"] for r in places.relevance_rank(pool, sha_tin)]
    s.check("a commuter's OWN district does not win by default",
            ranked.index("Origin") > 0,
            "coming from Sha Tin is not a request to eat in Sha Tin")
    s.eq("a place on the meeting spine wins instead", ranked[0], "Spine")
    s.check("and somewhere far from everyone loses to it",
            ranked.index("Spine") < ranked.index("Far"))
    s.eq("callable still beats well-located", ranked[-1], "NoPhone")
    s.eq("re-ranking never drops rows", len(places.relevance_rank(pool, silent)), len(pool))

    # --- the query --------------------------------------------------------
    query = places.build_query(places.AREAS["hk_island_north"])
    s.contains("query asks for restaurants and fast food", query, "restaurant|fast_food")
    s.contains("query requires a name tag", query, '["name"]')
    s.contains("query returns tags and centres", query, "out center tags")
    s.check("query carries a timeout so it cannot hang forever", "timeout:" in query)

    s.check("all district boxes are well formed",
            all(s0 < n0 and w0 < e0 for _, s0, w0, n0, e0 in places.DISTRICTS))
    s.check("all area boxes are well formed",
            all(b[0] < b[2] and b[1] < b[3] for b in places.AREAS.values()))
    return s
