"""Canonical runtime event model (Master sections 45, 46).

This is the *canonical replacement* for the lightweight observability
``EventKind`` vocabulary in :mod:`uap.observability.events`. The observability
bus stays as-is (it is a fan-out telemetry bus); this module defines the
structured, versioned, durable event contract that the runtime, the event store
and the decision trace all speak.

Design rules (Master sections 45, 46, 73):

* **Structured and versioned.** Every event carries ``schema_version`` so a
  consumer can evolve payloads without silently misreading history.
* **Closed vocabulary.** ``EventType`` is the complete set of canonical event
  types; :data:`EVENTSET_VERSION` versions the set itself.
* **Closed shape.** :class:`CanonicalEvent` forbids unknown top-level fields, so
  producers must use ``payload`` rather than inventing sibling keys.
* **Causal linkage.** ``correlation_id`` ties events to one logical request and
  ``parent_event_id`` encodes the causal chain that the decision trace (section
  26) references.
* **Never raises on validation.** :func:`validate_event` returns a list of
  human-readable violation strings (errors and warnings) instead of raising, so
  a producer can log-and-continue without a durable log write ever breaking the
  runtime it observes.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts import UTCDateTime, utc_now

__all__ = [
    "EVENTSET_VERSION",
    "CanonicalEvent",
    "EventType",
    "event_types",
    "validate_event",
]

#: Version of the *event set* (the ``EventType`` vocabulary) as a whole.
#: Bump when members are added/removed/renamed in a breaking way.
EVENTSET_VERSION = 1

class EventType(StrEnum):
    """The complete canonical runtime event vocabulary (Master section 45).

    Grouped by subject: execution lifecycle, node lifecycle, agent, tool, model
    selection, decision trace, approval, checkpointing, artifacts, knowledge,
    evaluation and generic error/retry signals.
    """

    # -- execution lifecycle ------------------------------------------------ #
    EXECUTION_STARTED = "execution_started"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_PAUSED = "execution_paused"
    EXECUTION_RESUMED = "execution_resumed"
    EXECUTION_FORKED = "execution_forked"

    # -- node lifecycle ----------------------------------------------------- #
    NODE_STARTED = "node_started"
    NODE_FINISHED = "node_finished"
    NODE_FAILED = "node_failed"
    NODE_SKIPPED = "node_skipped"

    # -- agents ------------------------------------------------------------- #
    AGENT_INVOKED = "agent_invoked"
    AGENT_COMPLETED = "agent_completed"

    # -- tools -------------------------------------------------------------- #
    TOOL_CALLED = "tool_called"
    TOOL_RESULT = "tool_result"
    TOOL_DENIED = "tool_denied"

    # -- model selection ---------------------------------------------------- #
    MODEL_SELECTED = "model_selected"
    MODEL_FALLBACK = "model_fallback"

    # -- decision trace (section 26) ---------------------------------------- #
    DECISION_RECORDED = "decision_recorded"

    # -- approval ----------------------------------------------------------- #
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"

    # -- checkpointing (sections 21, 37, 38) -------------------------------- #
    CHECKPOINT_SAVED = "checkpoint_saved"
    CHECKPOINT_RESTORED = "checkpoint_restored"

    # -- artifacts ---------------------------------------------------------- #
    ARTIFACT_CREATED = "artifact_created"

    # -- knowledge / learning (section 48) ---------------------------------- #
    KNOWLEDGE_PROPOSED = "knowledge_proposed"
    KNOWLEDGE_PROMOTED = "knowledge_promoted"

    # -- evaluation (section 47) -------------------------------------------- #
    EVALUATION_STARTED = "evaluation_started"
    EVALUATION_COMPLETED = "evaluation_completed"

    # -- generic ------------------------------------------------------------ #
    ERROR = "error"
    RETRY = "retry"

def event_types() -> list[str]:
    """Return the canonical event type strings, in declaration order."""

    return [member.value for member in EventType]

class CanonicalEvent(BaseModel):
    """One structured, versioned, durable runtime event (Master sections 45, 46).

    ``extra="forbid"`` keeps the envelope closed: event-specific data belongs in
    :attr:`payload`, whose keys are validated against :data:`PAYLOAD_SCHEMAS`
    by :func:`validate_event`.
    """

    model_config = ConfigDict(extra="forbid")

    #: Envelope version. Consumers must tolerate older versions they know.
    schema_version: int = 1
    event_type: EventType
    #: The execution this event belongs to (always present - section 46
    #: "execution correlation").
    execution_id: str
    #: Graph node / agent / tool this event is attached to, when applicable.
    node_id: str | None = None
    #: Event time (UTC). Defaults to the single platform clock.
    ts: UTCDateTime = Field(default_factory=utc_now)
    #: Event-specific structured data; keys validated per event type.
    payload: dict = Field(default_factory=dict)
    #: Ties events of one logical request together (section 46).
    correlation_id: str | None = None
    #: Causal parent; forms the chain the decision trace links into (section 26).
    parent_event_id: str | None = None

def validate_event(event: CanonicalEvent) -> list[str]:
    """Validate ``event`` against its payload schema. Never raises.

    Returns a list of human-readable violations:

    * ``"error: ..."``   - a required payload key is missing.
    * ``"warning: ..."`` - an unknown extra payload key was supplied.

    A valid event returns ``[]``. The payload schema is imported lazily to avoid
    a module import cycle with :mod:`uap.events.schemas`.
    """

    from uap.events.schemas import PAYLOAD_SCHEMAS  # local import: avoid cycle

    violations: list[str] = []

    payload = event.payload
    if not isinstance(payload, dict):
        return [
            f"error: payload for {event.event_type.value!r} must be a mapping, "
            f"got {type(payload).__name__}"
        ]

    spec = PAYLOAD_SCHEMAS.get(event.event_type)
    if spec is None:
        return [f"error: no payload schema for event type {event.event_type.value!r}"]

    required = spec["required"]
    optional = spec["optional"]

    for key in sorted(required):
        if key not in payload:
            violations.append(
                f"error: missing required payload key {key!r} "
                f"for {event.event_type.value}"
            )

    known = required | optional
    for key in sorted(payload):
        if key not in known:
            violations.append(
                f"warning: unknown payload key {key!r} for {event.event_type.value}"
            )

    return violations
