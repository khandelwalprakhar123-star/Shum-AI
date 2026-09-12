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
    return s
