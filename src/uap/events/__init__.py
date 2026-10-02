"""Canonical runtime event model (Master sections 45, 46).

Public surface:

* :class:`EventType` - the complete canonical event vocabulary.
* :class:`CanonicalEvent` - the structured, versioned, durable event envelope.
* :func:`event_types` / :func:`validate_event` - vocabulary + payload validation.
* :data:`EVENTSET_VERSION` - the version of the event set itself.
* :data:`PAYLOAD_SCHEMAS` - the per-event payload schema table.

This package is the canonical replacement for the lightweight
:mod:`uap.observability.events` vocabulary; the two coexist (the observability
bus remains a fan-out telemetry sink).
"""

from __future__ import annotations

from uap.events.model import (
    EVENTSET_VERSION,
    CanonicalEvent,
    EventType,
    event_types,
    validate_event,
)
from uap.events.schemas import PAYLOAD_SCHEMAS

__all__ = [
    "EVENTSET_VERSION",
    "CanonicalEvent",
    "EventType",
    "PAYLOAD_SCHEMAS",
    "event_types",
    "validate_event",
]
