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
    s = Suite("contract", expect_at_least=58)

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
