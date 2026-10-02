"""Concrete observability sinks (Master section 22).

Two dependency-free sinks ship with the platform:

* :class:`JsonlSink` - append-only newline-delimited JSON on the filesystem,
  the durable record used to reconstruct "what happened" after the fact.
* :class:`MemorySink` - an in-process buffer for tests and live inspection.

Both are plain callables, so they plug directly into :class:`EventBus`.
"""

from __future__ import annotations

import json
from pathlib import Path

from uap.observability.events import EventKind, ObsEvent

__all__ = ["JsonlSink", "MemorySink"]


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
    """Keep every event in memory and expose simple filtering queries."""

    def __init__(self) -> None:
        self.events: list[ObsEvent] = []

    def __call__(self, event: ObsEvent) -> None:
        self.events.append(event)

    def query(
        self,
        kind: EventKind | str | None = None,
        task_id: str | None = None,
    ) -> list[ObsEvent]:
        """Return events matching all supplied filters (``None`` = any)."""
        return [
            event
            for event in self.events
            if (kind is None or event.kind == kind)
            and (task_id is None or event.task_id == task_id)
        ]
