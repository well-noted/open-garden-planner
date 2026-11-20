"""Desktop-to-phone events for ``GET /api/stream`` (design 1.6). No Qt.

A small thread-safe ring buffer: the Qt thread calls :meth:`EventHub.publish` (plan changed,
records changed, the permission toggle flipped) and the server's stream handlers read events
newer than the sequence number a phone last saw. Bounded in count and in size, so a quiet or
absent phone costs nothing and a busy desktop cannot grow it.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

MAX_EVENTS = 100
#: Event kinds a phone is told about; anything else is refused at publish time.
KINDS = frozenset({"plan_changed", "records_changed", "settings_changed", "position"})


class EventHub:
    """Ring buffer of ``{seq, type, t, data}`` events, safe from any thread."""

    def __init__(self, *, clock: Any = time.time) -> None:
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._seq = 0
        self._clock = clock
        self._streams = 0

    def publish(self, kind: str, data: dict[str, Any] | None = None) -> int:
        if kind not in KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "type": kind, "t": round(self._clock(), 3), "data": data or {}})
            return self._seq

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def since(self, seq: int) -> list[dict[str, Any]]:
        """Events with a sequence number above *seq* (the oldest may have been dropped)."""
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    def open_stream(self, limit: int) -> bool:
        """Reserve a stream slot; False when *limit* streams are already open."""
        with self._lock:
            if self._streams >= limit:
                return False
            self._streams += 1
            return True

    def close_stream(self) -> None:
        with self._lock:
            self._streams = max(0, self._streams - 1)
