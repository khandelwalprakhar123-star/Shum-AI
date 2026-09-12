#!/usr/bin/env python3
"""bridge/server.py — the localhost glue between the group chat and the voice agent.

Everything the voice layer touches lives on 127.0.0.1. There is no tunnel, no
ngrok, no public webhook URL. That is a deliberate architectural choice and not
a shortcut: a cloudflared tunnel that dies at 16:04 is the single most common
way a hackathon voice demo fails, and we removed the possibility rather than
hoping.

Endpoints
  GET  /health   -> liveness, so you can tell "server down" from "agent down"
  GET  /config   -> agent id + booking identity, so no key is baked into HTML
  GET  /pending  -> the approved booking written by bot.py, or {}
  POST /dial     -> flip dial:true; the call page is polling and auto-starts
  POST /outcome  -> transcript + the agent's structured result, posted to chat
  GET  /         -> call_page.html

The last one matters more than it looks. getUserMedia is refused on file://
(gotcha 12), so the page MUST arrive over http://localhost. Serving it from the
same origin as the API also means the browser never needs a CORS preflight for
the calls that matter.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import pipeline  # noqa: E402
from envlite import env, env_flag, load_env, warn_if_tls_broken  # noqa: E402
from tgtext import esc  # noqa: E402

PENDING_PATH = HERE / "pending_call.json"
CALL_LOG_DIR = ROOT / "call_log"
PORT = 8080

_lock = threading.Lock()

# Telegram rate-limits edits to a message, and a voice call produces a turn
# every couple of seconds. Throttling here rather than dropping turns: each
# edit re-renders the WHOLE transcript, so a skipped edit loses nothing except
# a moment of latency.
_LIVE_EDIT_MIN_INTERVAL = 1.3
_last_live_edit = 0.0


# --------------------------------------------------------------------------
# pending_call.json — the one piece of shared state, deliberately on disk
# --------------------------------------------------------------------------
# A file rather than a socket or a queue because it is inspectable. When
# something is wrong at 15:50 you can `cat` it, and you can hand-edit it to
# re-run the call leg without touching Telegram at all.

def read_pending() -> dict:
    with _lock:
        if not PENDING_PATH.exists():
            return {}
        try:
            return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}


def write_pending(data: dict) -> None:
    with _lock:
        PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PENDING_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(PENDING_PATH)  # atomic: the page never reads a half-written file


def patch_pending(**fields) -> dict:
    current = read_pending()
    current.update(fields)
    write_pending(current)
    return current


# --------------------------------------------------------------------------
# Telegram — one function, so the call result lands back where the argument was
# --------------------------------------------------------------------------

def send_telegram(chat_id, text: str) -> bool:
    token = env("TELEGRAM_TOKEN")
    if not token or not chat_id:
        print(f"[bridge] no token/chat_id, would have sent:\n{text}")
        return False
    payload = urllib.parse.urlencode(
        {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=payload), timeout=15
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"[bridge] telegram send failed: {exc}")
        return False


# --------------------------------------------------------------------------
# Turning a call into a state transition
# --------------------------------------------------------------------------
# The ElevenLabs data-collection schema is evaluated server-side AFTER the call
# finishes, and is NOT delivered to the browser session. So the page can report
# what was said but not what it meant, and a `collected` field populated from
# the browser would be permanently empty.
#
# Two sources, best first:
#   1. the agent's own schema result, over the API (saw the audio)
#   2. Gemini reading the transcript (sees only text — labelled as such)

def _fetch_agent_analysis(conversation_id: str, api_key: str) -> dict:
    """Ask ElevenLabs for the agent's own data-collection result.

    The analysis is computed asynchronously once the call ends, so a request
    made the instant the page hangs up usually arrives before the result does.
    Three tries, four seconds apart, then give up quietly and let the
    transcript path handle it.
    """
    url = f"https://api.elevenlabs.io/v1/convai/conversations/{conversation_id}"
    for attempt in range(3):
        if attempt:
            time.sleep(4)
        try:
            req = urllib.request.Request(url, headers={"xi-api-key": api_key})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                TimeoutError, json.JSONDecodeError) as exc:
            print(f"[bridge] elevenlabs analysis attempt {attempt + 1}: {type(exc).__name__}")
            continue

        results = ((data.get("analysis") or {}).get("data_collection_results") or {})
        if not results:
            print(f"[bridge] analysis not ready yet (attempt {attempt + 1})")
            continue

        # Each entry is {"value": ..., "rationale": ...}; flatten to values.
        flat = {}
        for key, item in results.items():
            flat[key] = item.get("value") if isinstance(item, dict) else item
        if any(v not in (None, "") for v in flat.values()):
            return flat
    return {}


def collect_outcome(body: dict) -> tuple[dict, str]:
    """Return (structured_fields, source_label)."""
    conversation_id = body.get("conversation_id")
    api_key = env("ELEVENLABS_API_KEY")

    if conversation_id and api_key:
        agent_result = _fetch_agent_analysis(conversation_id, api_key)
        if agent_result:
            print("[bridge] outcome from the agent's own data-collection schema")
            return agent_result, "agent schema"

    derived = pipeline.extract_call_outcome(body.get("transcript") or [])
    if derived:
        print("[bridge] outcome derived from the transcript")
        return derived, "transcript (derived)"

    return {}, "none"


def edit_telegram(chat_id, message_id: int, text: str) -> bool:
    token = env("TELEGRAM_TOKEN")
    if not token or not chat_id or not message_id:
        return False
    payload = urllib.parse.urlencode({
        "chat_id": str(chat_id), "message_id": str(message_id),
        "text": text[:4000], "parse_mode": "HTML",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=payload), timeout=10
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        # "message is not modified" and rate limits both land here and are both
        # harmless: the next turn re-sends the full transcript anyway.
        print(f"[bridge] live edit skipped: {type(exc).__name__}")
        return False


def send_telegram_returning_id(chat_id, text: str):
    token = env("TELEGRAM_TOKEN")
    if not token or not chat_id:
        return None
    payload = urllib.parse.urlencode(
        {"chat_id": str(chat_id), "text": text[:4000], "parse_mode": "HTML"}
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=payload), timeout=15
        ) as resp:
            return (json.loads(resp.read().decode("utf-8")).get("result") or {}).get("message_id")
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[bridge] could not open the live transcript message: {exc}")
        return None


def render_live(pending: dict, turns: list[dict], finished: bool = False) -> str:
    """The message that updates in the chat while the call is happening.

    Showing the conversation as it happens is not decoration. The group
    delegated a phone call to an AI; watching it unfold is what makes that
    delegation something they can supervise rather than just trust, and it is
    the moment a human can say "no, that's wrong" while it still matters.
    """
    name = pending.get("restaurant_display") or pending.get("restaurant_name") or "the restaurant"
    head = (f"\u2705 <b>Call finished \u2014 {esc(name)}</b>" if finished
            else f"\U0001f4de <b>On the phone with {esc(name)}\u2026</b>")

    # Built by concatenation rather than one big f-string: an escape inside an
    # f-string expression is a syntax error before Python 3.12, and this has to
    # run on whatever python3 the laptop happens to have.
    dot = " \u00b7 "
    subtitle = str(pending.get("dial_number") or "")
    if pending.get("party_size"):
        subtitle += dot + "party of " + str(pending["party_size"])
    if pending.get("when_text"):
        subtitle += dot + str(pending["when_text"])

    lines = [head, "<i>" + esc(subtitle) + "</i>", ""]

    if not turns:
        lines.append("<i>connecting\u2026</i>")
    for turn in turns[-24:]:
        said = str(turn.get("message", "")).strip()
        if not said:
            continue
        if turn.get("source") == "user":
            lines.append(f"\U0001f3ea <b>{esc(said)}</b>")      # the restaurant
        else:
            lines.append(f"\U0001f916 {esc(said)}")             # our agent

    if not finished:
        lines.append("\n<i>live \u2014 this message updates as they talk</i>")
    return "\n".join(lines)


def format_outcome(pending: dict, body: dict) -> str:
    """Turn the call into a chat message a human can act on.

    The structured fields come from the agent's data-collection schema. Without
    that schema you have a phone call; with it you have a state transition, and
    the group chat can close its own loop.
    """
    collected = body.get("collected") or {}
    status = str(collected.get("status") or body.get("status") or "unknown").lower()
    name = pending.get("restaurant_display") or pending.get("restaurant_name") or "the restaurant"

    head = {
        "confirmed": f"✅ <b>Booked — {esc(name)}</b>",
        "booked": f"✅ <b>Booked — {esc(name)}</b>",
        "waitlist": f"⏳ <b>Waitlist — {esc(name)}</b>",
        "declined": f"❌ <b>No table — {esc(name)}</b>",
        "full": f"❌ <b>No table — {esc(name)}</b>",
        "no_answer": f"☎️ <b>No answer — {esc(name)}</b>",
    }.get(status, f"ℹ️ <b>Call finished — {esc(name)}</b>")

    lines = [head]
    if collected.get("confirmed_time"):
        lines.append(f"Time: {esc(collected['confirmed_time'])}")
    if collected.get("confirmed_party_size"):
        lines.append(f"Party: {esc(collected['confirmed_party_size'])}")
    if collected.get("wait_estimate_minutes"):
        lines.append(f"Wait: ~{esc(collected['wait_estimate_minutes'])} min")
    if collected.get("booking_name"):
        lines.append(f"Under: {esc(collected['booking_name'])}")
    if collected.get("staff_notes"):
        lines.append(f"Note: {esc(collected['staff_notes'])}")

    turns = body.get("transcript") or []
    if turns:
        lines.append("\n<b>Transcript</b>")
        for turn in turns[-14:]:
            who = "\U0001f916" if turn.get("source") in ("ai", "agent") else "\U0001f3ea"
            said = str(turn.get("message", "")).strip()
            if said:
                lines.append(f"{who} {esc(said)}")

    source = body.get("outcome_source")
    if source == "transcript (derived)":
        # Say where the structured fields came from. The agent's own schema saw
        # the audio; this read only the words. That difference matters if
        # somebody is about to turn up at a restaurant on the strength of it.
        lines.append("\n<i>Fields above were read back off the transcript, not confirmed "
                     "by the agent's own call analysis \u2014 worth a glance before you rely on them.</i>")

    if pending.get("demo_override"):
        lines.append(
            "\n<i>Safety rail: DEMO_PHONE was set, so this call went to a number "
            "we control, not to the restaurant.</i>"
        )
    return "\n".join(lines)


def archive_call(pending: dict, body: dict) -> Path:
    """Keep every call on disk. This is the backup-footage insurance policy."""
    CALL_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = CALL_LOG_DIR / f"call-{stamp}.json"
    path.write_text(
        json.dumps({"pending": pending, "outcome": body}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ShumAIBridge/1.0"

    # Only needed by the optional CopilotKit console on :3000. Missing CORS
    # headers surface as a bare "Failed to fetch" with no explanation at all
    # (gotcha 23), so they go in now rather than being debugged later.
    # Access-Control-Allow-Origin: * was applied to /dial, /outcome, /cancel and
    # /amend. Those endpoints take no credentials, so a wildcard meant ANY page
    # the operator happened to have open could arm a queued call or post text
    # into the group chat under the bot's name. The wildcard was not even
    # needed: the console proxies same-origin through app/api/bridge, and the
    # call page is served by this process. Scoped to the console's origin.
    CONSOLE_ORIGIN = env("CONSOLE_ORIGIN") or "http://localhost:3000"

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.CONSOLE_ORIGIN)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, payload, code: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self._json({"error": f"{path.name} missing"}, 404)
            return
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8")) or {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):  # noqa: N802
        route = urllib.parse.urlparse(self.path).path

        if route in ("/", "/index.html", "/call_page.html"):
            self._file(HERE / "call_page.html", "text/html; charset=utf-8")
        elif route == "/health":
            self._json({"ok": True, "service": "shum-ai-bridge", "port": PORT})
        elif route == "/live":
            pending = read_pending()
            self._json({"turns": pending.get("live_turns") or [],
                        "status": pending.get("status")})
        elif route == "/config":
            agent_id = env("ELEVENLABS_AGENT_ID")
            self._json(
                {
                    "agent_id": agent_id,
                    "agent_id_present": bool(agent_id),
                    "booker_name": env("BOOKER_NAME", "a guest"),
                    "callback_number": env("CALLBACK_NUMBER"),
                    "demo_phone_active": bool(env("DEMO_PHONE")),
                }
            )
        elif route == "/pending":
            self._json(read_pending())
        else:
            self._json({"error": "not found", "path": route}, 404)

    def do_POST(self):  # noqa: N802
        route = urllib.parse.urlparse(self.path).path
        body = self._body()

        if route == "/dial":
            pending = read_pending()
            if not pending:
                self._json({"error": "nothing pending"}, 409)
                return
            if not pending.get("dial_number"):
                self._json({"error": "pending has no dial_number"}, 409)
                return
            # Open the live transcript message now, so the group sees the call
            # start rather than only its result.
            live_id = send_telegram_returning_id(
                pending.get("chat_id"), render_live(pending, [])
            )

            # Auto-dial is opt-in, and the reason is a real trade-off rather
            # than caution for its own sake. Brief section 9.2 asks for TWO
            # humans: one pressing approve, one dialling. With AUTO_DIAL=1
            # there is one -- the person who approved in the chat. That is
            # still a human authorising this specific call to a number already
            # on the consent allowlist, which is why it is offered at all; but
            # it is one gate, not two, so it is never on unless asked for.
            dialled, how = (False, "AUTO_DIAL not set - dial it yourself")
            if env_flag("AUTO_DIAL"):
                dialled, how = dial_phone(pending.get("dial_number") or "")
            print(f"[bridge] {how}")

            self._json(patch_pending(
                dial=True, status="dialing", live_message_id=live_id,
                live_turns=[], auto_dialled=dialled,
            ))

        elif route == "/turn":
            # One conversational turn, pushed from the call page as it happens.
            global _last_live_edit
            pending = read_pending()
            said = str(body.get("message") or "").strip()
            if not said:
                self._json({"ok": True, "ignored": "empty turn"})
                return

            turns = list(pending.get("live_turns") or [])
            turns.append({"source": body.get("source") or "ai", "message": said})
            patch_pending(live_turns=turns)

            edited = False
            now = time.time()
            if now - _last_live_edit >= _LIVE_EDIT_MIN_INTERVAL:
                _last_live_edit = now
                edited = edit_telegram(
                    pending.get("chat_id"), pending.get("live_message_id"),
                    render_live(pending, turns),
                )
            self._json({"ok": True, "turns": len(turns), "edited": edited})

        elif route == "/outcome":
            pending = read_pending()
            collected, source = collect_outcome(body)
            body["collected"] = collected
            body["outcome_source"] = source

            # Close the live message off so it does not sit there saying
            # "live" forever, then post the structured result separately.
            turns = body.get("transcript") or pending.get("live_turns") or []
            if pending.get("live_message_id"):
                edit_telegram(pending.get("chat_id"), pending["live_message_id"],
                              render_live(pending, turns, finished=True))

            archived = archive_call(pending, body)
            text = format_outcome(pending, body)
            sent = send_telegram(pending.get("chat_id"), text)
            patch_pending(
                dial=False,
                status="done",
                outcome=collected,
                outcome_source=source,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"[bridge] call archived -> {archived}")
            self._json({"ok": True, "telegram_sent": sent,
                        "archived": archived.name, "outcome_source": source})

        elif route == "/amend":
            # Amend the queued booking before it is dialled.
            #
            # DELIBERATELY NARROW. dial_number, real_number, restaurant_name and
            # demo_override are NOT amendable through this endpoint, by anything,
            # ever. The number that gets dialled is settled by the poll result,
            # OpenStreetMap and the consent allowlist in bot.py -- three places
            # with real checks -- and an HTTP endpoint that could overwrite it
            # would route around every one of them. The operator console exposes
            # this to an LLM, which makes the restriction load-bearing rather
            # than tidy.
            pending = read_pending()
            if not pending or not pending.get("restaurant_name"):
                self._json({"error": "nothing pending to amend"}, 409)
                return

            patch: dict = {}
            errors: list[str] = []

            if "party_size" in body:
                try:
                    size = int(body["party_size"])
                except (TypeError, ValueError):
                    errors.append("party_size must be a whole number")
                else:
                    if 1 <= size <= 40:
                        patch["party_size"] = size
                    else:
                        errors.append("party_size must be between 1 and 40")

            for field, cap in (("when_text", 120), ("booking_name", 60), ("notes", 300)):
                if field in body:
                    value = str(body[field] or "").strip()
                    if not value:
                        errors.append(f"{field} cannot be empty")
                    else:
                        patch[field] = value[:cap]

            rejected = [k for k in body if k not in
                        ("party_size", "when_text", "booking_name", "notes")]
            if rejected:
                errors.append(
                    "not amendable here: " + ", ".join(sorted(rejected))
                    + " (the number to dial and the restaurant are settled by the poll, "
                      "OpenStreetMap and the consent allowlist)"
                )

            if errors:
                self._json({"error": "; ".join(errors)}, 400)
                return
            if not patch:
                self._json({"error": "no amendable fields supplied"}, 400)
                return

            patch["amended_at"] = datetime.now(timezone.utc).isoformat()
            updated = patch_pending(**patch)
            print(f"[bridge] amended: {', '.join(k for k in patch if k != 'amended_at')}")
            self._json(updated)

        elif route == "/cancel":
            self._json(patch_pending(dial=False, status="cancelled"))

        else:
            self._json({"error": "not found", "path": route}, 404)

    def log_message(self, fmt, *args):
        # Default logging writes a line per poll; the page polls twice a second.
        if "/pending" not in self.path:
            sys.stderr.write("[bridge] %s\n" % (fmt % args))


def dial_phone(number: str) -> tuple[bool, str]:
    """Ring the operator's own phone, automatically, for free.

    macOS hands a `tel:` URL to whichever app owns telephony -- on current
    macOS that is Phone.app, which places the call through a paired iPhone over
    Continuity. So the call goes out on the operator's real +852 cellular line:
    no telephony provider, no purchased number, no regulatory bundle, no cost,
    and the local caller ID that is the entire reason a Hong Kong restaurant
    picks up.

    This changes nothing about the transcript. The agent still runs in the call
    page over WebRTC and still hears through the laptop microphone. All it
    removes is a human tapping digits.

    TWO BUGS LIVED HERE, both of which reported success:

    1. The URL was "tel://<number>". That is not the tel: scheme -- tel takes no
       authority component -- and nothing on the system claims it. Measured:
       "tel://" launched no handler at all, while "tel:" launched Phone.app in
       1.2 seconds.
    2. Success was the return value of subprocess.Popen, which only says a
       process was spawned. `open` exits 0 whether or not any application
       handles the URL, so a dial that went nowhere reported "dialled: True".

    So it now uses tel: and CONFIRMS a telephony app actually came up before
    claiming anything. Whether that app then connects the call depends on
    "Calls from iPhone" being enabled and the iPhone being nearby -- which this
    process cannot see, and does not pretend to.
    """
    if sys.platform != "darwin":
        return False, f"auto-dial is macOS-only (this is {sys.platform})"

    digits = "".join(ch for ch in (number or "") if ch.isdigit() or ch == "+")
    if not digits:
        return False, "no number to dial"
    masked = digits[:-4] + "****" if len(digits) > 4 else "****"

    try:
        # tel:, not tel:// -- see above.
        subprocess.Popen(["open", f"tel:{digits}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, FileNotFoundError) as exc:
        return False, f"could not hand off the call: {exc}"

    # Confirm a telephony app actually took it. Without this the caller cannot
    # tell "the phone is ringing" from "nothing happened".
    for _ in range(8):
        time.sleep(0.4)
        try:
            running = subprocess.run(["pgrep", "-x", "Phone"], capture_output=True).returncode == 0
            if not running:
                running = subprocess.run(["pgrep", "-x", "FaceTime"],
                                         capture_output=True).returncode == 0
        except (OSError, FileNotFoundError):
            return True, f"handed tel:{masked} to the system (could not verify)"
        if running:
            # Deliberately NOT "dialling". Phone.app coming up proves the
            # handoff landed, not that a call was placed: on a Mac whose iPhone
            # has "Calls on Other Devices" switched off, Phone.app opens and
            # then says "iPhone Calls Not Available - your iPhone is not
            # configured". Observed on the build machine. This process cannot
            # see that banner, so it does not claim more than it knows.
            return True, (f"handed {masked} to the Phone app - watch the phone. "
                          "If nothing rings, iPhone > Settings > Cellular > "
                          "Calls on Other Devices needs to be on for this Mac.")

    return False, (f"handed tel:{masked} to the system but no telephony app came up. "
                   "Dial by hand; the number is on the approval card.")


def open_call_page() -> None:
    """Open the call desk in the default browser when the bridge starts.

    The tab has to exist for the microphone to exist -- WebRTC lives in a page,
    not in this process -- but nobody should have to remember to go and open
    it. Combined with the page arming itself on a remembered permission, the
    operator's only remaining job is the one that has to stay manual: dialling
    the phone and holding it to the laptop.

    Best effort and silent on failure. A headless run, a locked-down laptop or
    a machine with no browser are all fine; the page is still reachable.
    """
    if env_flag("NO_AUTO_OPEN"):
        print("[bridge] NO_AUTO_OPEN set \u2014 open the call page yourself")
        return

    url = f"http://localhost:{PORT}/"
    opener = {"darwin": ["open", url], "win32": ["cmd", "/c", "start", "", url]}.get(
        sys.platform, ["xdg-open", url]
    )
    try:
        subprocess.Popen(opener, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[bridge] opened the call desk at {url}")
    except (OSError, FileNotFoundError):
        print(f"[bridge] could not open a browser \u2014 go to {url} yourself")


def main() -> None:
    load_env(ROOT / ".env")
    warn_if_tls_broken("bridge")
    if not env("ELEVENLABS_AGENT_ID"):
        print("[bridge] WARNING: ELEVENLABS_AGENT_ID is empty — the call page will refuse to start.")
    print(f"[bridge] listening on http://localhost:{PORT}")
    threading.Timer(1.0, open_call_page).start()   # after the socket is up
    if env_flag("AUTO_DIAL"):
        print("[bridge] AUTO_DIAL on - approving in the chat will ring the phone itself")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
