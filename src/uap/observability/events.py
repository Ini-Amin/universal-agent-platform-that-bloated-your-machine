"""Observability event vocabulary (Master section 22).

One immutable, self-describing event shape is emitted by every subsystem:
workflows, nodes, agents, tool calls, MCP calls, retries, verification, human
approval, artifacts and errors. Events carry enough correlation fields
(``task_id``, ``workflow``, ``node``, ``agent``, ``model``) to answer the
Master section 22 questions - why did this happen, which agent, which tools,
which model, where it failed, how many retries, what evidence.

The bus is deliberately deterministic and single-threaded: sinks are invoked
in subscription order, and a failing sink is isolated so observability can
never break the workflow it observes.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts import UTCDateTime, utc_now

__all__ = ["EventKind", "ObsEvent", "EventBus", "Sink"]


class EventKind(StrEnum):
    """The complete observability vocabulary (Master section 22)."""

    TASK_STARTED = "task_started"
    TASK_FINISHED = "task_finished"
    NODE_STARTED = "node_started"
    NODE_FINISHED = "node_finished"
    AGENT_RUN = "agent_run"
    TOOL_CALL = "tool_call"
    MCP_CALL = "mcp_call"
    RETRY = "retry"
    VERIFICATION = "verification"
    APPROVAL = "approval"
    ARTIFACT = "artifact"
    ERROR = "error"


class ObsEvent(BaseModel):
    """A single observability record.

    ``extra="forbid"`` keeps the vocabulary closed: producers must use the
    declared correlation fields (or ``data``) rather than inventing new keys.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: UTCDateTime = Field(default_factory=utc_now)
    kind: EventKind
    task_id: str | None = None
    workflow: str | None = None
    node: str | None = None
    agent: str | None = None
    model: str | None = None
    duration_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


#: A sink is any callable that accepts one :class:`ObsEvent`.
Sink = Callable[[ObsEvent], None]


class EventBus:
    """Fan-out bus with failure isolation (Master section 22).

    ``emit`` never raises: an exception from any sink is caught and appended to
    :attr:`errors`, and the remaining sinks are still invoked. Ordering is
    deterministic (subscription order) under the single-threaded assumption.
    """

    def __init__(self) -> None:
        self._sinks: list[Sink] = []
        self._errors: list[str] = []

    def subscribe(self, sink: Sink) -> None:
        """Register ``sink`` to receive every subsequently emitted event."""
        if not callable(sink):
            raise TypeError("sink must be callable")
        self._sinks.append(sink)

    def emit(self, event: ObsEvent) -> None:
        """Deliver ``event`` to every sink, isolating any sink failure."""
        for sink in self._sinks:
            try:
                sink(event)
            except Exception as exc:  # noqa: BLE001 - observability must not raise
                self._errors.append(f"{type(exc).__name__}: {exc}")

    def emit_kind(self, kind: EventKind, **fields: Any) -> ObsEvent:
        """Build an :class:`ObsEvent` from ``fields``, emit it, and return it."""
        event = ObsEvent(kind=kind, **fields)
        self.emit(event)
        return event

    @property
    def errors(self) -> list[str]:
        """A snapshot of every sink failure observed so far."""
        return list(self._errors)
