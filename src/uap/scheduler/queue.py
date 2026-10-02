"""In-process admission + priority scheduler (Master section 40).

The :class:`Scheduler` is the resource-orchestration entry point: callers
``admit`` an item against a named quota, pop work in priority order with
``next``, and ``complete`` it to free the concurrency slot. Backpressure is
*explicit* - a full quota raises :class:`BackpressureError` rather than silently
growing an unbounded queue (section 40: "backpressure").

Determinism (section 73): the clock is injectable. Tests pass a fake ``clock``
to drive the rate-limit window without real sleeps.
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts import UTCDateTime, utc_now

from .quotas import BackpressureError, QuotaRegistry

__all__ = ["ScheduledItem", "Scheduler"]


class ScheduledItem(BaseModel):
    """One unit of schedulable work bound to a named quota (section 40)."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    quota: str
    priority: int = 0
    enqueued_at: UTCDateTime = Field(default_factory=utc_now)
    payload: dict = Field(default_factory=dict)


class Scheduler:
    """In-process admission + priority queue.

    Backpressure is explicit: :meth:`admit` raises :class:`BackpressureError`
    when the quota name is unknown, its concurrency budget is full, or its
    per-minute rate is exceeded (section 40). An admitted item counts as
    *running* until :meth:`complete` is called, then becomes available to
    :meth:`next` for dispatch.

    Ordering: :meth:`next` returns the highest-priority pending item, breaking
    ties FIFO by ``enqueued_at`` (then by admission order for identical
    timestamps, so the order is total and deterministic).
    """

    def __init__(
        self,
        quotas: QuotaRegistry | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._quotas = quotas if quotas is not None else QuotaRegistry()
        self._clock = clock if clock is not None else _default_clock
        # Min-heap of (-priority, enqueued_epoch, tiebreak, item_id); the item
        # body lives in ``_items`` so the heap entries stay cheap and comparable.
        self._heap: list[tuple[int, float, int, str]] = []
        # Every admitted-not-completed item (its concurrency slot is held).
        self._items: dict[str, ScheduledItem] = {}
        # item_ids still awaiting dispatch (subset of ``_items``).
        self._pending: set[str] = set()
        # item_id -> quota name, for every *running* (admitted, not completed) item.
        self._running: dict[str, str] = {}
        # quota name -> sorted list of recent admission epochs (rate window).
        self._admissions: dict[str, list[float]] = {}
        self._rejected = 0
        self._counter = itertools.count()

    # ------------------------------------------------------------------ #
    # Admission
    # ------------------------------------------------------------------ #

    def admit(self, item: ScheduledItem) -> None:
        """Admit ``item`` against its quota or raise :class:`BackpressureError`.

        An admitted item immediately counts as *running* (it holds a concurrency
        slot and consumes one rate-window token) and is enqueued for dispatch by
        :meth:`next`. Rejections increment the ``rejected`` counter.
        """

        quota = self._quotas.get(item.quota)
        if quota is None:
            self._rejected += 1
            raise BackpressureError(f"unknown quota {item.quota!r}")

        running_now = sum(1 for name in self._running.values() if name == item.quota)
        if running_now >= quota.max_concurrent:
            self._rejected += 1
            raise BackpressureError(
                f"quota {item.quota!r} at concurrency limit "
                f"({running_now}/{quota.max_concurrent})"
            )

        now = self._now_epoch()
        if quota.max_per_minute is not None:
            window = self._prune_window(item.quota, now)
            if len(window) >= quota.max_per_minute:
                self._rejected += 1
                raise BackpressureError(
                    f"quota {item.quota!r} rate limit exceeded "
                    f"({len(window)}/{quota.max_per_minute} per minute)"
                )
            window.append(now)

        # Admitted: record running state and enqueue for dispatch.
        self._running[item.item_id] = item.quota
        self._items[item.item_id] = item
        self._pending.add(item.item_id)
        heapq.heappush(
            self._heap,
            (
                -item.priority,
                item.enqueued_at.timestamp(),
                next(self._counter),
                item.item_id,
            ),
        )

    def complete(self, item_id: str) -> None:
        """Free the concurrency slot held by ``item_id`` (no-op if unknown)."""

        self._running.pop(item_id, None)
        self._items.pop(item_id, None)
        self._pending.discard(item_id)

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    def next(self) -> ScheduledItem | None:
        """Pop the highest-priority pending item (FIFO tiebreak), or ``None``.

        A popped item leaves the pending queue but remains *running* until
        :meth:`complete` is called, so its concurrency slot stays held while a
        worker drives it.
        """

        while self._heap:
            _, _, _, item_id = heapq.heappop(self._heap)
            if item_id in self._pending:
                self._pending.discard(item_id)
                return self._items.get(item_id)
        return None

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def pending(self) -> list[ScheduledItem]:
        """Return pending (admitted, not yet dispatched) items in dispatch order."""

        return [
            self._items[item_id]
            for *_, item_id in sorted(self._heap)
            if item_id in self._pending
        ]

    def running(self) -> list[ScheduledItem]:
        """Return items that have been admitted and not yet completed."""

        return [
            self._items[item_id]
            for item_id in self._running
            if item_id in self._items
        ]

    def stats(self) -> dict:
        """Return ``{pending, running: {quota: n}, rejected}`` (section 40)."""

        running_by_quota: dict[str, int] = {}
        for quota_name in self._running.values():
            running_by_quota[quota_name] = running_by_quota.get(quota_name, 0) + 1
        return {
            "pending": len(self._pending),
            "running": running_by_quota,
            "rejected": self._rejected,
        }

    def release_all(self) -> None:
        """Test helper: drop every running slot (does not touch pending items).

        Documented as a *test helper*: it clears the running set and the rate
        window so a test can simulate "every in-flight item finished" without
        tracking each ``item_id``.
        """

        self._running.clear()
        self._admissions.clear()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _now_epoch(self) -> float:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.timestamp()

    def _prune_window(self, quota_name: str, now: float) -> list[float]:
        """Return the per-quota admission window, dropping entries > 60s old."""

        window = self._admissions.setdefault(quota_name, [])
        cutoff = now - 60.0
        kept = [epoch for epoch in window if epoch > cutoff]
        self._admissions[quota_name] = kept
        return kept


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)
