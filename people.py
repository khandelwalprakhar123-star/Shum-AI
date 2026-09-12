#!/usr/bin/env python3
"""people.py — what the agent remembers about each person, between conversations.

The pitch has always been "it has read the last two hundred messages". This is
the next step: it remembers the people.

Priya saying "I can't eat pork" in July should not need re-saying in September.
A form would make her re-enter it every time; a chat agent with memory asks
once. That asymmetry is the entire argument for living in the chat, so the
memory is the product, not a cache.

Two rules keep it honest:

1. NOTHING IS REMEMBERED WITHOUT A QUOTE. A constraint is only written to a
   person's profile if the extractor attached the line it came from. Without
   that, one bad inference becomes permanent and silently shapes every future
   recommendation -- which is far worse than forgetting.

2. EVERY MEMORY IS CORRECTABLE AND INSPECTABLE. /who prints what it thinks it
   knows and who it learned it from; /forget removes it. An agent that
   accumulates unauditable beliefs about people is not one you should give a
   telephone.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from tgtext import esc

ROOT = Path(__file__).resolve().parent
PEOPLE_PATH = ROOT / "people.json"

# A person has to be seen saying something twice before a soft preference is
# treated as settled. Hard constraints count immediately -- an allergy does not
# need corroborating.
SOFT_CONFIRM_THRESHOLD = 2

# Vetoes belong to the GROUP, not to whoever happened to voice one. Recording
# "rejected hotpot" against every individual made /who claim Akshay had
# rejected something Priya said, which is the agent putting words in people's
# mouths -- exactly the failure that makes a memory untrustworthy.
GROUP_KEY = "__group__"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def load(path: Path | None = None) -> dict:
    target = path or PEOPLE_PATH
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        # A corrupt memory file must never stop the bot. Forgetting is
        # recoverable; refusing to start in front of an audience is not.
        print("[people] memory file unreadable — continuing with an empty memory")
        return {}


def save(store: dict, path: Path | None = None) -> None:
    target = path or PEOPLE_PATH
    try:
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    except OSError as exc:
        print(f"[people] could not save memory: {exc}")


def _group(store: dict, chat_id) -> dict:
    chat = store.setdefault(str(chat_id), {})
    group = chat.setdefault(GROUP_KEY, {})
    group.setdefault("vetoes", [])
    return group


def people_in(chat: dict) -> list[dict]:
    """Real people only, never the group pseudo-entry."""
    return [p for key, p in chat.items() if key != GROUP_KEY and isinstance(p, dict)]


def _profile(store: dict, chat_id, name: str) -> dict:
    chat = store.setdefault(str(chat_id), {})
    person = chat.setdefault(_key(name), {})
    person.setdefault("name", name.strip() or "someone")
    person.setdefault("hard", [])
    person.setdefault("soft", [])
    person.setdefault("vetoes", [])
    person.setdefault("areas", [])
    person.setdefault("first_seen", _now())
    person["last_seen"] = _now()
    if name.strip():
        person["name"] = name.strip()
    return person


# ---------------------------------------------------------------------------
# Learning
# ---------------------------------------------------------------------------

def _upsert(entries: list[dict], text: str, quote: str, field: str = "constraint") -> bool:
    """Add or reinforce one remembered item. Returns True if newly added."""
    key = _norm(text)
    for entry in entries:
        if _norm(entry.get(field, "")) == key:
            entry["times"] = int(entry.get("times", 1)) + 1
            entry["last_seen"] = _now()
            if quote and not entry.get("quote"):
                entry["quote"] = quote
            return False
    entries.append({
        field: text.strip(),
        "quote": quote.strip(),
        "times": 1,
        "first_seen": _now(),
        "last_seen": _now(),
    })
    return True


def learn(store: dict, chat_id, constraints: dict) -> list[str]:
    """Fold one /decide's extraction into the people store.

    Returns human-readable notes about what was newly learned, so the chat can
    be told rather than the memory growing invisibly.
    """
    learned: list[str] = []

    for bucket, field in (("hard", "constraint"), ("soft", "constraint")):
        for item in constraints.get(bucket) or []:
            who = str(item.get("who") or "").strip()
            text = str(item.get(field) or "").strip()
            quote = str(item.get("quote") or "").strip()
            # Rule 1: no quote, no memory. An unattributed or unevidenced
            # constraint would become a permanent invisible bias.
            if not who or not text or not quote:
                continue
            person = _profile(store, chat_id, who)
            if _upsert(person[bucket], text, quote):
                if bucket == "hard":
                    learned.append(f"{esc(person['name'])}: {esc(text)}")

    for item in constraints.get("vetoed") or []:
        thing = str(item.get("thing") or "").strip()
        quote = str(item.get("quote") or "").strip()
        if not thing or not quote:
            continue
        group = _group(store, chat_id)
        rejections = _clean_times(item.get("times_rejected"))
        if _upsert(group["vetoes"], thing, quote, field="thing"):
            learned.append(f"the group has vetoed {esc(thing)}")
        # Carry the extractor's own count across, since it read the whole
        # history and may have seen more rejections than this one pass.
        for entry in group["vetoes"]:
            if _norm(entry.get("thing", "")) == _norm(thing):
                entry["times"] = max(int(entry.get("times", 1)), rejections)

    for item in constraints.get("coming_from") or []:
        who = str((item or {}).get("who") or "").strip()
        place = str((item or {}).get("place") or "").strip()
        if not who or not place:
            continue
        person = _profile(store, chat_id, who)
        areas = person.setdefault("areas", [])
        if place not in areas:
            areas.append(place)
            learned.append(f"{esc(person['name'])} travels from {esc(place)}")

    return learned


def _clean_times(value) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def forget(store: dict, chat_id, name: str = "") -> str:
    """Drop one person's memory, or the whole chat's."""
    chat = store.get(str(chat_id)) or {}
    if not name:
        count = len(people_in(chat))
        store.pop(str(chat_id), None)
        return f"Forgotten everything \u2014 all {count} people and the group's vetoes."
    key = _key(name)
    if key in chat:
        gone = chat.pop(key)
        return f"Forgotten everything I knew about {esc(gone.get('name', name))}."
    return f"I don't have anything remembered for {esc(name)}."


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------

def prior_knowledge(store: dict, chat_id) -> str:
    # NOT escaped, deliberately: this string goes into an LLM prompt, not into
    # a Telegram message. HTML-escaping it would feed the model "&amp;" and
    # "&lt;" and teach it to repeat them back into a phone call.
    """The block handed to the extractor as things already established.

    Soft preferences below the confirmation threshold are deliberately left
    out: one offhand "pizza would be nice" is not a standing preference, and
    treating it as one makes the agent feel like it is putting words in
    people's mouths.
    """
    chat = store.get(str(chat_id)) or {}
    if not chat:
        return ""

    lines: list[str] = []
    for item in (chat.get(GROUP_KEY, {}).get("vetoes") or []):
        if int(item.get("times", 1)) >= SOFT_CONFIRM_THRESHOLD:
            lines.append(f"- THE GROUP has rejected {item.get('thing')} "
                         f"{item.get('times')}x before - treat it as vetoed")

    for person in sorted(people_in(chat), key=lambda p: p.get("name", "")):
        bits: list[str] = []
        for item in person.get("hard") or []:
            bits.append(f"{item.get('constraint')} (HARD, said {item.get('times', 1)}x)")
        for item in person.get("soft") or []:
            if int(item.get("times", 1)) >= SOFT_CONFIRM_THRESHOLD:
                bits.append(f"prefers {item.get('constraint')}")
        for area in person.get("areas") or []:
            bits.append(f"usually travels from {area}")
        if bits:
            lines.append(f"- {person.get('name')}: " + "; ".join(bits))

    if not lines:
        return ""
    return "\n".join(lines)


def render(store: dict, chat_id) -> str:
    """/who — everything remembered, with the line it was learned from."""
    chat = store.get(str(chat_id)) or {}
    if not chat:
        return ("<b>I don't know anyone here yet.</b>\n\n"
                "I learn from /decide: anything someone says about what they can or "
                "can't eat gets remembered, along with the line they said it in. "
                "Nothing is remembered without a quote to back it.")

    roster = people_in(chat)
    lines = [f"<b>What I remember about {len(roster)} people</b>"]

    vetoes = chat.get(GROUP_KEY, {}).get("vetoes") or []
    if vetoes:
        lines.append("\n<b>The group</b>")
        for item in vetoes:
            lines.append(f"  \u2022 vetoed <b>{esc(item.get('thing'))}</b> "
                         f"\u00b7 {esc(item.get('times'))}\u00d7")
            if item.get("quote"):
                lines.append(f"      \u201c{esc(item['quote'])}\u201d")

    for person in sorted(roster, key=lambda p: p.get("name", "")):
        lines.append(f"\n<b>{esc(person.get('name'))}</b>")
        for item in person.get("hard") or []:
            times = f" · said {esc(item['times'])}×" if int(item.get("times", 1)) > 1 else ""
            lines.append(f"  • <b>{esc(item.get('constraint'))}</b>{times}")
            if item.get("quote"):
                lines.append(f"      “{esc(item['quote'])}”")
        for item in person.get("soft") or []:
            mark = "" if int(item.get("times", 1)) >= SOFT_CONFIRM_THRESHOLD else " <i>(once, not counted yet)</i>"
            lines.append(f"  • <i>prefers</i> {esc(item.get('constraint'))}{mark}")
        for item in person.get("vetoes") or []:
            lines.append(f"  • rejected {esc(item.get('thing'))} {esc(item.get('times'))}×")
        for area in person.get("areas") or []:
            lines.append(f"  • travels from {esc(area)}")

    lines.append("\n<i>/forget NAME to drop someone, /forget all to wipe it.</i>")
    return "\n".join(lines)


def stats(store: dict, chat_id) -> tuple[int, int]:
    chat = store.get(str(chat_id)) or {}
    roster = people_in(chat)
    remembered = sum(len(p.get("hard") or []) + len(p.get("soft") or []) for p in roster)
    remembered += len(chat.get(GROUP_KEY, {}).get("vetoes") or [])
    return len(roster), remembered
