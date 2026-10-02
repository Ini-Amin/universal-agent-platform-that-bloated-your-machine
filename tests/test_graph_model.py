"""Tests for the canonical workflow graph (Master sections 18, 21, 36, 55).

Covers the data model round-trip + content hash, the port type system and every
structural validation rule. Pure data tests: no IO, no network.
"""

import pytest

from uap.graph import (
    EdgeKind,
    FanInPolicy,
    GraphEdge,
    GraphNode,
    GraphValidationError,
    GraphValidator,
    NodeKind,
    Port,
    PortType,
    WorkflowGraph,
    port_type_compatible,
    type_compatibility_matrix,
    validate_graph,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def port(name: str, type_: PortType = PortType.ANY, required: bool = True) -> Port:
    return Port(name=name, type=type_, required=required)


def input_node(node_id: str, out_type: PortType = PortType.ANY) -> GraphNode:
    return GraphNode(id=node_id, kind=NodeKind.INPUT, outputs=[port("out", out_type)])


def output_node(node_id: str, in_type: PortType = PortType.ANY) -> GraphNode:
    return GraphNode(id=node_id, kind=NodeKind.OUTPUT, inputs=[port("in", in_type)])


def data_edge(edge_id: str, source: str, target: str) -> GraphEdge:
    return GraphEdge(
        id=edge_id,
        source=source,
        source_port="out",
        target=target,
        target_port="in",
        kind=EdgeKind.DATA,
    )


def minimal_graph() -> WorkflowGraph:
    """INPUT -> OUTPUT, valid on every rule."""
    return WorkflowGraph(
        id="g-min",
        name="Minimal",
        nodes=[input_node("start", PortType.TEXT), output_node("end", PortType.TEXT)],
        edges=[data_edge("e1", "start", "end")],
    )


def error_codes(graph: WorkflowGraph, **kwargs) -> list[str]:
    return [
        issue.code
        for issue in GraphValidator(**kwargs).validate(graph)
        if issue.severity == "error"
    ]


def all_codes(graph: WorkflowGraph, **kwargs) -> list[str]:
    return [issue.code for issue in GraphValidator(**kwargs).validate(graph)]


# --------------------------------------------------------------------------- #
# 1. Minimal valid graph
# --------------------------------------------------------------------------- #


def test_minimal_valid_graph_validates_clean():
    issues = validate_graph(minimal_graph())
    assert issues == []


# --------------------------------------------------------------------------- #
# 2. Duplicate node id
# --------------------------------------------------------------------------- #


def test_duplicate_node_id_is_error():
    graph = WorkflowGraph(
        id="g-dup",
        name="Dup",
        nodes=[input_node("dup"), input_node("dup")],
        edges=[],
    )
    assert "duplicate_node_id" in error_codes(graph)


# --------------------------------------------------------------------------- #
# 3. Edge to missing node
# --------------------------------------------------------------------------- #


def test_edge_to_missing_node_is_error():
    graph = minimal_graph()
    graph.edges[0].source = "ghost"
    assert "edge_unknown_node" in error_codes(graph)


# --------------------------------------------------------------------------- #
# 4. Edge to missing port
# --------------------------------------------------------------------------- #


def test_edge_to_missing_port_is_error():
    graph = minimal_graph()
    graph.edges[0].source_port = "does_not_exist"
    codes = error_codes(graph)
    assert "edge_unknown_port" in codes


# --------------------------------------------------------------------------- #
# 5. Port type compatibility
# --------------------------------------------------------------------------- #


def test_incompatible_port_types_is_error():
    graph = WorkflowGraph(
        id="g-badtype",
        name="BadType",
        nodes=[input_node("a", PortType.EVIDENCE), output_node("b", PortType.CONTROL)],
        edges=[data_edge("e", "a", "b")],
    )
    assert "incompatible_port_type" in error_codes(graph)


@pytest.mark.parametrize(
    "source_type,target_type",
    [
        (PortType.ANY, PortType.CONTROL),
        (PortType.TEXT, PortType.JSON),
        (PortType.EVIDENCE, PortType.JSON),
        (PortType.ARTIFACT, PortType.JSON),
        (PortType.JSON, PortType.TEXT),
    ],
)
def test_compatible_port_pairs_are_clean(source_type, target_type):
    graph = WorkflowGraph(
        id="g-oktype",
        name="OkType",
        nodes=[input_node("a", source_type), output_node("b", target_type)],
        edges=[data_edge("e", "a", "b")],
    )
    assert error_codes(graph) == []


# --------------------------------------------------------------------------- #
# 6. Required input without incoming edge
# --------------------------------------------------------------------------- #


def test_required_input_without_incoming_edge_is_error():
    tool = GraphNode(id="tool", kind=NodeKind.TOOL, inputs=[port("in", PortType.TEXT)])
    graph = WorkflowGraph(id="g-req", name="Req", nodes=[tool], edges=[])
    assert "missing_required_input" in error_codes(graph)


def test_input_kind_node_is_exempt_from_required_inputs():
    node = GraphNode(
        id="src",
        kind=NodeKind.INPUT,
        inputs=[port("in", PortType.TEXT)],
        outputs=[port("out", PortType.TEXT)],
    )
    graph = WorkflowGraph(id="g-src", name="Src", nodes=[node], edges=[])
    assert "missing_required_input" not in error_codes(graph)


def test_optional_input_without_incoming_edge_is_clean():
    node = GraphNode(
        id="tool",
        kind=NodeKind.TOOL,
        inputs=[port("in", PortType.TEXT, required=False)],
    )
    graph = WorkflowGraph(id="g-opt", name="Opt", nodes=[node], edges=[])
    assert "missing_required_input" not in error_codes(graph)


# --------------------------------------------------------------------------- #
# 7. OUTPUT without incoming
# --------------------------------------------------------------------------- #


def test_output_without_incoming_edge_is_error():
    graph = WorkflowGraph(
        id="g-out",
        name="Out",
        nodes=[output_node("end")],
        edges=[],
    )
    assert "output_without_incoming" in error_codes(graph)


# --------------------------------------------------------------------------- #
# 8. CONDITION control edges
# --------------------------------------------------------------------------- #


def _condition_graph(branches: int) -> WorkflowGraph:
    cond = GraphNode(
        id="cond",
        kind=NodeKind.CONDITION,
        inputs=[port("in", PortType.CONTROL)],
        outputs=[port("yes", PortType.CONTROL), port("no", PortType.CONTROL)],
    )
    nodes = [input_node("start", PortType.CONTROL), cond]
    edges = [
        GraphEdge(
            id="cin",
            source="start",
            source_port="out",
            target="cond",
            target_port="in",
            kind=EdgeKind.CONTROL,
        )
    ]
    for index in range(branches):
        branch = GraphNode(
            id=f"branch{index}",
            kind=NodeKind.TOOL,
            inputs=[port("in", PortType.CONTROL)],
        )
        nodes.append(branch)
        edges.append(
            GraphEdge(
                id=f"cout{index}",
                source="cond",
                source_port="yes" if index == 0 else "no",
                target=branch.id,
                target_port="in",
                kind=EdgeKind.CONTROL,
                condition="yes" if index == 0 else "no",
            )
        )
    return WorkflowGraph(id="g-cond", name="Cond", nodes=nodes, edges=edges)


def test_condition_with_one_control_out_is_error():
    assert "condition_control_out" in error_codes(_condition_graph(1))


def test_condition_with_two_control_out_is_clean():
    assert error_codes(_condition_graph(2)) == []


# --------------------------------------------------------------------------- #
# 9. JOIN requires fan_in_policy
# --------------------------------------------------------------------------- #


def _join_graph(policy: FanInPolicy | None) -> WorkflowGraph:
    join = GraphNode(
        id="join",
        kind=NodeKind.JOIN,
        inputs=[port("in", PortType.TEXT)],
        outputs=[port("out", PortType.TEXT)],
    )
    return WorkflowGraph(
        id="g-join",
        name="Join",
        nodes=[input_node("start", PortType.TEXT), join, output_node("end", PortType.TEXT)],
        edges=[data_edge("e1", "start", "join"), data_edge("e2", "join", "end")],
        fan_in_policy=policy,
    )


def test_join_without_fan_in_policy_is_error():
    assert "join_missing_fan_in_policy" in error_codes(_join_graph(None))


def test_join_with_fan_in_policy_is_clean():
    assert error_codes(_join_graph(FanInPolicy.REQUIRE_ALL)) == []


# --------------------------------------------------------------------------- #
# 10. SUBWORKFLOW exact version reference
# --------------------------------------------------------------------------- #


def _subworkflow_graph(config: dict, depth: int = 0) -> WorkflowGraph:
    sub = GraphNode(
        id="sub",
        kind=NodeKind.SUBWORKFLOW,
        inputs=[port("in", PortType.ANY)],
        outputs=[port("out", PortType.ANY)],
        config=config,
    )
    return WorkflowGraph(
        id="g-sub",
        name="Sub",
        nodes=[input_node("start"), sub],
        edges=[data_edge("e1", "start", "sub")],
        subworkflow_depth=depth,
    )


def test_subworkflow_without_ref_is_error():
    assert "subworkflow_missing_ref" in error_codes(_subworkflow_graph({}))


def test_subworkflow_without_exact_version_is_error():
    assert "subworkflow_bad_ref" in error_codes(
        _subworkflow_graph({"workflow_ref": "recon"})
    )


def test_subworkflow_with_exact_version_is_clean():
    assert error_codes(_subworkflow_graph({"workflow_ref": "recon@v3"})) == []


# --------------------------------------------------------------------------- #
# 11. Subworkflow recursion guard
# --------------------------------------------------------------------------- #


def test_subworkflow_depth_exceeded_is_error():
    graph = _subworkflow_graph({"workflow_ref": "recon@v3"}, depth=1)
    codes = error_codes(graph, max_subworkflow_depth=1)
    assert "subworkflow_depth_exceeded" in codes


def test_subworkflow_depth_within_limit_is_clean():
    graph = _subworkflow_graph({"workflow_ref": "recon@v3"}, depth=0)
    assert error_codes(graph, max_subworkflow_depth=1) == []


# --------------------------------------------------------------------------- #
# 12. Unreachable node -> warning
# --------------------------------------------------------------------------- #


def test_unreachable_node_is_warning_not_error():
    detached = GraphNode(id="orphan", kind=NodeKind.TOOL)
    graph = WorkflowGraph(
        id="g-orphan",
        name="Orphan",
        nodes=[input_node("start"), output_node("end"), detached],
        edges=[data_edge("e1", "start", "end")],
    )
    issues = validate_graph(graph)
    warnings = [i for i in issues if i.severity == "warning"]
    errors = [i for i in issues if i.severity == "error"]
    assert any(i.code == "unreachable_node" and i.node_id == "orphan" for i in warnings)
    assert errors == []


# --------------------------------------------------------------------------- #
# 13. Cycles
# --------------------------------------------------------------------------- #


def _cycle_graph(allow_cycle: bool) -> WorkflowGraph:
    def tool(node_id: str) -> GraphNode:
        return GraphNode(
            id=node_id,
            kind=NodeKind.TOOL,
            inputs=[port("in", PortType.ANY)],
            outputs=[port("out", PortType.ANY)],
            config={"allow_cycle": True} if allow_cycle else {},
        )

    return WorkflowGraph(
        id="g-cycle",
        name="Cycle",
        nodes=[input_node("start"), tool("a"), tool("b")],
        edges=[
            data_edge("e1", "start", "a"),
            data_edge("e2", "a", "b"),
            data_edge("e3", "b", "a"),
        ],
    )


def test_cycle_without_allow_cycle_is_error():
    assert "unexpected_cycle" in error_codes(_cycle_graph(allow_cycle=False))


def test_cycle_with_allow_cycle_on_both_nodes_is_clean():
    assert error_codes(_cycle_graph(allow_cycle=True)) == []


def test_cycle_routed_by_condition_control_edges_is_clean():
    """Canonical loop: condition -> body -> condition, all CONTROL edges."""
    condition = GraphNode(
        id="cond",
        kind=NodeKind.CONDITION,
        inputs=[port("in", PortType.CONTROL)],
        outputs=[port("loop", PortType.CONTROL), port("exit", PortType.CONTROL)],
    )
    body = GraphNode(
        id="body",
        kind=NodeKind.TOOL,
        inputs=[port("in", PortType.CONTROL)],
        outputs=[port("out", PortType.CONTROL)],
    )
    # The condition's single CONTROL-in is the back-edge from the body, so the
    # graph satisfies the "exactly one CONTROL-in" rule while still looping.
    graph = WorkflowGraph(
        id="g-loop",
        name="Loop",
        nodes=[input_node("start", PortType.CONTROL), condition, body],
        edges=[
            GraphEdge(
                id="e1",
                source="start",
                source_port="out",
                target="body",
                target_port="in",
                kind=EdgeKind.CONTROL,
            ),
            GraphEdge(
                id="e2",
                source="cond",
                source_port="loop",
                target="body",
                target_port="in",
                kind=EdgeKind.CONTROL,
                condition="again",
            ),
            GraphEdge(
                id="e3",
                source="body",
                source_port="out",
                target="cond",
                target_port="in",
                kind=EdgeKind.CONTROL,
            ),
            GraphEdge(
                id="e4",
                source="cond",
                source_port="exit",
                target="body",
                target_port="in",
                kind=EdgeKind.CONTROL,
                condition="done",
            ),
        ],
    )
    assert error_codes(graph) == []


# --------------------------------------------------------------------------- #
# 14. content_hash stability
# --------------------------------------------------------------------------- #


def test_content_hash_stable_across_round_trip_and_key_order():
    graph = minimal_graph()
    data = graph.to_dict()
    reordered = {key: data[key] for key in reversed(list(data))}
    reordered["nodes"] = [
        {key: node[key] for key in reversed(list(node))} for node in data["nodes"]
    ]
    rebuilt = WorkflowGraph.from_dict(reordered)
    assert rebuilt.content_hash() == graph.content_hash()


def test_content_hash_changes_when_node_changes():
    graph = minimal_graph()
    before = graph.content_hash()
    graph.nodes[0].title = "renamed"
    assert graph.content_hash() != before


# --------------------------------------------------------------------------- #
# 15. to_dict / from_dict round trip
# --------------------------------------------------------------------------- #


def test_to_dict_from_dict_round_trip_equality():
    graph = WorkflowGraph(
        id="g-rt",
        name="RoundTrip",
        description="full surface",
        nodes=[
            GraphNode(
                id="start",
                kind=NodeKind.INPUT,
                title="Start",
                outputs=[port("out", PortType.TEXT)],
                position=(10.0, 20.0),
                version_ref="entry@v1",
            ),
            GraphNode(
                id="agent",
                kind=NodeKind.AGENT,
                inputs=[port("in", PortType.TEXT)],
                outputs=[port("out", PortType.JSON)],
                config={"agent_ref": "recon-agent@v3"},
                position=(30.0, 40.0),
            ),
            output_node("end", PortType.JSON),
        ],
        edges=[
            data_edge("e1", "start", "agent"),
            data_edge("e2", "agent", "end"),
        ],
        fan_in_policy=FanInPolicy.MIN_SUCCESS,
        min_success=1,
        timeout_s=30.0,
        subworkflow_depth=1,
    )
    rebuilt = WorkflowGraph.from_dict(graph.to_dict())
    assert rebuilt == graph
    assert rebuilt.to_dict() == graph.to_dict()


def test_to_dict_contains_no_pydantic_internals():
    data = minimal_graph().to_dict()
    assert set(data) == {
        "schema_version",
        "id",
        "name",
        "description",
        "nodes",
        "edges",
        "fan_in_policy",
        "min_success",
        "timeout_s",
        "subworkflow_depth",
    }
    assert isinstance(data["nodes"][0], dict)
    assert "kind" in data["nodes"][0]


# --------------------------------------------------------------------------- #
# 16. assert_valid
# --------------------------------------------------------------------------- #


def test_assert_valid_raises_with_all_error_codes():
    graph = WorkflowGraph(
        id="g-multi",
        name="Multi",
        nodes=[
            input_node("dup"),
            input_node("dup"),
            output_node("out"),  # no incoming edge
        ],
        edges=[
            GraphEdge(
                id="e-ghost",
                source="ghost",
                source_port="x",
                target="out",
                target_port="in",
            )
        ],
    )
    with pytest.raises(GraphValidationError) as excinfo:
        GraphValidator().assert_valid(graph)
    codes = {issue.code for issue in excinfo.value.issues}
    assert {"duplicate_node_id", "edge_unknown_node", "output_without_incoming"} <= codes
    assert all(issue.severity == "error" for issue in excinfo.value.issues)


def test_assert_valid_passes_on_valid_graph():
    GraphValidator().assert_valid(minimal_graph())  # does not raise


# --------------------------------------------------------------------------- #
# 17. type_compatibility_matrix
# --------------------------------------------------------------------------- #


def test_type_compatibility_matrix_covers_all_pairs():
    matrix = type_compatibility_matrix()
    assert len(matrix) == 36
    assert len(matrix) == len(PortType) ** 2
    for source in PortType:
        for target in PortType:
            assert f"{source.value}->{target.value}" in matrix


def test_type_compatibility_matrix_is_any_symmetric_and_documented():
    matrix = type_compatibility_matrix()
    for other in PortType:
        # ANY is compatible with everything in both directions.
        assert matrix[f"any->{other.value}"] is True
        assert matrix[f"{other.value}->any"] is True
    # Identical types are always compatible.
    for type_ in PortType:
        assert matrix[f"{type_.value}->{type_.value}"] is True
    # The four documented upcasts are the only non-ANY, non-identity allowances.
    assert matrix["text->json"] is True
    assert matrix["evidence->json"] is True
    assert matrix["artifact->json"] is True
    assert matrix["json->text"] is True
    assert matrix["text->evidence"] is False
    assert matrix["json->control"] is False


def test_port_type_compatible_matches_matrix():
    matrix = type_compatibility_matrix()
    for source in PortType:
        for target in PortType:
            assert port_type_compatible(source, target) is matrix[
                f"{source.value}->{target.value}"
            ]
