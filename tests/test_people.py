"""The people store: what it remembers, and what it refuses to remember.

The refusals matter more than the recall. An agent that accumulates
unattributable beliefs about people and then telephones a restaurant on their
behalf is worse than one that forgets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import people
from harness import Suite

SEPTEMBER = {
    "hard": [{"constraint": "no pork", "who": "Priya", "quote": "i can't eat pork"},
             {"constraint": "vegetarian", "who": "Akshay", "quote": "im vegetarian tho"}],
    "soft": [{"constraint": "pizza", "who": "Dan", "quote": "pizza would be nice"}],
    "vetoed": [{"thing": "hotpot", "times_rejected": 3, "quote": "not hotpot again"}],
    "coming_from": [{"who": "Dan", "place": "Sha Tin"}],
}


def run() -> Suite:
    s = Suite("people", expect_at_least=40)
    store: dict = {}

    learned = people.learn(store, -100, SEPTEMBER)
    s.contains("it reports learning Priya's constraint", learned, "Priya: no pork")
    s.contains("and Akshay's", learned, "Akshay: vegetarian")
    s.contains("and the group veto", learned, "the group has vetoed hotpot")
    s.contains("and where Dan travels from", learned, "Dan travels from Sha Tin")

    seen, remembered = people.stats(store, -100)
    s.eq("three people are known", seen, 3)
    s.eq("four things are remembered", remembered, 4)

    # --- the refusals -----------------------------------------------------
    unquoted: dict = {}
    people.learn(unquoted, -1, {
        "hard": [{"constraint": "hates coriander", "who": "Marcus", "quote": ""}],
        "soft": [{"constraint": "loves oysters", "who": "Marcus", "quote": "   "}],
        "vetoed": [{"thing": "sushi", "times_rejected": 2, "quote": ""}],
        "coming_from": [],
    })
    s.eq("a constraint with no quote is NEVER remembered", people.stats(unquoted, -1), (0, 0))

    unattributed: dict = {}
    people.learn(unattributed, -1, {
        "hard": [{"constraint": "no dairy", "who": "", "quote": "i can't do dairy"}],
        "soft": [], "vetoed": [], "coming_from": [],
    })
    s.eq("a constraint attributed to nobody is not remembered",
         people.stats(unattributed, -1), (0, 0))

    # --- reinforcement ----------------------------------------------------
    people.learn(store, -100, {
        "hard": [{"constraint": "no pork", "who": "Priya", "quote": "still no pork"}],
        "soft": [], "vetoed": [], "coming_from": [],
    })
    priya = store["-100"][people._key("Priya")]
    s.eq("saying it twice does not duplicate the entry", len(priya["hard"]), 1)
    s.eq("it increments the count instead", priya["hard"][0]["times"], 2)
    s.eq("the original quote is kept, not overwritten",
         priya["hard"][0]["quote"], "i can't eat pork")

    again = people.learn(store, -100, {
        "hard": [{"constraint": "no pork", "who": "Priya", "quote": "q"}],
        "soft": [], "vetoed": [], "coming_from": [],
    })
    s.eq("re-learning something known reports nothing new", again, [])

    # --- vetoes belong to the group, not to individuals -------------------
    # Recording a veto against every person made /who claim Akshay had
    # rejected something Priya said: the agent putting words in mouths.
    akshay = store["-100"][people._key("Akshay")]
    s.check("an individual carries no group veto", not akshay.get("vetoes"))
    group = store["-100"][people.GROUP_KEY]
    s.eq("the group carries it once", len(group["vetoes"]), 1)
    s.eq("and keeps the extractor's higher count", group["vetoes"][0]["times"], 3)
    s.check("the group is not counted as a person",
            people.GROUP_KEY not in [p.get("name") for p in people.people_in(store["-100"])])
    s.eq("people_in excludes the group entry", len(people.people_in(store["-100"])), 3)

    # --- recall -----------------------------------------------------------
    prior = people.prior_knowledge(store, -100)
    s.contains("recall states Priya's hard constraint", prior, "no pork")
    s.contains("and marks it HARD", prior, "HARD")
    s.contains("and Akshay's", prior, "vegetarian")
    s.contains("and the group veto with its count", prior, "rejected hotpot 3x")
    s.contains("and where Dan travels from", prior, "Sha Tin")

    # A single offhand remark is not a standing preference.
    s.check("a preference said once is withheld from recall",
            "pizza" not in prior,
            "one 'pizza would be nice' should not become a standing preference")
    people.learn(store, -100, {
        "soft": [{"constraint": "pizza", "who": "Dan", "quote": "pizza again?"}],
        "hard": [], "vetoed": [], "coming_from": [],
    })
    s.contains("said twice, it counts", people.prior_knowledge(store, -100), "pizza")

    s.eq("recall for an unknown chat is empty", people.prior_knowledge(store, -999), "")
    s.eq("recall for an empty store is empty", people.prior_knowledge({}, -100), "")

    # --- inspection -------------------------------------------------------
    shown = people.render(store, -100)
    s.contains("/who names the group section", shown, "The group")
    s.contains("/who quotes the line a constraint came from", shown, "i can't eat pork")
    s.contains("/who names the person", shown, "Priya")
    s.contains("/who explains how to correct it", shown, "/forget")
    s.contains("/who on an unknown chat explains how it learns",
               people.render({}, -100), "without a quote")

    # --- correction -------------------------------------------------------
    s.contains("forgetting one person confirms who", people.forget(store, -100, "Priya"), "Priya")
    s.eq("and they are gone", len(people.people_in(store["-100"])), 2)
    s.contains("forgetting a stranger says so", people.forget(store, -100, "Nobody"), "don't have")
    s.contains("forgetting everything reports the count", people.forget(store, -100, ""), "2 people")
    s.eq("and the chat is cleared", people.stats(store, -100), (0, 0))

    # --- durability -------------------------------------------------------
    tmp = Path(__file__).resolve().parent / "_test_people_roundtrip.json"
    try:
        fresh: dict = {}
        people.learn(fresh, -7, SEPTEMBER)
        people.save(fresh, tmp)
        s.check("memory is written to disk", tmp.exists())
        s.eq("and survives a round trip", people.load(tmp), fresh)

        tmp.write_text("{ not json", encoding="utf-8")
        s.eq("a corrupt memory file loads as empty rather than crashing",
             people.load(tmp), {})
        tmp.unlink()
        s.eq("a missing memory file is not an error", people.load(tmp), {})
    finally:
        if tmp.exists():
            tmp.unlink()

    s.eq("names match case- and punctuation-insensitively",
         people._key("Akshay (Ash)"), people._key("akshay ash"))
    # --- /who must survive a hostile name ---------------------------------
    #
    # bdbc200 escaped bot.py, pipeline.py and the bridge, and missed this file.
    # Every string here is interpolated into a parse_mode=HTML message, so one
    # person called "Ben & Jo" or a quote containing "<" made Telegram reject
    # the whole message with a 400 that nothing surfaces -- /who simply never
    # arrived, and the memory looked empty.
    from harness import unescaped as _unescaped  # noqa: PLC0415

    hostile = {}
    noted = people.learn(hostile, 77, {
        "hard": [{"who": "Ben & Jo", "constraint": "no <shellfish>",
                  "quote": "we can't do <shellfish> & nuts"}],
        "vetoed": [{"thing": "Tom & Jerry's", "quote": "not Tom & Jerry's again"}],
        "coming_from": [{"who": "A<B", "place": "Sha Tin & Tai Wai"}],
    })
    rendered = people.render(hostile, 77)
    s.eq("/who escapes every name, constraint and quote", _unescaped(rendered), [])
    s.contains("...and the ampersand survives as an entity", rendered, "Ben &amp; Jo")
    s.contains("...as do angle brackets in a quote", rendered, "&lt;shellfish&gt;")
    s.eq("the 'noted for next time' lines are escaped too",
         [u for line in noted for u in _unescaped(line)], [])
    s.eq("so is the /forget reply",
         _unescaped(people.forget(hostile, 77, "Ben & Jo")), [])

    # prior_knowledge() is the exception, on purpose: it goes into an LLM
    # prompt, not a Telegram message. Escaping it would feed the model
    # "&amp;" and teach it to say that out loud on a phone call.
    prompt = people.prior_knowledge(hostile, 77)
    s.check("prior_knowledge stays raw, because it is prompt text",
            "&amp;" not in prompt,
            "HTML entities in a prompt end up spoken to a restaurant")

    return s
