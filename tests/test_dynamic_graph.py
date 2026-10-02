"""Tests for dynamic graph mutation (Master section 20).

Deterministic and IO-free. A tiny recorder stub captures the ``REPLAN``
decision trace without touching PostgreSQL, and the final case proves the
mutated graph actually runs on :class:`GraphExecutor`.
"""

from __future__ import annotations

from typing import Any

from uap.dynamic import GraphMutation, GraphMutator, MutationResult
from uap.execution import GraphExecutor
from uap.graph import (
    EdgeKind,
    GraphEdge,
    GraphNode,
    NodeKind,
    Port,
    PortType,
    WorkflowGraph,
)
from uap.trace import DecisionTrace, DecisionType


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _port(name: str, *, required: bool = True) -> Port:
    return Port(name=name, type=PortType.ANY, required=required)


def _edge(edge_id: str, src: str, tgt: str, *, sp: str = "out", tp: str = "in") -> GraphEdge:
    return GraphEdge(
        id=edge_id, source=src, source_port=sp, target=tgt, target_port=tp,
        kind=EdgeKind.DATA,
    )


def _linear() -> WorkflowGraph:
    """start(INPUT) -> work(AGENT) -> end(OUTPUT)."""
    return WorkflowGraph(
        id="g",
        name="g",
        nodes=[
            GraphNode(id="start", kind=NodeKind.INPUT, outputs=[_port("out")]),
            GraphNode(
                id="work", kind=NodeKind.AGENT,
                inputs=[_port("in")], outputs=[_port("out")],
            ),
            GraphNode(id="end", kind=NodeKind.OUTPUT, inputs=[_port("in")]),
        ],
        edges=[_edge("e1", "start", "work"), _edge("e2", "work", "end")],
    )


class _RecorderStub:
    """Captures traces instead of persisting them (matches DecisionRecorder.record)."""

    def __init__(self) -> None:
        self.recorded: list[DecisionTrace] = []

    def record(self, trace: DecisionTrace) -> DecisionTrace:
        self.recorded.append(trace)
        return trace


class _FakeRuntime:
    def __init__(self, handlers: dict[str, Any] | None = None) -> None:
        self.handlers = handlers or {}
        self.calls: list[str] = []

    async def run_node(self, node: GraphNode, inputs: dict, ctx) -> dict:
        self.calls.append(node.id)
        handler = self.handlers.get(node.id)
        if handler is not None:
            return handler(node, inputs, ctx)
        if node.kind is NodeKind.OUTPUT:
            return dict(inputs)
        return {"out": inputs.get("in")}


# --------------------------------------------------------------------------- #
# 1-7. propose() transaction + validation
# --------------------------------------------------------------------------- #


def test_add_node_applies_to_copy_leaving_original_unchanged():
    g = _linear()
    original_count = len(g.nodes)
    # Optional input so the lone node is valid before any edge feeds it.
    mut = GraphMutation(
        kind="add_node",
        node=GraphNode(id="extra", kind=NodeKind.TOOL,
                       inputs=[_port("in", required=False)], outputs=[_port("out")]),
        reason="add a tool",
    )
    new_graph, result = GraphMutator().propose(g, mut)
    assert result.ok and result.applied
    assert len(new_graph.nodes) == original_count + 1
    assert len(g.nodes) == original_count  # original untouched


def test_add_edge_valid():
    g = _linear()
    # add a parallel tool node (optional in) then wire start -> tool.
    g, add = GraphMutator().propose(
        g,
        GraphMutation(
            kind="add_node",
            node=GraphNode(id="tool", kind=NodeKind.TOOL,
                           inputs=[_port("in", required=False)], outputs=[_port("out")]),
            reason="node",
        ),
    )
    assert add.ok
    new_graph, result = GraphMutator().propose(
        g, GraphMutation(kind="add_edge", edge=_edge("e3", "start", "tool"), reason="wire")
    )
    assert result.ok and result.applied
    assert any(e.id == "e3" for e in new_graph.edges)


def test_add_edge_to_missing_node_rejected():
    g = _linear()
    new_graph, result = GraphMutator().propose(
        g, GraphMutation(kind="add_edge", edge=_edge("e9", "start", "ghost"), reason="x")
    )
    assert not result.ok and not result.applied
    assert result.validation_issues  # issue listed
    assert new_graph is g  # original returned on rejection


def test_remove_node_removes_its_edges():
    g = _linear()
    _, result = GraphMutator(require_reason=False).propose(
        g, GraphMutation(kind="remove_node", target_id="work", reason="")
    )
    # removing 'work' detaches both e1 and e2; the graph is now invalid
    # (end has no incoming) so it is rejected - but we can still verify the
    # edge-removal happened on the working copy by checking the issue is about
    # the dangling output, not a dangling edge.
    assert not result.ok
    codes = {i["code"] for i in result.validation_issues}
    assert "edge_unknown_node" not in codes  # edges were removed with the node


def test_remove_edge_by_id():
    g = _linear()
    # Add a side tool (optional in) and a redundant edge we can safely remove.
    g, r1 = GraphMutator().propose(
        g,
        GraphMutation(
            kind="add_node",
            node=GraphNode(id="side", kind=NodeKind.TOOL,
                           inputs=[_port("in", required=False)], outputs=[_port("out")]),
            reason="n",
        ),
    )
    assert r1.ok
    g, r2 = GraphMutator().propose(
        g, GraphMutation(kind="add_edge", edge=_edge("eside", "work", "side"), reason="w")
    )
    assert r2.ok
    new_graph, result = GraphMutator().propose(
        g, GraphMutation(kind="remove_edge", target_id="eside", reason="drop")
    )
    assert result.ok and result.applied
    assert not any(e.id == "eside" for e in new_graph.edges)


def test_mutation_without_reason_rejected():
    g = _linear()
    mut = GraphMutation(
        kind="add_node",
        node=GraphNode(id="x", kind=NodeKind.TOOL,
                       inputs=[_port("in")], outputs=[_port("out")]),
        reason="   ",
    )
    _, result = GraphMutator(require_reason=True).propose(g, mut)
    assert not result.ok and not result.applied
    assert result.reason == "mutation requires a reason"


def test_validation_failure_rejects_and_lists_issues():
    g = _linear()
    # Add an OUTPUT node with no incoming edge -> validation error.
    mut = GraphMutation(
        kind="add_node",
        node=GraphNode(id="orphan_out", kind=NodeKind.OUTPUT, inputs=[_port("in")]),
        reason="orphan",
    )
    _, result = GraphMutator().propose(g, mut)
    assert not result.ok and not result.applied
    codes = {i["code"] for i in result.validation_issues}
    assert "output_without_incoming" in codes or "missing_required_input" in codes


# --------------------------------------------------------------------------- #
# 8-9. audit + serialization
# --------------------------------------------------------------------------- #


def test_audit_writes_replan_trace():
    g = _linear()
    mut = GraphMutation(
        kind="add_node",
        node=GraphNode(id="extra", kind=NodeKind.TOOL,
                       inputs=[_port("in", required=False)], outputs=[_port("out")]),
        reason="dynamic branch",
    )
    _, result = GraphMutator().propose(g, mut)
    rec = _RecorderStub()
    trace = GraphMutator().audit(mut, result, execution_id="exec-1", recorder=rec)
    assert rec.recorded == [trace]
    assert trace.decision_type is DecisionType.REPLAN
    assert trace.execution_id == "exec-1"
    assert trace.inputs_summary["kind"] == "add_node"
    assert trace.inputs_summary["applied"] is True


def test_mutation_result_round_trips_json():
    result = MutationResult(
        ok=False, applied=False,
        validation_issues=[{"code": "x", "message": "m"}],
        reason="nope",
    )
    restored = MutationResult.model_validate_json(result.model_dump_json())
    assert restored == result


# --------------------------------------------------------------------------- #
# 10. Integration: mutated graph runs on GraphExecutor
# --------------------------------------------------------------------------- #


async def test_mutated_graph_runs_the_new_node():
    """Insert a new node between start and end, rewire, and execute it."""
    # Base graph: start -> end directly.
    base = WorkflowGraph(
        id="g", name="g",
        nodes=[
            GraphNode(id="start", kind=NodeKind.INPUT, outputs=[_port("in")]),
            GraphNode(id="end", kind=NodeKind.OUTPUT, inputs=[_port("in")]),
        ],
        edges=[_edge("e1", "start", "end", sp="in")],
    )
    mutator = GraphMutator()
    # add a transform node
    g, r1 = mutator.propose(
        base,
        GraphMutation(
            kind="add_node",
            node=GraphNode(id="inc", kind=NodeKind.TOOL,
                           inputs=[_port("in", required=False)], outputs=[_port("out")]),
            reason="insert increment",
        ),
    )
    assert r1.ok
    # Wire start->inc->end first (end briefly has two feeders), then drop e1.
    g, r2 = mutator.propose(g, GraphMutation(kind="add_edge", edge=_edge("e2", "start", "inc", sp="in"), reason="rewire"))
    assert r2.ok
    g, r3 = mutator.propose(g, GraphMutation(kind="add_edge", edge=_edge("e3", "inc", "end"), reason="rewire"))
    assert r3.ok
    g, r4 = mutator.propose(g, GraphMutation(kind="remove_edge", target_id="e1", reason="rewire"))
    assert r4.ok and r4.applied

    runtime = _FakeRuntime({"inc": lambda n, i, c: {"out": i["in"] + 1}})
    executor = GraphExecutor(runtime)
    result = await executor.execute(g, {"in": 41}, execution_id="x")

    assert result.status == "completed", result.error
    assert "inc" in runtime.calls  # the dynamically added node actually ran
    assert result.node_status["inc"] == "completed"
