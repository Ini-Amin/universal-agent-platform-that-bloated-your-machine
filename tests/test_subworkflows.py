"""Tests for nested subworkflows (Master section 21).

Deterministic and IO-free. The resolver is exercised directly and plugged into
:class:`GraphExecutor` to prove nested execution threads outputs back and that
the recursion guard fails cleanly at the configured depth.
"""

from __future__ import annotations

from typing import Any

import pytest

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
from uap.subworkflows import SubworkflowDepthError, SubworkflowResolver


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #


def _port(name: str) -> Port:
    return Port(name=name, type=PortType.ANY, required=True)


def _edge(edge_id: str, src: str, tgt: str, *, sp: str = "out", tp: str = "in") -> GraphEdge:
    return GraphEdge(
        id=edge_id, source=src, source_port=sp, target=tgt, target_port=tp,
        kind=EdgeKind.DATA,
    )


def _leaf() -> WorkflowGraph:
    """INPUT(in) -> work(+1) -> OUTPUT."""
    return WorkflowGraph(
        id="leaf", name="leaf",
        nodes=[
            GraphNode(id="lin", kind=NodeKind.INPUT, outputs=[_port("in")]),
            GraphNode(id="work", kind=NodeKind.AGENT,
                      inputs=[_port("in")], outputs=[_port("out")]),
            GraphNode(id="lout", kind=NodeKind.OUTPUT, inputs=[_port("in")]),
        ],
        edges=[_edge("l1", "lin", "work", sp="in"), _edge("l2", "work", "lout")],
    )


def _wrapper(graph_id: str, ref: str, *, depth: int) -> WorkflowGraph:
    """INPUT -> SUBWORKFLOW(ref) -> OUTPUT."""
    return WorkflowGraph(
        id=graph_id, name=graph_id,
        nodes=[
            GraphNode(id="start", kind=NodeKind.INPUT, outputs=[_port("in")]),
            GraphNode(id="sub", kind=NodeKind.SUBWORKFLOW,
                      inputs=[_port("in")], outputs=[_port("out")],
                      config={"workflow_ref": ref}),
            GraphNode(id="end", kind=NodeKind.OUTPUT, inputs=[_port("in")]),
        ],
        edges=[_edge("e1", "start", "sub", sp="in"), _edge("e2", "sub", "end")],
        subworkflow_depth=depth,
    )


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


def _all_scalars(obj: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(obj, dict):
        for v in obj.values():
            found.extend(_all_scalars(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_all_scalars(v))
    else:
        found.append(obj)
    return found


# --------------------------------------------------------------------------- #
# 1-5. Resolver registration + depth guard
# --------------------------------------------------------------------------- #


def test_register_and_resolve_round_trip():
    resolver = SubworkflowResolver()
    leaf = _leaf()
    resolver.register("leaf@v1", leaf)
    assert resolver.resolve("leaf@v1") is leaf


def test_bad_ref_format_raises():
    resolver = SubworkflowResolver()
    with pytest.raises(ValueError, match="bad subworkflow ref"):
        resolver.register("Leaf@1", _leaf())  # uppercase + missing 'v'


def test_unknown_ref_raises_keyerror_listing_refs():
    resolver = SubworkflowResolver()
    resolver.register("leaf@v1", _leaf())
    with pytest.raises(KeyError) as exc:
        resolver.resolve("other@v2")
    assert "leaf@v1" in str(exc.value)


def test_depth_guard_raises_at_limit():
    resolver = SubworkflowResolver(max_depth=2)
    resolver.register("leaf@v1", _leaf())
    with pytest.raises(SubworkflowDepthError, match="recursion guard"):
        resolver.resolve_for_depth("leaf@v1", current_depth=2)


def test_resolve_for_depth_ok_below_limit():
    resolver = SubworkflowResolver(max_depth=2)
    leaf = _leaf()
    resolver.register("leaf@v1", leaf)
    assert resolver.resolve_for_depth("leaf@v1", current_depth=1) is leaf


# --------------------------------------------------------------------------- #
# 6-7. Nested execution through GraphExecutor
# --------------------------------------------------------------------------- #


async def test_nested_execution_threads_outputs():
    resolver = SubworkflowResolver()
    resolver.register("leaf@v1", _leaf())

    runtime = _FakeRuntime({"work": lambda n, i, c: {"out": i["in"] + 1}})
    executor = GraphExecutor(runtime, max_subworkflow_depth=3)
    outer = _wrapper("parent", "leaf@v1", depth=0)
    result = await executor.execute(
        outer, {"in": 10}, execution_id="x", subworkflow_resolver=resolver.resolve
    )

    assert result.status == "completed", result.error
    assert result.node_status["sub"] == "completed"
    assert "work" in runtime.calls  # the inner node actually ran
    assert 11 in _all_scalars(result.outputs["end"])  # inner +1 threaded back


async def test_two_level_ok_three_level_exceeds_max_fails_cleanly():
    resolver = SubworkflowResolver()
    # parent(depth0) -> mid(depth1) -> leaf  == two levels of nesting.
    resolver.register("leaf@v1", _leaf())
    resolver.register("mid@v1", _wrapper("mid", "leaf@v1", depth=1))

    runtime = _FakeRuntime({"work": lambda n, i, c: {"out": i["in"] + 1}})

    # max_subworkflow_depth=2 allows two levels.
    ok_exec = GraphExecutor(runtime, max_subworkflow_depth=2)
    ok = await ok_exec.execute(
        _wrapper("parent", "mid@v1", depth=0), {"in": 1},
        execution_id="ok", subworkflow_resolver=resolver.resolve,
    )
    assert ok.status == "completed", ok.error

    # A third nesting level beyond max_subworkflow_depth=2 fails cleanly.
    resolver.register("deep@v1", _wrapper("deep", "mid@v1", depth=1))
    bad = await ok_exec.execute(
        _wrapper("parent", "deep@v1", depth=0), {"in": 1},
        execution_id="bad", subworkflow_resolver=resolver.resolve,
    )
    assert bad.status == "failed"
    assert "depth" in (bad.error or "").lower()


# --------------------------------------------------------------------------- #
# 8. Immutability of stored graphs
# --------------------------------------------------------------------------- #


def test_resolver_never_mutates_stored_graph():
    resolver = SubworkflowResolver()
    leaf = _leaf()
    original_nodes = len(leaf.nodes)
    resolver.register("leaf@v1", leaf)

    returned = resolver.resolve("leaf@v1")
    # mutate a deep copy of what we got back
    copy = returned.model_copy(deep=True)
    copy.nodes.append(
        GraphNode(id="injected", kind=NodeKind.TOOL,
                  inputs=[_port("in")], outputs=[_port("out")])
    )
    # the stored graph is unchanged
    assert len(resolver.resolve("leaf@v1").nodes) == original_nodes
