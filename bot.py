#!/usr/bin/env python3
"""bot.py — the coordinator. Lives in the group chat, because that is the only
place the information exists.

This is not a restaurant recommender. Recommendation is solved and nobody in
Hong Kong is short of suggestions. What fails, every single week, is
CONVERGENCE: six people, forty messages, nobody commits, and at 19:40 somebody
says "just pick anything".

Two primitives a group chat does not have, and this bot adds:

  1. Memory of what was already agreed. Priya can't do pork. Marcus is coming
     in from Sha Tin so Central is a fight. Hotpot was vetoed twice this month.
     The budget conversation happened in July. None of that would ever be typed
     into a booking form — it exists only in the chat, which is exactly why the
     agent has to live in the chat.
  2. A closing mechanism. A poll with a deadline and a default, so the decision
     gets made rather than deferred.

Standard library only, deliberately. No python-telegram-bot, no aiohttp, no
pip install on conference wifi ten minutes before demos.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import exa_search
import people
import places
import pipeline
from envlite import env, env_flag, env_list, load_env, warn_if_tls_broken
from tgtext import esc, esc_join

PENDING_PATH = ROOT / "bridge" / "pending_call.json"
# Chat history on disk. The whole pitch of this project is "it has read the
# last two hundred messages", and holding those only in memory meant any crash
# or restart forgot the entire conversation -- permanently, because Telegram
# hands each update over exactly once and will not re-deliver it. Found the
# hard way: a restart mid-test turned a 40-line history into 2.
STATE_PATH = ROOT / "chat_state.json"
BRIDGE_BASE = "http://127.0.0.1:8080"
API_TIMEOUT = 40
HISTORY_LIMIT = 200
NONE_OPTION = "None of these — keep arguing"


# ===========================================================================
# Telegram transport
# ===========================================================================

class Telegram:
    def __init__(self, token: str):
        if not token:
            raise SystemExit(
                "TELEGRAM_TOKEN is empty. Get one from BotFather, then — and this is the\n"
                "step everyone forgets — send BotFather /setprivacy and choose DISABLE.\n"
                "Without it the bot cannot see group messages at all and nothing works."
            )
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, timeout: int | None = None, **params):
        clean = {}
        for key, value in params.items():
            if value is None:
                continue
            clean[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        body = urllib.parse.urlencode(clean).encode()
        try:
            req = urllib.request.Request(f"{self.base}/{method}", data=body)
            with urllib.request.urlopen(req, timeout=timeout or API_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("result") if payload.get("ok") else None
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("description", "")
            except Exception:
                pass
            print(f"[bot] {method} HTTP {exc.code} {detail}")
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            print(f"[bot] {method} failed: {type(exc).__name__}")
        return None

    def send(self, chat_id, text: str, **extra):
        return self.call("sendMessage", chat_id=chat_id, text=text,
                         parse_mode="HTML", disable_web_page_preview=True, **extra)


# ===========================================================================
# Per-chat state
# ===========================================================================

class ChatState:
    def __init__(self):
        self.history: deque[str] = deque(maxlen=HISTORY_LIMIT)
        self.poll_id: str | None = None
        self.poll_message_id: int | None = None
        self.poll_options: list[str] = []
        self.votes: dict[int, int] = {}          # user_id -> option index
        self.voter_names: dict[int, str] = {}
        self.picks: list[dict] = []
        self.constraints: dict = {}
        self.deciding = False
        self.approval_token: str | None = None
        self.approval_payload: dict | None = None
        # A question the bot has asked and is waiting on. Telling people to
        # "run /decide again" put the work back on them for something the bot
        # could simply ask about; this is the bot holding the thread instead.
        self.awaiting: dict | None = None
        self.pending_winner: dict | None = None

    def add(self, author: str, text: str) -> None:
        """Store one message, splitting multi-line into separate history lines.

        People paste several thoughts as one message:
            "ok so
             priya can't do pork
             and marcus is coming from sha tin"
        Each of those lines is its own constraint. Kept as a single blob, the
        extractor reliably finds the first one and loses the rest.
        """
        for line in (text or "").splitlines():
            trimmed = line.strip()
            if trimmed:
                self.history.append(f"{esc(author)}: {trimmed}")

    def as_text(self) -> str:
        return "\n".join(self.history)


STATE: dict[int, ChatState] = {}
PEOPLE: dict = {}          # chat_id -> name -> remembered profile


def state_for(chat_id: int) -> ChatState:
    if chat_id not in STATE:
        STATE[chat_id] = ChatState()
    return STATE[chat_id]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
# Everything a restart must not lose: the history, and enough of the decision
# in flight that /close still works after a crash between /decide and /close.

def save_state() -> None:
    try:
        blob = {
            str(chat_id): {
                "history": list(st.history),
                "constraints": st.constraints,
                "picks": st.picks,
                "poll_id": st.poll_id,
                "poll_message_id": st.poll_message_id,
                "poll_options": st.poll_options,
                "votes": {str(k): v for k, v in st.votes.items()},
                "voter_names": {str(k): v for k, v in st.voter_names.items()},
                "approval_token": st.approval_token,
                "approval_payload": st.approval_payload,
                "awaiting": st.awaiting,
                "pending_winner": st.pending_winner,
            }
            for chat_id, st in STATE.items()
        }
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_PATH)  # atomic: a crash mid-write cannot truncate it
    except OSError as exc:
        print(f"[bot] could not save state: {exc}")


def load_state() -> int:
    if not STATE_PATH.exists():
        return 0
    try:
        blob = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[bot] state file unreadable ({type(exc).__name__}); starting fresh")
        return 0

    restored = 0
    for raw_id, data in (blob or {}).items():
        try:
            chat_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        st = state_for(chat_id)
        for line in (data.get("history") or [])[-HISTORY_LIMIT:]:
            if isinstance(line, str) and line.strip():
                st.history.append(line)
        st.constraints = data.get("constraints") or {}
        st.picks = data.get("picks") or []
        st.poll_id = data.get("poll_id")
        st.poll_message_id = data.get("poll_message_id")
        st.poll_options = data.get("poll_options") or []
        # JSON turns integer keys into strings; user ids must come back as ints
        # or a re-vote after a restart counts as a second voter.
        st.votes = {int(k): v for k, v in (data.get("votes") or {}).items() if str(k).lstrip("-").isdigit()}
        st.voter_names = {int(k): v for k, v in (data.get("voter_names") or {}).items() if str(k).lstrip("-").isdigit()}
        st.approval_token = data.get("approval_token")
        st.approval_payload = data.get("approval_payload")
        st.awaiting = data.get("awaiting")
        st.pending_winner = data.get("pending_winner")
        restored += len(st.history)
    return restored


# ===========================================================================
# Safety rails. These are not polish.
# ===========================================================================

def resolve_dial_target(real_phone: str | None) -> tuple[str | None, bool, str | None]:
    """Decide what number actually gets dialled.

    Returns (dial_number, demo_override, refusal_reason).

    Order matters. DEMO_PHONE wins over everything: when it is set, every call
    routes to a number the operator controls no matter which restaurant won the
    poll. The poll still shows the real place and the approval card says out
    loud that the phone which rings is ours.

    With no DEMO_PHONE we are dialling a real business, so the consent
    allowlist becomes the gate. A number nobody agreed to is refused here, at
    the approval step, rather than trusted to operator discipline at 16:02.
    """
    demo = env("DEMO_PHONE")
    if demo:
        normalised = places.normalise_phone(demo) or demo
        return normalised, True, None

    if not real_phone:
        return None, False, "I don't have a phone number for that place, so I can't call it."

    allowlist = [places.normalise_phone(n) or n for n in env_list("CONSENTED_NUMBERS")]
    if real_phone in allowlist:
        return real_phone, False, None

    if env_flag("ALLOW_ANY_NUMBER"):
        print(f"[bot] WARNING: dialling {real_phone}, which is not on the consent allowlist.")
        return real_phone, False, None

    return None, False, (
        f"<b>Refusing to call.</b> {real_phone} is not on the consent allowlist.\n\n"
        "This agent only phones numbers that agreed in advance to receive a call from it. "
        "Add the number to <code>CONSENTED_NUMBERS</code> in <code>.env</code>, or set "
        "<code>DEMO_PHONE</code> to route the call to a phone you control."
    )


# ===========================================================================
# /decide
# ===========================================================================

def handle_decide(tg: Telegram, chat_id: int, state: ChatState) -> None:
    state.awaiting = None
    state.pending_winner = None
    if state.deciding:
        tg.send(chat_id, "Already working on it — give me a few seconds.")
        return
    if len(state.history) < 3:
        tg.send(
            chat_id,
            "I've only seen <b>%d</b> message%s in here so far.\n\n"
            "I can only read messages sent <i>after</i> I joined — Telegram doesn't give bots "
            "the backlog. Argue a bit and run /decide again."
            % (len(state.history), "" if len(state.history) == 1 else "s"),
        )
        return

    state.deciding = True
    try:
        tg.send(chat_id, f"Reading the last <b>{len(state.history)}</b> lines…")

        # Hand the extractor what it already knows about these people, so a
        # constraint Priya stated in July does not need restating today.
        known = people.prior_knowledge(PEOPLE, chat_id)
        constraints = pipeline.extract_constraints(state.as_text(), known=known)
        state.constraints = constraints

        rendered = pipeline.render_constraints(constraints)
        if known:
            seen, remembered = people.stats(PEOPLE, chat_id)
            rendered += (f"\n<i>{remembered} thing(s) already remembered about "
                         f"{seen} people were taken into account.</i>")
        tg.send(chat_id, rendered)

        # Learn from this pass. Only quoted constraints are kept, so one bad
        # inference cannot become a permanent invisible bias.
        newly = people.learn(PEOPLE, chat_id, constraints)
        people.save(PEOPLE)
        if newly:
            tg.send(chat_id,
                    "<b>Noted for next time</b>\n"
                    + "\n".join(f"\u2022 {item}" for item in newly[:6])
                    + "\n\n<i>/who to see everything I remember, /forget NAME to drop it.</i>")

        # The search follows the chat. areas_for() turns the districts people
        # actually named into bounding boxes; relevance_rank() then re-ranks
        # the pool against those districts, which matters because the cache is
        # territory-wide and constraint-blind by design. Nothing is assumed:
        # with no district mentioned, areas_for() returns everything.
        # Merge remembered origins into today's constraints before searching.
        # Dan told us he comes from Sha Tin three weeks ago; he should not have
        # to say it again for the search to take it into account.
        known_origins = {str((e or {}).get("place") or "").strip().lower()
                         for e in (constraints.get("coming_from") or [])}
        chat_people = (PEOPLE.get(str(chat_id)) or {})
        for person in people.people_in(chat_people):
            for area in person.get("areas") or []:
                if area and area.lower() not in known_origins:
                    constraints.setdefault("coming_from", []).append(
                        {"who": person.get("name", ""), "place": area, "from_memory": True}
                    )
                    known_origins.add(area.lower())

        # Remembered origins count too. Dan said he commutes from Sha Tin
        # three weeks ago; he should not have to repeat it for the search to
        # take it into account. This is the memory paying off in the SEARCH,
        # not only in the constraint list.
        seen_origins = {str((e or {}).get("place") or "").strip().lower()
                        for e in (constraints.get("coming_from") or [])}
        for person in people.people_in(PEOPLE.get(str(chat_id)) or {}):
            for area in person.get("areas") or []:
                if area and area.lower() not in seen_origins:
                    constraints.setdefault("coming_from", []).append(
                        {"who": person.get("name", ""), "place": area, "from_memory": True})
                    seen_origins.add(area.lower())

        wanted_areas = places.areas_for(constraints)
        # Whole pool, then rank by the chat's districts, THEN cut. The other
        # order silently discarded the rows the chat had asked for: the cache
        # is territory-wide and constraint-blind, so a 60-row cut taken before
        # relevance_rank left 1 of 15 Sha Tin rows alive -- indistinguishable
        # from "there are no restaurants near Sha Tin".
        osm_rows = places.search_places(areas=wanted_areas, limit=None)
        exa_rows = exa_search.search(constraints)
        candidates = places.relevance_rank(
            exa_search.merge(osm_rows, exa_rows), constraints
        )[:60]
        origins = places.origin_districts(constraints)
        destinations = places.destination_districts(constraints)
        fair = places.meeting_districts(origins)[:3] if origins and not destinations else []
        if origins or destinations:
            print(f"[bot] origins={origins} destinations={destinations} "
                  f"fair={fair} -> areas {wanted_areas}")
        if not candidates:
            tg.send(chat_id, "I couldn't find any candidate restaurants at all. Search layers are all down.")
            return

        proposal = pipeline.propose(constraints, candidates)
        picks = [p for p in proposal.get("picks", []) if p.get("name")][:3]
        if not picks:
            tg.send(chat_id, "I found places but couldn't narrow them to three. Try /decide again.")
            return
        state.picks = picks

        lines = ["<b>Three that fit</b>"]
        for index, pick in enumerate(picks, 1):
            phone_note = "" if pick.get("phone") else "  ⚠️ no number — I can't call this one"
            area = f" · {esc(pick['area'])}" if pick.get("area") else ""
            lines.append(f"\n<b>{index}. {esc(pick['name'])}</b>{area}{phone_note}")
            if pick.get("why"):
                lines.append(f"    {esc(pick['why'])}")
            if pick.get("satisfies"):
                lines.append(f"    ✓ {esc_join(pick['satisfies'][:3])}")
            if pick.get("fails"):
                lines.append(f"    ✗ {esc_join(pick['fails'][:2])}")
        if proposal.get("tradeoff_line"):
            lines.append(f"\n<i>{esc(proposal['tradeoff_line'])}</i>")
        callable_n = sum(1 for c in candidates if c.get("phone"))
        if destinations:
            where = " \u00b7 searched in " + esc_join(destinations)
        elif fair:
            where = (" \u00b7 nobody picked a spot, so I looked around "
                     + esc_join(fair) + " \u2014 fairest between " + esc_join(origins))
        else:
            where = ""
        lines.append(
            f"\n<i>from {len(candidates)} candidates, {callable_n} with a dialable number"
            f"{where} \u00b7 picks via {esc(proposal.get('source', '?'))}</i>"
        )
        tg.send(chat_id, "\n".join(lines))

        options = [p["name"][:95] for p in picks] + [NONE_OPTION]
        poll = tg.call(
            "sendPoll",
            chat_id=chat_id,
            question="Where are we eating?"[:295],
            options=options,
            # is_anonymous MUST be false. An anonymous poll delivers no
            # poll_answer updates at all, so votes are invisible to the bot and
            # /close has nothing to tally.
            is_anonymous=False,
            allows_multiple_answers=False,
        )
        if poll:
            state.poll_id = (poll.get("poll") or {}).get("id")
            state.poll_message_id = poll.get("message_id")
            state.poll_options = options
            state.votes = {}
            state.voter_names = {}
            save_state()
            tg.send(chat_id, "Vote above. <b>/close</b> when you're done and I'll take it from there.")
        else:
            tg.send(chat_id, "Couldn't post the poll. Reply with 1, 2 or 3 instead and use /close.")
    finally:
        state.deciding = False


# ===========================================================================
# /close
# ===========================================================================

def handle_close(tg: Telegram, chat_id: int, state: ChatState) -> None:
    if not state.picks:
        tg.send(chat_id, "Nothing to close — run /decide first.")
        return

    counts = [0] * len(state.poll_options or state.picks)

    # stopPoll is the authoritative tally: Telegram's own per-option counts.
    # Our poll_answer tracking is the fallback, and also the only way to know
    # WHO voted, which the booking card shows.
    if state.poll_message_id:
        stopped = tg.call("stopPoll", chat_id=chat_id, message_id=state.poll_message_id)
        if stopped:
            for index, option in enumerate(stopped.get("options") or []):
                if index < len(counts):
                    counts[index] = option.get("voter_count", 0)
    if not any(counts):
        for option_index in state.votes.values():
            if 0 <= option_index < len(counts):
                counts[option_index] += 1

    winner_index = max(range(len(counts)), key=lambda i: counts[i]) if any(counts) else 0
    if any(counts) and state.poll_options and winner_index == len(state.poll_options) - 1:
        tg.send(chat_id, "“None of these” won. Keep talking and run /decide again — "
                         "I'll re-read the chat and try different places.")
        state.poll_id = None
        state.pending_winner = None
        state.awaiting = None
        save_state()
        return

    if not any(counts):
        tg.send(chat_id, "<i>Nobody voted, so I'm taking the top pick by default. "
                         "A group chat has no closing mechanism; this is the closing mechanism.</i>")

    winner = state.picks[min(winner_index, len(state.picks) - 1)]
    tally = " · ".join(
        f"{esc(state.picks[i]['name'][:18])} {counts[i]}" for i in range(min(len(state.picks), len(counts)))
    )
    state.pending_winner = {"winner": winner, "tally": tally}
    save_state()
    present_booking(tg, chat_id, state)


QUESTION_LABELS = {
    "party_size": "exactly how many of you there are",
    "when_text": "exactly what time",
}


def present_booking(tg: Telegram, chat_id: int, state: ChatState) -> None:
    """Show the approval card, or ask for whatever is still missing.

    Called from /close and again from an ordinary reply once the missing
    details arrive, so answering a question in the chat carries straight on to
    the booking rather than making anyone re-run a command.
    """
    held = state.pending_winner or {}
    winner = held.get("winner")
    tally = held.get("tally", "")
    if not winner:
        tg.send(chat_id, "Nothing to book — run /decide first.")
        return

    party = state.constraints.get("party_size")
    when_text = state.constraints.get("when_text")

    missing = []
    vague_reason = ""
    if not party:
        missing.append("party_size")
    if not when_text:
        missing.append("when_text")
    else:
        # Truthy is not good enough. "this evening" sailed through a `if not
        # when_text` check and would have had the agent asking a restaurant to
        # hold a table at an hour it never named. A booking without a clock
        # time is not a booking, so a vague answer counts as missing.
        bookable, why = pipeline.time_is_bookable(when_text)
        if not bookable:
            missing.append("when_text")
            vague_reason = why
            state.constraints["when_text"] = None

    if missing:
        # Ask, and hold the thread. The next ordinary message in the chat is
        # treated as the answer -- no command, no repetition.
        state.awaiting = {"fields": missing, "asked_at": datetime.now(timezone.utc).isoformat()}
        save_state()
        asked = " and ".join(QUESTION_LABELS[field] for field in missing)
        note = (f"\n\n{esc(vague_reason)} \u2014 a restaurant needs a clock time."
                if vague_reason else "")
        tg.send(
            chat_id,
            f"🏆 <b>{esc(winner['name'])}</b> wins.\n<i>{tally}</i>\n\n"
            f"Before I call them — <b>{asked}?</b>{note}\n\n"
            "<i>Just say it here and I'll carry on. A restaurant can't hold a table "
            "for a vague answer, and I'm not going to guess one down the phone.</i>"
        )
        return

    state.awaiting = None
    hard_list = [h["constraint"] for h in state.constraints.get("hard", []) if h.get("constraint")]
    dial_number, demo_override, refusal = resolve_dial_target(winner.get("phone"))

    if refusal:
        extra = ""
        if winner.get("website"):
            extra = (f"\n\n🔗 They do have a booking page though:\n"
                     f"{esc(winner['website'])}\n\n"
                     "<i>Someone will have to book it there by hand.</i>")
        tg.send(chat_id, f"🏆 <b>{esc(winner['name'])}</b> wins.\n<i>{tally}</i>\n\n{refusal}{extra}")
        return

    token = uuid.uuid4().hex[:12]
    state.approval_token = token
    state.approval_payload = {
        "id": token,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "chat_id": chat_id,
        "restaurant_name": winner["name"],
        "restaurant_display": winner["name"],
        "real_number": winner.get("phone"),
        "dial_number": dial_number,
        "demo_override": demo_override,
        "party_size": party,
        "when_text": when_text,
        "booking_name": env("BOOKER_NAME", "a guest"),
        "callback_number": env("CALLBACK_NUMBER"),
        "constraints": hard_list,
        "notes": winner.get("why", ""),
        "website": winner.get("website"),
        "area": winner.get("area", ""),
        "vote_tally": tally,
        "dial": False,
        "status": "awaiting_approval",
    }

    card = [
        f"\U0001f3c6 <b>{esc(winner['name'])}</b> wins.",
        f"<i>{tally}</i>",
        "",
        "<b>I'm about to phone them.</b>",
        f"• Dialling: <code>{esc(dial_number)}</code>",
        f"• Party of {esc(party)}, {esc(when_text)}",
        f"• Under the name {esc(env('BOOKER_NAME', 'a guest'))}",
    ]
    if hard_list:
        card.append(f"• Mentioning: {esc_join(hard_list[:3])}")
    card.append("")
    if demo_override:
        card.append(
            "⚠️ <b>DEMO_PHONE is set.</b> The poll picked a real restaurant, but the "
            "phone that actually rings is one we control. Nobody uninvited gets called."
        )
    else:
        card.append(
            "ℹ️ This is a <b>real call to a real venue that consented in advance</b>. "
            "The agent says it is an AI in its first sentence. If it books a table, turn up or cancel."
        )
    card.append("\nA human presses the button, and a human dials the phone. I never call on my own.")

    save_state()
    tg.send(
        chat_id, "\n".join(card),
        reply_markup={
            "inline_keyboard": [[
                {"text": "✅ Approve the call", "callback_data": f"ok:{token}"},
                {"text": "❌ Cancel", "callback_data": f"no:{token}"},
            ]]
        },
    )


# ===========================================================================
# Approval
# ===========================================================================

def notify_bridge_dial() -> bool:
    try:
        req = urllib.request.Request(
            f"{BRIDGE_BASE}/dial", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def handle_callback(tg: Telegram, query: dict) -> None:
    data = query.get("data") or ""
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    who = (query.get("from") or {}).get("first_name", "someone")
    if chat_id is None:
        return
    state = state_for(chat_id)

    action, _, token = data.partition(":")
    if not state.approval_token or token != state.approval_token:
        tg.call("answerCallbackQuery", callback_query_id=query["id"],
                text="That card is stale — run /close again.", show_alert=True)
        return

    if action == "no":
        tg.call("answerCallbackQuery", callback_query_id=query["id"], text="Cancelled.")
        state.approval_token = None
        state.approval_payload = None
        state.awaiting = None
        state.pending_winner = None
        save_state()
        PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_PATH.write_text(json.dumps({"status": "cancelled"}, indent=2), encoding="utf-8")
        tg.send(chat_id, f"❌ {esc(who)} cancelled. Nothing was dialled.")
        return

    if action != "ok":
        return

    payload = dict(state.approval_payload or {})
    payload["approved_by"] = who
    payload["approved_at"] = datetime.now(timezone.utc).isoformat()
    payload["status"] = "approved"
    payload["dial"] = False

    PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    tg.call("answerCallbackQuery", callback_query_id=query["id"], text="Approved — handing to the call desk.")
    state.approval_token = None

    if notify_bridge_dial():
        tg.send(chat_id,
                f"✅ {esc(who)} approved it. The call desk is dialling "
                f"<code>{esc(payload['dial_number'])}</code> now — I'll post the transcript here.")
    else:
        payload["dial"] = True
        PENDING_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tg.send(chat_id,
                f"✅ {esc(who)} approved it, and the booking is queued for "
                f"<code>{esc(payload['dial_number'])}</code>.\n\n"
                "<i>The call desk isn't answering on :8080 though. Start it with "
                "<code>python3 bridge/server.py</code> and open "
                "<code>http://localhost:8080/</code> — it'll pick this up automatically.</i>")


# ===========================================================================
# Update routing
# ===========================================================================

HELP = (
    "<b>Shum-AI</b>\n\n"
    "I read this chat, pull out the constraints you've already agreed on, find three places "
    "that fit, run a poll — and once one of you approves, I <b>phone the restaurant</b> "
    "with a voice agent and book it.\n\n"
    "/decide — read the chat and propose three\n"
    "/close — close the poll, pick the winner, ask to call\n"
    "/status — what I've read and what I know\n"
    "/who — every preference I remember, with the quote it came from\n"
    "/forget NAME — drop someone; /forget all wipes it\n\n"
    "<i>I can only see messages sent after I joined. Just talk normally; I'm reading.</i>"
)


def handle_message(tg: Telegram, message: dict) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    state = state_for(chat_id)
    author = (message.get("from") or {}).get("first_name") or "someone"
    text = message.get("text") or message.get("caption") or ""

    if not text:
        return

    command = re.match(r"^/([a-z_]+)(?:@\w+)?\b", text.strip(), re.I)
    if not command:
        state.add(author, text)
        save_state()
        # If the bot asked a question, this message is very likely the answer.
        if state.awaiting:
            try_answer(tg, chat_id, state, text, author)
        return

    verb = command.group(1).lower()
    if verb in ("start", "help"):
        tg.send(chat_id, HELP)
    elif verb == "decide":
        handle_decide(tg, chat_id, state)
    elif verb == "close":
        handle_close(tg, chat_id, state)
    elif verb == "who":
        tg.send(chat_id, people.render(PEOPLE, chat_id))
    elif verb == "forget":
        target = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
        if target.lower() in ("all", "everyone", "everything"):
            target = ""
        message = people.forget(PEOPLE, chat_id, target)
        people.save(PEOPLE)
        tg.send(chat_id, message)
    elif verb == "status":
        constraint_count = len(state.constraints.get("hard", [])) + len(state.constraints.get("vetoed", []))
        tg.send(
            chat_id,
            f"<b>Status</b>\n"
            f"• {len(state.history)} lines of history (cap {HISTORY_LIMIT})\n"
            f"• {constraint_count} constraints from the last /decide\n"
            f"• {len(state.picks)} picks on the table\n"
            f"• poll open: {'yes' if state.poll_id else 'no'}\n"
            f"• DEMO_PHONE: {'set — all calls route to it' if env('DEMO_PHONE') else 'not set — real calls'}\n"
            f"• chat id: <code>{chat_id}</code>",
        )
    else:
        state.add(author, text)
        save_state()


def try_answer(tg: Telegram, chat_id: int, state: ChatState, text: str, author: str) -> None:
    """Treat an ordinary message as the answer to the question just asked.

    Deliberately forgiving. If the reply answers only half the question, the
    half that landed is kept and the bot asks for the rest, rather than
    discarding a good answer because the other one was missing. And if the
    reply is clearly about something else, nothing is said at all -- a bot that
    interrupts every message with "sorry, I didn't understand" is worse than
    one that waits.
    """
    needed = list((state.awaiting or {}).get("fields") or [])
    if not needed:
        state.awaiting = None
        return

    answer = pipeline.parse_answer(text, needed)
    got: list[str] = []
    for field in needed:
        value = answer.get(field)
        if not value:
            continue
        if field == "when_text" and not pipeline.time_is_bookable(value)[0]:
            continue        # parse_answer already filters, this is belt and braces
        state.constraints[field] = value
        got.append(field)

    if not got:
        # Silence is correct here. They may simply be still talking.
        return

    still = [field for field in needed if field not in got]
    state.awaiting = {"fields": still, "asked_at": (state.awaiting or {}).get("asked_at")} if still else None
    save_state()

    said = []
    if "party_size" in got:
        said.append(f"party of <b>{esc(state.constraints['party_size'])}</b>")
    if "when_text" in got:
        said.append(f"<b>{esc(state.constraints['when_text'])}</b>")
    tg.send(chat_id, f"Got it \u2014 {esc_join(said)}. Thanks {esc(author)}.")

    if still:
        asked = " and ".join(QUESTION_LABELS[field] for field in still)
        tg.send(chat_id, f"Still need <b>{asked}</b>?")
        return

    # Everything settled: carry straight on to the booking card.
    present_booking(tg, chat_id, state)


def handle_poll_answer(answer: dict) -> None:
    poll_id = answer.get("poll_id")
    user = answer.get("user") or {}
    chosen = answer.get("option_ids") or []
    for state in STATE.values():
        if state.poll_id == poll_id:
            if chosen:
                state.votes[user.get("id")] = chosen[0]
                state.voter_names[user.get("id")] = user.get("first_name", "?")
            else:
                state.votes.pop(user.get("id"), None)   # retracted vote
            save_state()
            return


ALLOWED = ["message", "poll_answer", "callback_query"]


def drain_backlog(tg: Telegram) -> int | None:
    """Absorb whatever getUpdates has queued, WITHOUT acting on any of it.

    On boot Telegram hands over everything since the last acknowledged offset.
    Replaying that means re-running a /decide from twenty minutes ago and
    re-firing a button press that was already handled — which crashes the bot
    before it has said hello. Messages go into history so context is not lost;
    commands and callbacks are dropped on the floor.
    """
    offset = None
    absorbed = 0
    for _ in range(12):
        batch = tg.call("getUpdates", offset=offset, timeout=0, limit=100,
                        allowed_updates=ALLOWED)
        if batch is None:
            # Could not ASK. This used to fall through to `return offset or 0`,
            # and 0 tells Telegram "start from the beginning" -- so it re-sent
            # the whole backlog and the live loop EXECUTED it: a /decide from
            # twenty minutes ago re-run, a button press re-fired. One network
            # blip at boot became a replay of every queued command. Returning
            # None makes main() retry instead of guessing.
            print("[bot] getUpdates unreachable while draining the backlog")
            return None
        if not batch:
            break
        for update in batch:
            offset = update["update_id"] + 1
            message = update.get("message")
            if message and (message.get("text") or "") and not message["text"].strip().startswith("/"):
                chat_id = (message.get("chat") or {}).get("id")
                if chat_id is not None:
                    state_for(chat_id).add(
                        (message.get("from") or {}).get("first_name") or "someone", message["text"]
                    )
                    absorbed += 1
    print(f"[bot] drained backlog: {absorbed} messages into history, 0 actions fired")
    return offset or 0


def main() -> None:
    load_env(ROOT / ".env")
    warn_if_tls_broken("bot")
    tg = Telegram(env("TELEGRAM_TOKEN"))

    me = tg.call("getMe", timeout=15)
    if not me:
        raise SystemExit("getMe failed — the token is wrong, or there is no network.")
    print(f"[bot] @{me.get('username')} online")
    if env("DEMO_PHONE"):
        print(f"[bot] SAFETY RAIL: DEMO_PHONE set, every call routes to {env('DEMO_PHONE')}")
    else:
        allow = env_list("CONSENTED_NUMBERS")
        print(f"[bot] live calling enabled; consent allowlist has {len(allow)} number(s)")

    global PEOPLE
    PEOPLE = people.load()
    if PEOPLE:
        total_people = sum(len(chat) for chat in PEOPLE.values())
        print(f"[bot] remembers {total_people} people across {len(PEOPLE)} chat(s)")

    restored = load_state()
    if restored:
        print(f"[bot] restored {restored} lines of history across {len(STATE)} chat(s) from disk")
    else:
        print("[bot] no saved history \u2014 starting with an empty memory")

    offset = drain_backlog(tg)
    while offset is None:
        # Refuse to enter the live loop without a confirmed drain: starting
        # from offset 0 would replay and execute everything still queued.
        print("[bot] retrying the drain in 3s before going live")
        time.sleep(3)
        offset = drain_backlog(tg)
    save_state()
    print("[bot] live. /decide in a group to start.")

    while True:
        updates = tg.call("getUpdates", offset=offset, timeout=25, limit=50,
                          allowed_updates=ALLOWED) or []
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    handle_message(tg, update["message"])
                elif "poll_answer" in update:
                    handle_poll_answer(update["poll_answer"])
                elif "callback_query" in update:
                    handle_callback(tg, update["callback_query"])
            except Exception as exc:  # one bad update must never kill the loop
                print(f"[bot] error handling update {update.get('update_id')}: {type(exc).__name__}: {exc}")
        if not updates:
            time.sleep(0.4)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[bot] stopped")
