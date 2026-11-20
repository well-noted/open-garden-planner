"""Request limits for the Field Remote listener. Stdlib only, no Qt.

Each class answers one row of the threat model (design 1.5): flooding, token guessing,
oversized bodies.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

Clock = Callable[[], float]

#: Body cap for every route except a photo upload (design 1.5, hostile uploads).
MAX_BODY_BYTES = 256 * 1024
#: Photo upload cap; the route that needs it opts in (WP-D).
MAX_PHOTO_BYTES = 12 * 1024 * 1024
#: Body cap for the one route that carries a photo (multipart: the photo plus a small JSON part).
MAX_UPLOAD_BODY_BYTES = MAX_PHOTO_BYTES + 256 * 1024
UPLOAD_ROUTES = frozenset({("POST", "/api/records")})
#: Reads per second and writes per second, per token (design 1.5, flooding).
READ_RATE = 20.0
WRITE_RATE = 5.0
#: Token guessing: this many failures from one address locks it out for the window.
MAX_FAILURES = 10
LOCKOUT_SECONDS = 300.0
#: Bound on tracked addresses, so a scan from many sources cannot grow memory.
_MAX_TRACKED = 1024


class TokenBucket:
    """Classic token bucket. Thread-safe; the server loop and tests share one clock seam."""

    def __init__(self, rate: float, burst: float | None = None, clock: Clock = time.monotonic) -> None:
        self._rate = rate
        self._burst = burst if burst is not None else max(1.0, rate)
        self._clock = clock
        self._tokens = self._burst
        self._last = clock()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            now = self._clock()
            self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


class RateLimiter:
    """One read bucket and one write bucket. There is one shared token, so this is per token."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._reads = TokenBucket(READ_RATE, clock=clock)
        self._writes = TokenBucket(WRITE_RATE, clock=clock)

    def allow(self, method: str) -> bool:
        safe = method.upper() in ("GET", "HEAD")
        return (self._reads if safe else self._writes).allow()


#: Pre-auth traffic (the public page) per address: no token yet, so no per-token bucket applies.
PUBLIC_RATE = 5.0
#: In-flight requests one address may hold open at once (slow-request and flood cap).
MAX_INFLIGHT_PER_IP = 8


class KeyedRateLimiter:
    """A token bucket per key (an address), with a bound on how many keys are remembered."""

    def __init__(self, rate: float, burst: float | None = None, clock: Clock = time.monotonic) -> None:
        self._rate, self._burst, self._clock = rate, burst, clock
        self._buckets: OrderedDict[str, TokenBucket] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            bucket = self._buckets.pop(key, None) or TokenBucket(self._rate, self._burst, self._clock)
            self._buckets[key] = bucket
            while len(self._buckets) > _MAX_TRACKED:
                self._buckets.popitem(last=False)
        return bucket.allow()


class FailureLockout:
    """Per-address lockout after repeated bad tokens (a LAN device guessing the token)."""

    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        window_s: float = LOCKOUT_SECONDS,
        clock: Clock = time.monotonic,
    ) -> None:
        self._max = max_failures
        self._window = window_s
        self._clock = clock
        self._failures: OrderedDict[str, list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _recent(self, address: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(address, []) if now - t < self._window]
        if recent:
            self._failures[address] = recent
            self._failures.move_to_end(address)
        else:
            self._failures.pop(address, None)
        return recent

    def is_locked(self, address: str) -> bool:
        with self._lock:
            return len(self._recent(address, self._clock())) >= self._max

    def record_failure(self, address: str) -> None:
        with self._lock:
            now = self._clock()
            recent = self._recent(address, now)
            recent.append(now)
            self._failures[address] = recent
            self._failures.move_to_end(address)
            while len(self._failures) > _MAX_TRACKED:
                self._failures.popitem(last=False)

    def reset(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)


class BodyTooLarge(BaseException):
    """Raised inside the receive wrapper once the body passes the cap.

    A ``BaseException`` on purpose: Starlette's error middleware catches ``Exception`` and would
    answer 500 before the guard could answer 413. This one passes through it to the guard."""


def declared_length(headers: list[tuple[bytes, bytes]]) -> int | None:
    """The Content-Length header as an int, or None if absent or malformed."""
    for key, value in headers:
        if key == b"content-length":
            try:
                length = int(value.decode("latin-1"))
            except ValueError:
                return -1
            return length if length >= 0 else -1
    return None


def limit_receive(receive: Callable[[], Awaitable[Any]], cap: int) -> Callable[[], Awaitable[Any]]:
    """Wrap an ASGI ``receive`` so a streamed body (chunked, or a lying Content-Length)
    raises ``BodyTooLarge`` as soon as it passes ``cap`` bytes."""
    seen = 0

    async def limited() -> Any:
        nonlocal seen
        message = await receive()
        if isinstance(message, dict) and message.get("type") == "http.request":
            seen += len(message.get("body", b""))
            if seen > cap:
                raise BodyTooLarge
        return message

    return limited
