"""Typed definition shells for the Global Library (Master sections 4-7).

The Global Library stores reusable *definitions* -- agents, tools, skills and
workflows -- as versioned documents. This module holds the typed shape of one
definition body ("spec"), one model per kind, so a spec is validated before it
is ever written as a version:

* :class:`AgentDefinitionSpec`  -- Master section 5 (Agent Model)
* :class:`ToolDefinitionSpec`   -- Master section 6 (Tool Model)
* :class:`SkillDefinitionSpec`  -- Master section 7 (Skill Model)
* :class:`WorkflowDefinitionSpec` -- Master section 2.3 (definitions, not runs)

These are deliberately *minimal shells*: they pin the fields the platform
guarantees and reject everything else (``extra="forbid"``), exactly like the
core contracts. New optional fields can be added without breaking stored specs;
removing or renaming one is a breaking change and must go through a new version
(Master section 2.4).

Cross-references (``skills``, ``tools``, ...) are stored as *strings* so a
definition can point at global-Library refs such as ``"recon-agent@v3"`` without
this module importing the Library service (which would be a cycle).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AgentDefinitionSpec",
    "ToolDefinitionSpec",
    "SkillDefinitionSpec",
    "WorkflowDefinitionSpec",
    "SPEC_MODELS",
]

# An execution boundary is either the host machine or a sandbox (Master section
# 6). Untrusted/AI-created tools default to "sandbox".
ExecutionBoundary = Literal["host", "sandbox"]
TrustLevel = Literal["trusted", "untrusted"]


class AgentDefinitionSpec(BaseModel):
    """The body of an Agent definition (Master section 5).

    An Agent reasons and decides; it is composed from a model, instructions, and
    *references* to skills, tools, context, memory and guardrails. Referenced
    resources live in the Library under their own exact versions -- this spec
    only pins the refs (Master section 4).
    """

    model_config = ConfigDict(extra="forbid")

    # Required: an agent without a model cannot run.
    model: str
    instructions: str = ""
    skills: list[str] = Field(default_factory=list)  # skill refs, e.g. "api-recon@v2"
    tools: list[str] = Field(default_factory=list)  # tool refs
    context: list[str] = Field(default_factory=list)  # context-template refs
    memory: list[str] = Field(default_factory=list)  # memory-scope refs
    guardrails: list[str] = Field(default_factory=list)


class ToolDefinitionSpec(BaseModel):
    """The body of a Tool definition (Master section 6).

    A Tool is an executable capability. ``trust_level`` plus
    ``execution_boundary`` capture the security stance: untrusted tools default
    to sandboxed execution unless policy explicitly says otherwise.
    """

    model_config = ConfigDict(extra="forbid")

    # Required: the tool's stable identifier, e.g. "http.request".
    id: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    trust_level: TrustLevel = "untrusted"
    execution_boundary: ExecutionBoundary = "sandbox"


class SkillDefinitionSpec(BaseModel):
    """The body of a Skill definition (Master section 7).

    A Skill is *methodology*, not an action: it says how tools should be
    combined. ``required_capabilities`` names abstract capabilities
    (``"http.request"``), while ``dependencies`` names concrete resources the
    methodology relies on (other skills or tools).
    """

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    methodology: str = ""
    required_capabilities: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class WorkflowDefinitionSpec(BaseModel):
    """The body of a Workflow definition (Master sections 2.3 and 4).

    A workflow definition is versioned and persistent; an execution is an
    independent runtime instance. ``graph_ref`` points at the canonical graph
    document rather than embedding mutable runtime state.
    """

    model_config = ConfigDict(extra="forbid")

    # Required: reference to the canonical graph, e.g. "graphs/research.v3.json".
    graph_ref: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)


# Maps a library kind to the model that validates its spec. The Library service
# looks specs up here, so adding a kind means adding one line.
SPEC_MODELS: dict[str, type[BaseModel]] = {
    "agent": AgentDefinitionSpec,
    "tool": ToolDefinitionSpec,
    "skill": SkillDefinitionSpec,
    "workflow": WorkflowDefinitionSpec,
}
