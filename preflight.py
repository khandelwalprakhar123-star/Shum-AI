#!/usr/bin/env python3
"""preflight.py — check every dependency before it matters.

Run this instead of guessing:

    python3 preflight.py

Each line is PASS, WARN or FAIL. A WARN degrades gracefully and you can demo
without it. A FAIL will stop the demo, and every FAIL prints the exact command
or click that fixes it.

The reason this file exists: the failure modes in this project are mostly
SILENT. An empty TLS trust store looks like a network outage. A bot with
privacy mode still on looks like a bot that is ignoring you. A retired Gemini
model looks like a bad prompt. Each one costs twenty minutes to diagnose under
pressure and four seconds to detect deliberately.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pipeline
import places
from envlite import check_tls, env, env_list, load_env

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)

results: list[tuple[str, str, str, str]] = []   # (level, area, message, fix)


def ok(area: str, message: str) -> None:
    results.append(("PASS", area, message, ""))


def warn(area: str, message: str, fix: str = "") -> None:
    results.append(("WARN", area, message, fix))


def bad(area: str, message: str, fix: str = "") -> None:
    results.append(("FAIL", area, message, fix))


def _get(url: str, headers: dict | None = None, timeout: int = 15):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------

def check_tls_store() -> None:
    problem = check_tls()
    if problem is None:
        ok("tls", "trust store populated")
    else:
        bad("tls", "TLS trust store is EMPTY — every HTTPS call will fail",
            f"open '/Applications/Python {sys.version_info.major}.{sys.version_info.minor}/"
            f"Install Certificates.command'")


def check_env_file() -> None:
    if not (ROOT / ".env").exists():
        bad("env", ".env does not exist", "cp .env.example .env  # then fill it in")
        return
    ok("env", ".env present")

    required = {
        "TELEGRAM_TOKEN": "BotFather -> /newbot",
        "GEMINI_API_KEY": "aistudio.google.com, no card needed",
        "ELEVENLABS_AGENT_ID": "elevenlabs.io -> Agents -> your agent -> copy the ID",
    }
    optional = {
        "EXA_API_KEY": "exa.ai — without it discovery returns [] and OSM carries the search",
        "ELEVENLABS_API_KEY": "without it the call outcome is derived from the transcript instead",
        "OPENROUTER_API_KEY": "without it there is no second model vendor",
        "BOOKER_NAME": "the agent will say 'a guest' on the phone",
        "CALLBACK_NUMBER": "the agent cannot leave a callback number",
    }
    for key, fix in required.items():
        if env(key):
            ok("env", f"{key} set")
        else:
            bad("env", f"{key} is empty", fix)
    for key, why in optional.items():
        value = env(key)
        if value and value != "+852":
            ok("env", f"{key} set")
        else:
            warn("env", f"{key} is empty", why)


def check_telegram() -> None:
    token = env("TELEGRAM_TOKEN")
    if not token:
        bad("telegram", "skipped — no token")
        return
    try:
        payload = _get(f"https://api.telegram.org/bot{token}/getMe")
    except urllib.error.HTTPError as exc:
        bad("telegram", f"getMe returned HTTP {exc.code} — the token is wrong",
            "BotFather -> /mybots -> API token, or /revoke and regenerate")
        return
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        bad("telegram", f"cannot reach Telegram ({type(exc).__name__})", "check the network")
        return

    me = payload.get("result") or {}
    ok("telegram", f"authenticated as @{me.get('username')}")

    # THE most common blocker in the whole project. With privacy mode on, the
    # bot receives only commands addressed to it and cannot see the argument
    # it exists to read — so /decide finds nothing and nothing looks broken.
    if me.get("can_read_all_group_messages") is True:
        ok("telegram", "group privacy DISABLED — the bot can read the conversation")
    else:
        bad("telegram",
            "privacy mode is ON — the bot CANNOT see group messages, so /decide will find nothing",
            "BotFather -> /setprivacy -> pick the bot -> Disable. "
            "Then REMOVE the bot from the group and add it again, or the old setting sticks.")


def check_gemini() -> None:
    if not env("GEMINI_API_KEY"):
        bad("gemini", "skipped — no key")
        return
    try:
        text = pipeline._call_gemini(
            'Reply with exactly this JSON and nothing else: {"ok": true}'
        )
    except pipeline.ModelUnavailable as exc:
        bad("gemini", f"every model in the chain refused: {exc}",
            "check the key at aistudio.google.com; or set OPENROUTER_API_KEY as a second vendor")
        return
    try:
        json.loads(text)
        ok("gemini", "responded with valid JSON")
    except json.JSONDecodeError:
        warn("gemini", "responded, but not with clean JSON — the salvage path will handle it")


def check_exa() -> None:
    key = env("EXA_API_KEY")
    if not key:
        warn("exa", "no key — discovery will return [] (this is fine)")
        return
    import exa_search
    rows = exa_search.search({"prefer_cuisines": ["thai"], "party_size": 4}, limit=3)
    if rows:
        ok("exa", f"returned {len(rows)} names, none carrying a phone number")
    else:
        warn("exa", "returned nothing — OSM will carry the search",
             "check credits at exa.ai; harmless either way")


def check_overpass() -> None:
    rows = [r for r in (places._row_from_element(e)
                        for e in places._fetch_overpass(
                            places.build_query(places.AREAS["hk_island_north"]))) if r]
    if not rows:
        warn("overpass", "live query returned nothing — the cache will carry it",
             "python3 places.py --refresh-cache  (needs working wifi)")
        return
    callable_n = sum(1 for r in rows if r.get("phone"))
    ok("overpass", f"live: {len(rows)} places, {callable_n} callable")


def check_cache() -> None:
    rows = places.read_cache()
    if not rows:
        warn("cache", "places_cache.json is empty — no offline safety net",
             "python3 places.py --refresh-cache")
        return
    callable_n = sum(1 for r in rows if r.get("phone"))
    if callable_n == 0:
        bad("cache", f"cache has {len(rows)} places but NONE are callable",
            "python3 places.py --refresh-cache")
    else:
        ok("cache", f"{len(rows)} places cached, {callable_n} callable "
                    f"({100 * callable_n // len(rows)}%)")


REQUIRED_SCHEMA_FIELDS = {
    "status", "confirmed_time", "confirmed_party_size",
    "wait_estimate_minutes", "staff_notes", "booking_name",
}


def check_elevenlabs() -> None:
    agent_id = env("ELEVENLABS_AGENT_ID")
    if not agent_id:
        bad("elevenlabs", "skipped — no agent id")
        return
    api_key = env("ELEVENLABS_API_KEY")
    if not api_key:
        warn("elevenlabs", f"agent id set ({agent_id[:8]}…) but no API key, so the agent's "
                           "config cannot be verified from here",
             "check by hand: authentication OFF, and a data-collection schema configured. "
             "See docs/elevenlabs-agent.md")
        return

    try:
        agent = _get(f"https://api.elevenlabs.io/v1/convai/agents/{agent_id}",
                     {"xi-api-key": api_key})
    except urllib.error.HTTPError as exc:
        # ElevenLabs distinguishes these two cases for us in the response body:
        # detail.status == "missing_permissions" means the key authenticated
        # fine and is simply scoped too narrowly. That distinction matters
        # because "HTTP 401" on its own sends you hunting for a typo in a key
        # that is completely correct.
        #
        # Do NOT try to infer this by probing another endpoint instead: every
        # endpoint carries its own permission, so /v1/user 401s for a
        # convai-scoped key too, and an earlier version of this check
        # confidently reported a valid key as rejected because of it.
        status, message = "", ""
        try:
            detail = (json.loads(exc.read().decode("utf-8", "replace")) or {}).get("detail") or {}
            status = str(detail.get("status") or "")
            message = str(detail.get("message") or "")
        except Exception:
            pass

        if status == "missing_permissions":
            needed = "convai_read"
            found = re.search(r"permission (\w+)", message)
            if found:
                needed = found.group(1)
            warn("elevenlabs",
                 f"the API key is valid but lacks the {needed} permission, so the agent's "
                 "config cannot be checked from here and the bridge cannot read the agent's "
                 "own post-call analysis",
                 "elevenlabs.io -> Settings -> API Keys -> edit this key -> enable "
                 "Conversational AI (read + write). Without it the outcome is derived from "
                 "the transcript by Gemini, which works and is labelled as derived in the chat.")
        elif exc.code in (401, 403):
            bad("elevenlabs", f"the API key was rejected (HTTP {exc.code}) "
                              f"{('- ' + message[:70]) if message else ''}",
                "elevenlabs.io -> Settings -> API Keys")
        elif exc.code == 404:
            bad("elevenlabs", f"no agent with id {agent_id[:14]}\u2026 (HTTP 404)",
                "copy the id again from elevenlabs.io -> Agents")
        else:
            bad("elevenlabs", f"cannot fetch the agent: HTTP {exc.code}")

        warn("elevenlabs", "so verify BY HAND: authentication OFF, prompt and first message "
                           "pasted, all six data-collection fields added",
             "docs/elevenlabs-agent.md")
        return
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        warn("elevenlabs", f"could not verify the agent ({type(exc).__name__})")
        return

    ok("elevenlabs", f"agent reachable: {agent.get('name') or agent_id[:8]}")

    platform = agent.get("platform_settings") or {}
    auth = (platform.get("auth") or {})
    if auth.get("enable_auth"):
        bad("elevenlabs", "agent authentication is ON — connecting by plain agentId will fail",
            "ElevenLabs -> your agent -> Security -> turn authentication OFF")
    else:
        ok("elevenlabs", "authentication OFF — plain agentId connection will work")

    collection = ((platform.get("data_collection") or {}) or
                  ((agent.get("conversation_config") or {}).get("data_collection") or {}))
    present = set(collection.keys()) if isinstance(collection, dict) else set()
    missing = REQUIRED_SCHEMA_FIELDS - present
    if not present:
        warn("elevenlabs", "no data-collection schema configured — the bridge will derive the "
                           "outcome from the transcript instead",
             "add the six fields in docs/elevenlabs-agent.md for a cleaner result")
    elif missing:
        warn("elevenlabs", f"data-collection schema is missing: {', '.join(sorted(missing))}",
             "see docs/elevenlabs-agent.md")
    else:
        ok("elevenlabs", "data-collection schema has all six fields")

    prompt = ((agent.get("conversation_config") or {}).get("agent") or {}).get("prompt") or {}
    prompt_text = str(prompt.get("prompt") or "")
    if prompt_text:
        disclosed = any(term in prompt_text.lower()
                        for term in ("ai assistant", "an ai", "automated assistant"))
        if disclosed:
            ok("elevenlabs", "agent prompt mentions being an AI")
        else:
            bad("elevenlabs", "agent prompt does not appear to disclose that it is an AI",
                "non-negotiable. See docs/elevenlabs-agent.md for the prompt to paste.")
        for var in ("restaurant_name", "party_size", "when_text"):
            if "{{" + var + "}}" not in prompt_text:
                warn("elevenlabs", f"prompt does not use {{{{{var}}}}} — the call will be generic",
                     "the call page passes these as dynamic variables; the prompt must read them")


def check_bridge() -> None:
    try:
        payload = _get("http://127.0.0.1:8080/health", timeout=3)
        ok("bridge", f"running on :8080 ({payload.get('service')})")
    except Exception:
        warn("bridge", "not running on :8080",
             "python3 bridge/server.py   # then open http://localhost:8080/")


def check_safety() -> None:
    demo = env("DEMO_PHONE")
    allow = [n for n in env_list("CONSENTED_NUMBERS") if n and n != "+852"]

    if demo:
        normalised = places.normalise_phone(demo)
        if not normalised:
            bad("safety", f"DEMO_PHONE={demo!r} is not a valid HK number",
                "use the form +852XXXXXXXX")
        else:
            ok("safety", f"DEMO_PHONE set — EVERY call routes to {normalised}, "
                         f"whichever restaurant wins")
        return

    if not allow:
        bad("safety", "no DEMO_PHONE and an EMPTY consent allowlist — every approval "
                      "will be refused, by design",
            "either set DEMO_PHONE to a phone you control, or put the consenting "
            "restaurant's number in CONSENTED_NUMBERS")
        return

    bad_numbers = [n for n in allow if not places.normalise_phone(n)]
    if bad_numbers:
        bad("safety", f"unparseable numbers on the allowlist: {bad_numbers}",
            "use the form +852XXXXXXXX")
    else:
        ok("safety", f"LIVE CALLING to {len(allow)} consented number(s): "
                     f"{', '.join(places.normalise_phone(n) for n in allow)}")
        warn("safety", "no DEMO_PHONE — a real venue will be dialled. Confirm they consented, "
                       "and if a table gets booked, turn up or cancel it.")


# ---------------------------------------------------------------------------

def main() -> int:
    load_env(ROOT / ".env")
    print(f"\n{BOLD}Shum-AI preflight{RESET}")
    print(f"{DIM}every dependency, checked live{RESET}\n")

    for step in (check_tls_store, check_env_file, check_telegram, check_gemini,
                 check_exa, check_overpass, check_cache, check_elevenlabs,
                 check_bridge, check_safety):
        try:
            step()
        except Exception as exc:
            bad(step.__name__.replace("check_", ""),
                f"the check itself crashed: {type(exc).__name__}: {exc}")

    width = max(len(area) for _, area, _, _ in results)
    for level, area, message, fix in results:
        colour = {"PASS": GREEN, "WARN": YELLOW, "FAIL": RED}[level]
        print(f"  {colour}{level}{RESET}  {area:<{width}}  {message}")
        if fix:
            print(f"        {' ' * width}  {DIM}fix: {fix}{RESET}")

    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    passes = [r for r in results if r[0] == "PASS"]

    print(f"\n{BOLD}{len(passes)} pass, {len(warns)} warn, {len(fails)} fail{RESET}")
    if fails:
        print(f"\n{RED}{BOLD}NOT READY{RESET} — fix the {len(fails)} FAIL line(s) above.\n")
        return 1
    if warns:
        print(f"\n{YELLOW}READY, with {len(warns)} degradation(s){RESET} — "
              f"each of those has a fallback. You can demo.\n")
        return 0
    print(f"\n{GREEN}{BOLD}ALL SYSTEMS GO{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
