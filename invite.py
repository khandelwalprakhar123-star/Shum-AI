"""invite.py — a Google Calendar invite anyone can click, with no account linking.

Google's /calendar/render?action=TEMPLATE endpoint takes the whole event in the
URL, so one link puts the booking in everybody's own calendar. No OAuth, no API
key, no scopes, no consent screen, nothing to expire mid-demo -- and it works
for people who do not use Google Calendar at all, because it is just a link
they can ignore.

Why not collect emails and send real invites: Telegram does not expose member
email addresses, and it should not. Asking six people to type an address into a
group chat to get a calendar entry is worse than a link they tap once, and
building an address book for a dinner is not a trade this project should make.

Nothing is emitted unless a real clock time can be pinned down. A calendar
entry at a guessed hour is worse than none: it is wrong in everybody's pocket,
silently, until they are late. A bare time with no date ("8pm") is not that
guess -- the hour is known and only the day is implied, so it resolves to the
next time that hour comes round, which is how everyone in the chat already
read it.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timedelta, timezone

HK = timezone(timedelta(hours=8))          # Asia/Hong_Kong, no DST
DEFAULT_MEAL_MINUTES = 90

_WEEKDAYS = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
             "thurs": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_clock(text: str) -> tuple[int, int] | None:
    """Hour and minute from "14:00", "2pm", "8:30 pm", "noon", "table at 8"."""
    low = (text or "").lower()
    if re.search(r"\bnoon|midday\b", low):
        return 12, 0
    if re.search(r"\bmidnight\b", low):
        return 0, 0

    found = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", low)
    if found:
        hour = int(found.group(1)) % 12
        if found.group(3) == "pm":
            hour += 12
        return hour, int(found.group(2) or 0)

    # A clock with no am/pm. "8pm" is unambiguous; "7:30" and "at 8" are not,
    # and reading them wrong is a TWELVE HOUR error sitting in six calendars.
    #
    # This books restaurant tables out of a group chat arguing about a meal,
    # so a bare 1-11 means the afternoon or evening: half past seven in the
    # MORNING is not a thing anyone here means. Three exceptions are honoured
    # rather than assumed away -- an explicit 24-hour hour (13 and up), a
    # written leading zero ("07:30", which somebody typed on purpose), and
    # text that actually names a morning or midday meal.
    morning = re.search(r"\b(breakfast|brunch|morning|lunch|midday|a\.?m\.?)\b", low)

    found = re.search(r"\b(\d{1,2}):(\d{2})\b", low)
    if found:
        hour, minute = int(found.group(1)), int(found.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        if 1 <= hour <= 11 and not found.group(1).startswith("0") and not morning:
            hour += 12
        return hour, minute

    # A bare hour, but only where the words make it a time: "table at 8",
    # "book us in for 7". Never a loose digit -- "party of 8" is not 8 o'clock.
    found = re.search(r"\b(?:at|for|around|by|from)\s+(\d{1,2})\b", low)
    if found:
        hour = int(found.group(1))
        if hour > 23:
            return None
        if 1 <= hour <= 11 and not morning:
            hour += 12
        return hour, 0
    return None


def _parse_day(text: str, now: datetime) -> datetime | None:
    """The date the booking falls on, or None if the words do not say."""
    low = (text or "").lower()
    if "tomorrow" in low:
        return now + timedelta(days=1)
    if "today" in low or "tonight" in low or "this " in low:
        return now
    for name, index in _WEEKDAYS.items():
        if re.search(r"\b" + name + r"[a-z]*\b", low):
            ahead = (index - now.weekday()) % 7
            return now + timedelta(days=ahead or 7)   # "Friday" said on Friday means next week
    return None


def event_time(when_text: str, confirmed_time: str = "",
               now: datetime | None = None) -> datetime | None:
    """Pin the booking to a real moment, or return None.

    The restaurant's confirmed time wins over what was requested -- it is the
    one that was actually agreed. The DAY still comes from the request, because
    staff say "eight o'clock", not "the eighth of October".
    """
    now = now or datetime.now(HK)
    clock = _parse_clock(confirmed_time) or _parse_clock(when_text)
    if clock is None:
        # No hour at all. "this evening", "lunchtime", "sometime Friday" --
        # there is nothing to put in a calendar but a guess, and a guess is
        # wrong in everybody's pocket until they are late.
        return None

    day = _parse_day(when_text, now) or _parse_day(confirmed_time, now)
    if day is None:
        # A bare clock with no day: "8pm", "table at 7:30". Read it the way a
        # person in the group chat reads it -- the next time it comes round.
        # This is NOT the guess the check above is guarding against: the hour
        # is known, and only the date is implied. Refusing here was wrong, and
        # it cost a real booking its calendar link: the chat said "8pm", which
        # is not vague to anyone, and the link was withheld anyway.
        day = now
        if (clock[0], clock[1]) <= (now.hour, now.minute):
            day = now + timedelta(days=1)

    return day.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)


def calendar_url(pending: dict, collected: dict | None = None,
                 now: datetime | None = None) -> str | None:
    """A Google Calendar template link for a confirmed booking, or None."""
    collected = collected or {}
    start = event_time(str(pending.get("when_text") or ""),
                       str(collected.get("confirmed_time") or ""), now=now)
    if start is None:
        return None

    name = str(pending.get("restaurant_display") or pending.get("restaurant_name") or "").strip()
    if not name:
        return None
    end = start + timedelta(minutes=DEFAULT_MEAL_MINUTES)
    party = collected.get("confirmed_party_size") or pending.get("party_size")

    if pending.get("demo_override"):
        # The safety rail was up: this call went to a number we control, not to
        # the restaurant. So there is no table. An entry that says "Dinner at
        # Mezzo" with the venue's real phone number in six people's calendars
        # is a reservation that does not exist -- and unlike the chat message,
        # nobody re-reads a calendar entry a week later to check. Say it here,
        # in the artefact itself, where the person who turns up will see it.
        details = ["REHEARSAL - there is no real reservation. Shum-AI placed this "
                   "call to a test number rather than to the restaurant "
                   "(DEMO_PHONE was set), so nothing was booked."]
    else:
        details = [f"Booked by Shum-AI on the phone. Under the name "
                   f"{pending.get('booking_name') or 'a guest'}."]
    if party:
        details.append(f"Party of {int(float(party))}.")
    if pending.get("real_number") and not pending.get("demo_override"):
        details.append(f"Restaurant: {pending['real_number']}")
    for note in (collected.get("staff_notes"), pending.get("notes")):
        if note:
            details.append(str(note))
    if pending.get("constraints"):
        details.append("Mentioned on the call: " + "; ".join(pending["constraints"]))

    params = {
        "action": "TEMPLATE",
        "text": (("[rehearsal] " if pending.get("demo_override") else "")
                 + (f"Dinner at {name}" if start.hour >= 17 else f"Lunch at {name}")),
        "dates": f"{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
                 f"/{end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
        "details": "\n".join(details),
        "ctz": "Asia/Hong_Kong",
    }
    location = ", ".join(x for x in (name, pending.get("area"), "Hong Kong") if x)
    if location:
        params["location"] = location

    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)
