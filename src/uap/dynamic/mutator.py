"""Graph mutator - the dynamic-graph transaction engine (Master section 20).

Applies a :class:`GraphMutation` to a *copy* of a running execution's graph and
runs it through the section-20 pipeline before accepting it. The persistent
workflow definition is never mutated (section 2.3, AI behavior rule 10).

Pipeline per :meth:`GraphMutator.propose`:

#. **transaction** - deep-copy the graph so the proposal is isolated.
#. **validation** - apply the mutation to the copy, then ``validate_graph``.
#. **policy** - with ``require_reason`` a mutation must carry a non-empty reason.
#. **audit** - :meth:`GraphMutator.audit` records a ``REPLAN`` decision trace.

Checkpoint + apply are the caller's responsibility (the mutator returns the new
graph; the runtime checkpoints and swaps it in).
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from uap.graph import GraphEdge, GraphNode, WorkflowGraph, validate_graph
from uap.trace import DecisionRecorder, DecisionTrace, DecisionType

__all__ = ["GraphMutation", "GraphMutator", "MutationResult"]

_FORBID = ConfigDict(extra="forbid")

MutationKind = Literal["add_node", "add_edge", "remove_node", "remove_edge"]


class GraphMutation(BaseModel):
    """A single proposed change to a running execution's graph."""

    model_config = _FORBID

    mutation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: MutationKind
    node: GraphNode | None = None
    edge: GraphEdge | None = None
    target_id: str | None = None
    reason: str


class MutationResult(BaseModel):
    """The outcome of proposing a mutation."""

    model_config = _FORBID

    ok: bool
    applied: bool
    validation_issues: list[dict] = Field(default_factory=list)
    reason: str


class GraphMutator:
    """Transactionally applies and validates runtime graph mutations."""

    def __init__(self, *, require_reason: bool = True) -> None:
        self.require_reason = require_reason

    def propose(
        self, graph: WorkflowGraph, mutation: GraphMutation
    ) -> tuple[WorkflowGraph, MutationResult]:
        """Return a new (deep-copied) graph plus a :class:`MutationResult`.

        On rejection the returned graph is the *original* (unchanged) and
        ``applied`` is ``False``; the caller discards it.
        """
        if self.require_reason and not mutation.reason.strip():
            return graph, MutationResult(
                ok=False,
                applied=False,
                reason="mutation requires a reason",
            )

        working = graph.model_copy(deep=True)
        try:
            self._apply(working, mutation)
        except ValueError as exc:
            return graph, MutationResult(
                ok=False,
                applied=False,
                validation_issues=[{"code": "mutation_invalid", "message": str(exc)}],
                reason=str(exc),
            )

        issues = validate_graph(working)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            return graph, MutationResult(
                ok=False,
                applied=False,
                validation_issues=[i.model_dump() for i in errors],
                reason="validation failed",
            )

        return working, MutationResult(
            ok=True,
            applied=True,
            validation_issues=[i.model_dump() for i in issues],
            reason=mutation.reason,
        )

    def audit(
        self,
        mutation: GraphMutation,
        result: MutationResult,
        *,
        execution_id: str,
        recorder: DecisionRecorder,
    ) -> DecisionTrace:
        """Record the mutation + outcome as a ``REPLAN`` decision trace (§26)."""
        trace = DecisionTrace(
            execution_id=execution_id,
            decision_type=DecisionType.REPLAN,
            chosen=f"{mutation.kind}:{mutation.mutation_id}",
            rationale=mutation.reason or result.reason,
            confidence=1.0 if result.applied else 0.0,
            inputs_summary={
                "mutation_id": mutation.mutation_id,
                "kind": mutation.kind,
                "target_id": mutation.target_id,
                "ok": result.ok,
                "applied": result.applied,
                "issue_count": len(result.validation_issues),
            },
        )
        return recorder.record(trace)

    # ------------------------------------------------------------------ #
    # Mutation application (operates in-place on the deep copy)
    # ------------------------------------------------------------------ #

    def _apply(self, graph: WorkflowGraph, mutation: GraphMutation) -> None:
        if mutation.kind == "add_node":
            self._add_node(graph, mutation)
        elif mutation.kind == "add_edge":
            self._add_edge(graph, mutation)
        elif mutation.kind == "remove_node":
            self._remove_node(graph, mutation)
        elif mutation.kind == "remove_edge":
            self._remove_edge(graph, mutation)
        else:  # pragma: no cover - Literal keeps this unreachable
            raise ValueError(f"unknown mutation kind: {mutation.kind!r}")

    @staticmethod
    def _add_node(graph: WorkflowGraph, mutation: GraphMutation) -> None:
        if mutation.node is None:
            raise ValueError("add_node requires 'node'")
        if any(n.id == mutation.node.id for n in graph.nodes):
            raise ValueError(f"node already exists: {mutation.node.id!r}")
        graph.nodes.append(mutation.node)

    @staticmethod
    def _add_edge(graph: WorkflowGraph, mutation: GraphMutation) -> None:
        if mutation.edge is None:
            raise ValueError("add_edge requires 'edge'")
        ids = {n.id for n in graph.nodes}
        for endpoint in (mutation.edge.source, mutation.edge.target):
            if endpoint not in ids:
                raise ValueError(f"edge endpoint missing: {endpoint!r}")
        graph.edges.append(mutation.edge)

    @staticmethod
    def _remove_node(graph: WorkflowGraph, mutation: GraphMutation) -> None:
        if not mutation.target_id:
            raise ValueError("remove_node requires 'target_id'")
        if not any(n.id == mutation.target_id for n in graph.nodes):
            raise ValueError(f"unknown node: {mutation.target_id!r}")
        graph.nodes = [n for n in graph.nodes if n.id != mutation.target_id]
        # dropping a node drops every edge touching it.
        graph.edges = [
            e
            for e in graph.edges
            if e.source != mutation.target_id and e.target != mutation.target_id
        ]

    @staticmethod
    def _remove_edge(graph: WorkflowGraph, mutation: GraphMutation) -> None:
        if not mutation.target_id:
            raise ValueError("remove_edge requires 'target_id'")
        if not any(e.id == mutation.target_id for e in graph.edges):
            raise ValueError(f"unknown edge: {mutation.target_id!r}")
        graph.edges = [e for e in graph.edges if e.id != mutation.target_id]
