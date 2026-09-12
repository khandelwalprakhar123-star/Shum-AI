"""The bot: backlog safety, poll shape, vote tally, and the safety rails.

Every Telegram call is intercepted, so this drives the real handlers and then
inspects exactly what would have been sent.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bot
import people
from harness import Suite, unescaped

TMP_PENDING = Path(__file__).resolve().parent / "_test_pending.json"


class FakeTelegram:
    """Records calls instead of making them. Queue results for getUpdates etc."""

    def __init__(self, results=None):
        self.calls: list[tuple[str, dict]] = []
        self.results = dict(results or {})

    def call(self, method, timeout=None, **params):
        self.calls.append((method, params))
        value = self.results.get(method)
        if isinstance(value, list):
            return value.pop(0) if value else None
        return value

    def send(self, chat_id, text, **extra):
        self.calls.append(("sendMessage", {"chat_id": chat_id, "text": text, **extra}))
        return {"message_id": 1}

    def sent_text(self) -> str:
        return "\n".join(p.get("text", "") for m, p in self.calls if m == "sendMessage")

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def find(self, method) -> dict | None:
        for m, p in self.calls:
            if m == method:
                return p
        return None


def msg(text, chat_id=-100, name="Marcus", update_id=1):
    return {"update_id": update_id, "message": {
        "chat": {"id": chat_id}, "from": {"first_name": name, "id": 7}, "text": text}}


SUITE_STATE = Path(__file__).resolve().parent / "_test_suite_state.json"
SUITE_PEOPLE = Path(__file__).resolve().parent / "_test_suite_people.json"


def run() -> Suite:
    s = Suite("bot", expect_at_least=103)
    bot.PENDING_PATH = TMP_PENDING
    # notify_bridge_dial makes a real POST to localhost:8080. Stub it, and
    # exercise both branches deliberately further down rather than letting a
    # unit test decide whether a live bridge happens to be listening.
    real_notify = bot.notify_bridge_dial
    bridge_calls: list[bool] = []
    bot.notify_bridge_dial = lambda: (bridge_calls.append(True), True)[1]
    # Redirect persistence for the entire suite. Without this, every handler
    # under test wrote into the project's real chat_state.json and the next
    # real bot start restored test chats as if they were a conversation.
    real_state_path = bot.STATE_PATH
    # Fingerprint the real state file so we can prove afterwards that the suite
    # did not touch it. Asserting it simply does not EXIST was wrong: a running
    # bot creates it legitimately, so that check passed only on machines where
    # the bot had never run, and went red the moment it had.
    real_state_before = (
        (real_state_path.exists(),
         real_state_path.stat().st_mtime_ns if real_state_path.exists() else 0,
         real_state_path.stat().st_size if real_state_path.exists() else 0)
    )
    bot.STATE_PATH = SUITE_STATE
    real_people_path = people.PEOPLE_PATH
    real_people_before = (
        (real_people_path.exists(),
         real_people_path.stat().st_mtime_ns if real_people_path.exists() else 0)
    )
    people.PEOPLE_PATH = SUITE_PEOPLE
    bot.PEOPLE = {}
    for key in ("DEMO_PHONE", "CONSENTED_NUMBERS", "ALLOW_ANY_NUMBER"):
        os.environ.pop(key, None)
    os.environ["BOOKER_NAME"] = "Prakhar"
    os.environ["CALLBACK_NUMBER"] = "+85298887777"

    # --- history handling -------------------------------------------------
    st = bot.ChatState()
    st.add("Priya", "ok so\npriya can't do pork\nand marcus is coming from sha tin")
    s.eq("a multi-line message becomes three history lines", len(st.history), 3)
    s.eq("each line is attributed to the speaker", st.history[1], "Priya: priya can't do pork")
    st.add("Dan", "   \n\n  ")
    s.eq("blank lines are not stored", len(st.history), 3)
    st.add("Dan", "single line")
    s.eq("a single-line message adds one line", len(st.history), 4)

    big = bot.ChatState()
    for i in range(250):
        big.add("X", f"message {i}")
    s.eq("history is capped at 200", len(big.history), bot.HISTORY_LIMIT)
    s.contains("the cap drops the oldest, not the newest", big.as_text(), "message 249")
    s.check("the oldest message really is gone", "message 0:" not in big.as_text())

    # --- backlog drain: absorb, never act --------------------------------
    # getUpdates hands back everything since the last acknowledged offset. If
    # a stale /decide or a stale button press is replayed, the bot dies before
    # it says hello.
    bot.STATE.clear()
    backlog = [
        msg("hey where are we eating", update_id=10),
        msg("/decide", update_id=11),
        msg("not hotpot", update_id=12),
        {"update_id": 13, "callback_query": {"id": "cb1", "data": "ok:stale",
         "from": {"first_name": "Dan"}, "message": {"chat": {"id": -100}}}},
        msg("/close", update_id=14),
    ]
    tg = FakeTelegram({"getUpdates": [backlog, []]})
    offset = bot.drain_backlog(tg)
    s.eq("offset advances past the whole backlog", offset, 15)
    s.eq("plain messages are absorbed into history", len(bot.STATE[-100].history), 2)
    s.check("no /decide was fired from the backlog", "sendPoll" not in tg.methods())
    s.check("no message was sent while draining", "sendMessage" not in tg.methods())
    s.check("no stale button press was answered", "answerCallbackQuery" not in tg.methods())
    s.eq("only getUpdates was called", set(tg.methods()), {"getUpdates"})

    # "Could not ask" is not "nothing queued". This used to return 0, and 0
    # tells Telegram to start from the beginning -- so one network blip at boot
    # made it re-send the whole backlog, which the live loop then EXECUTED:
    # a /decide from twenty minutes ago re-run, a handled button press
    # re-fired. Returning None makes main() retry instead of guessing.
    bot.STATE.clear()
    tg = FakeTelegram({"getUpdates": None})
    s.eq("an unreachable getUpdates drains to None, not 0",
         bot.drain_backlog(tg), None)
    bot.STATE.clear()
    tg = FakeTelegram({"getUpdates": [[]]})
    s.eq("an empty queue really is offset 0", bot.drain_backlog(tg), 0)
    # A failure AFTER absorbing one batch must keep the offset it earned,
    # otherwise those updates come back too.
    bot.STATE.clear()
    tg = FakeTelegram({"getUpdates": [[msg("hello", update_id=40)], None]})
    s.eq("a mid-drain failure keeps the offset already earned",
         bot.drain_backlog(tg), None)

    # --- /decide guards ---------------------------------------------------
    bot.STATE.clear()
    tg = FakeTelegram()
    bot.handle_message(tg, msg("/decide")["message"])
    s.contains("/decide on an empty chat explains the bot cannot see backlog",
               tg.sent_text(), "after")
    s.check("/decide with too little history posts no poll", "sendPoll" not in tg.methods())

    tg = FakeTelegram()
    state = bot.state_for(-100)
    state.deciding = True
    bot.handle_decide(tg, -100, state)
    s.contains("a concurrent /decide is refused", tg.sent_text(), "Already working")
    state.deciding = False

    # --- /close: tally and winner ----------------------------------------
    def seeded_state(counts, options=None):
        st = bot.state_for(-100)
        st.picks = [
            {"name": "Samsen Wanchai", "phone": "+85228033960", "area": "Wan Chai", "why": "thai"},
            {"name": "Chom Chom", "phone": "+85228519969", "area": "Central", "why": "viet"},
            {"name": "No Phone Place", "phone": None, "area": "Central", "why": "x"},
        ]
        st.poll_options = options or [p["name"] for p in st.picks] + [bot.NONE_OPTION]
        st.poll_message_id = 99
        st.poll_id = "poll-1"
        st.constraints = {"party_size": 6, "when_text": "Friday 8pm",
                          "hard": [{"constraint": "no pork", "who": "Priya", "quote": "q"}]}
        st.votes = {}
        return st

    os.environ["CONSENTED_NUMBERS"] = "+85228519969"
    st = seeded_state(None)
    tg = FakeTelegram({"stopPoll": {"options": [
        {"voter_count": 1}, {"voter_count": 4}, {"voter_count": 0}, {"voter_count": 0}]}})
    bot.handle_close(tg, -100, st)
    s.contains("the poll is stopped", tg.methods(), "stopPoll")
    s.contains("the winner is the option with most votes", tg.sent_text(), "Chom Chom")
    s.contains("the tally is shown", tg.sent_text(), "4")
    card = tg.find("sendMessage")
    s.check("an approve/cancel keyboard is attached",
            any("inline_keyboard" in str(p.get("reply_markup", "")) for _, p in tg.calls))
    s.contains("the card names the number it will dial", tg.sent_text(), "+85228519969")
    s.contains("the card states the party size", tg.sent_text(), "6")
    s.contains("the card says a human dials", tg.sent_text(), "human")
    s.contains("the card discloses this is a real consented venue", tg.sent_text(), "consented")

    # Our own poll_answer tracking is the fallback when stopPoll returns nothing.
    st = seeded_state(None)
    st.votes = {1: 0, 2: 0, 3: 1}
    tg = FakeTelegram({"stopPoll": None})
    bot.handle_close(tg, -100, st)
    s.contains("with stopPoll unavailable, tracked votes decide it", tg.sent_text(), "Samsen Wanchai")

    # "None of these" is a real outcome and must not become a booking.
    st = seeded_state(None)
    tg = FakeTelegram({"stopPoll": {"options": [
        {"voter_count": 0}, {"voter_count": 1}, {"voter_count": 0}, {"voter_count": 5}]}})
    bot.handle_close(tg, -100, st)
    s.contains("'None of these' winning ends the round", tg.sent_text(), "Keep talking")
    s.check("'None of these' produces no approval card",
            not any("inline_keyboard" in str(p.get("reply_markup", "")) for _, p in tg.calls))

    # No votes at all: decide by default. A group chat has no closing
    # mechanism; this is the closing mechanism.
    st = seeded_state(None)
    tg = FakeTelegram({"stopPoll": {"options": [{"voter_count": 0}] * 4}})
    bot.handle_close(tg, -100, st)
    s.contains("nobody voting still produces a decision", tg.sent_text(), "default")
    s.contains("the default is the top pick", tg.sent_text(), "Samsen Wanchai")

    st = bot.state_for(-100)
    st.picks = []
    tg = FakeTelegram()
    bot.handle_close(tg, -100, st)
    s.contains("/close before /decide is refused", tg.sent_text(), "run /decide first")

    # --- poll shape -------------------------------------------------------
    bot.STATE.clear()
    st = bot.state_for(-100)
    for i in range(6):
        st.add("Marcus", f"line {i}")
    import pipeline as pl
    import places as pls
    import exa_search as ex
    saved = (pl.extract_constraints, pl.propose, pls.search_places, ex.search)
    pl.extract_constraints = lambda text, known="": {"party_size": 6, "when_text": "Friday 8pm",
                                           "hard": [], "soft": [], "vetoed": [], "coming_from": [],
                                           "prefer_cuisines": [], "avoid_cuisines": [],
                                           "open_questions": [], "source": "stub", "summary_line": "s"}
    pl.propose = lambda c, cand: {"picks": [
        {"name": "A Place", "phone": "+85228033960", "area": "Central", "why": "w", "satisfies": [], "fails": []},
        {"name": "B Place", "phone": "+85228519969", "area": "Central", "why": "w", "satisfies": [], "fails": []},
        {"name": "C Place", "phone": None, "area": "Central", "why": "w", "satisfies": [], "fails": []},
    ], "tradeoff_line": "t", "source": "stub"}
    pls.search_places = lambda *a, **k: [{"name": "A Place", "phone": "+85228033960"}]
    ex.search = lambda *a, **k: []
    try:
        tg = FakeTelegram({"sendPoll": {"message_id": 55, "poll": {"id": "poll-xyz"}}})
        bot.handle_decide(tg, -100, st)
        poll = tg.find("sendPoll")
        s.check("a poll is posted", poll is not None)
        # is_anonymous MUST be false, or no poll_answer updates arrive at all
        # and every vote is invisible to the bot.
        s.eq("is_anonymous is false", poll.get("is_anonymous"), False)
        s.eq("single choice only", poll.get("allows_multiple_answers"), False)
        options = json.loads(poll["options"]) if isinstance(poll["options"], str) else poll["options"]
        s.eq("three picks plus an escape hatch", len(options), 4)
        s.eq("the escape hatch is last", options[-1], bot.NONE_OPTION)
        s.contains("the constraint list is posted before the poll", tg.sent_text(), "What I read in the chat")
        s.contains("places with no number are flagged as uncallable", tg.sent_text(), "can't call")
        s.eq("poll id is remembered for tallying", st.poll_id, "poll-xyz")
        s.eq("poll message id is remembered for stopPoll", st.poll_message_id, 55)
        s.eq("votes reset on a new poll", st.votes, {})
    finally:
        pl.extract_constraints, pl.propose, pls.search_places, ex.search = saved

    # --- it asks, rather than telling you to run a command again ----------
    # Defaulting to "party of 4, this evening" had the agent saying a made-up
    # time out loud to a real restaurant. But the first fix told the group to
    # "run /decide and /close again", which pushes work back onto them for
    # something the bot can simply ask. Now it asks and holds the thread.
    os.environ["CONSENTED_NUMBERS"] = "+85228519969"
    os.environ.pop("DEMO_PHONE", None)

    def closed_with(constraints):
        st = seeded_state(None)
        st.constraints = dict(constraints)
        tg = FakeTelegram({"stopPoll": {"options": [
            {"voter_count": 0}, {"voter_count": 3}, {"voter_count": 0}, {"voter_count": 0}]}})
        bot.handle_close(tg, -100, st)
        return st, tg

    st, tg = closed_with({"when_text": "Friday 8pm", "hard": []})
    s.contains("missing party size is asked about", tg.sent_text(), "how many of you")
    s.check("and the time is not asked about again", "what time" not in tg.sent_text())
    s.eq("the question is recorded so the next message answers it",
         st.awaiting["fields"], ["party_size"])
    s.check("no approval card yet", not any(
        "inline_keyboard" in str(p.get("reply_markup", "")) for _, p in tg.calls))
    s.contains("it says why it will not guess", tg.sent_text(), "not going to guess")
    s.check("it does NOT tell the group to re-run a command",
            "/decide" not in tg.sent_text() and "/close" not in tg.sent_text())

    st, tg = closed_with({"party_size": 6, "hard": []})
    s.contains("missing time is asked about", tg.sent_text(), "what time")
    s.eq("only the time is outstanding", st.awaiting["fields"], ["when_text"])

    st, tg = closed_with({"hard": []})
    s.eq("both missing are asked together", st.awaiting["fields"], ["party_size", "when_text"])
    s.contains("in one question", tg.sent_text(), "how many of you")
    s.contains("covering both", tg.sent_text(), "what time")

    # A vague time must be treated as MISSING, not accepted.
    st, tg = closed_with({"party_size": 4, "when_text": "this evening", "hard": []})
    s.eq("'this evening' counts as no time at all", st.awaiting["fields"], ["when_text"])
    s.contains("and the chat is told why", tg.sent_text(), "clock time")
    s.contains("quoting what it rejected", tg.sent_text(), "this evening")
    s.check("no approval card for a vague time", not any(
        "inline_keyboard" in str(p.get("reply_markup", "")) for _, p in tg.calls))
    s.eq("the vague value is cleared rather than left to leak onto the call",
         st.constraints.get("when_text"), None)

    st, tg = closed_with({"party_size": 4, "when_text": "friday at 8pm", "hard": []})
    s.eq("an exact time passes straight through", st.awaiting, None)
    s.check("and produces the card", any(
        "inline_keyboard" in str(p.get("reply_markup", "")) for _, p in tg.calls))

    # --- and an ordinary reply answers it ---------------------------------
    import pipeline as pl3
    saved3 = pl3.parse_answer
    pl3.parse_answer = lambda text, needed: pl3.keyword_answer(text, needed)
    try:
        st, tg = closed_with({"hard": []})
        st.winner_for_test = None
        tg2 = FakeTelegram()
        bot.try_answer(tg2, -100, st, "just the 2 of us", "Kai")
        s.eq("a plain reply is understood", st.constraints.get("party_size"), 2)
        s.contains("and acknowledged", tg2.sent_text(), "party of")
        s.eq("the half that is still missing is still asked for",
             st.awaiting["fields"], ["when_text"])
        s.contains("explicitly", tg2.sent_text(), "Still need")
        s.check("no card while a question is open", not any(
            "inline_keyboard" in str(p.get("reply_markup", "")) for _, p in tg2.calls))

        tg3 = FakeTelegram()
        bot.try_answer(tg3, -100, st, "2pm today", "Kai")
        s.eq("the second half lands too", st.constraints.get("when_text"), "2pm today")
        s.eq("and the question is closed", st.awaiting, None)
        s.check("NOW the approval card appears, with no command re-run", any(
            "inline_keyboard" in str(p.get("reply_markup", "")) for _, p in tg3.calls))
        s.contains("and it carries the answered details", tg3.sent_text(), "2pm today")

        # Both at once should finish in one step.
        st, tg = closed_with({"hard": []})
        tg4 = FakeTelegram()
        bot.try_answer(tg4, -100, st, "table for 4 tomorrow at 7pm", "Dan")
        s.eq("one reply can answer everything", st.awaiting, None)
        s.eq("party size from it", st.constraints.get("party_size"), 4)
        s.check("card straight away", any(
            "inline_keyboard" in str(p.get("reply_markup", "")) for _, p in tg4.calls))

        # An unrelated message must not provoke a reply.
        st, tg = closed_with({"hard": []})
        tg5 = FakeTelegram()
        bot.try_answer(tg5, -100, st, "lol ok whatever", "Dan")
        s.eq("an unrelated message is answered with silence", tg5.calls, [])
        s.eq("and the question stays open", st.awaiting["fields"], ["party_size", "when_text"])

        # /decide starts a clean round.
        st.awaiting = {"fields": ["party_size"]}
        st.pending_winner = {"winner": {"name": "x"}, "tally": ""}
        st.history.clear()
        tg6 = FakeTelegram()
        bot.handle_decide(tg6, -100, st)
        s.eq("a fresh /decide clears any open question", st.awaiting, None)
        s.eq("and drops the held winner", st.pending_winner, None)
    finally:
        pl3.parse_answer = saved3

    # --- the candidate search follows the chat ----------------------------
    bot.STATE.clear()
    st = bot.state_for(-100)
    for i in range(6):
        st.add("Dan", f"line {i}")
    import pipeline as pl2
    import places as pls2
    import exa_search as ex2
    saved2 = (pl2.extract_constraints, pl2.propose, pls2.search_places, ex2.search)
    seen_areas: list = []
    pl2.extract_constraints = lambda text, known="": {
        "party_size": 6, "when_text": "Friday 8pm", "hard": [], "soft": [], "vetoed": [],
        "coming_from": [{"who": "Dan", "place": "Sha Tin"}], "prefer_cuisines": [],
        "avoid_cuisines": [], "open_questions": [], "source": "stub", "summary_line": "s"}
    def capture(areas=None, **kw):
        seen_areas.append(areas)
        return [{"name": "A Place", "phone": "+85228033960", "area": "Sha Tin"}]
    pls2.search_places = capture
    ex2.search = lambda *a, **k: []
    pl2.propose = lambda c, cand: {"picks": [
        {"name": "A Place", "phone": "+85228033960", "area": "Sha Tin", "why": "w",
         "satisfies": [], "fails": []}], "tradeoff_line": "", "source": "stub"}
    try:
        tg = FakeTelegram({"sendPoll": {"message_id": 7, "poll": {"id": "p"}}})
        bot.handle_decide(tg, -100, st)
        s.check("search_places is given constraint-derived areas, not called bare",
                seen_areas and seen_areas[0] is not None, f"got {seen_areas}")
        s.contains("the origin's area is among them", seen_areas[0] or [], "sha_tin")
        s.contains("the chat is told where it searched", tg.sent_text(), "Sha Tin")
    finally:
        pl2.extract_constraints, pl2.propose, pls2.search_places, ex2.search = saved2

    # --- poll_answer routing ---------------------------------------------
    bot.STATE.clear()
    st = bot.state_for(-100)
    st.poll_id = "poll-abc"
    bot.handle_poll_answer({"poll_id": "poll-abc", "user": {"id": 1, "first_name": "Dan"}, "option_ids": [2]})
    s.eq("a vote is recorded against the right chat", st.votes, {1: 2})
    s.eq("the voter's name is kept", st.voter_names[1], "Dan")
    bot.handle_poll_answer({"poll_id": "poll-abc", "user": {"id": 1, "first_name": "Dan"}, "option_ids": []})
    s.eq("a retracted vote is removed", st.votes, {})
    bot.handle_poll_answer({"poll_id": "other-poll", "user": {"id": 2}, "option_ids": [0]})
    s.eq("a vote for an unknown poll is ignored", st.votes, {})

    # --- SAFETY RAILS -----------------------------------------------------
    os.environ.pop("DEMO_PHONE", None)
    os.environ["CONSENTED_NUMBERS"] = "+85228519969, +852 2803 3960"
    number, demo, refusal = bot.resolve_dial_target("+85228519969")
    s.eq("a consented number is dialled", number, "+85228519969")
    s.eq("and is not flagged as a demo override", demo, False)
    s.eq("with no refusal", refusal, None)

    s.check("the allowlist tolerates spaced entries",
            bot.resolve_dial_target("+85228033960")[0] == "+85228033960")

    number, demo, refusal = bot.resolve_dial_target("+85299998888")
    s.eq("a number nobody consented to is NOT dialled", number, None)
    s.contains("and the refusal explains why", refusal, "consent allowlist")
    s.contains("and points at the fix", refusal, "DEMO_PHONE")

    number, demo, refusal = bot.resolve_dial_target(None)
    s.eq("a winner with no number cannot be called", number, None)
    s.contains("and says so plainly", refusal, "phone number")

    os.environ["DEMO_PHONE"] = "+852 9111 2222"
    number, demo, refusal = bot.resolve_dial_target("+85299998888")
    s.eq("DEMO_PHONE overrides the winner's real number", number, "+85291112222")
    s.eq("and is flagged as an override", demo, True)
    s.eq("and overrides the allowlist refusal too", refusal, None)
    s.eq("DEMO_PHONE also overrides a consented number", bot.resolve_dial_target("+85228519969")[0], "+85291112222")

    st = seeded_state(None)
    tg = FakeTelegram({"stopPoll": {"options": [{"voter_count": 3}, {"voter_count": 0}, {"voter_count": 0}, {"voter_count": 0}]}})
    bot.handle_close(tg, -100, st)
    s.contains("the chat is told out loud when the rail is active", tg.sent_text(), "DEMO_PHONE is set")
    s.contains("and that the real restaurant is still what won", tg.sent_text(), "Samsen Wanchai")
    os.environ.pop("DEMO_PHONE", None)

    os.environ["ALLOW_ANY_NUMBER"] = "1"
    s.eq("the documented escape hatch works when set deliberately",
         bot.resolve_dial_target("+85299998888")[0], "+85299998888")
    os.environ["ALLOW_ANY_NUMBER"] = "0"
    s.eq("and is off by default", bot.resolve_dial_target("+85299998888")[0], None)

    # --- approval handoff -------------------------------------------------
    if TMP_PENDING.exists():
        TMP_PENDING.unlink()
    os.environ["CONSENTED_NUMBERS"] = "+85228519969"
    st = seeded_state(None)
    tg = FakeTelegram({"stopPoll": {"options": [{"voter_count": 0}, {"voter_count": 3}, {"voter_count": 0}, {"voter_count": 0}]}})
    bot.handle_close(tg, -100, st)
    token = st.approval_token
    s.check("an approval token is issued", bool(token))
    s.check("nothing is written to disk before approval", not TMP_PENDING.exists())

    tg = FakeTelegram()
    bot.handle_callback(tg, {"id": "cb", "data": f"ok:{token}",
                             "from": {"first_name": "Dan"}, "message": {"chat": {"id": -100}}})
    s.check("approval writes the pending call", TMP_PENDING.exists())
    written = json.loads(TMP_PENDING.read_text())
    s.eq("pending records the restaurant", written["restaurant_name"], "Chom Chom")
    s.eq("pending records the number to dial", written["dial_number"], "+85228519969")
    s.eq("pending records who approved", written["approved_by"], "Dan")
    s.eq("pending records the chat to report back to", written["chat_id"], -100)
    s.eq("pending carries the party size", written["party_size"], 6)
    s.eq("pending carries the booking name", written["booking_name"], "Prakhar")
    s.contains("pending carries the hard constraints to mention", written["constraints"], "no pork")
    s.check("the bridge being down is handled, not silently swallowed",
            written["dial"] is True or "call desk" in tg.sent_text())
    s.eq("the token is single-use", st.approval_token, None)

    s.check("approval notified the bridge through the stub, not the network",
            bool(bridge_calls))

    # The bridge being down must be handled, not silently swallowed.
    bot.notify_bridge_dial = lambda: False
    st = seeded_state(None)
    st.constraints = {"party_size": 6, "when_text": "Friday 8pm", "hard": []}
    tg = FakeTelegram({"stopPoll": {"options": [
        {"voter_count": 0}, {"voter_count": 3}, {"voter_count": 0}, {"voter_count": 0}]}})
    bot.handle_close(tg, -100, st)
    down_token = st.approval_token
    tg = FakeTelegram()
    bot.handle_callback(tg, {"id": "cb", "data": f"ok:{down_token}",
                             "from": {"first_name": "Dan"}, "message": {"chat": {"id": -100}}})
    written_down = json.loads(TMP_PENDING.read_text())
    s.eq("with the bridge down the booking is queued with dial already true",
         written_down["dial"], True)
    s.contains("and the chat is told how to start the call desk",
               tg.sent_text(), "bridge/server.py")
    bot.notify_bridge_dial = lambda: (bridge_calls.append(True), True)[1]

    tg = FakeTelegram()
    bot.handle_callback(tg, {"id": "cb", "data": "ok:stale-token",
                             "from": {"first_name": "Dan"}, "message": {"chat": {"id": -100}}})
    s.contains("a stale card is rejected", str(tg.find("answerCallbackQuery")), "stale")

    st = seeded_state(None)
    tg = FakeTelegram({"stopPoll": {"options": [{"voter_count": 0}, {"voter_count": 3}, {"voter_count": 0}, {"voter_count": 0}]}})
    bot.handle_close(tg, -100, st)
    tg = FakeTelegram()
    bot.handle_callback(tg, {"id": "cb", "data": f"no:{st.approval_token}",
                             "from": {"first_name": "Priya"}, "message": {"chat": {"id": -100}}})
    s.eq("cancelling clears the pending call", json.loads(TMP_PENDING.read_text())["status"], "cancelled")
    s.contains("and says nothing was dialled", tg.sent_text(), "Nothing was dialled")

    # --- the booking card must survive a real restaurant name ------------
    # OSM is full of "Fish & Chips" and "R&B Tea". If the card is the message
    # Telegram refuses, the approve button never appears and the flow
    # dead-ends mid-pitch with no error anywhere.
    os.environ["CONSENTED_NUMBERS"] = "+85228519969"
    os.environ["DEMO_PHONE"] = "+85291112222"
    st = seeded_state(None)
    st.picks = [{"name": "Fish & Chips <Central>", "phone": "+85228519969",
                 "area": "Sheung Wan & Central", "why": "cheap & close",
                 "satisfies": ["no pork & no beef"], "fails": ["a & b"],
                 "website": "https://x.test/?a=1&b=2"}]
    st.poll_options = ["Fish & Chips <Central>", bot.NONE_OPTION]
    st.constraints = {"party_size": 2, "when_text": "2pm & later",
                      "hard": [{"constraint": "no pork & no beef"}]}
    tg = FakeTelegram({"stopPoll": {"options": [{"voter_count": 3}, {"voter_count": 0}]}})
    bot.handle_close(tg, -100, st)
    s.eq("the booking card survives an ampersand in the venue name",
         unescaped(tg.sent_text()), [])
    s.check("and the approve button is actually attached", any(
        "inline_keyboard" in str(p.get("reply_markup", "")) for _, p in tg.calls))
    s.contains("the name is still readable", tg.sent_text(), "Fish &amp; Chips")
    os.environ.pop("DEMO_PHONE", None)

    st = seeded_state(None)
    st.constraints = {"hard": []}
    tg = FakeTelegram({"stopPoll": {"options": [
        {"voter_count": 0}, {"voter_count": 3}, {"voter_count": 0}, {"voter_count": 0}]}})
    bot.handle_close(tg, -100, st)
    s.eq("the follow-up question renders cleanly", unescaped(tg.sent_text()), [])
    tg2 = FakeTelegram()
    bot.try_answer(tg2, -100, st, "2 of us at 2pm & no later", "A & B")
    s.eq("so does the acknowledgement, with an ampersand in the name",
         unescaped(tg2.sent_text()), [])

    # --- a booking link is a real answer, not a shrug ---------------------
    os.environ["CONSENTED_NUMBERS"] = "+85228519969"
    os.environ.pop("DEMO_PHONE", None)
    st = seeded_state(None)
    st.picks = [{"name": "Link Only", "phone": None, "website": "https://example.com/book",
                 "area": "Central", "why": "x"}]
    st.poll_options = ["Link Only", bot.NONE_OPTION]
    st.constraints = {"party_size": 6, "when_text": "Friday 8pm", "hard": []}
    tg = FakeTelegram({"stopPoll": {"options": [{"voter_count": 3}, {"voter_count": 0}]}})
    bot.handle_close(tg, -100, st)
    s.contains("an uncallable winner still yields its booking page",
               tg.sent_text(), "https://example.com/book")
    s.contains("and says a human has to finish it", tg.sent_text(), "by hand")
    s.check("but no call is queued", not any(
        "inline_keyboard" in str(p.get("reply_markup", "")) for _, p in tg.calls))

    st = seeded_state(None)
    st.picks = [{"name": "Nothing", "phone": None, "website": None, "area": "", "why": "x"}]
    st.poll_options = ["Nothing", bot.NONE_OPTION]
    st.constraints = {"party_size": 6, "when_text": "Friday 8pm", "hard": []}
    tg = FakeTelegram({"stopPoll": {"options": [{"voter_count": 3}, {"voter_count": 0}]}})
    bot.handle_close(tg, -100, st)
    s.contains("with neither number nor link it says so plainly",
               tg.sent_text(), "phone number")
    s.check("and invents no link", "http" not in tg.sent_text())

    # --- history survives a restart ---------------------------------------
    # Found live and it is the worst possible bug for this project: history was
    # in memory only, so a restart turned a 40-line conversation into 2 --
    # permanently, because Telegram hands each update over exactly once and
    # will not re-deliver. A project whose pitch is "it has read the last two
    # hundred messages" cannot forget them on a crash.
    tmp_state = Path(__file__).resolve().parent / "_test_state.json"
    suite_state = bot.STATE_PATH
    bot.STATE_PATH = tmp_state
    try:
        bot.STATE.clear()
        live = bot.state_for(-4242)
        live.add("Priya", "i can't eat pork")
        live.add("Dan", "coming from sha tin\nand not hotpot again")
        live.constraints = {"party_size": 6, "when_text": "Friday 8pm", "hard": []}
        live.picks = [{"name": "Somewhere", "phone": "+85228033960"}]
        live.poll_id = "poll-persist"
        live.poll_message_id = 321
        live.poll_options = ["a", "b", "c", bot.NONE_OPTION]
        live.votes = {99: 1}
        live.voter_names = {99: "Dan"}
        live.approval_token = "tok123"
        live.approval_payload = {"id": "tok123", "restaurant_name": "Somewhere"}
        bot.save_state()
        s.check("state is written to disk", tmp_state.exists())

        before = list(live.history)
        bot.STATE.clear()
        s.eq("simulated restart really empties memory", bot.STATE, {})

        restored_lines = bot.load_state()
        after = bot.state_for(-4242)
        s.eq("every history line comes back", list(after.history), before)
        s.eq("the line count is reported", restored_lines, len(before))
        s.eq("multi-line splitting survived the round trip", len(after.history), 3)
        s.eq("constraints come back", after.constraints.get("party_size"), 6)
        s.eq("picks come back so /close still works after a crash",
             after.picks[0]["name"], "Somewhere")
        s.eq("the open poll comes back", after.poll_id, "poll-persist")
        s.eq("the poll message id comes back", after.poll_message_id, 321)
        # JSON stringifies dict keys; a re-vote after a restart must not count
        # as a second voter.
        s.eq("vote keys come back as ints, not strings", after.votes, {99: 1})
        s.check("voter names come back as ints too", 99 in after.voter_names)
        s.eq("a pending approval token survives", after.approval_token, "tok123")

        tmp_state.write_text("{ not json", encoding="utf-8")
        bot.STATE.clear()
        s.eq("a corrupt state file starts fresh instead of crashing", bot.load_state(), 0)

        tmp_state.unlink()
        bot.STATE.clear()
        s.eq("no state file is not an error", bot.load_state(), 0)
    finally:
        if tmp_state.exists():
            tmp_state.unlink()
        bot.STATE_PATH = suite_state
        bot.STATE.clear()

    # --- misc -------------------------------------------------------------
    tg = FakeTelegram()
    bot.handle_message(tg, msg("/status")["message"])
    s.contains("/status reports which rail is active", tg.sent_text(), "DEMO_PHONE")
    tg = FakeTelegram()
    bot.handle_message(tg, msg("/help")["message"])
    s.contains("/help explains it phones the restaurant", tg.sent_text(), "phone the restaurant")

    st = bot.state_for(-777)
    bot.handle_message(FakeTelegram(), msg("/unknowncommand", chat_id=-777)["message"])
    s.eq("an unknown command is stored as chat, not treated as a command", len(st.history), 1)

    s.raises("an empty token is refused at construction", SystemExit, bot.Telegram, "")

    if TMP_PENDING.exists():
        TMP_PENDING.unlink()
    if SUITE_STATE.exists():
        SUITE_STATE.unlink()
    if SUITE_PEOPLE.exists():
        SUITE_PEOPLE.unlink()
    people.PEOPLE_PATH = real_people_path
    bot.PEOPLE = {}
    bot.notify_bridge_dial = real_notify
    bot.STATE_PATH = real_state_path
    bot.STATE.clear()
    for key in ("DEMO_PHONE", "CONSENTED_NUMBERS", "ALLOW_ANY_NUMBER"):
        os.environ.pop(key, None)

    # The suite must not create or modify the real state file. Whether one
    # already exists is none of the suite's business.
    real_state_after = (
        (real_state_path.exists(),
         real_state_path.stat().st_mtime_ns if real_state_path.exists() else 0,
         real_state_path.stat().st_size if real_state_path.exists() else 0)
    )
    s.eq("the suite did not create or modify the real chat_state.json",
         real_state_after, real_state_before)
    real_people_after = (
        (real_people_path.exists(),
         real_people_path.stat().st_mtime_ns if real_people_path.exists() else 0)
    )
    s.eq("the suite did not create or modify the real people.json",
         real_people_after, real_people_before)
    s.check("the suite's own temp state file is cleaned up", not SUITE_STATE.exists())
    return s
