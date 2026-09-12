#!/usr/bin/env python3
"""setup_agent.py — provision the ElevenLabs agent from docs/elevenlabs-agent.md.

    python3 setup_agent.py --dry-run    # show what would be sent
    python3 setup_agent.py              # create or update the agent
    python3 setup_agent.py --list       # what agents exist on this account

Why this exists rather than a dashboard checklist: the agent's prompt has to
reference seven dynamic variables by exact name, and its data-collection schema
has to use six exact field names, or things fail SILENTLY. A prompt with a
misspelled variable makes a fluent, confident call that mentions no dietary
constraint and no time. A schema with a renamed field posts an empty outcome to
the chat. Neither raises an error anywhere.

So the documentation is the single source of truth and this script parses it.
The prompt a human reads and the prompt the agent runs cannot drift, because
they are the same bytes. The contract test suite already pins that doc against
the variables bridge/call_page.html actually sends, which closes the loop:
doc -> agent -> call page -> bridge, all four checked.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from envlite import env, load_env

DOC = ROOT / "docs" / "elevenlabs-agent.md"
API = "https://api.elevenlabs.io/v1/convai"
AGENT_NAME = "Shum-AI booking agent"
MAX_CALL_SECONDS = 180  # free tier is 15 agent-minutes/month in total

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


# ---------------------------------------------------------------------------
# Parse the documentation
# ---------------------------------------------------------------------------

def _heading_pos(text: str, label: str) -> int:
    """Find a heading by its TEXT, at any level and with any numbering.

    Matching on "### 1. First message" broke the moment the doc used "## 2.".
    The words are the stable part; the level and the number are not.
    """
    found = re.search(rf"^#{{1,6}}\s*(?:\d+[.)]\s*)?{re.escape(label)}\s*$",
                      text, re.M | re.I)
    if not found:
        raise SystemExit(f"no heading matching {label!r} in {DOC.name}")
    return found.end()


def _block_after(text: str, label: str) -> str:
    """The first fenced code block following a heading."""
    fence = re.search(r"```[a-z]*\n(.*?)```", text[_heading_pos(text, label):], re.S)
    if not fence:
        raise SystemExit(f"no code block found under {label!r} in {DOC.name}")
    return fence.group(1).strip()


def parse_doc() -> tuple[str, str, dict]:
    if not DOC.exists():
        raise SystemExit(f"{DOC} is missing")
    text = DOC.read_text(encoding="utf-8")

    first_message = _block_after(text, "First message")
    system_prompt = _block_after(text, "System prompt")

    # The schema table: | `field` | type | `description` |
    section = text[_heading_pos(text, "Data-collection schema"):]
    next_heading = re.search(r"^#{1,6}\s", section, re.M)
    if next_heading:
        section = section[:next_heading.start()]

    schema: dict[str, dict] = {}
    for row in re.finditer(r"^\|\s*`([a-z_]+)`\s*\|\s*(\w+)\s*\|\s*`(.+?)`\s*\|",
                           section, re.M):
        name, kind, description = row.group(1), row.group(2).lower(), row.group(3).strip()
        schema[name] = {
            "type": "number" if kind in ("number", "integer", "float") else kind,
            "description": description,
        }

    if not schema:
        raise SystemExit("could not parse the data-collection table from the doc")
    return first_message, system_prompt, schema


def variables_in(prompt: str, first_message: str) -> set[str]:
    return set(re.findall(r"\{\{([a-z_]+)\}\}", prompt + first_message))


def variables_the_page_sends() -> set[str]:
    page = (ROOT / "bridge" / "call_page.html").read_text(encoding="utf-8")
    block = page[page.index("const vars = {"):]
    return set(re.findall(r"^\s*([a-z_]+):", block[:block.index("};")], re.M))


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _call(path: str, payload=None, method="GET") -> dict:
    key = env("ELEVENLABS_API_KEY")
    if not key:
        raise SystemExit("ELEVENLABS_API_KEY is not set in .env")
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"xi-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}\n{detail}") from exc


def build_config(first_message: str, system_prompt: str, schema: dict,
                 variables: set[str]) -> dict:
    return {
        "name": AGENT_NAME,
        "conversation_config": {
            "agent": {
                "prompt": {"prompt": system_prompt},
                "first_message": first_message,
                # English out. ElevenLabs TTS has no Cantonese at all -- the
                # model list has Mandarin and no Yue -- while Scribe reads
                # Cantonese at 5.9% WER. So: speaks English, understands
                # Cantonese, which is how a large share of Hong Kong service
                # calls already run.
                "language": "en",
                "dynamic_variables": {
                    # Placeholders so the dashboard preview does not render
                    # "{{restaurant_name}}" literally when testing.
                    "dynamic_variable_placeholders": {
                        name: f"<{name}>" for name in sorted(variables)
                    }
                },
            },
            "conversation": {"max_duration_seconds": MAX_CALL_SECONDS},
        },
        "platform_settings": {
            # Authentication OFF, or connecting by plain agentId fails and the
            # call page would need signed URLs.
            "auth": {"enable_auth": False},
            "data_collection": schema,
        },
    }


def main() -> int:
    load_env(ROOT / ".env")
    dry_run = "--dry-run" in sys.argv

    if "--list" in sys.argv:
        agents = _call("/agents?page_size=30").get("agents") or []
        current = env("ELEVENLABS_AGENT_ID")
        print(f"\n{len(agents)} agent(s) on this account:")
        for agent in agents:
            mark = f"  {GREEN}<- .env points here{RESET}" if agent.get("agent_id") == current else ""
            print(f"  {agent.get('agent_id')}  {agent.get('name')!r}{mark}")
        if current and not any(a.get("agent_id") == current for a in agents):
            print(f"\n{RED}.env points at {current}, which is NOT on this account.{RESET}")
            print(f"{DIM}Most likely the agent and the API key belong to different "
                  f"ElevenLabs accounts or workspaces.{RESET}")
        return 0

    first_message, system_prompt, schema = parse_doc()
    used = variables_in(system_prompt, first_message)
    sent = variables_the_page_sends()

    print(f"\n{BOLD}Parsed from {DOC.name}{RESET}")
    print(f"  first message   {len(first_message)} chars")
    print(f"  system prompt   {len(system_prompt)} chars")
    print(f"  schema fields   {', '.join(sorted(schema))}")
    print(f"  variables       {', '.join(sorted(used))}")

    # Refuse to provision a mismatch. This is the silent failure the whole
    # script exists to prevent, so it is a hard stop rather than a warning.
    missing = used - sent
    unused = sent - used
    if missing:
        print(f"\n{RED}REFUSING: the prompt uses variables the call page never sends: "
              f"{sorted(missing)}{RESET}")
        print(f"{DIM}The agent would ask about a blank. Fix the doc or the call page.{RESET}")
        return 1
    if unused:
        print(f"{YELLOW}note: the call page sends {sorted(unused)}, which the prompt ignores{RESET}")
    print(f"  {GREEN}variable contract OK{RESET} — every {{{{variable}}}} is one the page sends")

    if not first_message.lower().startswith("hello, i'm an ai"):
        print(f"\n{RED}REFUSING: the first message must disclose being an AI in its first "
              f"sentence.{RESET}")
        return 1
    print(f"  {GREEN}AI disclosure present{RESET} in the first sentence spoken")

    config = build_config(first_message, system_prompt, schema, used)

    if dry_run:
        print(f"\n{DIM}--dry-run: nothing sent. Payload:{RESET}")
        print(json.dumps(config, indent=2)[:1500] + "\n...")
        return 0

    existing = env("ELEVENLABS_AGENT_ID")
    on_account = {a.get("agent_id") for a in (_call("/agents?page_size=30").get("agents") or [])}

    if existing and existing in on_account:
        _call(f"/agents/{existing}", config, "PATCH")
        agent_id = existing
        print(f"\n{GREEN}updated{RESET} existing agent {agent_id}")
    else:
        if existing:
            print(f"\n{YELLOW}{existing} is not on this account — creating a new agent{RESET}")
        agent_id = _call("/agents/create", config, "POST").get("agent_id")
        print(f"\n{GREEN}created{RESET} agent {agent_id}")

    # Read it back. Trusting a 200 is how you end up with an agent that is
    # missing the thing you just set.
    live = _call(f"/agents/{agent_id}")
    conv = live.get("conversation_config") or {}
    agent = conv.get("agent") or {}
    platform = live.get("platform_settings") or {}
    collected = platform.get("data_collection") or {}

    print(f"\n{BOLD}Verified on the server{RESET}")
    checks = [
        ("prompt stored", len(str((agent.get("prompt") or {}).get("prompt") or "")) > 500),
        ("first message stored", bool(agent.get("first_message"))),
        ("AI disclosure in the stored first message",
         "ai assistant" in str(agent.get("first_message") or "").lower()),
        ("authentication OFF", not (platform.get("auth") or {}).get("enable_auth")),
        (f"all {len(schema)} schema fields present", set(schema) <= set(collected)),
        ("prompt reads {{restaurant_name}}",
         "{{restaurant_name}}" in str((agent.get("prompt") or {}).get("prompt") or "")),
    ]
    for label, passed in checks:
        print(f"  {GREEN + 'yes' + RESET if passed else RED + 'NO ' + RESET}  {label}")

    if set(schema) - set(collected):
        print(f"  {YELLOW}missing from the server: {sorted(set(schema) - set(collected))}{RESET}")

    print(f"\n{BOLD}Put this in .env:{RESET}\n  ELEVENLABS_AGENT_ID={agent_id}\n")
    return 0 if all(p for _, p in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
