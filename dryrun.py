#!/usr/bin/env python3
"""dryrun.py — run the whole brain against the real APIs, with no Telegram.

    python3 dryrun.py

This is exactly what /decide does: read a conversation, name the constraints
with the evidence attached, search for candidates, pick three. The only thing
it does not do is post to a chat or place a call.

It exists for two reasons. Before a demo it proves the expensive half works
without spending a Telegram group or an agent-minute. And for anyone evaluating
this repo, it is a way to see the actual product -- the constraint extraction --
without creating a bot, disabling privacy mode and inviting it to a group.

Pass a file to use your own conversation:
    python3 dryrun.py my_chat.txt
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import exa_search
import pipeline
import places
from envlite import load_env, warn_if_tls_broken

BOLD, DIM, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[0m"

# A realistic argument. Constraints are scattered, restated, contradicted and
# buried -- including two that were settled weeks ago and never repeated, which
# is the whole point. Nobody would ever type these into a form.
SAMPLE = """Marcus: right. friday. where are we going
Priya: not hotpot
Dan: why not hotpot
Priya: we did hotpot in august AND the week before that
Marcus: she's right, twice
Dan: fine fine
Priya: also just flagging again, i don't eat pork. not a preference, i actually can't
Marcus: noted
Dan: im coming from sha tin straight from work so somewhere i can actually get to
Dan: last time we did causeway bay and i got there at 9
Marcus: ok so 6 of us?
Priya: 6 yes
Dan: is aisha coming
Marcus: no she's away
Priya: 6 then
Marcus: what are we spending
Dan: we said like 250-300 each back in july and that felt right
Marcus: yeah let's stay around there
Priya: honestly i'd love thai or vietnamese, something with actual vegetables
Dan: im easy as long as it's not a 40 min walk from an mtr
Marcus: friday 8pm then?
Priya: 8 works
Dan: 8 is good
Marcus: someone book something
Dan: someone always says that and then nobody does
Priya: lol"""


def timed(label: str, fn, *args, **kwargs):
    start = time.time()
    result = fn(*args, **kwargs)
    print(f"{DIM}  {label}: {time.time() - start:.2f}s{RESET}")
    return result


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def main() -> int:
    load_env(ROOT / ".env")
    if not warn_if_tls_broken("dryrun"):
        return 1

    chat = Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else SAMPLE

    # bot.py splits multi-line messages into separate history lines before the
    # extractor ever sees them, because people paste several thoughts at once
    # and each one is its own constraint. Mirror that here or the dry run is
    # not testing the same input the bot produces.
    lines = [ln.strip() for ln in chat.splitlines() if ln.strip()]
    print(f"\n{BOLD}Shum-AI dry run{RESET}")
    print(f"{DIM}{len(lines)} lines of conversation, no Telegram, no phone call{RESET}\n")

    print(f"{BOLD}1. Reading the argument{RESET}")
    constraints = timed("extract_constraints", pipeline.extract_constraints, "\n".join(lines))
    print(f"  {DIM}source: {constraints.get('source')}{RESET}\n")
    print(strip_html(pipeline.render_constraints(constraints)))

    print(f"\n{BOLD}2. Finding candidates{RESET}")
    # The same three steps, in the same order, as handle_decide(): pick the
    # districts from the constraints, take the WHOLE pool, then rank by those
    # districts and cut. Doing any of it differently would make this script's
    # "exactly what /decide does" claim a lie, which is worse than not having
    # the script -- it is what people evaluate the repo with.
    wanted_areas = places.areas_for(constraints)
    osm = timed("overpass", places.search_places, wanted_areas, None)
    exa = timed("exa", exa_search.search, constraints)
    candidates = places.relevance_rank(exa_search.merge(osm, exa), constraints)[:60]
    callable_n = sum(1 for c in candidates if c.get("phone"))
    origins = places.origin_districts(constraints)
    destinations = places.destination_districts(constraints)
    print(f"  {len(osm)} from OpenStreetMap, {len(exa)} from Exa, "
          f"{len(candidates)} ranked, {GREEN}{callable_n} callable{RESET}")
    print(f"  {DIM}districts searched: {', '.join(wanted_areas)}{RESET}")
    if origins and not destinations:
        fair = places.meeting_districts(origins)[:3]
        print(f"  {DIM}nobody named a destination, so the fair middle between "
              f"{', '.join(origins)} is {', '.join(fair)}{RESET}")
    if exa:
        print(f"  {DIM}Exa query: {exa_search.build_query(constraints)[:100]}...{RESET}")

    print(f"\n{BOLD}3. Choosing three{RESET}")
    proposal = timed("propose", pipeline.propose, constraints, candidates)
    print(f"  {DIM}source: {proposal.get('source')}{RESET}\n")

    for index, pick in enumerate(proposal.get("picks", []), 1):
        if not pick.get("name"):
            print(f"  {YELLOW}{index}. (a pick came back with no name - skipped){RESET}")
            continue
        phone = pick.get("phone") or f"{YELLOW}no number - cannot be called{RESET}"
        print(f"  {BOLD}{index}. {pick['name']}{RESET}"
              f"{(' · ' + pick['area']) if pick.get('area') else ''}")
        print(f"     {phone}")
        if pick.get("why"):
            print(f"     {pick['why']}")
        if pick.get("satisfies"):
            print(f"     {GREEN}satisfies:{RESET} {', '.join(pick['satisfies'][:4])}")
        if pick.get("fails"):
            print(f"     {YELLOW}fails:{RESET} {', '.join(pick['fails'][:3])}")
        print()
    if proposal.get("tradeoff_line"):
        print(f"  {DIM}{proposal['tradeoff_line']}{RESET}")

    # The checks that decide whether a demo is worth running.
    print(f"\n{BOLD}Sanity{RESET}")
    picks = proposal.get("picks", [])
    verdicts = [
        ("picked exactly three", len(picks) == 3),
        ("every pick is a real candidate, not invented",
         all(places.find_by_name(candidates, p["name"]) for p in picks)),
        ("at least one pick is callable", any(p.get("phone") for p in picks)),
        ("constraints came from a model, not the keyword fallback",
         constraints.get("source") in ("gemini", "openrouter")),
        ("picks came from a model, not the heuristic",
         proposal.get("source") in ("gemini", "openrouter")),
        ("found the dietary constraint",
         any("pork" in str(h).lower() for h in constraints.get("hard", []))),
        ("found the repeat veto",
         any("hotpot" in str(v.get("thing", "")).lower() for v in constraints.get("vetoed", []))),
        ("found the travel origin",
         any("sha tin" in str(c.get("place", "")).lower() for c in constraints.get("coming_from", []))),
    ]
    for label, passed in verdicts:
        print(f"  {GREEN + 'yes' + RESET if passed else YELLOW + 'NO ' + RESET}  {label}")

    return 0 if all(v for _, v in verdicts) else 1


if __name__ == "__main__":
    sys.exit(main())
