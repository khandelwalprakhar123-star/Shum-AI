"""tests/harness.py — a test framework small enough to trust.

Not unittest and not pytest, for one reason: this suite's central guarantee is
that NO TEST TOUCHES THE NETWORK. Enforcing that needs a hard kill switch over
urllib rather than a convention, and it needs "a call I didn't expect" to be a
failure rather than a silent pass on conference wifi that happens to work.

So FakeNet replaces urllib.request.urlopen with a queue of canned responses and
raises on anything unqueued. A test that accidentally reaches Gemini fails
loudly instead of passing slowly.

The other thing this gives us is an honest tally. A skipped test is not a
passing test: the four outcomes are counted separately and
the runner treats fail, crash AND skip as reasons to exit non-zero.
"""

from __future__ import annotations

import io
import json
import re
import traceback
import urllib.error
import urllib.request


# ---------------------------------------------------------------------------
# Canned responses
# ---------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __init__(self, payload, status: int = 200):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        super().__init__(raw)
        self.status = status
        self.code = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def getcode(self):
        return self.status


def json_response(payload, status: int = 200):
    return lambda request: FakeResponse(payload, status)


def http_error(code: int, body: str = "{}"):
    def raise_it(request):
        raise urllib.error.HTTPError(
            getattr(request, "full_url", "http://test"), code, f"HTTP {code}",
            {}, io.BytesIO(body.encode()),
        )
    return raise_it


def url_error(reason: str = "connection refused"):
    def raise_it(request):
        raise urllib.error.URLError(reason)
    return raise_it


def timeout_error():
    def raise_it(request):
        raise TimeoutError("timed out")
    return raise_it


def gemini_ok(obj):
    """A well-formed Gemini generateContent response wrapping `obj` as JSON."""
    return json_response(
        {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]}
    )


def gemini_truncated(partial: str = '{"party_size": 6, "hard": [{"constra',
                     thoughts: int = 1962):
    """A response that hit MAX_TOKENS because thinking ate the budget.

    This is what the real failure looked like: finishReason MAX_TOKENS, ~1962
    tokens spent thinking, and a fragment of JSON that no parser can salvage.
    """
    return json_response({
        "candidates": [{"content": {"parts": [{"text": partial}]},
                        "finishReason": "MAX_TOKENS"}],
        "usageMetadata": {"thoughtsTokenCount": thoughts, "candidatesTokenCount": 71},
    })


def openrouter_ok(obj):
    return json_response({"choices": [{"message": {"content": json.dumps(obj)}}]})


def overpass_ok(elements):
    return json_response({"elements": elements})


# ---------------------------------------------------------------------------
# The network kill switch
# ---------------------------------------------------------------------------
# FakeNet only guards code that runs INSIDE its context. That was not enough.
#
# bot.notify_bridge_dial() builds its own urllib request, and its caller
# swallows connection errors -- so with bridge/server.py running and a booking
# queued, `python3 tests/run.py` POSTed to the real http://127.0.0.1:8080/dial
# and flipped the live pending_call.json to dial:true. The call page
# auto-starts the agent on exactly that transition.
#
# A test run placing a phone call with nobody pressing anything violates brief
# the two-human rule, and it happened while the runner printed "network fully mocked".
# Proven from this machine's bridge log: two POST /dial 409 entries with no
# human involved; they were 409 only because nothing was queued at the time.
#
# So the default is now a hard failure. Anything reaching the network outside a
# FakeNet raises instead of quietly succeeding.

_REAL_URLOPEN = urllib.request.urlopen


def _forbidden_urlopen(request, *args, **kwargs):
    url = getattr(request, "full_url", str(request))
    raise AssertionError(
        f"NETWORK CALL OUTSIDE FakeNet: {url}\n"
        "This suite's central claim is that the network is fully mocked. A call "
        "that escapes FakeNet can reach the live bridge and arm a real phone "
        "call. Wrap it in FakeNet, or stub the function that makes it."
    )


urllib.request.urlopen = _forbidden_urlopen

class FakeNet:
    """Install a queue of responses. Anything beyond the queue is a failure."""

    def __init__(self, responses=None, strict: bool = True):
        self.responses = list(responses or [])
        self.strict = strict
        self.requests: list[dict] = []
        self._real = None

    def __enter__(self):
        self._real = urllib.request.urlopen
        urllib.request.urlopen = self._handle
        return self

    def __exit__(self, *exc):
        # Restores the guard, not the real urlopen: outside a FakeNet the
        # network must stay unreachable for the rest of the run.
        urllib.request.urlopen = self._real
        return False

    def _handle(self, request, *args, **kwargs):
        url = getattr(request, "full_url", str(request))
        body = getattr(request, "data", None)
        self.requests.append({
            "url": url,
            "headers": {k.lower(): v for k, v in (getattr(request, "header_items", lambda: [])())},
            "body": body.decode("utf-8", "replace") if isinstance(body, bytes) else body,
            "method": getattr(request, "get_method", lambda: "GET")(),
        })
        if not self.responses:
            if self.strict:
                raise AssertionError(f"unmocked network call to {url}")
            raise urllib.error.URLError("no response queued")
        return self.responses.pop(0)(request)

    # convenience accessors used by the suites
    def urls(self) -> list[str]:
        return [r["url"] for r in self.requests]

    def last(self) -> dict:
        return self.requests[-1] if self.requests else {}

    def header(self, name: str, index: int = 0) -> str | None:
        try:
            return self.requests[index]["headers"].get(name.lower())
        except IndexError:
            return None


# ---------------------------------------------------------------------------
# Tally
# ---------------------------------------------------------------------------

class Suite:
    def __init__(self, name: str, expect_at_least: int = 1):
        self.name = name
        self.expect_at_least = expect_at_least
        self.passed = 0
        self.failed: list[str] = []
        self.crashed: list[str] = []
        self.skipped: list[str] = []

    @property
    def total(self) -> int:
        return self.passed + len(self.failed) + len(self.crashed) + len(self.skipped)

    def check(self, label: str, condition, detail: str = "") -> bool:
        try:
            if condition:
                self.passed += 1
                return True
            self.failed.append(f"{label}{(' — ' + detail) if detail else ''}")
            return False
        except Exception:
            self.crashed.append(f"{label}\n{traceback.format_exc(limit=3)}")
            return False

    def eq(self, label: str, got, want) -> bool:
        return self.check(label, got == want, f"got {got!r}, want {want!r}")

    def ne(self, label: str, got, unwanted) -> bool:
        return self.check(label, got != unwanted, f"got {got!r}, should not equal {unwanted!r}")

    def contains(self, label: str, haystack, needle) -> bool:
        try:
            ok = needle in haystack
        except TypeError:
            ok = False
        return self.check(label, ok, f"{needle!r} not found in {str(haystack)[:120]!r}")

    def raises(self, label: str, exc_type, fn, *args, **kwargs) -> bool:
        try:
            fn(*args, **kwargs)
        except exc_type:
            self.passed += 1
            return True
        except Exception as exc:
            self.failed.append(f"{label} — raised {type(exc).__name__}, wanted {exc_type.__name__}")
            return False
        self.failed.append(f"{label} — raised nothing, wanted {exc_type.__name__}")
        return False

    def skip(self, label: str, why: str) -> None:
        # A skip is NOT a pass. It counts against the run, because a suite
        # silently not running while the report says "0 failed" is worse than
        # a red build.
        self.skipped.append(f"{label} — {why}")

    def crash(self, label: str, exc: BaseException) -> None:
        self.crashed.append(f"{label}\n{''.join(traceback.format_exception_only(type(exc), exc))}")

    def summary(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "failed": self.failed,
            "crashed": self.crashed,
            "skipped": self.skipped,
            "total": self.total,
            "expect_at_least": self.expect_at_least,
            "under_expected": self.total < self.expect_at_least,
            "ok": not self.failed and not self.crashed and not self.skipped
                  and self.total >= self.expect_at_least,
        }


# Telegram rejects a bare "&" or "<" that is not a valid entity or one of the
# tags it supports, and Telegram.call swallows the 400 -- so the message simply
# never arrives. This is the check that would have caught it.
_TG_BAD = re.compile(
    r"&(?!(?:amp|lt|gt|quot|#\d+);)"
    r"|<(?!/?(?:b|strong|i|em|u|ins|s|strike|del|a|code|pre|tg-spoiler|blockquote)[\s>/])"
)


def unescaped(text: str) -> list[str]:
    return [m.group(0) for m in _TG_BAD.finditer(text or "")]
