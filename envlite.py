"""envlite — a .env reader in 30 lines of stdlib.

Deliberately not python-dotenv. This project installs nothing: on a hackathon
laptop, `pip install` is a coin flip against a conference wifi captive portal,
and every dependency is one more thing that can be the reason the demo doesn't
run. Everything here is importable from a stock python3.
"""

from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env(path: str | Path | None = None) -> dict[str, str]:
    """Read KEY=VALUE lines into os.environ without clobbering real env vars.

    Real environment always wins, so `DEMO_PHONE=+85298765432 python3 bot.py`
    overrides the file — which is how you flip the safety rail on stage without
    editing anything.
    """
    target = Path(path) if path else ROOT / ".env"
    found: dict[str, str] = {}
    if not target.exists():
        return found

    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split(" #")[0].strip().strip("'\"")
        if not key:
            continue
        found[key] = value
        os.environ.setdefault(key, value)
    return found


def env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def env_flag(key: str) -> bool:
    return env(key).lower() in {"1", "true", "yes", "on"}


def env_list(key: str) -> list[str]:
    return [part.strip() for part in env(key).split(",") if part.strip()]


def check_tls() -> str | None:
    """Detect an empty CA store before it becomes a mystery at 15:55.

    The python.org macOS installer does NOT wire Python into the system
    keychain. It ships its own OpenSSL expecting a cert.pem that the installer
    never creates, so `curl https://...` works perfectly while every
    urllib.request call in the same shell dies with

        SSL: CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate

    Every network call this project makes — Telegram, Gemini, Overpass, Exa —
    goes through urllib. So on an affected machine NOTHING works, and the error
    points at certificates rather than at the one-line fix.

    Found the hard way on a fresh Python 3.14.4 install, an hour before demos.
    Checked at startup because a clear message now is worth more than a correct
    traceback later. No network needed: an empty trust store is detectable
    directly.
    """
    try:
        stats = ssl.create_default_context().cert_store_stats()
    except Exception:  # pragma: no cover - ssl is always importable in practice
        return None
    if stats.get("x509_ca", 0) > 0:
        return None

    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    return (
        "TLS trust store is EMPTY — every HTTPS call will fail with\n"
        "  SSL: CERTIFICATE_VERIFY_FAILED (unable to get local issuer certificate)\n"
        "even though curl works fine in the same shell.\n\n"
        "If you installed Python from python.org on macOS, run this once:\n"
        f"  open '/Applications/Python {version}/Install Certificates.command'\n\n"
        "Or point Python at an existing bundle:\n"
        "  export SSL_CERT_FILE=$(python3 -m certifi)"
    )


def warn_if_tls_broken(label: str = "") -> bool:
    """Print the TLS diagnosis if there is one. Returns True when healthy."""
    problem = check_tls()
    if problem is None:
        return True
    prefix = f"[{label}] " if label else ""
    print(f"\n{prefix}{'=' * 68}")
    for line in problem.splitlines():
        print(f"{prefix}{line}")
    print(f"{prefix}{'=' * 68}\n")
    return False
