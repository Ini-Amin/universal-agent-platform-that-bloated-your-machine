"""Resource quotas and the quota registry (Master section 40).

A :class:`Quota` is a named concurrency / rate budget. The :class:`QuotaRegistry`
is a plain, injectable container (no global mutable state - section 73): the
scheduler is handed a registry rather than reaching for a module singleton.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["Quota", "QuotaRegistry", "BackpressureError"]


class Quota(BaseModel):
    """A named resource budget (section 40: concurrency, rate limiting, priority)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    #: Maximum number of items that may be *running* at once under this quota.
    max_concurrent: int = 4
    #: Optional admissions-per-minute ceiling (``None`` = unlimited rate).
    max_per_minute: int | None = None
    #: Default scheduling priority for items under this quota (higher runs first).
    priority: int = 0


class BackpressureError(Exception):
    """Raised by the scheduler when a quota cannot admit an item (section 40).

    Signals explicit backpressure: an unknown quota, a full concurrency budget,
    or an exceeded rate limit. Callers back off rather than silently queueing
    unbounded work.
    """


class QuotaRegistry:
    """An injectable set of named quotas (section 73: no hidden singletons)."""

    def __init__(self) -> None:
        self._quotas: dict[str, Quota] = {}

    def set(self, quota: Quota) -> None:
        """Register (or replace) a quota by name."""

        self._quotas[quota.name] = quota

    def get(self, name: str) -> Quota | None:
        """Return the quota named ``name``, or ``None`` when unknown."""

        return self._quotas.get(name)

    def all(self) -> list[Quota]:
        """Return every registered quota, in insertion order."""

        return list(self._quotas.values())
