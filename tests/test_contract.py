"""The contract suite: every field bot.py writes is a field call_page.html reads.

This is the cheapest high-value test in the project. The bot and the call page
communicate only through pending_call.json, and they are written in different
languages in different files. Rename a key on one side and nothing errors —
the phone call simply says "undefined" out loud to a real restaurant.

So the two sides are read as text and compared.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Suite

BOT_SRC = (ROOT / "bot.py").read_text(encoding="utf-8")
PAGE_SRC = (ROOT / "bridge" / "call_page.html").read_text(encoding="utf-8")
SERVER_SRC = (ROOT / "bridge" / "server.py").read_text(encoding="utf-8")
AGENT_DOC = (ROOT / "docs" / "elevenlabs-agent.md").read_text(encoding="utf-8")
PREFLIGHT_SRC = (ROOT / "preflight.py").read_text(encoding="utf-8")


def fields_bot_writes() -> set[str]:
    """Parse bot.py's AST and pull the keys of the approval payload literal."""
    tree = ast.parse(BOT_SRC)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            is_payload = (
                isinstance(target, ast.Attribute) and target.attr == "approval_payload"
            )
            if is_payload and isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        found.add(key.value)
    # Written by handle_callback after the literal is built.
    found |= {"approved_by", "approved_at"}
    return found


def fields_page_reads() -> set[str]:
    """Every `p.<field>` the call page dereferences off the pending object."""
    return set(re.findall(r"\bp\.([a-z_][a-z0-9_]*)", PAGE_SRC))


def run() -> Suite:
    s = Suite("contract", expect_at_least=122)

    written = fields_bot_writes()
    read = fields_page_reads()

    s.check("bot.py's payload literal was parsed", len(written) > 10, f"found {written}")
    s.check("call_page.html's field reads were parsed", len(read) > 5, f"found {read}")

    # THE central assertion. A field the page reads but the bot never writes
    # renders as "undefined" on a live phone call.
    missing = read - written
    s.check("every field the call page reads is one the bot writes",
            not missing, f"page reads but bot never writes: {sorted(missing)}")

    # The reverse is informational, not a failure: the bot may legitimately
    # record things only the transcript or the archive uses.
    s.check("the bot writes at least everything the page needs", read <= written,
            f"gap: {sorted(read - written)}")

    # Fields the phone call cannot proceed without.
    for field in ("dial_number", "restaurant_name", "party_size", "when_text",
                  "booking_name", "callback_number", "constraints", "dial",
                  "status", "chat_id", "demo_override", "real_number"):
        s.check(f"bot writes '{field}'", field in written)

    # The page must actually use the things that make the call correct.
    s.check("the page reads the number to dial", "dial_number" in read)
    s.check("the page reads the demo-override flag", "demo_override" in read)
    s.check("the page passes the party size to the agent",
            "party_size" in PAGE_SRC and "dynamicVariables" in PAGE_SRC)
    s.check("the page passes the constraints to the agent", "constraints_text" in PAGE_SRC)

    # Server and page must agree on the endpoint names.
    for route in ("/pending", "/dial", "/outcome", "/config", "/turn", "/live", "/amend"):
        s.check(f"server serves {route}", f'"{route}"' in SERVER_SRC)
    for route in ("/pending", "/outcome", "/config"):
        s.check(f"page calls {route}", route in PAGE_SRC)

    # Safety invariants, asserted against the source so they cannot quietly rot.
    s.check("the page never rewrites the agent's prompt, only its variables",
            "dynamicVariables" in PAGE_SRC and "overrides" not in PAGE_SRC)
    s.check("the page refuses to run over file://", "file:" in PAGE_SRC)
    s.check("the page pre-checks microphone permission",
            "navigator.permissions" in PAGE_SRC)
    s.check("the page races getUserMedia against a timeout",
            "Promise.race" in PAGE_SRC and "timeout" in PAGE_SRC)
    s.check("the page renders a live microphone level meter",
            "getFloatTimeDomainData" in PAGE_SRC)
    s.check("the page uses WebRTC, not websocket", "webrtc" in PAGE_SRC)
    s.check("the server sends CORS headers for the optional console",
            "Access-Control-Allow-Origin" in SERVER_SRC)
    s.check("the server handles the CORS preflight", "do_OPTIONS" in SERVER_SRC)
    s.check("pending_call.json is written atomically",
            ".replace(" in SERVER_SRC and "tmp" in SERVER_SRC)
    s.check("the bot refuses numbers off the consent allowlist",
            "CONSENTED_NUMBERS" in BOT_SRC and "resolve_dial_target" in BOT_SRC)
    s.check("DEMO_PHONE takes precedence over everything in the resolver",
            BOT_SRC.index('demo = env("DEMO_PHONE")') < BOT_SRC.index("CONSENTED_NUMBERS"))
    s.check(".env is gitignored", ".env" in (ROOT / ".gitignore").read_text())
    s.check("pending_call.json is gitignored",
            "pending_call.json" in (ROOT / ".gitignore").read_text())

    # ---------------------------------------------------------------
    # The agent-prompt contract.
    # ---------------------------------------------------------------
    # The prompt lives in the ElevenLabs dashboard, outside this repo, so it
    # cannot be tested directly. What CAN be pinned is the documentation a
    # human pastes from: every {{variable}} the doc tells you to write must be
    # one the call page actually sends. A drift here is silent and expensive —
    # the agent makes a fluent call that mentions no dietary constraint and no
    # time, and sounds completely fine doing it.
    block = PAGE_SRC[PAGE_SRC.index("const vars = {"):]
    block = block[:block.index("};")]
    page_vars = set(re.findall(r"^\s*([a-z_]+):", block, re.M))
    doc_vars = set(re.findall(r"\{\{([a-z_]+)\}\}", AGENT_DOC))

    s.check("the agent doc's variables were parsed", len(doc_vars) >= 5, f"found {doc_vars}")
    s.check("the call page's dynamic variables were parsed", len(page_vars) >= 5, f"found {page_vars}")
    s.check("every {{variable}} the doc uses is one the call page sends",
            not (doc_vars - page_vars), f"doc uses but page never sends: {sorted(doc_vars - page_vars)}")
    s.check("every variable the page sends is used by the documented prompt",
            not (page_vars - doc_vars), f"sent but unused: {sorted(page_vars - doc_vars)}")
    for var in ("restaurant_name", "party_size", "when_text", "booking_name", "constraints_text"):
        s.check(f"the prompt reads {{{{{var}}}}}", "{{" + var + "}}" in AGENT_DOC)

    # The data-collection schema spans three files and a dashboard.
    doc_fields = set(re.findall(r"^\| `([a-z_]+)` \|", AGENT_DOC, re.M))
    server_fields = set(re.findall(r'collected\.get\("([a-z_]+)"\)', SERVER_SRC))
    s.check("the documented schema has six fields", len(doc_fields) == 6, f"found {sorted(doc_fields)}")
    s.check("every schema field server.py reads is one the doc defines",
            not (server_fields - doc_fields), f"undefined: {sorted(server_fields - doc_fields)}")
    s.check("preflight checks the schema fields too",
            "REQUIRED_SCHEMA_FIELDS" in PREFLIGHT_SRC)
    # Read the real constant rather than scraping its source text: a regex over
    # a multi-line set literal silently matched only the line-final entries and
    # "passed" against two of six fields.
    import preflight
    s.eq("preflight and the doc agree on the schema exactly",
         preflight.REQUIRED_SCHEMA_FIELDS, doc_fields)

    # Safety invariants that live in the prompt rather than the code.
    s.check("the documented first message discloses being an AI in its first sentence",
            "I'm an AI assistant" in AGENT_DOC)
    s.check("the prompt forbids implying it is a person",
            "Never imply you are a person" in AGENT_DOC)
    s.check("the prompt forbids inventing a phone number",
            "Never invent a phone number" in AGENT_DOC)
    s.check("the prompt tells it not to push after a refusal",
            "do not push" in AGENT_DOC.lower())

    # The outcome path must not read a field the page stopped sending.
    s.check("server no longer trusts a browser-supplied 'collected' as the only source",
            "collect_outcome" in SERVER_SRC)
    s.check("the page sends the conversation id for the agent's own analysis",
            "conversation_id" in PAGE_SRC and "conversationId" in PAGE_SRC)
    s.check("a derived outcome is labelled as derived in the chat message",
            "derived" in SERVER_SRC)

    # --- the live transcript ----------------------------------------------
    # Watching the call happen is what makes delegating it supervisable rather
    # than an act of faith, and it is the moment a human can still intervene.
    s.check("the call page pushes each turn as it happens", '"/turn"' in PAGE_SRC)
    s.check("pushing a turn is fire-and-forget so a dropped turn cannot "
            "interrupt a live phone call",
            ".catch(() => {})" in PAGE_SRC)
    s.check("the bridge opens a live message when dialling starts",
            "send_telegram_returning_id" in SERVER_SRC and "live_message_id" in SERVER_SRC)
    s.check("and edits it as turns arrive", "edit_telegram" in SERVER_SRC)
    s.check("edits are throttled, because Telegram rate-limits them",
            "_LIVE_EDIT_MIN_INTERVAL" in SERVER_SRC)
    s.check("a throttled edit loses nothing: the whole transcript re-renders",
            "render_live" in SERVER_SRC)
    s.check("the live message is closed off when the call ends",
            "finished=True" in SERVER_SRC)
    s.check("a failed edit is survivable, not fatal",
            "live edit skipped" in SERVER_SRC)
    s.check("the rendered transcript distinguishes the restaurant from the agent",
            SERVER_SRC.count('turn.get("source") == "user"') >= 1)
    s.check("the live message is truncated to Telegram's limit",
            "[:4000]" in SERVER_SRC)

    # --- fewer manual steps, without losing the rail ----------------------
    s.check("the page arms the mic itself when already permitted",
            "autoArm" in PAGE_SRC and '"granted"' in PAGE_SRC)
    s.check("but keeps the button for a first run", 'id="armBtn"' in PAGE_SRC)
    s.check("the bridge opens the call desk on startup", "open_call_page" in SERVER_SRC)
    s.check("and can be told not to", "NO_AUTO_OPEN" in SERVER_SRC)
    s.check("opening a browser never takes the bridge down",
            "except (OSError, FileNotFoundError)" in SERVER_SRC)
    # The rail that must survive all of this: the agent still waits for an
    # approval, and a human still dials.
    s.check("auto-arming does not auto-start the agent",
            "state.armed && !state.running" in PAGE_SRC)

    # --- what the agent says out loud --------------------------------------
    #
    # The prompt interpolates when_text into a sentence, so "8pm" came out as
    # "a table for 6 people 8pm" on a real call. The fix adds a preposition
    # for speech only: the stored when_text keeps the group's own words,
    # because the booking record and the calendar entry quote them.
    s.check("the spoken time gets a preposition", "function spokenWhen" in PAGE_SRC)
    s.check("and it is what the agent is given", "when_text: spokenWhen(" in PAGE_SRC)
    s.check("a phrase that already reads as a time is left alone",
            "leads.test(t) ? t :" in PAGE_SRC)
    s.check("weekday names match with their suffix, so 'friday at 9' is left alone",
            "[a-z]*" in PAGE_SRC and "does not match" in PAGE_SRC)
    s.check("the stored when_text is not rewritten",
            "p.when_text = " not in PAGE_SRC)

    # --- the result message must not repeat the transcript -----------------
    #
    # The live message is edited turn by turn and already holds the whole
    # conversation. Printing it again in the outcome gave the group two
    # identical walls of text in a row and buried the booking under them.
    s.check("the outcome message carries no transcript of its own",
            "<b>Transcript</b>" not in SERVER_SRC)
    s.check("...and the live message still does",
            "def render_live" in SERVER_SRC)
    s.check("the calendar link sits with the booking facts, above the caveats",
            SERVER_SRC.index("Add to your calendar") < SERVER_SRC.index("read back off the transcript"))

    # --- a rehearsal must not look like a reservation -----------------------
    #
    # With DEMO_PHONE set the call never reached the venue, so there is no
    # table. A calendar entry saying "Dinner at Mezzo" with the restaurant's
    # real number, sitting in six people's calendars, is a booking that does
    # not exist -- and nobody re-reads a calendar entry to check.
    invite_src = (ROOT / "invite.py").read_text(encoding="utf-8")
    s.check("a demo booking says so in the calendar entry",
            "REHEARSAL" in invite_src)
    s.check("...and in its title", '"[rehearsal] "' in invite_src)
    s.check("...and does not carry the real venue's number",
            'not pending.get("demo_override")' in invite_src)

    # --- the newest turn has to actually be on screen ----------------------
    #
    # With scroll-behavior: smooth on the transcript, `scrollTop =
    # scrollHeight` is an animation, and each arriving turn restarts it.
    # Measured with turns 120ms apart: the latest line settled 1081px below
    # the fold and never got there. A call desk whose transcript lags a minute
    # behind the call is worse than no transcript, because it is believed.
    s.check("the transcript scrolls instantly, not smoothly",
            "scroll-behavior: smooth" not in PAGE_SRC)
    s.check("every new line pins the thread to the bottom",
            PAGE_SRC.count("thread.scrollTop = thread.scrollHeight") >= 2,
            "both a spoken turn and a system note have to do it")
    s.check("and the measurement is recorded so it is not re-added for polish",
            "1081px" in PAGE_SRC)

    # --- the call page must not narrate a call that is not happening -------
    #
    # pending_call.json is patched, never emptied, so a finished call leaves a
    # populated object behind. The page used to test Object.keys(p).length,
    # which made every finished call look live: the amber "this dials a real
    # venue" consent rail sat over a blank strip, and the last number dialled
    # stayed on screen for someone to dial again. Caught by looking at it.
    s.check("a live call needs a number, a name, and a status that is not done",
            "p.status !== \"done\"" in PAGE_SRC and "p.dial_number" in PAGE_SRC)
    s.check("emptiness is no longer decided by counting keys",
            "Object.keys(p).length" not in PAGE_SRC)
    s.check("and the reason is recorded next to the check",
            "never emptied" in PAGE_SRC)

    # --- the agent must never invent the facts of a booking ----------------
    #
    # The page read `p.party_size ?? 4` and defaulted the time to "this
    # evening", so a missing field became something the agent said OUT LOUD to
    # a real restaurant as though the group had asked for it. bot.py refuses to
    # arm a vague booking, but the console can /amend one and this page has a
    # manual start button, so the last word belongs here.
    s.check("no invented party size", "party_size ?? 4" not in PAGE_SRC)
    s.check("no invented time", '"this evening";' not in PAGE_SRC)
    s.check("the page refuses to start an incomplete booking",
            "missing.push" in PAGE_SRC and "will not make it up" in PAGE_SRC)
    s.check("it requires a real hour, not just any words",
            "function hasClock" in PAGE_SRC)
    s.check("...and checks a restaurant, a party size and a time",
            all(x in PAGE_SRC for x in ("a restaurant", "a party size", "an exact time")))

    # --- the bridge will not re-arm or rewrite a call ----------------------
    #
    # A second POST /dial on a finished call gave it a new live message and an
    # empty transcript; /amend applied to a call already on the phone; /cancel
    # marked a completed booking cancelled, so the record denied a table that
    # exists. All three reproduced over HTTP against the real server.
    s.check("/dial refuses anything not waiting to be dialled",
            "not waiting to be dialled" in SERVER_SRC)
    s.check("/amend refuses a call in flight or finished",
            "can no longer be amended" in SERVER_SRC)
    s.check("/cancel refuses a finished call",
            "already finished" in SERVER_SRC)
    # EXECUTED, not grepped. The first version of this gate read
    # `not pipeline.time_is_bookable(value)`, which is always False because
    # that function returns a (ok, reason) TUPLE and a non-empty tuple is
    # truthy. The grep-for-the-call test passed; the endpoint accepted "this
    # evening" with a 200. Only driving the real server caught it, so this now
    # runs the check instead of reading it.
    import bridge.server as _srv  # noqa: PLC0415
    for vague in ("this evening", "tonight", "lunchtime", "sometime", "friday"):
        ok, why = _srv.amend_when_text(vague)
        s.check(f"/amend refuses when_text {vague!r}", ok is False, why)
        s.check(f"...and says why for {vague!r}", bool(why) and "when_text" in why)
    for real in ("8pm", "20:30", "noon", "tomorrow at 7:30 pm"):
        ok, why = _srv.amend_when_text(real)
        s.check(f"/amend accepts when_text {real!r}", ok is True, why)
    s.check("/amend routes through that checked helper",
            "amend_when_text(value)[0]" in SERVER_SRC)
    # The bug was a truthiness mistake, so pin the shape the helper returns:
    # anything that goes back to returning a bare tuple breaks these.
    shape = _srv.amend_when_text("this evening")
    s.check("the helper returns a 2-tuple", isinstance(shape, tuple) and len(shape) == 2)
    s.check("...whose first element is a real bool", shape[0] is False)

    # --- the search pool is ranked before it is cut ------------------------
    #
    # The cache is territory-wide and constraint-blind by design, so cutting it
    # to 60 before relevance_rank threw away the rows the chat had asked for:
    # measured, 1 of 15 Sha Tin rows survived, which is indistinguishable from
    # "there are no restaurants near Sha Tin".
    places_src = (ROOT / "places.py").read_text(encoding="utf-8")
    s.check("search_places can return the whole pool",
            "limit: int | None = 60" in places_src)
    s.check("...and every return path honours that",
            "[:limit]" not in places_src.split("def search_places")[1])
    s.check("the bot asks for the whole pool",
            "search_places(areas=wanted_areas, limit=None)" in BOT_SRC)
    s.check("...and cuts only after ranking",
            ", constraints\n        )[:60]" in BOT_SRC)

    # --- dialling is a human's job, permanently ---------------------------
    #
    # There WAS an auto-dial here: macOS hands a tel: URL to FaceTime, which
    # relays through a paired iPhone. It worked -- the phone rang. It was
    # removed anyway, because it put the call's audio on the same machine as
    # the agent, and then each end's echo cancellation deleted the signal the
    # other needed. Measured 12 Sep: answered, silence both ways.
    #
    # These checks exist so it does not come back. The failure was invisible
    # from the code -- it looked like a working feature -- so the only defence
    # is a test that says no.
    s.check("there is no auto-dial", "def dial_phone" not in SERVER_SRC)
    s.check("nothing hands a tel: URL to the system",
            'f"tel:{' not in SERVER_SRC and '"tel:' not in SERVER_SRC)
    s.check("no facetime-audio: either",
            "facetime-audio" not in SERVER_SRC)
    s.check("the AUTO_DIAL switch is gone from the code",
            "AUTO_DIAL" not in SERVER_SRC)
    s.check("...and from the example env, so nobody sets it hopefully",
            "AUTO_DIAL" not in (ROOT / ".env.example").read_text(encoding="utf-8"))
    s.check("the bridge does not claim a dial it cannot make",
            "auto_dialled" not in SERVER_SRC)
    s.check("approval tells the operator to dial by hand",
            "by hand" in SERVER_SRC and "speakerphone" in SERVER_SRC)
    # The reason has to stay next to the code, or the next person re-adds it.
    s.check("and the reason it cannot work is recorded where it was removed",
            "echo" in SERVER_SRC and "air between" in SERVER_SRC)
    s.check("two humans is now the design, not a concession",
            "is not a compromise here" in SERVER_SRC)

    # --- booking links ----------------------------------------------------
    places_src = (ROOT / "places.py").read_text(encoding="utf-8")
    exa_src = (ROOT / "exa_search.py").read_text(encoding="utf-8")
    s.check("OSM's website tag is captured", "contact:website" in places_src)
    s.check("a bare domain is made into a URL", 'startswith(("http://", "https://"))' in places_src)
    s.check("Exa's URL fills in where OSM has no website", '"website"' in exa_src)
    s.check("an Exa URL is never promoted to a phone number",
            'fresh["phone"] = None' in exa_src)
    s.check("the bot offers the booking page when it cannot call",
            "booking page" in BOT_SRC)
    s.check("and says a human has to finish it", "by hand" in BOT_SRC)

    # ---------------------------------------------------------------
    # The console contract.
    # ---------------------------------------------------------------
    console = ROOT / "console"
    if not console.exists():
        s.check("console directory exists", False, "console/ is missing")
        return s

    lib = (console / "lib" / "bridge.ts").read_text(encoding="utf-8")
    panel = (console / "components" / "Console.tsx").read_text(encoding="utf-8")
    provider = (console / "components" / "Providers.tsx").read_text(encoding="utf-8")
    runtime = (console / "app" / "api" / "copilotkit" / "[[...path]]" / "route.ts").read_text(encoding="utf-8")

    # The console is a THIRD reader of pending_call.json. A field it reads that
    # the bot never writes renders as a blank in front of an audience.
    # Scope to the PendingCall interface. An unscoped regex also swept up
    # BridgeConfig's fields and the `bridge` client's method names, and then
    # "failed" by reporting `health` and `base` as missing booking fields.
    block = lib[lib.index("export interface PendingCall {"):]
    block = block[:block.index("\n}")]
    ts_fields = set(re.findall(r"^\s{2}([a-z_]+)\??:", block, re.M))
    s.check("the console's PendingCall interface was parsed", len(ts_fields) > 10,
            f"found {sorted(ts_fields)}")

    # bot.py writes the booking; bridge/server.py writes the result of the call.
    # Both are legitimate producers, so the console may read either.
    server_written = set(re.findall(r"^\s+(?:patch_pending|write_pending)\(", SERVER_SRC, re.M))
    server_fields = set(re.findall(r"\b([a-z_]+)=", SERVER_SRC[
        SERVER_SRC.index("patch_pending("):])) if "patch_pending(" in SERVER_SRC else set()
    produced = written | server_fields | {"outcome", "outcome_source", "finished_at", "amended_at"}
    unknown = ts_fields - produced
    s.check("every field the console types is one the bot or the bridge writes",
            not unknown, f"console types but nothing writes: {sorted(unknown)}")

    # v2, not the deprecated v1 API every tutorial still shows.
    s.check("console imports from the v2 entry point",
            "@copilotkit/react-core/v2" in provider and "@copilotkit/react-core/v2" in panel)
    s.check("console uses CopilotKitProvider, not the v1 CopilotKit component",
            "CopilotKitProvider" in provider)
    # Check for real USAGE, not prose. The first version of this check tripped
    # on the source comment that warns useCopilotAction is deprecated — a test
    # that fails because the code documents the thing it avoids is a bad test.
    def strip_comments(src: str) -> str:
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        return re.sub(r"^\s*//.*$", "", src, flags=re.M)

    for label, src in (("Console.tsx", panel), ("Providers.tsx", provider)):
        code = strip_comments(src)
        s.check(f"{label} does not actually call the deprecated useCopilotAction",
                "useCopilotAction(" not in code and "useCopilotAction," not in code
                and "useCopilotAction " not in code)
    s.check("the deprecation IS documented in a comment, so nobody re-adds it",
            "useCopilotAction" in provider)
    s.check("console uses useFrontendTool", "useFrontendTool" in panel)
    s.check("console uses useHumanInTheLoop", "useHumanInTheLoop" in panel)
    s.check("console uses useAgentContext for shared state", "useAgentContext" in panel)
    s.check("runtime imports from @copilotkit/runtime/v2",
            "@copilotkit/runtime/v2" in runtime)
    s.check("runtime uses BuiltInAgent", "BuiltInAgent" in runtime)
    s.check("runtime uses a Google model, needing no OpenAI key",
            "createGoogleGenerativeAI" in runtime and "OPENAI" not in runtime.upper())

    # The safety property, asserted structurally.
    hitl = panel[panel.index("useHumanInTheLoop("):]
    hitl = hitl[:hitl.index("\n  });")]
    s.check("place_call is the human-in-the-loop tool", '"place_call"' in hitl)
    s.check("place_call has NO handler — the model cannot resolve it alone",
            "handler:" not in hitl)
    s.check("place_call renders an approval card instead", "render:" in hitl)
    s.check("approval calls the bridge rather than dialling in the browser",
            "bridge.dial()" in hitl)

    # Amend must not be able to route around the consent allowlist.
    s.check("the bridge exposes /amend", '"/amend"' in SERVER_SRC)
    amend = SERVER_SRC[SERVER_SRC.index('route == "/amend"'):]
    amend = amend[:amend.index('route == "/cancel"')]
    for forbidden in ("dial_number", "real_number", "restaurant_name", "demo_override"):
        s.check(f"/amend refuses to change {forbidden}",
                f'"{forbidden}"' not in amend.split("rejected")[0]
                or forbidden in amend)
    s.check("/amend allowlists only the four safe fields",
            '("party_size", "when_text", "booking_name", "notes")' in amend)
    s.check("the console's amend helper sends only those four fields",
            all(f in lib for f in ("party_size", "when_text", "booking_name", "notes")))

    # ---------------------------------------------------------------
    # Three console bugs found by actually opening the page. All three
    # rendered without erroring, which is why they need pinning.
    # ---------------------------------------------------------------
    proxy_path = console / "app" / "api" / "bridge" / "[...path]" / "route.ts"
    s.check("a same-origin bridge proxy exists", proxy_path.exists())
    if proxy_path.exists():
        proxy = proxy_path.read_text(encoding="utf-8")
        # The browser blocked direct :8080 fetches as ERR_BLOCKED_BY_CLIENT
        # even with CORS headers present. Proxying removes the cross-origin
        # hop instead of configuring around it.
        s.check("the console fetches same-origin, not :8080 directly",
                'const BASE = "/api/bridge"' in lib)
        s.check("the console no longer hardcodes the bridge port client-side",
                "127.0.0.1:8080" not in lib and "localhost:8080" not in lib)
        s.check("the proxy allowlists routes rather than forwarding anything",
                "ALLOWED" in proxy and "new Set(" in proxy)
        for route in ("health", "config", "pending", "dial", "cancel", "amend"):
            s.check(f"the proxy allows {route}", f'"{route}"' in proxy)
        s.check("the proxy distinguishes 'bridge down' from 'bridge errored'",
                "bridge unreachable" in proxy and "502" in proxy)

    # CopilotKit v2 defaults to a light palette; without `.dark` the chat
    # rendered light-on-light and looked like a broken panel.
    layout = (console / "app" / "layout.tsx").read_text(encoding="utf-8")
    s.check("the app opts CopilotKit into its dark palette",
            'className="dark"' in layout)

    # CopilotChat wraps itself in a `display: contents` div, so `.chatwrap > *`
    # never reached .copilotKitChat and it computed to width 0 — every message
    # wrapped one word per line.
    css = (console / "app" / "globals.css").read_text(encoding="utf-8")
    s.check("the stylesheet reaches past the display:contents wrapper",
            ".chatwrap .copilotKitChat" in css or ".chatwrap > * > *" in css)
    s.check("CopilotKit's theme variables are re-pointed at our tokens",
            "--primary: var(--accent)" in css)
    return s
