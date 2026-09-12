"""tgtext.py — make text safe for Telegram's HTML parse mode.

Every message this project sends uses parse_mode=HTML, and almost every message
interpolates text nobody on this team wrote: OpenStreetMap venue names, quotes
lifted verbatim out of the group chat, and transcribed speech from whoever
answered the phone.

"Fish & Chips". "R&B Tea". Someone typing "2 < 3". Telegram answers
400 can't parse entities, Telegram.call catches it and returns None, and the
message simply never arrives. Nothing raises, nothing is logged above debug,
and the most exposed messages are the two that carry the whole demo: the
constraint list (which quotes the chat on purpose) and the call outcome (which
quotes a stranger's speech).

So: escape at every interpolation point, and keep the tags we author ourselves
by building them around already-escaped values rather than escaping afterwards.

NOT for sendPoll options. Those are plain text, not HTML, so escaping there
would show the group "Fish &amp; Chips".
"""

from __future__ import annotations

import html


def esc(value) -> str:
    """Escape one interpolated value for parse_mode=HTML.

    quote=False on purpose: Telegram does not need &quot; and escaping it makes
    ordinary apostrophes and quotation marks in a restaurant name unreadable.
    """
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def esc_all(values) -> list[str]:
    return [esc(v) for v in (values or [])]


def esc_join(values, separator: str = ", ") -> str:
    return separator.join(esc_all(values))
