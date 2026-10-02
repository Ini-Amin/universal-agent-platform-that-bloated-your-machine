"""Canonical workflow graph (Master sections 18 and 55).

A renderer-independent typed graph shared by the frontend, backend, AI workflow
generator, CLI, Git and runtime. See :mod:`uap.graph.model` for the data model,
:mod:`uap.graph.ports` for the port type system and :mod:`uap.graph.validation`
for structural validation.
"""

from .model import (
    EdgeKind,
    FanInPolicy,
    GraphEdge,
    GraphNode,
    NodeKind,
    Port,
    PortType,
    WorkflowGraph,
)
from .ports import port_type_compatible, type_compatibility_matrix
from .validation import (
    GraphValidationError,
    GraphValidator,
    ValidationIssue,
    validate_graph,
)

__all__ = [
    "PortType",
    "Port",
    "NodeKind",
    "EdgeKind",
    "FanInPolicy",
    "GraphNode",
    "GraphEdge",
    "WorkflowGraph",
    "port_type_compatible",
    "type_compatibility_matrix",
    "GraphValidationError",
    "ValidationIssue",
    "GraphValidator",
    "validate_graph",
]
