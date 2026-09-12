"""test_invite.py — the calendar link.

The invariant that matters most here is the refusal: no clock time, no link.
A calendar entry at a guessed hour is wrong in everybody's pocket, quietly,
until they are late. Half the checks below exist to keep that refusal honest.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime

import invite
from harness import Suite

# Saturday 12 September 2026, 13:00 Hong Kong.
NOW = datetime(2026, 9, 12, 13, 0, tzinfo=invite.HK)

PENDING = {
    "restaurant_display": "Nha Trang",
    "restaurant_name": "nha trang",
    "area": "Quarry Bay",
    "when_text": "today at 2 pm",
    "booking_name": "Prakhar",
    "party_size": 2,
    # Deliberately undialable: no Hong Kong number starts with a 0.
    # A plausible-looking one in a fixture is a real number belonging
    # to somebody, and this project has invented one HK number too many.
    "real_number": "+85200000000",
    "constraints": ["no pork", "no beef"],
}


def query_of(url: str) -> dict:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}


def run() -> Suite:
    s = Suite("invite", expect_at_least=44)

    # ---- the clock parser -------------------------------------------------
    for text, want in [
        ("14:00", (14, 0)), ("2pm", (14, 0)), ("2 pm", (14, 0)),
        ("8:30 pm", (20, 30)), ("7pm", (19, 0)), ("noon", (12, 0)),
        ("12am", (0, 0)), ("12pm", (12, 0)), ("19:45", (19, 45)),
        ("table for 8 at 8", None),          # "8" alone is not a time here
    ]:
        s.eq(f"clock {text!r}", invite._parse_clock(text), want)

    s.eq("no clock in 'this evening'", invite._parse_clock("this evening"), None)
    s.eq("no clock in 'lunchtime'", invite._parse_clock("lunchtime"), None)
    s.eq("25:00 is not a time", invite._parse_clock("25:00"), None)
    s.eq("junk minutes rejected", invite._parse_clock("12:75"), None)

    # ---- the day parser ---------------------------------------------------
    s.eq("tomorrow", invite._parse_day("tomorrow 7pm", NOW).date(),
         datetime(2026, 9, 13).date())
    s.eq("tonight is today", invite._parse_day("tonight at 8", NOW).date(),
         datetime(2026, 9, 12).date())
    s.eq("next Friday from a Saturday", invite._parse_day("friday 8pm", NOW).date(),
         datetime(2026, 9, 18).date())
    s.eq("Saturday said ON a Saturday means next week",
         invite._parse_day("saturday 8pm", NOW).date(), datetime(2026, 9, 19).date())
    s.eq("a bare time names no day", invite._parse_day("at 8pm", NOW), None)

    # ---- event_time: the refusal -----------------------------------------
    s.eq("refuses 'this evening'", invite.event_time("this evening", now=NOW), None)
    s.eq("refuses 'sometime next week'",
         invite.event_time("sometime next week", now=NOW), None)
    s.eq("refuses an empty request", invite.event_time("", now=NOW), None)
    s.eq("refuses a day with no clock", invite.event_time("friday", now=NOW), None)

    pinned = invite.event_time("today at 2 pm", now=NOW)
    s.eq("pins the requested time", (pinned.hour, pinned.minute), (14, 0))

    # The restaurant's word beats the request: "we can do 8:30, not 8".
    moved = invite.event_time("tomorrow at 8pm", "20:30", now=NOW)
    s.eq("the confirmed time wins over the request",
         (moved.hour, moved.minute), (20, 30))
    s.eq("...while the DAY still comes from the request", moved.date(),
         datetime(2026, 9, 13).date())

    # ---- the URL ----------------------------------------------------------
    url = invite.calendar_url(PENDING, {"confirmed_time": "14:00",
                                        "confirmed_party_size": 2}, now=NOW)
    s.check("a confirmed booking yields a link", bool(url))
    s.check("it is the no-auth render endpoint",
            url.startswith("https://calendar.google.com/calendar/render?"),
            "anything needing OAuth is the wrong endpoint for a group chat")
    q = query_of(url)
    s.eq("action is TEMPLATE", q.get("action"), "TEMPLATE")
    s.eq("timezone is stated explicitly", q.get("ctz"), "Asia/Hong_Kong")
    s.contains("title names the place", q.get("text", ""), "Nha Trang")
    s.eq("a 2pm booking is lunch", q.get("text"), "Lunch at Nha Trang")
    s.contains("location is findable", q.get("location", ""), "Quarry Bay")
    s.contains("location says Hong Kong", q.get("location", ""), "Hong Kong")
    s.contains("details carry the booking name", q.get("details", ""), "Prakhar")
    s.contains("details carry the party size", q.get("details", ""), "Party of 2")
    s.contains("details carry the restaurant's number",
               q.get("details", ""), "+85200000000")
    s.contains("details repeat what was asked on the call",
               q.get("details", ""), "no pork")

    # 06:00Z is 14:00 in Hong Kong. Google reads Z-stamps as UTC, so an
    # off-by-eight here puts dinner at six in the morning.
    s.contains("dates are UTC Z-stamps", q.get("dates", ""), "20260912T060000Z")
    s.contains("...and run 90 minutes", q.get("dates", ""), "20260912T073000Z")

    evening = dict(PENDING, when_text="tomorrow at 8pm")
    s.eq("an 8pm booking is dinner",
         query_of(invite.calendar_url(evening, {}, now=NOW)).get("text"),
         "Dinner at Nha Trang")

    # ---- the URL's refusals ----------------------------------------------
    s.eq("no link without a time",
         invite.calendar_url(dict(PENDING, when_text="this evening"), {}, now=NOW), None)
    s.eq("no link without a restaurant",
         invite.calendar_url({"when_text": "today at 2pm"}, {}, now=NOW), None)
    s.eq("no link for an empty pending", invite.calendar_url({}, {}, now=NOW), None)
    s.check("missing collected is not a crash",
            invite.calendar_url(PENDING, None, now=NOW) is not None)

    # A party size arriving as a float string must not render "Party of 4.0".
    floaty = query_of(invite.calendar_url(PENDING, {"confirmed_party_size": "4.0"}, now=NOW))
    s.contains("party size renders as a whole number",
               floaty.get("details", ""), "Party of 4")
    s.check("...and not as a float", "4.0" not in floaty.get("details", ""))

    # ---- it must survive the outcome message ------------------------------
    import bridge.server as server  # noqa: PLC0415 - after sys.path is set up

    text = server.format_outcome(PENDING, {
        "collected": {"status": "confirmed", "confirmed_time": "14:00",
                      "confirmed_party_size": 2, "booking_name": "Prakhar"},
        "transcript": [],
    })
    s.contains("a confirmed call offers the link", text, "calendar.google.com")
    s.contains("...as a tappable anchor", text, 'href="https://calendar.google.com')
    s.check("the ampersands in the href are entity-escaped",
            "&amp;action=TEMPLATE" in text or "?action=TEMPLATE&amp;" in text,
            "a raw & inside an HTML href breaks Telegram's parser and eats the message")

    vague = server.format_outcome(dict(PENDING, when_text="this evening"), {
        "collected": {"status": "confirmed"}, "transcript": [],
    })
    s.check("a vague time offers no link", "calendar.google.com" not in vague)
    s.contains("...and says why", vague, "guessed one would be worse")

    for bad in ("declined", "full", "no_answer"):
        nope = server.format_outcome(PENDING, {
            "collected": {"status": bad, "confirmed_time": "14:00"}, "transcript": [],
        })
        s.check(f"no calendar link on {bad}", "calendar.google.com" not in nope,
                "there is no booking to put in a calendar")

    return s


if __name__ == "__main__":
    print(run().summary())
