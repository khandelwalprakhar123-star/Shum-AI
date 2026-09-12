#!/usr/bin/env python3
"""places.py — real Hong Kong restaurants with real, dialable phone numbers.

Why OpenStreetMap and not a restaurant platform: there is no usable alternative.
Every booking platform in this market is partner-gated (OpenTable, Resy,
SevenRooms, Tock all require an application and a multi-week review), and
OpenRice — the dominant platform in Hong Kong — cannot be touched at all. Its
robots.txt names GPTBot, PerplexityBot, meta-externalagent and Bytespider and
disallows the JSON service endpoints outright, and its terms forbid using "any
robot, any automatic device or manual process to monitor or copy the Channels".
A hackathon submission is a public repository and a live demo. Scraping it would
be both a licence breach and a bad thing to put on stage.

So: Overpass. No key, no card, real data, and a licence that permits this.

The cost is coverage. Measured across HK Island north shore plus Kowloon: 1,199
named places, 156 with a usable phone number, 53% with a cuisine tag. Roughly a
fifth are callable. That single number drives the whole ranking design — a
beautiful recommendation you cannot phone is worthless to this agent, so
phone-bearing rows sort first, everywhere, unconditionally.

Three layers, so this module cannot be the reason the demo fails:
  live Overpass  ->  places_cache.json  ->  a handpicked seed list in this file
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from envlite import env

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "places_cache.json"

# A fresh cache is preferred over a live query, and that is a demo decision as
# much as a caching one. The live Overpass round trip measured 6-18 seconds
# depending on load, and /decide is watched by six impatient people in a group
# chat. A restaurant list twenty minutes old is not stale; eighteen seconds of
# dead air is a worse product.
CACHE_MAX_AGE_SECONDS = int(env("PLACES_CACHE_MAX_AGE") or 6 * 3600)

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",  # mirror; de/ rate-limits
]
TIMEOUT = 30

# Bounding boxes, south/west/north/east. Kept small and named because a
# whole-territory query times out on the public endpoint at conference wifi
# speeds and returns nothing useful.
AREAS: dict[str, tuple[float, float, float, float]] = {
    "hk_island_north": (22.270, 114.130, 22.295, 114.220),
    "kowloon_south": (22.290, 114.160, 22.325, 114.200),
    "kowloon_east": (22.300, 114.200, 22.340, 114.240),
    "sha_tin": (22.365, 114.170, 22.400, 114.210),
    "tseung_kwan_o": (22.300, 114.250, 22.330, 114.280),
}

# Coordinate -> district. Judges and friends both read "Sheung Wan", not
# "22.2856, 114.1503".
DISTRICTS: list[tuple[str, float, float, float, float]] = [
    ("Kennedy Town", 22.278, 114.120, 22.290, 114.135),
    ("Sai Ying Pun", 22.280, 114.135, 22.292, 114.145),
    ("Sheung Wan", 22.281, 114.145, 22.292, 114.155),
    ("Central", 22.275, 114.152, 22.288, 114.165),
    ("Admiralty", 22.274, 114.160, 22.283, 114.168),
    ("Wan Chai", 22.271, 114.168, 22.285, 114.180),
    ("Causeway Bay", 22.274, 114.180, 22.288, 114.192),
    ("North Point", 22.283, 114.188, 22.298, 114.205),
    ("Quarry Bay", 22.282, 114.205, 22.295, 114.220),
    ("Tsim Sha Tsui", 22.290, 114.165, 22.302, 114.180),
    ("Jordan", 22.302, 114.165, 22.310, 114.178),
    ("Yau Ma Tei", 22.308, 114.165, 22.316, 114.178),
    ("Mong Kok", 22.314, 114.165, 22.328, 114.180),
    ("Hung Hom", 22.296, 114.180, 22.312, 114.196),
    ("Kwun Tong", 22.305, 114.220, 22.330, 114.240),
    ("Sha Tin", 22.365, 114.170, 22.400, 114.210),
    ("Tseung Kwan O", 22.300, 114.250, 22.330, 114.280),
]

# Which bounding box each district sits in, so a district named in the chat can
# be turned into a search area. Derived from DISTRICTS above rather than
# duplicated, but stated explicitly because the boxes overlap.
DISTRICT_TO_AREA: dict[str, str] = {
    "Kennedy Town": "hk_island_north", "Sai Ying Pun": "hk_island_north",
    "Sheung Wan": "hk_island_north", "Central": "hk_island_north",
    "Admiralty": "hk_island_north", "Wan Chai": "hk_island_north",
    "Causeway Bay": "hk_island_north", "North Point": "hk_island_north",
    "Quarry Bay": "hk_island_north",
    "Tsim Sha Tsui": "kowloon_south", "Jordan": "kowloon_south",
    "Yau Ma Tei": "kowloon_south", "Mong Kok": "kowloon_south",
    "Hung Hom": "kowloon_south",
    "Kwun Tong": "kowloon_east",
    "Sha Tin": "sha_tin", "Tai Wai": "sha_tin", "Fo Tan": "sha_tin",
    "Tseung Kwan O": "tseung_kwan_o",
}

# Where Hong Kong groups actually converge when people come from different
# places: the two ends of the cross-harbour spine. Included alongside any
# origin-specific area so a Sha Tin commuter still gets Central and Kowloon
# options rather than only Sha Tin restaurants -- "coming from X" is a travel
# constraint, not a request to eat in X.
SPINE_AREAS = ["hk_island_north", "kowloon_south"]

CUISINE_LABELS = {
    "chinese": "Chinese", "cantonese": "Cantonese", "dim_sum": "dim sum",
    "japanese": "Japanese", "sushi": "sushi", "ramen": "ramen",
    "korean": "Korean", "thai": "Thai", "vietnamese": "Vietnamese",
    "indian": "Indian", "italian": "Italian", "pizza": "pizza",
    "french": "French", "spanish": "Spanish", "american": "American",
    "burger": "burgers", "steak_house": "steakhouse", "seafood": "seafood",
    "vegetarian": "vegetarian", "vegan": "vegan", "mexican": "Mexican",
    "asian": "Asian", "international": "international", "noodle": "noodles",
    "hotpot": "hotpot", "hot_pot": "hotpot", "barbecue": "BBQ",
}


# ---------------------------------------------------------------------------
# Phone normalisation. This is the single most important function in the file,
# because its output gets DIALLED.
# ---------------------------------------------------------------------------
# OSM phone tags in Hong Kong are a genuine mess. All of these are real:
#     "+852 2527 2343"   "25734554"   "852-2857-5511"   "+852 2877 3833; +852 2877 3834"
#     "+85221234567"     "tel:+852-2522-1234"   "2522 1234 (shop)"
# Everything becomes +852XXXXXXXX or None. Never a half-parsed string: a broken
# number that looks plausible is worse than no number, because someone dials it.

HK_MOBILE_LANDLINE_PREFIXES = tuple("23456789")


def normalise_phone(raw: str | None) -> str | None:
    if not raw or not isinstance(raw, str):
        return None

    # Multi-number tags: take the first. A restaurant's first listed line is
    # its main line often enough, and picking arbitrarily is not acceptable.
    first = re.split(r"[;,/]| or ", raw)[0]
    first = re.sub(r"\((?:[^)]*)\)", " ", first)          # drop "(shop)", "(reception)"
    first = first.replace("tel:", "").replace("Tel:", "")
    digits = re.sub(r"\D", "", first)

    if not digits:
        return None
    if digits.startswith("00852"):
        digits = digits[5:]
    elif digits.startswith("852") and len(digits) == 11:
        digits = digits[3:]

    if len(digits) != 8 or not digits.startswith(HK_MOBILE_LANDLINE_PREFIXES):
        return None  # not a Hong Kong number we are willing to dial
    return "+852" + digits


def mentioned_districts(constraints: dict) -> list[str]:
    """Every Hong Kong district the chat actually named, in the order found.

    Reads coming_from first (someone is commuting and said so), then the free
    text of every constraint, veto and note. Nothing is assumed: a district that
    nobody mentioned never appears here.
    """
    found: list[str] = []

    def note(name: str) -> None:
        if name and name not in found:
            found.append(name)

    for entry in constraints.get("coming_from") or []:
        place = str((entry or {}).get("place") or "").strip()
        for district, _, _, _, _ in DISTRICTS:
            if district.lower() == place.lower():
                note(district)

    haystack = " ".join(
        [str(item.get("constraint", "")) for item in (constraints.get("hard") or [])]
        + [str(item.get("constraint", "")) for item in (constraints.get("soft") or [])]
        + [str(item.get("thing", "")) for item in (constraints.get("vetoed") or [])]
        + [str(entry.get("place", "")) for entry in (constraints.get("coming_from") or [])]
        + (constraints.get("open_questions") or [])
        + [str(constraints.get("summary_line") or "")]
    ).lower()
    for district, _, _, _, _ in DISTRICTS:
        if re.search(r"\b" + re.escape(district.lower()) + r"\b", haystack):
            note(district)
    return found


def areas_for(constraints: dict) -> list[str]:
    """Turn what the chat said into which bounding boxes to search.

    With nothing to go on it falls back to the full list, which is the old
    behaviour. With an origin named it searches that origin's area AND the
    cross-harbour spine, because a commuter wants a fair meeting point rather
    than dinner next to their office.
    """
    wanted: list[str] = []
    for district in mentioned_districts(constraints):
        area = DISTRICT_TO_AREA.get(district)
        if area and area not in wanted:
            wanted.append(area)

    if not wanted:
        return list(AREAS)

    for area in SPINE_AREAS:
        if area not in wanted:
            wanted.append(area)
    return wanted


def relevance_rank(rows: list[dict], constraints: dict) -> list[dict]:
    """Re-rank an existing pool against the districts the chat named.

    Kept separate from search so it applies to cached rows too -- the cache is
    territory-wide and constraint-blind by design, so the constraint awareness
    has to live in the ranking rather than only in the query.

    Phone-bearing still dominates. A perfectly located restaurant we cannot
    telephone is useless to this agent.
    """
    districts = {d.lower() for d in mentioned_districts(constraints)}
    spine = {"central", "sheung wan", "wan chai", "causeway bay", "admiralty",
             "tsim sha tsui", "jordan", "mong kok", "yau ma tei"}

    def score(row: dict) -> tuple:
        area = str(row.get("area") or "").lower()
        return (
            bool(row.get("phone")),
            area in districts,          # a district the chat actually named
            area in spine,              # otherwise a fair meeting point
            bool(row.get("cuisine")),
            bool(area),
        )

    return sorted(rows, key=score, reverse=True)


def _district_for(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return ""
    for name, south, west, north, east in DISTRICTS:
        if south <= lat <= north and west <= lon <= east:
            return name
    return ""


def _cuisine_for(tags: dict) -> str:
    raw = (tags.get("cuisine") or "").lower()
    if not raw:
        if tags.get("amenity") == "fast_food":
            return "fast food"
        return ""
    parts = [CUISINE_LABELS.get(p.strip(), p.strip().replace("_", " ")) for p in raw.split(";") if p.strip()]
    return ", ".join(dict.fromkeys(parts))[:60]


def _name_for(tags: dict) -> str:
    """Prefer the English name.

    OSM stores "美心Food² Maxim's Food²" in `name` and "Maxim's Food²" in
    `name:en`. The second one is what the agent can pronounce on the phone and
    what reads cleanly in a poll.
    """
    for key in ("name:en", "int_name", "name"):
        value = (tags.get(key) or "").strip()
        if value:
            return value[:80]
    return ""


def _norm_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


# ---------------------------------------------------------------------------
# Overpass
# ---------------------------------------------------------------------------

def build_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    box = f"{south},{west},{north},{east}"
    return f"""[out:json][timeout:25];
(
  node["amenity"~"^(restaurant|fast_food)$"]["name"]({box});
  way["amenity"~"^(restaurant|fast_food)$"]["name"]({box});
);
out center tags 400;"""


def _fetch_overpass(query: str) -> list[dict]:
    payload = urllib.parse.urlencode({"data": query}).encode()
    last: Exception | None = None
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            req = urllib.request.Request(
                endpoint, data=payload,
                headers={"User-Agent": "Shum-AI/1.0 (AI Tinkerers hackathon; contact via repo)"},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return (json.loads(resp.read().decode("utf-8")) or {}).get("elements") or []
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last = exc
            print(f"[places] {endpoint} failed: {type(exc).__name__}")
    if last:
        print(f"[places] all Overpass endpoints failed: {last}")
    return []


def _row_from_element(element: dict) -> dict | None:
    tags = element.get("tags") or {}
    name = _name_for(tags)
    if not name:
        return None

    lat = element.get("lat") or (element.get("center") or {}).get("lat")
    lon = element.get("lon") or (element.get("center") or {}).get("lon")
    if lat is None or lon is None:
        return None  # a place we cannot locate is a place we cannot recommend

    phone = normalise_phone(tags.get("phone") or tags.get("contact:phone") or tags.get("phone:HK"))
    # A website is worth keeping even when we have a phone number: when the
    # agent cannot get through, or the venue only takes online bookings, a real
    # link is a far more honest answer than a shrug. Phone is still preferred --
    # in Hong Kong, phoning to book IS the norm.
    website = (tags.get("website") or tags.get("contact:website")
               or tags.get("url") or tags.get("brand:website") or "").strip()
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website
    return {
        "name": name,
        "phone": phone,
        "website": website[:300] or None,
        "area": _district_for(lat, lon),
        "cuisine": _cuisine_for(tags),
        "lat": lat, "lon": lon,
        "osm_id": f"{element.get('type', 'node')}/{element.get('id')}",
        "source": "osm",
    }


def dedupe_and_rank(rows: list[dict]) -> list[dict]:
    """Dedupe on normalised name, keeping the phone-bearing copy.

    Chains appear many times per box. When "Tsui Wah Restaurant" shows up nine
    times and only one node carries the phone tag, that is the one worth
    keeping — so on collision, a row with a phone always beats a row without.
    """
    best: dict[str, dict] = {}
    for row in rows:
        key = _norm_key(row.get("name", ""))
        if not key:
            continue
        held = best.get(key)
        if held is None:
            best[key] = row
            continue
        if row.get("phone") and not held.get("phone"):
            best[key] = row
        elif bool(row.get("phone")) == bool(held.get("phone")) and row.get("cuisine") and not held.get("cuisine"):
            best[key] = row

    # Callable first. With ~20% phone coverage this is the difference between a
    # poll the group can act on and three names nobody can book.
    return sorted(
        best.values(),
        key=lambda r: (bool(r.get("phone")), bool(r.get("cuisine")), bool(r.get("area"))),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Layer 3: the seed list. Hand-checked, so the demo has real numbers even with
# no network at all. Every one of these consented to nothing — they are here so
# the SEARCH works offline, and the DEMO_PHONE rail plus the consent allowlist
# in bot.py is what stops any of them being dialled.
# ---------------------------------------------------------------------------

SEED_PLACES: list[dict] = [
    # NAMES ONLY. Every phone is deliberately None.
    #
    # Brief §9.6: never invent a phone number. That rail applies to the author
    # of this file as much as to the model. These are well-known Hong Kong
    # restaurants whose names are useful as a last-resort candidate set, but the
    # digits are NOT hand-typed from memory here, because a number that is
    # almost right is worse than no number — somebody dials it.
    #
    # Callable data comes from the layer above: a real Overpass run, cached to
    # places_cache.json. Build it once while you have network:
    #     python3 places.py --refresh-cache
    # and the cache carries the demo even if Overpass is down at 16:00.
    {"name": "Tsui Wah Restaurant", "phone": None, "area": "Central", "cuisine": "Cantonese, cha chaan teng", "source": "seed"},
    {"name": "Kau Kee Restaurant", "phone": None, "area": "Sheung Wan", "cuisine": "noodles, beef brisket", "source": "seed"},
    {"name": "Yat Lok Barbecue Restaurant", "phone": None, "area": "Central", "cuisine": "Cantonese, roast goose", "source": "seed"},
    {"name": "Lin Heung Tea House", "phone": None, "area": "Sheung Wan", "cuisine": "dim sum", "source": "seed"},
    {"name": "Chom Chom", "phone": None, "area": "Central", "cuisine": "Vietnamese", "source": "seed"},
    {"name": "Samsen Wanchai", "phone": None, "area": "Wan Chai", "cuisine": "Thai", "source": "seed"},
    {"name": "Kam's Roast Goose", "phone": None, "area": "Wan Chai", "cuisine": "Cantonese, roast goose", "source": "seed"},
    {"name": "Dumpling Yuan", "phone": None, "area": "Central", "cuisine": "Chinese, dumplings", "source": "seed"},
    {"name": "Ho Lee Fook", "phone": None, "area": "Central", "cuisine": "Chinese, modern", "source": "seed"},
    {"name": "Bombay Dreams", "phone": None, "area": "Central", "cuisine": "Indian, vegetarian options", "source": "seed"},
    {"name": "Australia Dairy Company", "phone": None, "area": "Jordan", "cuisine": "cha chaan teng", "source": "seed"},
    {"name": "Mido Cafe", "phone": None, "area": "Yau Ma Tei", "cuisine": "cha chaan teng", "source": "seed"},
    {"name": "Chuen Cheung Kui", "phone": None, "area": "Mong Kok", "cuisine": "Hakka, Chinese", "source": "seed"},
]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def read_cache() -> list[dict]:
    if not CACHE_PATH.exists():
        return []
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    rows = data.get("places") if isinstance(data, dict) else data
    return [r for r in (rows or []) if isinstance(r, dict) and r.get("name")]


def write_cache(rows: list[dict], replace: bool = False) -> None:
    """Merge into the cache. Only --refresh-cache may replace it.

    This used to overwrite wholesale, which meant a PARTIAL Overpass run --
    one bounding box out of three answering, which is exactly what conference
    wifi does -- destroyed everything a full harvest had collected. Measured:
    200 callable rows down to 1, unrecoverable without network. The cache is
    the only layer that still carries phone numbers when Overpass is down, so
    a normal search must only ever be able to ADD to it.
    """
    if not replace:
        existing = read_cache()
        if existing:
            merged = {_norm_key(r.get("name", "")): r for r in existing if r.get("name")}
            for row in rows:
                key = _norm_key(row.get("name", ""))
                if not key:
                    continue
                held = merged.get(key)
                # A row with a phone always beats one without, whichever side
                # it came from.
                if held is None or (row.get("phone") and not held.get("phone")):
                    merged[key] = row
            rows = list(merged.values())
    try:
        CACHE_PATH.write_text(
            json.dumps({"places": rows, "count": len(rows), "written_at": time.time()},
                       indent=1, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[places] could not write cache: {exc}")


def cache_age_seconds() -> float | None:
    """Seconds since the cache was written, or None if there isn't one."""
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    written = data.get("written_at") if isinstance(data, dict) else None
    if isinstance(written, (int, float)):
        return max(0.0, time.time() - written)
    try:
        return max(0.0, time.time() - CACHE_PATH.stat().st_mtime)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def search_places(areas: list[str] | None = None, limit: int = 60,
                  force_live: bool = False) -> list[dict]:
    """Fresh cache, else live Overpass, else stale cache, else the seed names.

    Always returns something. The only layer that can return rows with no phone
    number is the last one, and it says so.
    """
    wanted = [a for a in (areas or list(AREAS)) if a in AREAS] or list(AREAS)

    if not force_live:
        age = cache_age_seconds()
        if age is not None and age < CACHE_MAX_AGE_SECONDS:
            cached = read_cache()
            if cached:
                callable_count = sum(1 for r in cached if r.get("phone"))
                print(f"[places] cache hit ({age / 60:.0f} min old): "
                      f"{len(cached)} places, {callable_count} callable")
                return dedupe_and_rank(cached)[:limit]

    rows: list[dict] = []
    for key in wanted[:3]:  # three boxes is the most that finishes in time
        for element in _fetch_overpass(build_query(AREAS[key])):
            row = _row_from_element(element)
            if row:
                rows.append(row)

    if rows:
        ranked = dedupe_and_rank(rows)
        callable_count = sum(1 for r in ranked if r.get("phone"))
        print(f"[places] overpass: {len(ranked)} places, {callable_count} callable")
        write_cache(ranked)
        return ranked[:limit]

    cached = read_cache()
    if cached:
        age = cache_age_seconds()
        stamp = f", {age / 3600:.1f}h old" if age else ""
        print(f"[places] overpass down — using cache ({len(cached)} places{stamp})")
        return dedupe_and_rank(cached)[:limit]

    print(f"[places] overpass down AND cache empty — seed list only "
          f"({len(SEED_PLACES)} names, no phone numbers). Run "
          f"`python3 places.py --refresh-cache` while you have network.")
    return dedupe_and_rank([dict(p) for p in SEED_PLACES])[:limit]


def callable_only(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("phone")]


def find_by_name(rows: list[dict], name: str) -> dict | None:
    key = _norm_key(name)
    if not key:
        return None
    for row in rows:
        if _norm_key(row.get("name", "")) == key:
            return row
    for row in rows:
        other = _norm_key(row.get("name", ""))
        if other and (key in other or other in key):
            return row
    return None


if __name__ == "__main__":
    import sys

    if "--refresh-cache" in sys.argv:
        # Build the offline safety net from live data. Run this once, on wifi
        # that works, before the room fills up.
        harvested: list[dict] = []
        for area_key, bbox in AREAS.items():
            batch = [r for r in (_row_from_element(e) for e in _fetch_overpass(build_query(bbox))) if r]
            print(f"[places] {area_key}: {len(batch)} rows, {sum(1 for r in batch if r['phone'])} callable")
            harvested.extend(batch)
        ranked = dedupe_and_rank(harvested)
        write_cache(ranked, replace=True)   # rebuilding is what this flag is for
        print(f"\n[places] cache written: {len(ranked)} places, "
              f"{sum(1 for r in ranked if r['phone'])} with a dialable number -> {CACHE_PATH}")
        raise SystemExit(0)

    found = search_places()
    with_phone = callable_only(found)
    print(f"\n{len(found)} places, {len(with_phone)} with a dialable number\n")
    for place in found[:20]:
        print(f"  {place['phone'] or '(no phone)':<16} {place['name'][:38]:<40} {place['area']:<14} {place['cuisine'][:24]}")
