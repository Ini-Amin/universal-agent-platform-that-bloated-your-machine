"""Tests for the Context Compiler (Master section 15, 16, 29).

Covers the full assignment checklist: caps are hard, scoring is deterministic,
only relevant context survives, and empty inputs never raise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from uap.context import ContextBudget, ContextCompiler
from uap.contracts.models import (
    Domain,
    Memory,
    MemoryCategory,
    Observation,
    TaskSpec,
    UserModel,
    WorkflowState,
    WorkflowStatus,
)

# --------------------------------------------------------------------------- #
# Factories
# --------------------------------------------------------------------------- #


def _task(goal: str = "explain subnetting", **inputs: object) -> TaskSpec:
    return TaskSpec(
        domain=Domain.LEARNING,
        goal=goal,
        input=dict(inputs),
        constraints={},
    )


def _memory(
    content: str,
    *,
    memory_id: str,
    relevance: float = 1.0,
    created_at: datetime | None = None,
) -> Memory:
    return Memory(
        memory_id=memory_id,
        category=MemoryCategory.LONG_TERM_FACTS,
        content=content,
        relevance=relevance,
        created_at=created_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _state(task_id: str = "t1", **data: object) -> WorkflowState:
    return WorkflowState(
        task_id=task_id,
        workflow="LearningWorkflow",
        status=WorkflowStatus.RUNNING,
        current_node="plan",
        node_history=["start", "plan"],
        data=dict(data),
    )


def _user_model(*notes: str) -> UserModel:
    return UserModel(
        user_id="u1",
        observations=[
            Observation(domain="learning", note=note, evidence="quiz-42")
            for note in notes
        ],
    )


def _keys(context) -> list[str]:
    return [section.key for section in context.context_sections]


# --------------------------------------------------------------------------- #
# 1. Empty everything -> valid AgentContext, 0 sections
# --------------------------------------------------------------------------- #


def test_empty_everything_yields_valid_context_with_zero_sections() -> None:
    context = ContextCompiler().compile(_task())

    assert context.context_sections == []
    assert context.extras["selected_count"] == 0
    assert context.extras["dropped_count"] == 0
    assert context.available_tools == []
    assert context.available_skills == []
    assert context.budget == {"max_sections": 8, "max_total_chars": 8000}


# --------------------------------------------------------------------------- #
# 2. Only task -> 0 sections still valid
# --------------------------------------------------------------------------- #


def test_task_only_still_produces_valid_context() -> None:
    task = _task()
    context = ContextCompiler().compile(task, memory=[], knowledge=[])

    assert context.task is task
    assert context.context_sections == []
    assert context.extras == {"selected_count": 0, "dropped_count": 0}


# --------------------------------------------------------------------------- #
# 3. Relevance gating: relevant memory in, irrelevant memory out
# --------------------------------------------------------------------------- #


def test_relevant_memory_selected_and_irrelevant_memory_excluded() -> None:
    relevant = _memory("subnetting practice: VLSM and CIDR", memory_id="rel")
    irrelevant = _memory("best pizza recipe with basil", memory_id="irr")

    context = ContextCompiler().compile(
        _task("explain subnetting"), memory=[relevant, irrelevant]
    )

    keys = _keys(context)
    assert "memory:rel" in keys
    assert "memory:irr" not in keys
    assert context.extras["selected_count"] == 1
    assert context.extras["dropped_count"] == 1


# --------------------------------------------------------------------------- #
# 4. max_sections cap
# --------------------------------------------------------------------------- #


def test_max_sections_cap_enforced() -> None:
    goal = "subnetting"
    memories = [
        _memory(f"subnetting note {i}", memory_id=f"m{i:02d}") for i in range(20)
    ]

    context = ContextCompiler(ContextBudget(max_sections=8)).compile(
        _task(goal), memory=memories
    )

    assert len(context.context_sections) <= 8
    assert context.extras["selected_count"] <= 8
    assert context.extras["selected_count"] + context.extras["dropped_count"] == 20


# --------------------------------------------------------------------------- #
# 5. max_total_chars cap
# --------------------------------------------------------------------------- #


def test_max_total_chars_enforced() -> None:
    big = "subnetting " * 5000  # 55,000 chars, well over the cap
    memories = [_memory(big, memory_id=f"m{i}") for i in range(5)]

    context = ContextCompiler().compile(_task("subnetting"), memory=memories)

    total = sum(len(section.content) for section in context.context_sections)
    assert total <= 8000
    assert len(context.context_sections) <= 8


# --------------------------------------------------------------------------- #
# 6. Section truncation to max_chars_per_section
# --------------------------------------------------------------------------- #


def test_section_truncated_to_max_chars_per_section() -> None:
    long_content = "subnetting " * 1000  # 11,000 chars
    memory = _memory(long_content, memory_id="long")

    budget = ContextBudget(max_chars_per_section=2000, max_total_chars=100_000)
    context = ContextCompiler(budget).compile(_task("subnetting"), memory=[memory])

    assert len(context.context_sections) == 1
    assert len(context.context_sections[0].content) == 2000


# --------------------------------------------------------------------------- #
# 7. Source weighting: task_state beats memory on equal overlap
# --------------------------------------------------------------------------- #


def test_task_state_outranks_memory_on_equal_overlap() -> None:
    state = _state(topic="subnetting")
    memory = _memory("subnetting subnetting", memory_id="m1")

    context = ContextCompiler().compile(
        _task("subnetting"), memory=[memory], task_state=state
    )

    keys = _keys(context)
    assert keys[0] == "task_state"
    assert "memory:m1" in keys


# --------------------------------------------------------------------------- #
# 8. Memory.relevance multiplies score
# --------------------------------------------------------------------------- #


def test_memory_relevance_multiplies_score() -> None:
    weak = _memory("subnetting", memory_id="weak", relevance=0.1)
    strong = _memory("subnetting", memory_id="strong", relevance=1.0)

    # Budget of one section: only the high-relevance memory survives.
    context = ContextCompiler(ContextBudget(max_sections=1)).compile(
        _task("subnetting"), memory=[weak, strong]
    )

    assert _keys(context) == ["memory:strong"]


# --------------------------------------------------------------------------- #
# 9. Recency tiebreak deterministic
# --------------------------------------------------------------------------- #


def test_recency_tiebreak_is_deterministic() -> None:
    older = _memory(
        "subnetting", memory_id="old", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc)
    )
    newer = _memory(
        "subnetting", memory_id="new", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    compiler = ContextCompiler(ContextBudget(max_sections=1))
    task = _task("subnetting")

    first = compiler.compile(task, memory=[older, newer])
    second = compiler.compile(task, memory=[newer, older])

    assert _keys(first) == ["memory:new"]
    assert first.model_dump() == second.model_dump()


# --------------------------------------------------------------------------- #
# 10. Determinism: same inputs twice -> identical model_dump()
# --------------------------------------------------------------------------- #


def test_determinism_identical_model_dump() -> None:
    task = _task("explain subnetting", topic="CIDR")
    memories = [
        _memory("subnetting basics", memory_id="m1", relevance=0.5),
        _memory("cidr notation", memory_id="m2", relevance=0.9),
        _memory("pizza recipe", memory_id="m3"),
    ]
    knowledge = [
        {"key": "kb1", "content": "subnetting masks", "score": 0.2},
        {"key": "kb2", "content": "unrelated trivia"},
    ]
    state = _state(topic="subnetting")
    model = _user_model("struggles with subnetting math")

    compiler = ContextCompiler()
    first = compiler.compile(
        task, memory=memories, user_model=model, knowledge=knowledge, task_state=state
    )
    second = compiler.compile(
        task, memory=memories, user_model=model, knowledge=knowledge, task_state=state
    )

    assert first.model_dump() == second.model_dump()


# --------------------------------------------------------------------------- #
# 11. user_model section present with relevant observations
# --------------------------------------------------------------------------- #


def test_user_model_section_present_with_relevant_observation() -> None:
    model = _user_model("prefers subnetting drills over theory")

    context = ContextCompiler().compile(_task("subnetting"), user_model=model)

    keys = _keys(context)
    assert "user_model" in keys
    section = next(s for s in context.context_sections if s.key == "user_model")
    assert "subnetting drills" in section.content


def test_user_model_without_relevant_observation_is_dropped() -> None:
    model = _user_model("loves pizza")

    context = ContextCompiler().compile(_task("subnetting"), user_model=model)

    assert "user_model" not in _keys(context)


# --------------------------------------------------------------------------- #
# 12. Knowledge item with provided score contributes
# --------------------------------------------------------------------------- #


def test_knowledge_item_with_provided_score_contributes() -> None:
    knowledge = [{"key": "subnets", "content": "subnetting cheatsheet", "score": 0.7}]

    context = ContextCompiler().compile(_task("subnetting"), knowledge=knowledge)

    keys = _keys(context)
    assert "knowledge:subnets" in keys
    section = next(
        s for s in context.context_sections if s.key == "knowledge:subnets"
    )
    assert section.content == "subnetting cheatsheet"


def test_knowledge_without_score_still_scores_by_overlap() -> None:
    knowledge = [{"key": "plain", "content": "subnetting explained"}]

    context = ContextCompiler().compile(_task("subnetting"), knowledge=knowledge)

    assert "knowledge:plain" in _keys(context)


# --------------------------------------------------------------------------- #
# 13. extras counts correct
# --------------------------------------------------------------------------- #


def test_extras_counts_selected_plus_dropped_equals_candidates() -> None:
    task = _task("subnetting")
    memories = [
        _memory("subnetting a", memory_id="a"),
        _memory("subnetting b", memory_id="b"),
        _memory("pizza", memory_id="c"),
    ]
    knowledge = [{"key": "k", "content": "subnetting c"}]
    state = _state(topic="subnetting")
    model = _user_model("subnetting interest")

    context = ContextCompiler().compile(
        task,
        memory=memories,
        user_model=model,
        knowledge=knowledge,
        task_state=state,
    )

    # candidates: 3 memory + 1 knowledge + 1 user_model + 1 task_state = 6
    assert context.extras["selected_count"] + context.extras["dropped_count"] == 6
    assert context.extras["selected_count"] == len(context.context_sections)


# --------------------------------------------------------------------------- #
# Extra hardening
# --------------------------------------------------------------------------- #


def test_task_state_only_yields_one_section() -> None:
    context = ContextCompiler().compile(_task("subnetting"), task_state=_state())

    assert _keys(context) == ["task_state"]


def test_zero_budget_yields_no_sections() -> None:
    state = _state(topic="subnetting")
    budget = ContextBudget(max_sections=0, max_total_chars=0)
    context = ContextCompiler(budget).compile(_task("subnetting"), task_state=state)

    assert context.context_sections == []


def test_public_api_importable() -> None:
    from uap.context import ContextBudget as B
    from uap.context import ContextCompiler as C

    assert isinstance(B(), B)
    assert isinstance(C().budget, B)
