"""Tests for the graph execution engine (Master sections 18-21, 35-37).

Deterministic, IO-free: a ``FakeRuntime`` stands in for the real node runtime
and a small graph-builder DSL keeps each case readable.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from uap.approval import ApprovalGate
from uap.contracts.models import ApprovalRequest
from uap.execution import (
    ExecutionResult,
    GraphExecutor,
    NodeExecutionError,
    PauseExecution,
    evaluate,
)
from uap.graph import (
    EdgeKind,
    FanInPolicy,
    GraphEdge,
    GraphNode,
    NodeKind,
    Port,
    PortType,
    WorkflowGraph,
)

# --------------------------------------------------------------------------- #
# Graph-builder DSL
# --------------------------------------------------------------------------- #


def port(name: str, type_: PortType = PortType.ANY, required: bool = True) -> Port:
    return Port(name=name, type=type_, required=required)


def input_node(node_id: str, out_name: str = "out", inputs: list[Port] | None = None) -> GraphNode:
    return GraphNode(
        id=node_id, kind=NodeKind.INPUT, inputs=inputs or [], outputs=[port(out_name)]
    )


def output_node(node_id: str, in_name: str = "in") -> GraphNode:
    return GraphNode(id=node_id, kind=NodeKind.OUTPUT, inputs=[port(in_name)])


def work_node(node_id: str, kind: NodeKind = NodeKind.AGENT) -> GraphNode:
    return GraphNode(
        id=node_id, kind=kind, inputs=[port("in")], outputs=[port("out")]
    )


def data_edge(edge_id: str, source: str, target: str, *, sp: str = "out", tp: str = "in") -> GraphEdge:
    return GraphEdge(
        id=edge_id, source=source, source_port=sp, target=target, target_port=tp,
        kind=EdgeKind.DATA,
    )


def control_edge(
    edge_id: str, source: str, target: str, *, sp: str = "out", tp: str = "in",
    condition: str | None = None,
) -> GraphEdge:
    return GraphEdge(
        id=edge_id, source=source, source_port=sp, target=target, target_port=tp,
        kind=EdgeKind.CONTROL, condition=condition,
    )


def error_edge(edge_id: str, source: str, target: str, *, sp: str = "out", tp: str = "in") -> GraphEdge:
    return GraphEdge(
        id=edge_id, source=source, source_port=sp, target=target, target_port=tp,
        kind=EdgeKind.ERROR,
    )


def graph(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    graph_id: str = "g",
    fan_in_policy: FanInPolicy | None = None,
    min_success: int | None = None,
    timeout_s: float | None = None,
    subworkflow_depth: int = 0,
) -> WorkflowGraph:
    return WorkflowGraph(
        id=graph_id, name=graph_id, nodes=nodes, edges=edges,
        fan_in_policy=fan_in_policy, min_success=min_success,
        timeout_s=timeout_s, subworkflow_depth=subworkflow_depth,
    )


# --------------------------------------------------------------------------- #
# FakeRuntime
# --------------------------------------------------------------------------- #


class FakeRuntime:
    """Maps node id -> async handler ``(node, inputs, ctx) -> {port: value}``."""

    def __init__(self, handlers: dict[str, Any] | None = None) -> None:
        self.handlers = handlers or {}
        self.calls: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0

    async def run_node(self, node: GraphNode, inputs: dict[str, Any], ctx) -> dict[str, Any]:
        self.calls.append(node.id)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            handler = self.handlers.get(node.id)
            if handler is None:
                if node.kind is NodeKind.OUTPUT:
                    return dict(inputs)
                return {"out": inputs.get("in")}
            result = handler(node, inputs, ctx)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        finally:
            self.concurrent -= 1


async def _sleep_then(seconds: float, value: Any) -> dict[str, Any]:
    await asyncio.sleep(seconds)
    return {"out": value}


def _all_scalars(obj: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        for value in obj.values():
            found.extend(_all_scalars(value))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(_all_scalars(value))
    else:
        found.append(obj)
    return found


# --------------------------------------------------------------------------- #
# 1-3. Linear + parallel scheduling
# --------------------------------------------------------------------------- #


def _linear_graph() -> WorkflowGraph:
    return graph(
        [input_node("start"), work_node("a"), work_node("b"), output_node("end")],
        [
            data_edge("e1", "start", "a"),
            data_edge("e2", "a", "b"),
            data_edge("e3", "b", "end"),
        ],
    )


async def test_linear_graph_completes_with_correct_outputs():
    runtime = FakeRuntime(
        {
            "a": lambda node, inputs, ctx: {"out": inputs["in"] + 1},
            "b": lambda node, inputs, ctx: {"out": inputs["in"] * 2},
        }
    )
    executor = GraphExecutor(runtime)
    result = await executor.execute(_linear_graph(), {"out": 10}, execution_id="x")

    assert result.status == "completed"
    assert result.error is None
    assert result.outputs["end"] == {"in": 22}
    assert result.order == ["start", "a", "b", "end"]
    assert result.node_status["end"] == "completed"


def _fanout_graph() -> WorkflowGraph:
    return graph(
        [
            input_node("start"),
            work_node("p1"),
            work_node("p2"),
            output_node("o1"),
            output_node("o2"),
        ],
        [
            data_edge("e1", "start", "p1"),
            data_edge("e2", "start", "p2"),
            data_edge("e3", "p1", "o1"),
            data_edge("e4", "p2", "o2"),
        ],
    )


async def test_parallel_branches_run_concurrently():
    runtime = FakeRuntime(
        {
            "p1": lambda node, inputs, ctx: _sleep_then(0.05, 1),
            "p2": lambda node, inputs, ctx: _sleep_then(0.05, 2),
        }
    )
    executor = GraphExecutor(runtime, max_parallel=8)
    started = time.perf_counter()
    result = await executor.execute(_fanout_graph(), {"out": 0}, execution_id="x")
    elapsed = time.perf_counter() - started

    assert result.status == "completed"
    assert result.node_status["p1"] == "completed"
    assert result.node_status["p2"] == "completed"
    assert runtime.max_concurrent == 2
    assert elapsed < 0.09, f"branches did not overlap: {elapsed:.3f}s"


async def test_max_parallel_one_runs_sequentially():
    runtime = FakeRuntime(
        {
            "p1": lambda node, inputs, ctx: _sleep_then(0.05, 1),
            "p2": lambda node, inputs, ctx: _sleep_then(0.05, 2),
        }
    )
    executor = GraphExecutor(runtime, max_parallel=1)
    started = time.perf_counter()
    result = await executor.execute(_fanout_graph(), {"out": 0}, execution_id="x")
    elapsed = time.perf_counter() - started

    assert result.status == "completed"
    assert runtime.max_concurrent == 1
    assert elapsed >= 0.1, f"branches overlapped: {elapsed:.3f}s"


# --------------------------------------------------------------------------- #
# 4-6. Condition nodes
# --------------------------------------------------------------------------- #


def _condition_graph(*, default: bool, conditions: tuple[str, ...] = ("inputs.flag == true", "inputs.flag == false")) -> WorkflowGraph:
    cond = GraphNode(
        id="cond",
        kind=NodeKind.CONDITION,
        inputs=[port("in", PortType.CONTROL)],
        outputs=[port("yes", PortType.CONTROL), port("no", PortType.CONTROL)],
    )
    nodes = [input_node("start"), cond]
    edges = [control_edge("cin", "start", "cond", tp="in")]
    branch_ids = ["yes_branch", "no_branch"]
    for index, condition in enumerate(conditions):
        branch_id = branch_ids[index]
        nodes.append(work_node(branch_id, NodeKind.TOOL))
        edges.append(
            control_edge(
                f"cout{index}", "cond", branch_id,
                sp="yes" if index == 0 else "no", tp="in", condition=condition,
            )
        )
    if default:
        nodes.append(work_node("default_branch", NodeKind.TOOL))
        edges.append(control_edge("cdef", "cond", "default_branch", sp="yes", tp="in", condition="default"))
    return graph(nodes, edges, graph_id="g-cond")


async def test_condition_true_branch_routes_and_other_is_skipped():
    decisions: list[dict] = []
    executor = GraphExecutor(FakeRuntime())
    result = await executor.execute(
        _condition_graph(default=False), {"flag": True},
        execution_id="x", decision_sink=decisions.append,
    )

    assert result.status == "completed"
    assert result.node_status["yes_branch"] == "completed"
    assert result.node_status["no_branch"] == "skipped"
    assert decisions and decisions[0]["decision_type"] == "condition"
    assert decisions[0]["chosen"] == "cout0"
    assert "cout1" in decisions[0]["alternatives"]


async def test_condition_no_match_and_no_default_fails_with_reason():
    runtime = FakeRuntime()
    executor = GraphExecutor(runtime)
    g = _condition_graph(default=False, conditions=("inputs.flag == true", "inputs.missing == true"))
    result = await executor.execute(g, {"flag": False}, execution_id="x")

    assert result.status == "failed"
    assert "no branch matched" in (result.error or "")
    assert result.node_status["cond"] == "failed"


async def test_condition_default_branch_taken():
    decisions: list[dict] = []
    executor = GraphExecutor(FakeRuntime())
    result = await executor.execute(
        _condition_graph(default=True, conditions=("inputs.flag == true", "inputs.other == true")),
        {"flag": False},
        execution_id="x",
        decision_sink=decisions.append,
    )

    assert result.status == "completed"
    assert result.node_status["default_branch"] == "completed"
    assert decisions[0]["chosen"] == "default"


# --------------------------------------------------------------------------- #
# 7-10. JOIN fan-in policies
# --------------------------------------------------------------------------- #


def _join_graph(policy: FanInPolicy, *, min_success: int | None = None, timeout_s: float | None = None) -> WorkflowGraph:
    join = GraphNode(id="join", kind=NodeKind.JOIN, inputs=[port("in")], outputs=[port("out")])
    return graph(
        [input_node("start"), work_node("p1"), work_node("p2"), join, output_node("end")],
        [
            data_edge("e1", "start", "p1"),
            data_edge("e2", "start", "p2"),
            data_edge("e3", "p1", "join"),
            data_edge("e4", "p2", "join"),
            data_edge("e5", "join", "end"),
        ],
        graph_id="g-join",
        fan_in_policy=policy,
        min_success=min_success,
        timeout_s=timeout_s,
    )


def _fail(node, inputs, ctx):
    raise NodeExecutionError("boom")


async def test_join_require_all_fails_when_one_branch_fails():
    runtime = FakeRuntime({"p1": _fail, "p2": lambda n, i, c: {"out": 2}})
    executor = GraphExecutor(runtime)
    result = await executor.execute(_join_graph(FanInPolicy.REQUIRE_ALL), {"out": 0}, execution_id="x")

    assert result.status == "failed"
    assert result.node_status["join"] == "failed"
    assert "require_all" in (result.error or "")


async def test_join_allow_partial_completes_with_successes():
    runtime = FakeRuntime(
        {"p1": _fail, "p2": lambda n, i, c: {"out": 2}, "join": lambda n, i, c: dict(i)}
    )
    executor = GraphExecutor(runtime)
    result = await executor.execute(_join_graph(FanInPolicy.ALLOW_PARTIAL), {"out": 0}, execution_id="x")

    assert result.status == "completed"
    assert result.node_status["join"] == "completed"
    assert result.node_results["join"] == {"in": 2}


async def test_join_min_success_requires_threshold():
    failing = FakeRuntime({"p1": _fail, "p2": lambda n, i, c: {"out": 2}})
    result = await GraphExecutor(failing).execute(
        _join_graph(FanInPolicy.MIN_SUCCESS, min_success=2), {"out": 0}, execution_id="x"
    )
    assert result.status == "failed"
    assert result.node_status["join"] == "failed"

    succeeding = FakeRuntime({"p1": lambda n, i, c: {"out": 1}, "p2": lambda n, i, c: {"out": 2}})
    result_ok = await GraphExecutor(succeeding).execute(
        _join_graph(FanInPolicy.MIN_SUCCESS, min_success=2), {"out": 0}, execution_id="x"
    )
    assert result_ok.status == "completed"
    assert result_ok.node_status["join"] == "completed"


async def test_join_timeout_proceeds_with_arrived_branches():
    runtime = FakeRuntime(
        {
            "p1": lambda n, i, c: _sleep_then(0.01, 1),
            "p2": lambda n, i, c: _sleep_then(0.4, 2),
            "join": lambda n, i, c: dict(i),
        }
    )
    executor = GraphExecutor(runtime)
    started = time.perf_counter()
    result = await executor.execute(
        _join_graph(FanInPolicy.TIMEOUT, timeout_s=0.05), {"out": 0}, execution_id="x"
    )
    elapsed = time.perf_counter() - started

    assert result.status == "completed"
    assert result.node_status["join"] == "completed"
    assert result.node_status["p2"] == "skipped"  # abandoned after the deadline
    assert result.node_results["join"] == {"in": 1}  # best-effort: arrived branches only
    assert elapsed < 0.3, f"join waited past its deadline: {elapsed:.3f}s"


# --------------------------------------------------------------------------- #
# 11-12. Subworkflows
# --------------------------------------------------------------------------- #


def _sub_graph(graph_id: str, ref: str, *, depth: int) -> WorkflowGraph:
    sub = GraphNode(
        id="sub", kind=NodeKind.SUBWORKFLOW,
        inputs=[port("in")], outputs=[port("out")],
        config={"workflow_ref": ref},
    )
    return graph(
        [input_node("start", "in"), sub, output_node("end")],
        [data_edge("e1", "start", "sub", sp="in"), data_edge("e2", "sub", "end")],
        graph_id=graph_id, subworkflow_depth=depth,
    )


async def test_subworkflow_nested_execution_threads_outputs():
    leaf = graph(
        [input_node("lin", "in"), work_node("work"), output_node("lout")],
        [data_edge("l1", "lin", "work", sp="in"), data_edge("l2", "work", "lout")],
        graph_id="leaf",
    )
    mid = _sub_graph("mid", "leaf@v1", depth=1)

    def resolver(ref: str) -> WorkflowGraph:
        return {"leaf@v1": leaf, "mid@v1": mid}[ref]

    runtime = FakeRuntime({"work": lambda n, i, c: {"out": i["in"] + 1}})
    executor = GraphExecutor(runtime, max_subworkflow_depth=3)
    result = await executor.execute(
        _sub_graph("parent", "mid@v1", depth=0), {"in": 10},
        execution_id="x", subworkflow_resolver=resolver,
    )

    assert result.status == "completed", result.error
    assert result.node_status["sub"] == "completed"
    assert "work" in runtime.calls  # the leaf node actually ran
    assert 11 in _all_scalars(result.outputs["end"])  # threaded through two levels


async def test_subworkflow_depth_exceeded_fails():
    leaf = _sub_graph("leaf", "leaf@v1", depth=2)
    mid = _sub_graph("mid", "leaf@v1", depth=1)

    def resolver(ref: str) -> WorkflowGraph:
        return {"leaf@v1": leaf, "mid@v1": mid}[ref]

    executor = GraphExecutor(FakeRuntime(), max_subworkflow_depth=1)
    result = await executor.execute(
        _sub_graph("parent", "mid@v1", depth=0), {"in": 1},
        execution_id="x", subworkflow_resolver=resolver,
    )

    assert result.status == "failed"
    assert "depth" in (result.error or "").lower()


# --------------------------------------------------------------------------- #
# 13. Approval pause + resume
# --------------------------------------------------------------------------- #


def _approval_graph() -> WorkflowGraph:
    approval = GraphNode(
        id="approve", kind=NodeKind.APPROVAL,
        inputs=[port("in")], outputs=[port("out")],
    )
    return graph(
        [input_node("start"), approval, output_node("end")],
        [data_edge("e1", "start", "approve"), data_edge("e2", "approve", "end")],
        graph_id="g-approval",
    )


async def test_approval_pauses_then_resumes_without_rerunning_node():
    gate = ApprovalGate()
    request = gate.request("task-1", "publish")

    async def approval_handler(node, inputs, ctx):
        raise PauseExecution(request)

    runtime = FakeRuntime({"approve": approval_handler})
    executor = GraphExecutor(runtime)

    paused = await executor.execute(_approval_graph(), {"out": 1}, execution_id="x", approval_gate=gate)
    assert paused.status == "paused"
    assert paused.pending_approval is not None
    assert paused.pending_approval.approval_id == request.approval_id
    assert paused.node_status["approve"] == "paused"

    gate.decide(request.approval_id, approved=True, decided_by="tester")
    resumed = await executor.execute(
        _approval_graph(), {"out": 1}, execution_id="x",
        approval_gate=gate, resume_state=paused,
    )

    assert resumed.status == "completed"
    assert resumed.node_status["approve"] == "completed"
    assert resumed.node_results["approve"]["approval"].approval_id == request.approval_id
    assert runtime.calls.count("approve") == 1  # never re-run on resume


async def test_approval_resume_rejected_fails():
    gate = ApprovalGate()
    request = gate.request("task-1", "publish")

    async def approval_handler(node, inputs, ctx):
        raise PauseExecution(request)

    runtime = FakeRuntime({"approve": approval_handler})
    executor = GraphExecutor(runtime)
    paused = await executor.execute(_approval_graph(), {"out": 1}, execution_id="x", approval_gate=gate)

    gate.decide(request.approval_id, approved=False, decided_by="tester")
    resumed = await executor.execute(
        _approval_graph(), {"out": 1}, execution_id="x",
        approval_gate=gate, resume_state=paused,
    )
    assert resumed.status == "failed"
    assert "rejected" in (resumed.error or "")


# --------------------------------------------------------------------------- #
# 14. Error routing
# --------------------------------------------------------------------------- #


def _error_graph(with_error_edge: bool) -> WorkflowGraph:
    nodes = [input_node("start"), work_node("a"), output_node("end")]
    edges = [data_edge("e1", "start", "a"), data_edge("e2", "a", "end")]
    if with_error_edge:
        nodes.append(work_node("handler", NodeKind.TOOL))
        edges.append(error_edge("ee", "a", "handler"))
    return graph(nodes, edges, graph_id="g-err")


async def test_node_failure_with_error_edge_continues():
    runtime = FakeRuntime({"a": _fail, "handler": lambda n, i, c: {"out": "recovered"}})
    executor = GraphExecutor(runtime)
    result = await executor.execute(_error_graph(True), {"out": 0}, execution_id="x")

    assert result.status == "completed"
    assert result.node_status["a"] == "failed"
    assert result.node_status["handler"] == "completed"
    assert result.node_status["end"] == "skipped"


async def test_node_failure_without_error_edge_fails_execution():
    runtime = FakeRuntime({"a": _fail})
    executor = GraphExecutor(runtime)
    result = await executor.execute(_error_graph(False), {"out": 0}, execution_id="x")

    assert result.status == "failed"
    assert result.node_status["a"] == "failed"
    assert "boom" in (result.error or "")


# --------------------------------------------------------------------------- #
# 15-17. Validation, budget, events
# --------------------------------------------------------------------------- #


async def test_invalid_graph_fails_before_running_anything():
    duplicate = GraphNode(id="dup", kind=NodeKind.AGENT)
    g = graph(
        [input_node("start"), duplicate, GraphNode(id="dup", kind=NodeKind.AGENT), output_node("end")],
        [data_edge("e1", "start", "dup"), data_edge("e2", "dup", "end")],
    )
    runtime = FakeRuntime()
    executor = GraphExecutor(runtime)
    result = await executor.execute(g, {"out": 1}, execution_id="x")

    assert result.status == "failed"
    assert "duplicate_node_id" in (result.error or "")
    assert runtime.calls == []


async def test_missing_required_input_fails():
    node = input_node("start", "out", [port("required_in", required=True)])
    g = graph([node, output_node("end")], [data_edge("e1", "start", "end")])
    result = await GraphExecutor(FakeRuntime()).execute(g, {}, execution_id="x")

    assert result.status == "failed"
    assert "required_in" in (result.error or "")


async def test_max_steps_budget_exhausted():
    executor = GraphExecutor(FakeRuntime(), max_steps=2)
    result = await executor.execute(_linear_graph(), {"out": 1}, execution_id="x")

    assert result.status == "failed"
    assert "step budget exhausted" in (result.error or "")


async def test_event_sink_receives_transition_events():
    events: list[tuple[str, dict]] = []
    executor = GraphExecutor(FakeRuntime())
    await executor.execute(
        _linear_graph(), {"out": 1}, execution_id="x",
        event_sink=lambda kind, payload: events.append((kind, payload)),
    )

    kinds = [kind for kind, _ in events]
    assert kinds == [
        "node_started", "node_finished",
        "node_started", "node_finished",
        "node_started", "node_finished",
        "node_started", "node_finished",
        "execution_completed",
    ]
    assert events[-1][1]["status"] == "completed"
    assert events[1][1]["node_id"] == "start"


# --------------------------------------------------------------------------- #
# 18. Condition evaluator unit tests
# --------------------------------------------------------------------------- #


_SCOPE = {
    "inputs": {"count": 5, "flag": True, "name": "uap"},
    "node": {"plan": {"confidence": 0.9, "tags": ["a", "b"]}},
}


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("inputs.count == 5", True),
        ("inputs.count != 4", True),
        ("inputs.count > 4", True),
        ("inputs.count >= 5", True),
        ("inputs.count < 6", True),
        ("inputs.count <= 5", True),
        ("inputs.count > 5", False),
        ("inputs.flag == true", True),
        ("inputs.flag == false", False),
        ("true", True),
        ("false", False),
        ("node.plan.confidence > 0.5", True),
        ("node.plan.confidence < 0.5", False),
        ("inputs.count in [1, 3, 5]", True),
        ("inputs.count not in [1, 3, 5]", False),
        ("inputs.name == 'uap'", True),
        ('inputs.name == "other"', False),
    ],
)
def test_evaluate_operators(expression, expected):
    result, _ = evaluate(expression, _SCOPE)
    assert result is expected


def test_evaluate_missing_path_returns_false_with_reason():
    result, reason = evaluate("inputs.nope == 1", _SCOPE)
    assert result is False
    assert reason == "missing path: inputs.nope"


@pytest.mark.parametrize(
    "expression",
    ["", "inputs.count", "inputs.count >", "inputs.count >> 3", "inputs.count ==== 3"],
)
def test_evaluate_invalid_expression_never_raises(expression):
    result, reason = evaluate(expression, _SCOPE)
    assert result is False
    assert "invalid expression" in reason


def test_evaluate_unknown_operator():
    result, reason = evaluate("inputs.count ~= 5", _SCOPE)
    assert result is False
    assert "unknown operator" in reason


def test_evaluate_type_mismatch_is_false():
    result, reason = evaluate("inputs.name > 3", _SCOPE)
    assert result is False
    assert "type mismatch" in reason
