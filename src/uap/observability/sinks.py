"""Concrete observability sinks (Master section 22).

Two dependency-free sinks ship with the platform:

* :class:`JsonlSink` - append-only newline-delimited JSON on the filesystem,
  the durable record used to reconstruct "what happened" after the fact.
* :class:`MemorySink` - an in-process buffer for tests and live inspection.

Both are plain callables, so they plug directly into :class:`EventBus`.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from uap.observability.events import EventKind, ObsEvent

#: Default cap for the in-process buffers (``MemorySink``, ``EventStream``):
#: a long-running server must not grow without bound.
DEFAULT_MAX_EVENTS = 10_000

__all__ = ["DEFAULT_MAX_EVENTS", "JsonlSink", "MemorySink"]


class JsonlSink:
    """Append one JSON line per event to ``path`` (parents created on demand)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def __call__(self, event: ObsEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            event.model_dump(mode="json"), ensure_ascii=False, default=str
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


class MemorySink:
    """Keep the most recent ``max_events`` events, evicting the oldest first.

    ``max_events=0`` disables buffering entirely. :attr:`events` is an
    oldest-first snapshot and :attr:`dropped` counts everything the cap
    evicted, so a reader can tell "nothing happened" from "history was
    truncated" instead of silently trusting a partial buffer.
    """

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS) -> None:
        self.max_events = max_events
        self._events: deque[ObsEvent] = deque(maxlen=max_events)
        self._appended = 0

    def __call__(self, event: ObsEvent) -> None:
        self._appended += 1
        self._events.append(event)

    @property
    def events(self) -> list[ObsEvent]:
        """A snapshot of the buffered events, oldest first."""
        return list(self._events)

    @property
    def dropped(self) -> int:
        """How many events the cap evicted since construction."""
        return self._appended - len(self._events)

    def query(
        self,
        kind: EventKind | str | None = None,
        task_id: str | None = None,
    ) -> list[ObsEvent]:
        """Return buffered events matching all supplied filters (``None`` = any)."""
        return [
            event
            for event in self._events
            if (kind is None or event.kind == kind)
            and (task_id is None or event.task_id == task_id)
        ]
