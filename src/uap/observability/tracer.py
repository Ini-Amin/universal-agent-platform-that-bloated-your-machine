"""Latency-tracking context manager (Master section 22).

``Tracer`` brackets a unit of work and emits a matching "finished" event with a
measured ``duration_ms``. On an exception it emits an ``ERROR`` event carrying
the original error and re-raises so the workflow's own error handling still
runs - observability records failure, it never swallows it.
"""

from __future__ import annotations

import time
from typing import Any

from uap.observability.events import EventBus, EventKind, ObsEvent

__all__ = ["Tracer"]

#: Start kinds that have a distinct finished counterpart.
_FINISH_KIND: dict[EventKind, EventKind] = {
    EventKind.TASK_STARTED: EventKind.TASK_FINISHED,
    EventKind.NODE_STARTED: EventKind.NODE_FINISHED,
}


class Tracer:
    """Context manager that times a unit of work and reports to ``bus``.

    Usage::

        with Tracer(bus, EventKind.NODE_STARTED, task_id=task_id, node="plan"):
            ...  # do the node's work
    """

    def __init__(self, bus: EventBus, kind: EventKind, **fields: Any) -> None:
        self.bus = bus
        self.kind = kind
        self.fields: dict[str, Any] = dict(fields)
        self._started: float | None = None

    def _event(self, kind: EventKind, **extra: Any) -> ObsEvent:
        payload = {**self.fields, **extra}
        payload.pop("kind", None)  # never let fields shadow the event kind
        return ObsEvent(kind=kind, **payload)

    def __enter__(self) -> "Tracer":
        self._started = time.perf_counter()
        self.bus.emit(self._event(self.kind))
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        elapsed_ms = (
            (time.perf_counter() - self._started) * 1000.0
            if self._started is not None
            else 0.0
        )
        if exc is not None:
            self.bus.emit(
                self._event(
                    EventKind.ERROR,
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=elapsed_ms,
                )
            )
            return False  # propagate the original exception untouched
        finish_kind = _FINISH_KIND.get(self.kind, self.kind)
        self.bus.emit(self._event(finish_kind, duration_ms=elapsed_ms))
        return False
