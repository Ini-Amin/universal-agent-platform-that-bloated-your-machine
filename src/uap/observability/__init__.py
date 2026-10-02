"""Observability package public API (Master section 22).

Cross-cutting, dependency-free telemetry: a fan-out :class:`EventBus`, the
:class:`ObsEvent` / :class:`EventKind` vocabulary, filesystem and in-memory
sinks, and a :class:`Tracer` context manager for latency and error capture.
"""

from uap.observability.events import EventBus, EventKind, ObsEvent, Sink
from uap.observability.sinks import JsonlSink, MemorySink
from uap.observability.tracer import Tracer

__all__ = [
    "EventBus",
    "ObsEvent",
    "EventKind",
    "JsonlSink",
    "MemorySink",
    "Tracer",
    "Sink",
]
