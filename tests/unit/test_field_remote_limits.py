"""Field Remote limits: token bucket, per-address lockout, body caps (ADR-081)."""

from __future__ import annotations

import asyncio

import pytest

from open_garden_planner.field_remote import limits


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_bucket_spends_burst_then_refills() -> None:
    clock = FakeClock()
    bucket = limits.TokenBucket(rate=2.0, burst=2.0, clock=clock)
    assert [bucket.allow() for _ in range(3)] == [True, True, False]
    clock.now += 0.5  # one token back
    assert bucket.allow()
    assert not bucket.allow()


def test_reads_and_writes_have_separate_budgets() -> None:
    clock = FakeClock()
    rate = limits.RateLimiter(clock=clock)
    assert sum(rate.allow("POST") for _ in range(20)) == int(limits.WRITE_RATE)
    assert sum(rate.allow("GET") for _ in range(30)) == int(limits.READ_RATE)


def test_lockout_after_max_failures_then_expires_and_resets() -> None:
    clock = FakeClock()
    lock = limits.FailureLockout(max_failures=3, window_s=60, clock=clock)
    for _ in range(2):
        lock.record_failure("10.0.0.9")
    assert not lock.is_locked("10.0.0.9")
    lock.record_failure("10.0.0.9")
    assert lock.is_locked("10.0.0.9")
    assert not lock.is_locked("10.0.0.10")  # per address
    clock.now += 61
    assert not lock.is_locked("10.0.0.9")
    lock.record_failure("10.0.0.9")
    lock.reset("10.0.0.9")
    assert not lock.is_locked("10.0.0.9")


def test_lockout_memory_is_bounded() -> None:
    lock = limits.FailureLockout(clock=FakeClock())
    for index in range(limits._MAX_TRACKED + 50):
        lock.record_failure(f"a{index}")
    assert len(lock._failures) == limits._MAX_TRACKED


def test_declared_length() -> None:
    assert limits.declared_length([(b"content-length", b"12")]) == 12
    assert limits.declared_length([]) is None
    assert limits.declared_length([(b"content-length", b"abc")]) == -1
    assert limits.declared_length([(b"content-length", b"-4")]) == -1


def test_limit_receive_stops_a_streamed_body_over_the_cap() -> None:
    chunks = [{"type": "http.request", "body": b"x" * 6, "more_body": True}] * 3

    async def receive() -> dict[str, object]:
        return chunks.pop(0)

    async def drain() -> None:
        limited = limits.limit_receive(receive, 10)
        await limited()  # 6 bytes
        await limited()  # 12 bytes: over

    with pytest.raises(limits.BodyTooLarge):
        asyncio.run(drain())
