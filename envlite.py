"""envlite — a .env reader in 30 lines of stdlib.

Deliberately not python-dotenv. This project installs nothing: on a hackathon
laptop, `pip install` is a coin flip against a conference wifi captive portal,
and every dependency is one more thing that can be the reason the demo doesn't
run. Everything here is importable from a stock python3.
"""

from __future__ import annotations

import os
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
