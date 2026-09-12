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
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from envlite import env, load_env, warn_if_tls_broken  # noqa: E402

PENDING_PATH = HERE / "pending_call.json"
CALL_LOG_DIR = ROOT / "call_log"
PORT = 8080

_lock = threading.Lock()


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


def format_outcome(pending: dict, body: dict) -> str:
    """Turn the call into a chat message a human can act on.

    The structured fields come from the agent's data-collection schema. Without
    that schema you have a phone call; with it you have a state transition, and
    the group chat can close its own loop.
    """
    collected = body.get("collected") or {}
    status = (collected.get("status") or body.get("status") or "unknown").lower()
    name = pending.get("restaurant_display") or pending.get("restaurant_name") or "the restaurant"

    head = {
        "confirmed": f"✅ <b>Booked — {name}</b>",
        "booked": f"✅ <b>Booked — {name}</b>",
        "waitlist": f"⏳ <b>Waitlist — {name}</b>",
        "declined": f"❌ <b>No table — {name}</b>",
        "full": f"❌ <b>No table — {name}</b>",
        "no_answer": f"☎️ <b>No answer — {name}</b>",
    }.get(status, f"ℹ️ <b>Call finished — {name}</b>")

    lines = [head]
    if collected.get("confirmed_time"):
        lines.append(f"Time: {collected['confirmed_time']}")
    if collected.get("confirmed_party_size"):
        lines.append(f"Party: {collected['confirmed_party_size']}")
    if collected.get("wait_estimate_minutes"):
        lines.append(f"Wait: ~{collected['wait_estimate_minutes']} min")
    if collected.get("booking_name"):
        lines.append(f"Under: {collected['booking_name']}")
    if collected.get("staff_notes"):
        lines.append(f"Note: {collected['staff_notes']}")

    turns = body.get("transcript") or []
    if turns:
        lines.append("\n<b>Transcript</b>")
        for turn in turns[-14:]:
            who = "\U0001f916" if turn.get("source") in ("ai", "agent") else "\U0001f3ea"
            said = str(turn.get("message", "")).strip()
            if said:
                lines.append(f"{who} {said}")

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
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
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
            self._json(patch_pending(dial=True, status="dialing"))

        elif route == "/outcome":
            pending = read_pending()
            archived = archive_call(pending, body)
            text = format_outcome(pending, body)
            sent = send_telegram(pending.get("chat_id"), text)
            patch_pending(
                dial=False,
                status="done",
                outcome=body.get("collected") or {},
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            print(f"[bridge] call archived -> {archived}")
            self._json({"ok": True, "telegram_sent": sent, "archived": archived.name})

        elif route == "/cancel":
            self._json(patch_pending(dial=False, status="cancelled"))

        else:
            self._json({"error": "not found", "path": route}, 404)

    def log_message(self, fmt, *args):
        # Default logging writes a line per poll; the page polls twice a second.
        if "/pending" not in self.path:
            sys.stderr.write("[bridge] %s\n" % (fmt % args))


def main() -> None:
    load_env(ROOT / ".env")
    warn_if_tls_broken("bridge")
    if not env("ELEVENLABS_AGENT_ID"):
        print("[bridge] WARNING: ELEVENLABS_AGENT_ID is empty — the call page will refuse to start.")
    print(f"[bridge] listening on http://localhost:{PORT}")
    print(f"[bridge] open the call page at  http://localhost:{PORT}/")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
