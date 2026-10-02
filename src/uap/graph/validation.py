"""Graph validator for the canonical workflow graph (sections 17, 18, 21).

Section 17 requires every AI or user graph mutation to pass schema/dependency
validation before it is applied; section 18 allows loops and conditions but only
in well-formed shapes; section 21 requires a recursion guard for subworkflows.
This module is the single place those structural rules live.

``validate_graph`` always returns a full report and never raises;
``GraphValidator.assert_valid`` raises :class:`GraphValidationError` when any
``error``-severity issue is present. Warnings (e.g. unreachable nodes) never
block a mutation.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .model import EdgeKind, FanInPolicy, NodeKind, WorkflowGraph
from .ports import port_type_compatible

__all__ = [
    "GraphValidationError",
    "ValidationIssue",
    "GraphValidator",
    "validate_graph",
]

# An exact version pin such as "recon@v3" (section 2.4). The name is any
# non-empty prefix; the suffix must be ``@v`` followed by digits.
_EXACT_VERSION_RE = re.compile(r"^.+@v\d+$")


class GraphValidationError(Exception):
    """Raised by :meth:`GraphValidator.assert_valid` when errors are present."""

    def __init__(self, issues: list["ValidationIssue"]) -> None:
        self.issues = list(issues)
        codes = ", ".join(issue.code for issue in self.issues)
        super().__init__(f"graph validation failed ({len(self.issues)}): {codes}")


class ValidationIssue(BaseModel):
    """A single validation finding."""

    model_config = ConfigDict(extra="forbid")

    severity: Literal["error", "warning"]
    code: str
    node_id: str | None = None
    edge_id: str | None = None
    message: str


class GraphValidator:
    """Structural validator with a configurable subworkflow recursion guard."""

    def __init__(self, *, max_subworkflow_depth: int = 3) -> None:
        self.max_subworkflow_depth = max_subworkflow_depth

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def validate(self, graph: WorkflowGraph) -> list[ValidationIssue]:
        """Return every issue found; never raises."""
        try:
            return self._validate(graph)
        except Exception as exc:  # noqa: BLE001 - a validator must not crash
            return [
                ValidationIssue(
                    severity="error",
                    code="validator_crash",
                    message=f"{type(exc).__name__}: {exc}",
                )
            ]

    def assert_valid(self, graph: WorkflowGraph) -> None:
        """Raise :class:`GraphValidationError` if any error-severity issue exists."""
        errors = [i for i in self.validate(graph) if i.severity == "error"]
        if errors:
            raise GraphValidationError(errors)

    # ------------------------------------------------------------------ #
    # Rules
    # ------------------------------------------------------------------ #

    def _validate(self, graph: WorkflowGraph) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        nodes_by_id = {}
        for node in graph.nodes:
            nodes_by_id.setdefault(node.id, node)

        self._check_unique_node_ids(graph, issues)
        self._check_unique_edge_ids(graph, issues)
        resolved = self._check_edge_endpoints(graph, nodes_by_id, issues)
        self._check_port_compatibility(resolved, issues)
        self._check_required_inputs(graph, nodes_by_id, resolved, issues)
        self._check_outputs(graph, resolved, issues)
        self._check_conditions(graph, nodes_by_id, resolved, issues)
        self._check_joins(graph, issues)
        self._check_subworkflows(graph, nodes_by_id, issues)
        self._check_reachability(graph, nodes_by_id, issues)
        self._check_cycles(graph, nodes_by_id, issues)
        return issues

    @staticmethod
    def _check_unique_node_ids(
        graph: WorkflowGraph, issues: list[ValidationIssue]
    ) -> None:
        seen: set[str] = set()
        for node in graph.nodes:
            if node.id in seen:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="duplicate_node_id",
                        node_id=node.id,
                        message=f"duplicate node id {node.id!r}",
                    )
                )
            seen.add(node.id)

    @staticmethod
    def _check_unique_edge_ids(
        graph: WorkflowGraph, issues: list[ValidationIssue]
    ) -> None:
        seen: set[str] = set()
        for edge in graph.edges:
            if edge.id in seen:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="duplicate_edge_id",
                        edge_id=edge.id,
                        message=f"duplicate edge id {edge.id!r}",
                    )
                )
            seen.add(edge.id)

    @staticmethod
    def _check_edge_endpoints(
        graph: WorkflowGraph,
        nodes_by_id: dict,
        issues: list[ValidationIssue],
    ) -> list:
        """Return edges whose endpoints resolve to real nodes and ports."""
        resolved = []
        for edge in graph.edges:
            source = nodes_by_id.get(edge.source)
            target = nodes_by_id.get(edge.target)
            if source is None or target is None:
                missing = edge.source if source is None else edge.target
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="edge_unknown_node",
                        edge_id=edge.id,
                        message=f"edge {edge.id!r} references unknown node {missing!r}",
                    )
                )
                continue
            source_port = next(
                (p for p in source.outputs if p.name == edge.source_port), None
            )
            target_port = next(
                (p for p in target.inputs if p.name == edge.target_port), None
            )
            if source_port is None:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="edge_unknown_port",
                        edge_id=edge.id,
                        node_id=source.id,
                        message=(
                            f"edge {edge.id!r} references unknown output port "
                            f"{edge.source_port!r} on node {source.id!r}"
                        ),
                    )
                )
                continue
            if target_port is None:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="edge_unknown_port",
                        edge_id=edge.id,
                        node_id=target.id,
                        message=(
                            f"edge {edge.id!r} references unknown input port "
                            f"{edge.target_port!r} on node {target.id!r}"
                        ),
                    )
                )
                continue
            resolved.append((edge, source_port, target_port))
        return resolved

    @staticmethod
    def _check_port_compatibility(
        resolved: list, issues: list[ValidationIssue]
    ) -> None:
        for edge, source_port, target_port in resolved:
            if not port_type_compatible(source_port.type, target_port.type):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="incompatible_port_type",
                        edge_id=edge.id,
                        message=(
                            f"edge {edge.id!r}: {source_port.type.value} -> "
                            f"{target_port.type.value} is not a compatible port type"
                        ),
                    )
                )

    @staticmethod
    def _check_required_inputs(
        graph: WorkflowGraph,
        nodes_by_id: dict,
        resolved: list,
        issues: list[ValidationIssue],
    ) -> None:
        fed: set[tuple[str, str]] = {(e.target, e.target_port) for e, _, _ in resolved}
        for node in graph.nodes:
            if node.kind is NodeKind.INPUT:
                continue  # INPUT nodes are graph sources by definition
            for port in node.inputs:
                if port.required and (node.id, port.name) not in fed:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            code="missing_required_input",
                            node_id=node.id,
                            message=(
                                f"node {node.id!r}: required input port "
                                f"{port.name!r} has no incoming edge"
                            ),
                        )
                    )

    @staticmethod
    def _check_outputs(
        graph: WorkflowGraph, resolved: list, issues: list[ValidationIssue]
    ) -> None:
        has_incoming: set[str] = {e.target for e, _, _ in resolved}
        for node in graph.nodes:
            if node.kind is NodeKind.OUTPUT and node.id not in has_incoming:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="output_without_incoming",
                        node_id=node.id,
                        message=f"OUTPUT node {node.id!r} has no incoming edge",
                    )
                )

    @staticmethod
    def _check_conditions(
        graph: WorkflowGraph,
        nodes_by_id: dict,
        resolved: list,
        issues: list[ValidationIssue],
    ) -> None:
        for node in graph.nodes:
            if node.kind is not NodeKind.CONDITION:
                continue
            control_in = [
                e for e, _, _ in resolved
                if e.target == node.id and e.kind is EdgeKind.CONTROL
            ]
            if len(control_in) != 1:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="condition_control_in",
                        node_id=node.id,
                        message=(
                            f"CONDITION node {node.id!r} must have exactly one "
                            f"CONTROL incoming edge (found {len(control_in)})"
                        ),
                    )
                )
            control_out = [
                e for e, _, _ in resolved
                if e.source == node.id and e.kind is EdgeKind.CONTROL
            ]
            if len(control_out) < 2:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="condition_control_out",
                        node_id=node.id,
                        message=(
                            f"CONDITION node {node.id!r} must have at least two "
                            f"CONTROL outgoing edges (found {len(control_out)})"
                        ),
                    )
                )
            conditions = [e.condition for e in control_out]
            if any(not c for c in conditions):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="condition_missing_condition",
                        node_id=node.id,
                        message=(
                            f"CONDITION node {node.id!r} has a CONTROL outgoing "
                            f"edge without a condition"
                        ),
                    )
                )
            elif len(set(conditions)) != len(conditions):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="condition_duplicate_branch",
                        node_id=node.id,
                        message=(
                            f"CONDITION node {node.id!r} has duplicate branch "
                            f"conditions"
                        ),
                    )
                )

    @staticmethod
    def _check_joins(graph: WorkflowGraph, issues: list[ValidationIssue]) -> None:
        for node in graph.nodes:
            if node.kind is NodeKind.JOIN and graph.fan_in_policy is None:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="join_missing_fan_in_policy",
                        node_id=node.id,
                        message=(
                            f"JOIN node {node.id!r} requires an explicit "
                            f"fan_in_policy (section 36)"
                        ),
                    )
                )

    def _check_subworkflows(
        self,
        graph: WorkflowGraph,
        nodes_by_id: dict,
        issues: list[ValidationIssue],
    ) -> None:
        for node in graph.nodes:
            if node.kind is not NodeKind.SUBWORKFLOW:
                continue
            ref = node.config.get("workflow_ref")
            if not isinstance(ref, str) or not ref:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="subworkflow_missing_ref",
                        node_id=node.id,
                        message=(
                            f"SUBWORKFLOW node {node.id!r} requires "
                            f"config['workflow_ref']"
                        ),
                    )
                )
            elif not _EXACT_VERSION_RE.match(ref):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="subworkflow_bad_ref",
                        node_id=node.id,
                        message=(
                            f"SUBWORKFLOW node {node.id!r} ref {ref!r} is not an "
                            f"exact version pin (expected 'name@vN')"
                        ),
                    )
                )
            if graph.subworkflow_depth >= self.max_subworkflow_depth:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="subworkflow_depth_exceeded",
                        node_id=node.id,
                        message=(
                            f"SUBWORKFLOW node {node.id!r}: depth "
                            f"{graph.subworkflow_depth} >= max "
                            f"{self.max_subworkflow_depth} (recursion guard, §21)"
                        ),
                    )
                )

    @staticmethod
    def _check_reachability(
        graph: WorkflowGraph,
        nodes_by_id: dict,
        issues: list[ValidationIssue],
    ) -> None:
        adjacency: dict[str, set[str]] = {n.id: set() for n in graph.nodes}
        incoming: dict[str, int] = {n.id: 0 for n in graph.nodes}
        for edge in graph.edges:
            if edge.source in adjacency and edge.target in adjacency:
                adjacency[edge.source].add(edge.target)
                incoming[edge.target] += 1
        inputs = [n.id for n in graph.nodes if n.kind is NodeKind.INPUT]
        roots = inputs or [nid for nid, deg in incoming.items() if deg == 0]
        visited: set[str] = set()
        stack = list(roots)
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            stack.extend(adjacency.get(current, ()))
        for node in graph.nodes:
            if node.id not in visited:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="unreachable_node",
                        node_id=node.id,
                        message=f"node {node.id!r} is unreachable from any INPUT",
                    )
                )

    @staticmethod
    def _check_cycles(
        graph: WorkflowGraph,
        nodes_by_id: dict,
        issues: list[ValidationIssue],
    ) -> None:
        components = _strongly_connected_components(
            [n.id for n in graph.nodes],
            [(e.source, e.target) for e in graph.edges],
        )
        for component in components:
            members = set(component)
            internal = [
                e for e in graph.edges
                if e.source in members and e.target in members
            ]
            has_self_loop = any(e.source == e.target for e in internal)
            if len(component) == 1 and not has_self_loop:
                continue
            allowed_by_flag = all(
                nodes_by_id[nid].config.get("allow_cycle") is True
                for nid in component
            )
            # A loop is routed by a CONDITION when every edge of the cycle is a
            # CONTROL edge and at least one of them leaves a CONDITION node. The
            # back-edge into the condition comes from the loop body, so requiring
            # *every* edge to leave a CONDITION would reject the canonical shape
            # (condition -> body -> condition).
            allowed_by_control = bool(internal) and all(
                e.kind is EdgeKind.CONTROL for e in internal
            ) and any(
                nodes_by_id[e.source].kind is NodeKind.CONDITION for e in internal
            )
            if not (allowed_by_flag or allowed_by_control):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        code="unexpected_cycle",
                        node_id=sorted(component)[0],
                        message=(
                            "cycle involving nodes "
                            f"{sorted(component)!r} is not permitted; cycles "
                            "must run through CONTROL edges of CONDITION nodes "
                            "or set config['allow_cycle']=True on every node"
                        ),
                    )
                )


def validate_graph(graph: WorkflowGraph) -> list[ValidationIssue]:
    """Full validation report; never raises (convenience wrapper)."""
    return GraphValidator().validate(graph)


# --------------------------------------------------------------------------- #
# Tarjan's strongly-connected-components (iterative - no recursion limit)
# --------------------------------------------------------------------------- #


def _strongly_connected_components(
    nodes: list[str], edges: list[tuple[str, str]]
) -> list[list[str]]:
    adjacency: dict[str, list[str]] = {n: [] for n in nodes}
    for source, target in edges:
        if source in adjacency and target in adjacency:
            adjacency[source].append(target)

    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    result: list[list[str]] = []

    for root in nodes:
        if root in indices:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_index = work.pop()
            if child_index == 0:
                indices[node] = index_counter
                lowlink[node] = index_counter
                index_counter += 1
                stack.append(node)
                on_stack.add(node)

            recursed = False
            neighbours = adjacency[node]
            for position in range(child_index, len(neighbours)):
                neighbour = neighbours[position]
                if neighbour not in indices:
                    work.append((node, position + 1))
                    work.append((neighbour, 0))
                    recursed = True
                    break
                if neighbour in on_stack:
                    lowlink[node] = min(lowlink[node], indices[neighbour])
            if recursed:
                continue

            if lowlink[node] == indices[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                result.append(component)
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
    return result
