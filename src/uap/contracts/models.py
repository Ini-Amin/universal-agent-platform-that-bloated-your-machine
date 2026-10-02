"""Core domain contracts for the Universal Agent Platform (Master section 27).

These models are the shared language between the Entry Workflow, domain
workflows, agents, tools, and the cross-cutting infrastructure (context, memory,
verification, artifacts, approval, checkpointing). They deliberately know
nothing about the UI, the database, HTTP routes, MCP, or LangGraph internals
(Master sections 10 and 29).
"""

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

__all__ = [
    "UTCDateTime",
    "utc_now",
    "Domain",
    "TaskMode",
    "AgentStatus",
    "WorkflowStatus",
    "ArtifactStatus",
    "ApprovalState",
    "MemoryCategory",
    "UserRequest",
    "TaskSpec",
    "ContextSection",
    "AgentContext",
    "ToolCall",
    "ToolResult",
    "TokenUsage",
    "VerificationCriterion",
    "VerificationResult",
    "Artifact",
    "ApprovalRequest",
    "Observation",
    "Memory",
    "UserModel",
    "AgentResult",
    "WorkflowState",
    "WorkflowResult",
    "NodeView",
]


def utc_now() -> datetime:
    """The single clock used for every default timestamp."""
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        # Naive input is treated as already-UTC by contract producers.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_ensure_utc)]

_MODEL_CONFIG = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #


class Domain(StrEnum):
    LEARNING = "learning"
    RESEARCH = "research"
    CODING = "coding"
    BBP = "bbp"
    DATA = "data"
    UNKNOWN = "unknown"


class TaskMode(StrEnum):
    INTERACTIVE = "interactive"
    AUTONOMOUS = "autonomous"


class AgentStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    CHECKPOINTED = "checkpointed"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactStatus(StrEnum):
    DRAFT = "draft"
    FINAL = "final"
    SUPERSEDED = "superseded"


class ApprovalState(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


class MemoryCategory(StrEnum):
    TASK_HISTORY = "task_history"
    USER_PREFERENCES = "user_preferences"
    LEARNING_PROGRESS = "learning_progress"
    LONG_TERM_FACTS = "long_term_facts"
    PROJECT_CONTEXT = "project_context"
    PREVIOUS_ARTIFACTS = "previous_artifacts"


# --------------------------------------------------------------------------- #
# Nested value objects
# --------------------------------------------------------------------------- #


class ContextSection(BaseModel):
    """One compiled context block handed to an agent."""

    model_config = _MODEL_CONFIG
    key: str
    content: str


class TokenUsage(BaseModel):
    """Cost of one agent run."""

    model_config = _MODEL_CONFIG
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0


class VerificationCriterion(BaseModel):
    """A single explicit check performed by a verifier (Master section 17)."""

    model_config = _MODEL_CONFIG
    criterion: str
    passed: bool
    evidence: str | None = None


class Observation(BaseModel):
    """One evidence-backed note about a user (Master section 16).

    Deliberately label-free: the note records evidence, not a verdict.
    """

    model_config = _MODEL_CONFIG
    domain: str
    note: str
    evidence: str
    recorded_at: UTCDateTime = Field(default_factory=utc_now)


# --------------------------------------------------------------------------- #
# Core domain objects
# --------------------------------------------------------------------------- #


class UserRequest(BaseModel):
    """Raw input before the Entry Workflow does anything with it."""

    model_config = _MODEL_CONFIG
    raw_input: str
    user_id: str | None = None
    session_id: str | None = None
    created_at: UTCDateTime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskSpec(BaseModel):
    """Entry Workflow -> Domain Workflow contract (Master section 4)."""

    model_config = _MODEL_CONFIG
    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    domain: Domain
    goal: str
    input: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    mode: TaskMode = TaskMode.INTERACTIVE
    verification: bool = True
    created_at: UTCDateTime = Field(default_factory=utc_now)


class AgentContext(BaseModel):
    """Everything an agent may look at; produced by the Context Compiler."""

    model_config = _MODEL_CONFIG
    task: TaskSpec
    context_sections: list[ContextSection] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)
    available_skills: list[str] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    extras: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """A tool invocation requested by an agent."""

    model_config = _MODEL_CONFIG
    call_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    started_at: UTCDateTime = Field(default_factory=utc_now)


class ToolResult(BaseModel):
    """What a tool gave back (registry-level, implementation agnostic)."""

    model_config = _MODEL_CONFIG
    call_id: str
    tool: str
    ok: bool
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    approval_id: str | None = None


class Artifact(BaseModel):
    """A structured output with metadata (Master section 19)."""

    model_config = _MODEL_CONFIG
    artifact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    type: str
    version: int = 1
    created_at: UTCDateTime = Field(default_factory=utc_now)
    source: str
    status: ArtifactStatus = ArtifactStatus.DRAFT
    uri: str | None = None
    content_ref: str | None = None


class VerificationResult(BaseModel):
    """Explicit criteria-based verdict (Master section 17)."""

    model_config = _MODEL_CONFIG
    verifier: str
    passed: bool
    criteria: list[VerificationCriterion] = Field(default_factory=list)
    score: float | None = None
    notes: str | None = None


class ApprovalRequest(BaseModel):
    """Explicit human decision point (Master section 20)."""

    model_config = _MODEL_CONFIG
    approval_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    action: str
    details: dict[str, Any] = Field(default_factory=dict)
    state: ApprovalState = ApprovalState.PENDING_APPROVAL
    requested_at: UTCDateTime = Field(default_factory=utc_now)
    decided_at: UTCDateTime | None = None
    decided_by: str | None = None


class Memory(BaseModel):
    """One durable, scored, expirable memory entry (Master section 16)."""

    model_config = _MODEL_CONFIG
    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    category: MemoryCategory
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    relevance: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: UTCDateTime = Field(default_factory=utc_now)
    expires_at: UTCDateTime | None = None
    last_accessed_at: UTCDateTime | None = None


class UserModel(BaseModel):
    """User preferences plus evidence-backed observations (Master section 16)."""

    model_config = _MODEL_CONFIG
    user_id: str
    preferences: dict[str, Any] = Field(default_factory=dict)
    domain_skill_levels: dict[str, str] = Field(default_factory=dict)
    observations: list[Observation] = Field(default_factory=list)
    updated_at: UTCDateTime = Field(default_factory=utc_now)


class AgentResult(BaseModel):
    """The return value of one agent run (Master section 10)."""

    model_config = _MODEL_CONFIG
    agent_name: str
    status: AgentStatus
    output: str
    artifacts: list[Artifact] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    verification: VerificationResult | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    error: str | None = None


class WorkflowState(BaseModel):
    """Checkpointable workflow state (Master section 21).

    `node_history` + `current_node` locate the resume point; `version` is the
    optimistic-concurrency counter a writer bumps on every mutation.
    """

    model_config = _MODEL_CONFIG
    task_id: str
    workflow: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    current_node: str | None = None
    node_history: list[str] = Field(default_factory=list)
    retries: int = 0
    pending_approval: ApprovalRequest | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    updated_at: UTCDateTime = Field(default_factory=utc_now)


class WorkflowResult(BaseModel):
    """What a domain workflow returns to the caller."""

    model_config = _MODEL_CONFIG
    task_id: str
    workflow: str
    status: WorkflowStatus
    output: str
    artifacts: list[Artifact] = Field(default_factory=list)
    verification: VerificationResult | None = None
    error: str | None = None

class NodeView(BaseModel):
    """What a node's work looks like on the canvas (canvas-as-stage).

    A node publishes a *view* so the user sees what the work PRODUCED, not just
    that the node turned green. The canvas renders one of ``kind``:

    * ``markdown`` / ``code`` -> ``text`` (``language`` for code fences).
    * ``image`` / ``video`` / ``iframe`` -> ``url`` (+ ``caption``).
    * ``html`` -> ``text`` (the UI may render it in a sandbox).
    * ``whiteboard`` -> ``url`` (defaults to a hosted Excalidraw).
    * ``placeholder`` -> ``text``/``caption`` naming why nothing rendered.

    Deliberately **permissive** (``extra="allow"``): unlike the strict core
    contracts, a view is a renderer hint bag. The UI owns rendering and may read
    additional keys (``poster``, ``alt``, ``sandbox``, ``html`` ...) without the
    backend having to model each one. Required shape is only ``kind``.
    """

    model_config = ConfigDict(extra="allow")

    kind: Literal[
        "html",
        "markdown",
        "image",
        "video",
        "iframe",
        "code",
        "whiteboard",
        "placeholder",
    ]
    title: str = ""
    url: str | None = None
    text: str | None = None
    caption: str | None = None
    language: str | None = None

