"""Contract tests for build Step 1 - Core Contracts (Master section 27)."""

import re
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

CORE_OBJECTS = [
    "UserRequest",
    "TaskSpec",
    "AgentContext",
    "AgentResult",
    "ToolCall",
    "ToolResult",
    "WorkflowState",
    "WorkflowResult",
    "Artifact",
    "VerificationResult",
    "ApprovalRequest",
    "Memory",
    "UserModel",
]


def make_request() -> UserRequest:
    return UserRequest(
        raw_input="Teach me subnetting from scratch",
        user_id="u1",
        session_id="s1",
        metadata={"lang": "id"},
    )


def make_task() -> TaskSpec:
    return TaskSpec(
        domain=Domain.RESEARCH,
        goal="survey subnetting literature",
        input={"topic": "subnetting"},
        constraints={"language": "id", "difficulty": "beginner"},
        mode=TaskMode.AUTONOMOUS,
        verification=False,
    )


def make_artifact() -> Artifact:
    return Artifact(
        task_id="t1",
        type="report.md",
        source="research_agent",
        status=ArtifactStatus.FINAL,
        uri="/tmp/artifacts/t1/report.md",
        content_ref="sha256:abc",
    )


def make_tool_call() -> ToolCall:
    return ToolCall(tool="http.get", args={"url": "https://example.com"})


def make_tool_result() -> ToolResult:
    return ToolResult(
        call_id="c1", tool="http.get", ok=True, result={"status": 200}, duration_ms=12.5
    )


def make_context() -> AgentContext:
    return AgentContext(
        task=make_task(),
        context_sections=[ContextSection(key="memory", content="some memory")],
        available_tools=["http.get", "read_file"],
        available_skills=["api_recon"],
        budget={"tokens": 50000},
        extras={"trace_id": "x"},
    )


def make_agent_result() -> AgentResult:
    return AgentResult(
        agent_name="researcher",
        status=AgentStatus.PARTIAL,
        output="## findings",
        artifacts=[make_artifact()],
        tool_calls=[make_tool_call()],
        verification=make_verification(),
        usage=TokenUsage(tokens_in=10, tokens_out=20, latency_ms=3.5),
        error=None,
    )


def make_verification() -> VerificationResult:
    return VerificationResult(
        verifier="evidence_checker",
        passed=True,
        criteria=[
            VerificationCriterion(criterion="has sources", passed=True, evidence="[1]")
        ],
        score=0.9,
        notes=None,
    )


def make_approval() -> ApprovalRequest:
    return ApprovalRequest(
        task_id="t1",
        action="publish_report",
        details={"channel": "email"},
        state=ApprovalState.APPROVED,
        requested_at=utc_now(),
        decided_at=utc_now(),
        decided_by="human:1",
    )


def make_workflow_state() -> WorkflowState:
    return WorkflowState(
        task_id="t1",
        workflow="research",
        status=WorkflowStatus.CHECKPOINTED,
        current_node="research",
        node_history=["intake", "plan", "research"],
        retries=1,
        pending_approval=make_approval(),
        artifacts=[make_artifact()],
        data={"sources": [1, 2, 3]},
        version=4,
    )


def make_workflow_result() -> WorkflowResult:
    return WorkflowResult(
        task_id="t1",
        workflow="research",
        status=WorkflowStatus.COMPLETED,
        output="done",
        artifacts=[make_artifact()],
        verification=make_verification(),
    )


def make_memory() -> Memory:
    return Memory(
        category=MemoryCategory.PROJECT_CONTEXT,
        content="target scope is example.com",
        metadata={"source": "brain"},
        relevance=0.75,
        expires_at=None,
        last_accessed_at=utc_now(),
    )


def make_user_model() -> UserModel:
    return UserModel(
        user_id="u1",
        preferences={"language": "id"},
        domain_skill_levels={"learning": "advanced", "research": "beginner"},
        observations=[
            Observation(domain="coding", note="likes pytest", evidence="observed run")
        ],
    )


def all_samples() -> list[BaseModel]:
    return [
        make_request(),
        make_task(),
        make_context(),
        make_agent_result(),
        make_tool_call(),
        make_tool_result(),
        make_workflow_state(),
        make_workflow_result(),
        make_artifact(),
        make_verification(),
        make_approval(),
        make_memory(),
        make_user_model(),
    ]


@pytest.mark.parametrize("model", all_samples(), ids=lambda m: type(m).__name__)
def test_json_round_trip(model: BaseModel) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model


def test_nested_workflow_state_round_trip() -> None:
    state = make_workflow_state()
    assert state.pending_approval is not None
    assert state.artifacts
    restored = WorkflowState.model_validate_json(state.model_dump_json())
    assert restored == state
    assert restored.pending_approval == state.pending_approval
    assert restored.artifacts == state.artifacts


def test_nested_agent_result_round_trip() -> None:
    result = make_agent_result()
    assert result.tool_calls
    restored = AgentResult.model_validate_json(result.model_dump_json())
    assert restored == result
    assert restored.tool_calls == result.tool_calls
    assert restored.usage == result.usage


@pytest.mark.parametrize(
    "cls,kwargs",
    [
        (TaskSpec, {"domain": "not_a_domain", "goal": "g"}),
        (TaskSpec, {"domain": "research", "goal": "g", "mode": "reckless"}),
        (AgentResult, {"agent_name": "a", "status": "kinda", "output": ""}),
        (WorkflowState, {"task_id": "t", "workflow": "w", "status": "limbo"}),
        (WorkflowResult, {"task_id": "t", "workflow": "w", "status": "meh", "output": ""}),
        (Artifact, {"task_id": "t", "type": "x", "source": "s", "status": "brand_new"}),
        (
            ApprovalRequest,
            {"task_id": "t", "action": "a", "state": "on_second_thought"},
        ),
        (Memory, {"category": "lunch_orders", "content": "c"}),
        (UserModel, {"user_id": "u", "observations": [{"nope": 1}]}),
    ],
)
def test_invalid_values_rejected(cls, kwargs) -> None:
    with pytest.raises(ValidationError):
        cls(**kwargs)


def test_enum_values_are_the_spec_strings() -> None:
    assert Domain.LEARNING == "learning"
    assert Domain.BBP == "bbp"
    assert Domain.UNKNOWN == "unknown"
    assert TaskMode.INTERACTIVE == "interactive"
    assert WorkflowStatus.AWAITING_APPROVAL == "awaiting_approval"
    assert ApprovalState.PENDING_APPROVAL == "pending_approval"
    assert ArtifactStatus.SUPERSEDED == "superseded"
    assert MemoryCategory.LONG_TERM_FACTS == "long_term_facts"


def test_uuid_defaults_are_unique() -> None:
    ids = [TaskSpec(domain=Domain.DATA, goal="g").task_id for _ in range(25)]
    ids += [make_artifact().artifact_id for _ in range(1)]
    assert len(set(ids)) == len(ids)
    assert all(call.call_id for call in [make_tool_call()])
    approvals = {make_approval().approval_id for _ in range(5)}
    assert len(approvals) == 5


def test_default_timestamps_are_utc_aware() -> None:
    now = utc_now()
    for model in all_samples():
        for name, value in model:
            if isinstance(value, datetime):
                assert value.tzinfo is not None, f"{type(model).__name__}.{name}"
                assert value.utcoffset() == timedelta(0), f"{type(model).__name__}.{name}"


def test_datetime_coercion_from_naive_and_string() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)
    request = UserRequest.model_validate({**make_request().model_dump(), "created_at": naive})
    assert request.created_at.utcoffset() == timedelta(0)
    assert request.created_at.hour == 12

    task = TaskSpec.model_validate(
        {
            "domain": "coding",
            "goal": "g",
            "created_at": "2026-01-01T12:00:00+07:00",
        }
    )
    assert task.created_at.utcoffset() == timedelta(0)
    assert task.created_at == datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)


def test_task_spec_defaults() -> None:
    spec = TaskSpec(domain=Domain.UNKNOWN, goal="clarify")
    assert spec.mode == TaskMode.INTERACTIVE
    assert spec.verification is True
    assert spec.input == {}
    assert spec.constraints == {}
    assert spec.task_id


def test_memory_relevance_bounds() -> None:
    base = {"category": "long_term_facts", "content": "c"}
    with pytest.raises(ValidationError):
        Memory(relevance=1.5, **base)
    with pytest.raises(ValidationError):
        Memory(relevance=-0.1, **base)
    assert Memory(relevance=0.0, **base).relevance == 0.0
    assert Memory(relevance=1.0, **base).relevance == 1.0


def test_optional_fields_default_to_none() -> None:
    request = make_request()
    assert request.user_id is None or isinstance(request.user_id, str)
    bare = UserRequest(raw_input="hi")
    assert bare.user_id is None and bare.session_id is None and bare.metadata == {}

    result = AgentResult(agent_name="a", status=AgentStatus.FAILED, output="")
    assert result.artifacts == [] and result.tool_calls == []
    assert result.verification is None and result.error is None
    assert result.usage == TokenUsage()

    state = WorkflowState(task_id="t", workflow="w")
    assert state.status == WorkflowStatus.PENDING
    assert state.current_node is None
    assert state.node_history == [] and state.data == {}
    assert state.version == 1 and state.retries == 0
    assert state.pending_approval is None and state.artifacts == []


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(domain="learning", goal="g", surprise=True)
    with pytest.raises(ValidationError):
        WorkflowState(task_id="t", workflow="w", surprise=True)


def test_unknown_tool_result_is_json_safe() -> None:
    res = ToolResult(call_id="c", tool="db.query", ok=False, error="boom", result=None)
    assert ToolResult.model_validate_json(res.model_dump_json()) == res


def test_all_core_objects_exported() -> None:
    for name in CORE_OBJECTS:
        assert hasattr(contracts, name), name
        assert name in contracts.__all__, name
    for name in contracts.__all__:
        assert name in dir(contracts), name


def test_contracts_stay_infrastructure_free() -> None:
    """Master section 29.9: no UI/db/HTTP/MCP/LangGraph coupling in contracts."""
    import inspect
    import uap.contracts.models as models

    source = inspect.getsource(models)
    forbidden = re.findall(
        r"^\s*(?:from|import)\s+(fastapi|langgraph|langgraph_core|sqlalchemy|"
        r"sqlite3|requests|httpx|aiohttp|uvicorn|starlette|redis|anyio)\b",
        source,
        flags=re.MULTILINE,
    )
    assert forbidden == []


def test_model_config_forbids_unknown_fields() -> None:
    for model in all_samples():
        with pytest.raises(ValidationError):
            type(model).model_validate({**model.model_dump(), "typo_field": 1})
