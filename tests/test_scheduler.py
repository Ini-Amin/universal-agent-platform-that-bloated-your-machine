"""Resource scheduler tests (Master section 40).

Pure in-process tests - no database. The scheduler's clock is injectable, so the
rate-limit window is driven deterministically by a fake clock rather than real
time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from uap.scheduler import (
    BackpressureError,
    Quota,
    QuotaRegistry,
    ScheduledItem,
    Scheduler,
)


def _registry(*quotas: Quota) -> QuotaRegistry:
    registry = QuotaRegistry()
    for quota in quotas:
        registry.set(quota)
    return registry


def _item(item_id: str, quota: str = "default", *, priority: int = 0, at: datetime | None = None) -> ScheduledItem:
    kwargs: dict = {"item_id": item_id, "quota": quota, "priority": priority}
    if at is not None:
        kwargs["enqueued_at"] = at
    return ScheduledItem(**kwargs)


# 1. admit/complete lifecycle
def test_admit_then_complete_lifecycle():
    sched = Scheduler(_registry(Quota(name="default", max_concurrent=2)))
    sched.admit(_item("a"))
    assert [i.item_id for i in sched.running()] == ["a"]
    assert [i.item_id for i in sched.pending()] == ["a"]

    popped = sched.next()
    assert popped is not None and popped.item_id == "a"
    # Dispatched but still running (slot held) until complete().
    assert [i.item_id for i in sched.running()] == ["a"]
    assert sched.pending() == []

    sched.complete("a")
    assert sched.running() == []


# 2. unknown quota -> BackpressureError
def test_admit_unknown_quota_raises():
    sched = Scheduler(_registry(Quota(name="default")))
    with pytest.raises(BackpressureError):
        sched.admit(_item("a", quota="nope"))
    assert sched.stats()["rejected"] == 1


# 3. concurrency limit enforced
def test_concurrency_limit_enforced():
    sched = Scheduler(_registry(Quota(name="default", max_concurrent=2)))
    sched.admit(_item("a"))
    sched.admit(_item("b"))
    with pytest.raises(BackpressureError):
        sched.admit(_item("c"))
    assert sched.stats()["running"] == {"default": 2}


# 4. complete frees a slot
def test_complete_frees_a_slot():
    sched = Scheduler(_registry(Quota(name="default", max_concurrent=1)))
    sched.admit(_item("a"))
    with pytest.raises(BackpressureError):
        sched.admit(_item("b"))
    sched.complete("a")
    sched.admit(_item("b"))  # now fits
    assert [i.item_id for i in sched.running()] == ["b"]


# 5. rate limit max_per_minute enforced within the window (fake clock)
def test_rate_limit_enforced_within_window():
    now = {"t": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    sched = Scheduler(
        _registry(Quota(name="rl", max_concurrent=100, max_per_minute=2)),
        clock=lambda: now["t"],
    )
    sched.admit(_item("a", quota="rl"))
    sched.admit(_item("b", quota="rl"))
    with pytest.raises(BackpressureError):
        sched.admit(_item("c", quota="rl"))  # 3rd within the same minute

    # Advance past the 60s window: the earlier admissions age out.
    now["t"] = now["t"] + timedelta(seconds=61)
    sched.admit(_item("d", quota="rl"))  # window cleared, admitted


# 6. next() priority order
def test_next_returns_highest_priority_first():
    sched = Scheduler(_registry(Quota(name="default", max_concurrent=10)))
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sched.admit(_item("low", priority=0, at=base))
    sched.admit(_item("high", priority=5, at=base))
    sched.admit(_item("mid", priority=1, at=base))
    assert [sched.next().item_id for _ in range(3)] == ["high", "mid", "low"]


# 7. FIFO tiebreak within equal priority
def test_fifo_tiebreak_within_equal_priority():
    sched = Scheduler(_registry(Quota(name="default", max_concurrent=10)))
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sched.admit(_item("second", priority=1, at=base + timedelta(seconds=2)))
    sched.admit(_item("first", priority=1, at=base + timedelta(seconds=1)))
    sched.admit(_item("third", priority=1, at=base + timedelta(seconds=3)))
    assert [sched.next().item_id for _ in range(3)] == ["first", "second", "third"]


# 8. pending/running/stats correctness
def test_pending_running_stats_correctness():
    sched = Scheduler(_registry(Quota(name="default", max_concurrent=10)))
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sched.admit(_item("a", priority=1, at=base))
    sched.admit(_item("b", priority=2, at=base))
    assert {i.item_id for i in sched.pending()} == {"a", "b"}
    assert {i.item_id for i in sched.running()} == {"a", "b"}
    stats = sched.stats()
    assert stats["pending"] == 2
    assert stats["running"] == {"default": 2}
    assert stats["rejected"] == 0

    # Dispatching highest-priority removes it from pending but not from running.
    assert sched.next().item_id == "b"
    assert {i.item_id for i in sched.pending()} == {"a"}
    assert {i.item_id for i in sched.running()} == {"a", "b"}
    assert sched.stats()["pending"] == 1


# 9. rejected counter increments
def test_rejected_counter_increments():
    sched = Scheduler(_registry(Quota(name="default", max_concurrent=1)))
    sched.admit(_item("a"))
    for _ in range(3):
        with pytest.raises(BackpressureError):
            sched.admit(_item("b"))
    assert sched.stats()["rejected"] == 3


# 10. release_all clears running
def test_release_all_clears_running():
    sched = Scheduler(_registry(Quota(name="default", max_concurrent=1)))
    sched.admit(_item("a"))
    assert sched.running()
    sched.release_all()
    assert sched.running() == []
    # A freed quota admits again immediately.
    sched.admit(_item("b"))
    assert [i.item_id for i in sched.running()] == ["b"]


# 11. registry get/all round-trip
def test_registry_get_and_all():
    registry = _registry(Quota(name="x", priority=3), Quota(name="y"))
    assert registry.get("x").priority == 3
    assert registry.get("missing") is None
    assert {q.name for q in registry.all()} == {"x", "y"}
