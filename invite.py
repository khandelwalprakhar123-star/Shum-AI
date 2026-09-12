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

Nothing is emitted unless a real date AND a real clock time can be pinned down.
A calendar entry at a guessed hour is worse than no calendar entry: it is wrong
in everybody's pocket, silently, a week later.
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
    """Hour and minute from "14:00", "2pm", "8:30 pm", "noon"."""
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

    found = re.search(r"\b(\d{1,2}):(\d{2})\b", low)          # 24-hour
    if found:
        hour, minute = int(found.group(1)), int(found.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
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
        return None
    day = _parse_day(when_text, now) or _parse_day(confirmed_time, now)
    if day is None:
        return None
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

    details = [f"Booked by Shum-AI on the phone. Under the name "
               f"{pending.get('booking_name') or 'a guest'}."]
    if party:
        details.append(f"Party of {int(float(party))}.")
    if pending.get("real_number"):
        details.append(f"Restaurant: {pending['real_number']}")
    for note in (collected.get("staff_notes"), pending.get("notes")):
        if note:
            details.append(str(note))
    if pending.get("constraints"):
        details.append("Mentioned on the call: " + "; ".join(pending["constraints"]))

    params = {
        "action": "TEMPLATE",
        "text": f"Dinner at {name}" if start.hour >= 17 else f"Lunch at {name}",
        "dates": f"{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
                 f"/{end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
        "details": "\n".join(details),
        "ctz": "Asia/Hong_Kong",
    }
    location = ", ".join(x for x in (name, pending.get("area"), "Hong Kong") if x)
    if location:
        params["location"] = location

    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)
