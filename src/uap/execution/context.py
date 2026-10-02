"""Execution context, node runtime protocol and execution result types.

This module defines the *interfaces* the graph execution engine talks to
(Master sections 20, 21, 23, 35, 37). The engine in :mod:`uap.execution.engine`
is deliberately free of any domain, agent or framework dependency: a caller
injects a :class:`NodeRuntime` that actually runs a node, and optionally an
approval gate, a subworkflow resolver, an event sink and a decision sink.

Design notes (section 73 - explicit interfaces, dependency injection):

* :class:`NodeRuntime` is a :class:`typing.Protocol`, so any object with a
  matching ``run_node`` method satisfies it (structural typing).
* :class:`ExecutionContext` carries *all* mutable run state, so nothing lives
  in module-level globals.
* :class:`PauseExecution` is control flow, not an error: it unwinds a node
  cleanly and carries the :class:`~uap.contracts.models.ApprovalRequest` the
  engine surfaces on the paused :class:`ExecutionResult`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from uap.contracts.models import ApprovalRequest
from uap.graph import GraphNode, WorkflowGraph

__all__ = [
    "NodeRuntime",
    "NodeExecutionError",
    "PauseExecution",
    "ExecutionContext",
    "ExecutionResult",
]


@runtime_checkable
class NodeRuntime(Protocol):
    """Runs a single graph node and returns its output ports.

    Implementations return ``{output_port_name: value}``. Raising
    :class:`NodeExecutionError` (or any other exception) fails the node. Raising
    :class:`PauseExecution` pauses the whole execution.
    """

    async def run_node(
        self, node: GraphNode, inputs: dict[str, Any], ctx: "ExecutionContext"
    ) -> dict[str, Any]:
        """Return ``{output_port_name: value}`` for ``node``."""
        ...


class NodeExecutionError(Exception):
    """Raised by a :class:`NodeRuntime` to fail a node with a clear message."""


class PauseExecution(Exception):
    """Raised by a node (or approval hook) to pause the execution.

    Carries the :class:`~uap.contracts.models.ApprovalRequest` that the engine
    puts on :attr:`ExecutionResult.pending_approval`.
    """

    def __init__(self, approval: ApprovalRequest) -> None:
        super().__init__(f"execution paused awaiting approval {approval.approval_id}")
        self.approval = approval


@dataclass
class ExecutionContext:
    """Mutable state for one execution, shared with the injected runtime.

    The engine owns every transition; a node runtime may *read* the context
    (e.g. to inspect ``metadata`` or prior ``node_results``) but must not assume
    anything about scheduling.
    """

    execution_id: str
    graph: WorkflowGraph
    node_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    node_status: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    approval_gate: Any | None = None
    subworkflow_resolver: Callable[[str], WorkflowGraph] | None = None
    node_runtime: NodeRuntime | None = None
    event_sink: Callable[[str, dict], None] | None = None
    decision_sink: Callable[[dict], None] | None = None


@dataclass
class ExecutionResult:
    """The terminal (or paused) outcome of :meth:`GraphExecutor.execute`."""

    status: str  # "completed" | "failed" | "paused"
    outputs: dict[str, Any] = field(default_factory=dict)
    node_results: dict[str, dict] = field(default_factory=dict)
    node_status: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    pending_approval: ApprovalRequest | None = None
    order: list[str] = field(default_factory=list)
