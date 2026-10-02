"""Resource scheduler (Master section 40).

Public surface:

* :class:`Quota` / :class:`QuotaRegistry` - named concurrency + rate budgets.
* :class:`Scheduler` - in-process admission + priority queue with explicit
  backpressure (:class:`BackpressureError`).
* :class:`ScheduledItem` - one unit of schedulable work bound to a quota.
"""

from __future__ import annotations

from .quotas import BackpressureError, Quota, QuotaRegistry
from .queue import ScheduledItem, Scheduler

__all__ = [
    "BackpressureError",
    "Quota",
    "QuotaRegistry",
    "ScheduledItem",
    "Scheduler",
]
