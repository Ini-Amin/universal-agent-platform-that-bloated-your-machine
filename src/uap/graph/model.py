"""Canonical, renderer-independent workflow graph (Master sections 18 and 55).

One graph model shared by the frontend renderer, the backend, the AI workflow
generator, the CLI, Git and the runtime (section 55). It knows nothing about a
UI framework, a database or an execution engine: it is pure typed data plus a
canonical JSON projection and a content hash used for Git/versioning
(sections 2.4 and 49).

Design notes:

* ``NodeKind`` is the hybrid typed graph vocabulary from section 18: the canvas
  stays visually flexible, but every node carries typed ports.
* ``position`` is a renderer hint only and never participates in semantics or in
  the content hash ordering contract.
* ``version_ref`` pins an exact Library version (section 2.4), e.g. ``recon@v3``.
* ``subworkflow_depth`` is the recursion guard for nested workflows (section 21).
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "PortType",
    "Port",
    "NodeKind",
    "EdgeKind",
    "FanInPolicy",
    "GraphNode",
    "GraphEdge",
    "WorkflowGraph",
]

_FORBID = ConfigDict(extra="forbid")


class PortType(StrEnum):
    """Types that can flow along a typed edge (section 18)."""

    ANY = "any"
    TEXT = "text"
    JSON = "json"
    EVIDENCE = "evidence"
    ARTIFACT = "artifact"
    CONTROL = "control"


class Port(BaseModel):
    """A typed connection point on a node."""

    model_config = _FORBID

    name: str
    type: PortType = PortType.ANY
    required: bool = True
    description: str = ""


class NodeKind(StrEnum):
    """Node vocabulary of the hybrid typed graph (sections 18-21)."""

    INPUT = "input"
    OUTPUT = "output"
    AGENT = "agent"
    TOOL = "tool"
    CONDITION = "condition"
    PARALLEL = "parallel"
    JOIN = "join"
    SUBWORKFLOW = "subworkflow"
    SYNTHESIS = "synthesis"
    APPROVAL = "approval"
    KNOWLEDGE = "knowledge"
    EVALUATION = "evaluation"


class EdgeKind(StrEnum):
    """Edge semantics: data flow, control flow, or error propagation."""

    DATA = "data"
    CONTROL = "control"
    ERROR = "error"


class FanInPolicy(StrEnum):
    """Configurable fan-in policies (section 36)."""

    REQUIRE_ALL = "require_all"
    ALLOW_PARTIAL = "allow_partial"
    MIN_SUCCESS = "min_success"
    TIMEOUT = "timeout"


class GraphNode(BaseModel):
    """A typed node in the canonical graph."""

    model_config = _FORBID

    id: str
    kind: NodeKind
    title: str = ""
    inputs: list[Port] = Field(default_factory=list)
    outputs: list[Port] = Field(default_factory=list)
    # kind-specific payload, e.g. agent ref "name@v2", tool ref, condition
    # expression, subworkflow ref, knowledge source, evaluation spec.
    config: dict[str, Any] = Field(default_factory=dict)
    # canvas coordinates - a renderer hint, never semantics.
    position: tuple[float, float] | None = None
    # exact version pin (section 2.4), e.g. "recon-agent@v3".
    version_ref: str | None = None


class GraphEdge(BaseModel):
    """A typed, port-to-port edge between two nodes."""

    model_config = _FORBID

    id: str
    source: str
    source_port: str
    target: str
    target_port: str
    kind: EdgeKind = EdgeKind.DATA
    # only meaningful for CONTROL edges leaving a CONDITION node.
    condition: str | None = None


class WorkflowGraph(BaseModel):
    """The canonical workflow definition (a versioned artifact, section 2.3)."""

    model_config = _FORBID

    schema_version: int = 1
    id: str
    name: str
    description: str = ""
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    # ``None`` is permitted so a JOIN node can be flagged as misconfigured; the
    # default is the safe ``require_all`` policy.
    fan_in_policy: FanInPolicy | None = FanInPolicy.REQUIRE_ALL
    min_success: int | None = None
    timeout_s: float | None = None
    # recursion guard for nested workflows (section 21).
    subworkflow_depth: int = 0

    # ------------------------------------------------------------------ #
    # Canonical JSON projection (section 55: frontend/backend/AI/Git/CLI)
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        """Return a plain-JSON dict with a stable field order.

        Uses pydantic's JSON serializer under the hood and immediately re-parses
        it, so no pydantic internals (types, ``__fields__``, ...) can leak and
        every value is a JSON primitive/list/dict.
        """
        return json.loads(self.model_dump_json())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowGraph":
        """Rebuild a graph from its canonical dict projection."""
        return cls.model_validate(data)

    def canonical_json(self) -> str:
        """Deterministic JSON: keys sorted recursively, compact separators.

        Key order therefore never affects the content hash, while changing any
        semantic value does.
        """
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def content_hash(self) -> str:
        """SHA-256 of the canonical JSON, for Git/version pinning (§49/§2.4)."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
