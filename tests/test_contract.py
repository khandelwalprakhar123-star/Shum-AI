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
    s = Suite("contract", expect_at_least=18)

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
    for route in ("/pending", "/dial", "/outcome", "/config"):
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
    return s
