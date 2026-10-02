"""Edge-case contract tests for build Step 1 (Master sections 19-21, 27).

Self-contained: every factory here is independent of tests/test_contracts.py so
either file can run alone. Deterministic: no sleeps, no network, no clock reads
beyond tz-awareness assertions.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ValidationError

import uap.contracts as contracts
from uap.contracts import (
    AgentContext,
    AgentResult,
    AgentStatus,
    ApprovalRequest,
    ApprovalState,
    Artifact,
    ArtifactStatus,
    ContextSection,
    Domain,
    Memory,
    MemoryCategory,
    Observation,
    TaskMode,
    TaskSpec,
    TokenUsage,
    ToolCall,
    ToolResult,
    UserModel,
    UserRequest,
    VerificationCriterion,
    VerificationResult,
    WorkflowResult,
    WorkflowState,
    WorkflowStatus,
    utc_now,
)

# --------------------------------------------------------------------------- #
# Factories (local copies; no cross-file imports)
# --------------------------------------------------------------------------- #


def make_task(goal: str = "survey subnetting literature") -> TaskSpec:
    return TaskSpec(domain=Domain.RESEARCH, goal=goal, mode=TaskMode.AUTONOMOUS)


def make_artifact(task_id: str = "t1") -> Artifact:
    return Artifact(
        task_id=task_id,
        type="report.md",
        source="research_agent",
        status=ArtifactStatus.FINAL,
        uri="/tmp/artifacts/t1/report.md",
        content_ref="sha256:abc",
    )


def make_verification() -> VerificationResult:
    return VerificationResult(
        verifier="evidence_checker",
        passed=True,
        criteria=[
            VerificationCriterion(criterion="has sources", passed=True, evidence="[1]"),
            VerificationCriterion(criterion="no fabrications", passed=False),
        ],
        score=0.9,
        notes="ok",
    )


def make_approval(state: ApprovalState = ApprovalState.APPROVED) -> ApprovalRequest:
    return ApprovalRequest(
        task_id="t1",
        action="publish_report",
        details={"channel": "email"},
        state=state,
        requested_at=utc_now(),
        decided_at=utc_now(),
        decided_by="human:1",
    )


def make_memory() -> Memory:
    return Memory(
        category=MemoryCategory.PROJECT_CONTEXT,
        content="target scope is example.com",
        metadata={"source": "brain"},
        relevance=0.75,
        expires_at=utc_now(),
        last_accessed_at=utc_now(),
    )


def make_checkpointed_state() -> WorkflowState:
    """A workflow that stopped after Node B and must resume at Node C."""
    return WorkflowState(
        task_id="t1",
        workflow="research",
        status=WorkflowStatus.CHECKPOINTED,
        current_node="node_b",
        node_history=["intake", "plan", "node_a", "node_b"],
        retries=2,
        pending_approval=make_approval(ApprovalState.PENDING_APPROVAL),
        artifacts=[make_artifact(), make_artifact()],
        data={"sources": [1, 2, 3], "partial": {"seen": True}},
        version=7,
    )


# --------------------------------------------------------------------------- #
# 1. Checkpoint / resume contract (Master section 21)
# --------------------------------------------------------------------------- #


def test_checkpoint_state_resume_point_survives_json_round_trip() -> None:
    state = make_checkpointed_state()
    restored = WorkflowState.model_validate_json(state.model_dump_json())

    assert restored == state
    # The resume point must survive exactly.
    assert restored.status is WorkflowStatus.CHECKPOINTED
    assert restored.current_node == "node_b"
    assert restored.node_history == ["intake", "plan", "node_a", "node_b"]
    assert restored.data == {"sources": [1, 2, 3], "partial": {"seen": True}}
    assert restored.version == 7
    assert restored.retries == 2
    assert restored.task_id == "t1" and restored.workflow == "research"


def test_checkpoint_state_resume_point_survives_python_round_trip() -> None:
    state = make_checkpointed_state()
    restored = WorkflowState.model_validate(state.model_dump())
    assert restored.node_history == state.node_history
    assert restored.current_node == state.current_node
    assert restored.data == state.data
    assert restored.version == state.version


def test_version_is_a_plain_int_that_round_trips() -> None:
    assert WorkflowState(task_id="t", workflow="w").version == 1
    for version in (1, 2, 0, 99, 2**31 - 1):
        state = WorkflowState(task_id="t", workflow="w", version=version)
        assert isinstance(state.version, int)
        restored = WorkflowState.model_validate_json(state.model_dump_json())
        assert restored.version == version


def test_empty_node_history_and_null_current_node_round_trip() -> None:
    state = WorkflowState(task_id="t", workflow="w", version=1)
    restored = WorkflowState.model_validate_json(state.model_dump_json())
    assert restored.current_node is None
    assert restored.node_history == []


# --------------------------------------------------------------------------- #
# 2. Approval lifecycle (Master section 20)
# --------------------------------------------------------------------------- #


def test_approval_pending_defaults_have_no_decision() -> None:
    approval = ApprovalRequest(task_id="t1", action="publish_report")
    assert approval.state is ApprovalState.PENDING_APPROVAL
    assert approval.decided_at is None
    assert approval.decided_by is None


def test_approval_pending_to_approved_to_rejected_transitions() -> None:
    pending = ApprovalRequest(task_id="t1", action="publish_report")

    approved = ApprovalRequest.model_validate_json(
        pending.model_copy(
            update={
                "state": ApprovalState.APPROVED,
                "decided_at": utc_now(),
                "decided_by": "human:1",
            }
        ).model_dump_json()
    )
    assert approved.state is ApprovalState.APPROVED
    assert approved.decided_by == "human:1"
    assert approved.decided_at is not None
    assert approved.decided_at.tzinfo is not None

    rejected = ApprovalRequest.model_validate_json(
        approved.model_copy(
            update={
                "state": ApprovalState.REJECTED,
                "decided_at": utc_now(),
                "decided_by": "human:2",
            }
        ).model_dump_json()
    )
    assert rejected.state is ApprovalState.REJECTED
    assert rejected.decided_by == "human:2"
    assert rejected.decided_at is not None


def test_approval_rejected_without_decision_fields_is_allowed() -> None:
    """Current behavior: the contract does not couple `state` to decided_*."""
    rejected = ApprovalRequest(
        task_id="t1", action="publish_report", state=ApprovalState.REJECTED
    )
    assert rejected.decided_at is None
    assert rejected.decided_by is None
    restored = ApprovalRequest.model_validate_json(rejected.model_dump_json())
    assert restored == rejected
    assert restored.state is ApprovalState.REJECTED
    assert restored.decided_at is None


def test_approval_decision_timestamps_round_trip_survive() -> None:
    approval = make_approval(ApprovalState.APPROVED)
    restored = ApprovalRequest.model_validate_json(approval.model_dump_json())
    assert restored.decided_at == approval.decided_at
    assert restored.decided_by == approval.decided_by
    assert restored.requested_at == approval.requested_at


def test_workflow_awaiting_approval_state_round_trips_with_approval() -> None:
    state = WorkflowState(
        task_id="t1",
        workflow="research",
        status=WorkflowStatus.AWAITING_APPROVAL,
        pending_approval=ApprovalRequest(task_id="t1", action="publish_report"),
    )
    restored = WorkflowState.model_validate_json(state.model_dump_json())
    assert restored.status is WorkflowStatus.AWAITING_APPROVAL
    assert restored.pending_approval is not None
    assert restored.pending_approval.state is ApprovalState.PENDING_APPROVAL


# --------------------------------------------------------------------------- #
# 3. Artifact metadata (Master section 19)
# --------------------------------------------------------------------------- #


def test_artifact_minimal_construction_carries_all_metadata() -> None:
    artifact = Artifact(task_id="t1", type="report.md", source="research_agent")
    dumped = artifact.model_dump()

    # Every metadata field named in Master section 19 exists and defaults.
    for field in ("artifact_id", "task_id", "type", "version", "created_at", "source", "status"):
        assert field in dumped, field
    assert artifact.version == 1
    assert artifact.status is ArtifactStatus.DRAFT
    assert artifact.created_at.tzinfo is not None
    assert artifact.uri is None and artifact.content_ref is None
    assert artifact.artifact_id and artifact.task_id == "t1"
    assert artifact.type == "report.md" and artifact.source == "research_agent"


def test_artifact_ids_unique_across_100_instances() -> None:
    ids = [Artifact(task_id="t1", type="x.md", source="a").artifact_id for _ in range(100)]
    assert len(set(ids)) == 100
    assert all(isinstance(i, str) and i for i in ids)


def test_artifact_version_and_status_round_trip_exactly() -> None:
    artifact = Artifact(
        task_id="t1",
        type="patch",
        source="coding_agent",
        version=3,
        status=ArtifactStatus.SUPERSEDED,
        uri="/tmp/p.patch",
        content_ref="sha256:def",
    )
    restored = Artifact.model_validate_json(artifact.model_dump_json())
    assert restored == artifact
    assert restored.version == 3
    assert restored.status is ArtifactStatus.SUPERSEDED


# --------------------------------------------------------------------------- #
# 4. Nested round-trips
# --------------------------------------------------------------------------- #


def test_agent_result_full_nested_round_trip() -> None:
    result = AgentResult(
        agent_name="researcher",
        status=AgentStatus.PARTIAL,
        output="## findings",
        artifacts=[make_artifact(), make_artifact()],
        tool_calls=[
            ToolCall(tool="http.get", args={"url": "https://example.com"}),
            ToolCall(tool="read_file", args={"path": "/tmp/a"}),
        ],
        verification=make_verification(),
        usage=TokenUsage(tokens_in=10, tokens_out=20, latency_ms=3.5),
        error=None,
    )
    restored = AgentResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert len(restored.tool_calls) == 2
    assert [c.tool for c in restored.tool_calls] == ["http.get", "read_file"]
    assert restored.verification is not None
    assert len(restored.verification.criteria) == 2
    assert restored.verification.criteria[0] == result.verification.criteria[0]
    assert [a.artifact_id for a in restored.artifacts] == [
        a.artifact_id for a in result.artifacts
    ]
    assert restored.usage == TokenUsage(tokens_in=10, tokens_out=20, latency_ms=3.5)


def test_agent_result_empty_nested_lists_round_trip() -> None:
    result = AgentResult(agent_name="a", status=AgentStatus.FAILED, output="")
    restored = AgentResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert restored.artifacts == [] and restored.tool_calls == []
    assert restored.verification is None


def test_workflow_state_nested_approval_and_artifacts_round_trip() -> None:
    state = make_checkpointed_state()
    restored = WorkflowState.model_validate_json(state.model_dump_json())
    assert restored.pending_approval == state.pending_approval
    assert restored.artifacts == state.artifacts
    assert [a.artifact_id for a in restored.artifacts] == [
        a.artifact_id for a in state.artifacts
    ]
    assert restored.pending_approval.state is ApprovalState.PENDING_APPROVAL


def test_memory_standalone_round_trip() -> None:
    memory = make_memory()
    restored = Memory.model_validate_json(memory.model_dump_json())
    assert restored == memory
    assert restored.category is MemoryCategory.PROJECT_CONTEXT
    assert restored.relevance == 0.75
    assert restored.expires_at == memory.expires_at
    assert restored.last_accessed_at == memory.last_accessed_at
    assert restored.metadata == {"source": "brain"}


def test_context_and_user_model_nested_round_trips() -> None:
    context = AgentContext(
        task=make_task(),
        context_sections=[ContextSection(key="memory", content="memo")],
        available_tools=["http.get"],
    )
    restored_ctx = AgentContext.model_validate_json(context.model_dump_json())
    assert restored_ctx == context
    assert restored_ctx.task == context.task
    assert restored_ctx.context_sections == context.context_sections
    assert restored_ctx.available_tools == ["http.get"]

    user = UserModel(
        user_id="u1",
        observations=[Observation(domain="coding", note="likes pytest", evidence="run")],
    )
    restored_user = UserModel.model_validate_json(user.model_dump_json())
    assert restored_user == user
    assert restored_user.observations[0].evidence == "run"


# --------------------------------------------------------------------------- #
# 5. Validation edges
# --------------------------------------------------------------------------- #

EXTRA_FIELD_CASES = [
    pytest.param(
        TaskSpec,
        {"domain": Domain.LEARNING, "goal": "g", "surprise": 1},
        id="TaskSpec",
    ),
    pytest.param(
        Artifact,
        {"task_id": "t", "type": "x.md", "source": "a", "surprise": 1},
        id="Artifact",
    ),
    pytest.param(
        ApprovalRequest,
        {"task_id": "t", "action": "a", "surprise": 1},
        id="ApprovalRequest",
    ),
    pytest.param(
        Memory,
        {"category": MemoryCategory.PROJECT_CONTEXT, "content": "c", "surprise": 1},
        id="Memory",
    ),
    pytest.param(
        VerificationResult,
        {"verifier": "v", "passed": True, "surprise": 1},
        id="VerificationResult",
    ),
    pytest.param(
        WorkflowState,
        {"task_id": "t", "workflow": "w", "surprise": 1},
        id="WorkflowState",
    ),
]


@pytest.mark.parametrize("cls,kwargs", EXTRA_FIELD_CASES)
def test_extra_forbid_rejects_unknown_fields(cls, kwargs) -> None:
    with pytest.raises(ValidationError):
        cls(**kwargs)


@pytest.mark.parametrize(
    "cls,kwargs",
    [
        pytest.param(TaskSpec, {"domain": "gaming", "goal": "g"}, id="Domain-gaming"),
        pytest.param(
            TaskSpec,
            {"domain": Domain.CODING, "goal": "g", "mode": "reckless"},
            id="TaskMode",
        ),
        pytest.param(
            WorkflowState,
            {"task_id": "t", "workflow": "w", "status": "limbo"},
            id="WorkflowStatus",
        ),
        pytest.param(
            Artifact,
            {"task_id": "t", "type": "x", "source": "s", "status": "brand_new"},
            id="ArtifactStatus",
        ),
        pytest.param(
            ApprovalRequest,
            {"task_id": "t", "action": "a", "state": "on_second_thought"},
            id="ApprovalState",
        ),
        pytest.param(
            Memory,
            {"category": "lunch_orders", "content": "c"},
            id="MemoryCategory",
        ),
        pytest.param(
            AgentResult,
            {"agent_name": "a", "status": "kinda", "output": ""},
            id="AgentStatus",
        ),
    ],
)
def test_invalid_enum_strings_rejected(cls, kwargs) -> None:
    with pytest.raises(ValidationError):
        cls(**kwargs)


NAIVE_DATETIME_CASES = [
    pytest.param(
        UserRequest,
        {"raw_input": "hi"},
        "created_at",
        id="UserRequest",
    ),
    pytest.param(TaskSpec, {"domain": Domain.CODING, "goal": "g"}, "created_at", id="TaskSpec"),
    pytest.param(
        Artifact, {"task_id": "t", "type": "x", "source": "s"}, "created_at", id="Artifact"
    ),
    pytest.param(ToolCall, {"tool": "http.get"}, "started_at", id="ToolCall"),
    pytest.param(
        Observation,
        {"domain": "d", "note": "n", "evidence": "e"},
        "recorded_at",
        id="Observation",
    ),
]


@pytest.mark.parametrize("cls,kwargs,field", NAIVE_DATETIME_CASES)
def test_naive_datetime_becomes_utc_aware(cls, kwargs, field) -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    model = cls.model_validate({**kwargs, field: naive})
    value = getattr(model, field)
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)
    # Naive input is treated as already-UTC: the wall clock is preserved.
    assert value == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_aware_datetime_converted_to_utc() -> None:
    spec = TaskSpec.model_validate(
        {"domain": "coding", "goal": "g", "created_at": "2026-01-01T12:00:00+07:00"}
    )
    assert spec.created_at.utcoffset() == timedelta(0)
    assert spec.created_at == datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)


RELEVANCE_BASE = {"category": MemoryCategory.LONG_TERM_FACTS, "content": "c"}


@pytest.mark.parametrize("value", [0.0, 0.25, 0.75, 1.0])
def test_memory_relevance_in_bounds_accepted(value: float) -> None:
    assert Memory(relevance=value, **RELEVANCE_BASE).relevance == value


@pytest.mark.parametrize("value", [-0.001, -1.0, 1.001, 1.5, float("nan"), float("inf")])
def test_memory_relevance_out_of_bounds_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        Memory(relevance=value, **RELEVANCE_BASE)


def test_missing_required_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(goal="g")  # no domain
    with pytest.raises(ValidationError):
        Artifact(type="x", source="s")  # no task_id
    with pytest.raises(ValidationError):
        WorkflowState(workflow="w")  # no task_id
    with pytest.raises(ValidationError):
        Memory(content="c")  # no category


# --------------------------------------------------------------------------- #
# 6. JSON safety
# --------------------------------------------------------------------------- #


def _reject_constant(_value: str):
    raise AssertionError(f"non-finite constant in JSON: {_value}")


# Minimal valid instances, one per exported model.
MINIMAL_INSTANCES: dict[str, BaseModel] = {
    "UserRequest": UserRequest(raw_input="hi"),
    "TaskSpec": TaskSpec(domain=Domain.UNKNOWN, goal="g"),
    "ContextSection": ContextSection(key="k", content="c"),
    "AgentContext": AgentContext(task=TaskSpec(domain=Domain.UNKNOWN, goal="g")),
    "ToolCall": ToolCall(tool="http.get"),
    "ToolResult": ToolResult(call_id="c", tool="http.get", ok=True),
    "TokenUsage": TokenUsage(),
    "VerificationCriterion": VerificationCriterion(criterion="c", passed=True),
    "VerificationResult": VerificationResult(verifier="v", passed=True),
    "Artifact": Artifact(task_id="t", type="x.md", source="s"),
    "ApprovalRequest": ApprovalRequest(task_id="t", action="a"),
    "Observation": Observation(domain="d", note="n", evidence="e"),
    "Memory": Memory(category=MemoryCategory.LONG_TERM_FACTS, content="c"),
    "UserModel": UserModel(user_id="u"),
    "AgentResult": AgentResult(agent_name="a", status=AgentStatus.SUCCESS, output=""),
    "WorkflowState": WorkflowState(task_id="t", workflow="w"),
    "WorkflowResult": WorkflowResult(
        task_id="t", workflow="w", status=WorkflowStatus.COMPLETED, output=""
    ),
}


def test_minimal_instances_cover_every_exported_model() -> None:
    exported = {
        name
        for name in contracts.__all__
        if isinstance(getattr(contracts, name, None), type)
        and issubclass(getattr(contracts, name), BaseModel)
    }
    assert exported == set(MINIMAL_INSTANCES), exported ^ set(MINIMAL_INSTANCES)


@pytest.mark.parametrize("name", sorted(MINIMAL_INSTANCES))
def test_model_dump_json_is_finite_and_parsable(name: str) -> None:
    model = MINIMAL_INSTANCES[name]
    dumped = model.model_dump_json()

    parsed = json.loads(dumped, parse_constant=_reject_constant)
    assert isinstance(parsed, dict), name
    for token in ("NaN", "Infinity", "-Infinity"):
        assert token not in dumped, f"{name} leaked {token}"

    # Same guarantee when the payload carries nesting.
    restored = type(model).model_validate_json(dumped)
    assert restored == model


def test_non_finite_floats_never_reach_json() -> None:
    """Any-typed payload fields must not emit NaN/Infinity either."""
    with pytest.raises(ValidationError):
        Memory(relevance=float("nan"), **RELEVANCE_BASE)
    ok = ToolResult(
        call_id="c", tool="db.query", ok=False, error="boom", result=None
    )
    assert json.loads(ok.model_dump_json(), parse_constant=_reject_constant)["result"] is None


# --------------------------------------------------------------------------- #
# 7. Unicode
# --------------------------------------------------------------------------- #

INDONESIAN_GOAL = "memahami subnetting dari dasar"


def test_task_spec_unicode_round_trip() -> None:
    spec = TaskSpec(
        domain=Domain.LEARNING,
        goal=INDONESIAN_GOAL,
        input={"topic": "subnetting", "glossary": {"en": "subnet", "zh": "子网划分"}},
        constraints={"language": "id", "difficulty": "beginner", "focus": "🚀"},
        mode=TaskMode.INTERACTIVE,
    )
    raw = spec.model_dump_json()
    restored = TaskSpec.model_validate_json(raw)

    assert restored == spec
    assert restored.goal == INDONESIAN_GOAL
    assert restored.input["glossary"]["zh"] == "子网划分"
    assert restored.constraints["focus"] == "🚀"

    # The escape-on-serialize behavior must not change the decoded string.
    decoded = json.loads(raw)
    assert decoded["goal"] == INDONESIAN_GOAL
    assert decoded["input"]["glossary"]["zh"] == "子网划分"
    assert decoded["constraints"]["focus"] == "🚀"


@pytest.mark.parametrize(
    "model_factory",
    [
        pytest.param(
            lambda: UserRequest(raw_input="tolong bantu saya 🚀 學習 subnetting"),
            id="UserRequest",
        ),
        pytest.param(
            lambda: Memory(
                category=MemoryCategory.LEARNING_PROGRESS,
                content="Belajar subnetting dari dasar 从零开始 ✅",
            ),
            id="Memory",
        ),
        pytest.param(
            lambda: ApprovalRequest(
                task_id="t1",
                action="发布报告 publish_report",
                details={"note": "お願いします 🙏"},
            ),
            id="ApprovalRequest",
        ),
        pytest.param(
            lambda: AgentResult(
                agent_name="peneliti",
                status=AgentStatus.SUCCESS,
                output="Laporan selesai 報告完成 🎉",
            ),
            id="AgentResult",
        ),
        pytest.param(
            lambda: Artifact(
                task_id="t1", type="laporan.md", source="agen_peneliti", uri="/tmp/laporan.md"
            ),
            id="Artifact",
        ),
    ],
)
def test_unicode_payloads_round_trip(model_factory) -> None:
    model = model_factory()
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model
