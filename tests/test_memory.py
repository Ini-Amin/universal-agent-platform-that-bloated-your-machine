"""Tests for Step 11 - Memory + User Model (Master sections 15, 16, 21, 29).

Memory is separated from runtime state: these tests cover persistence,
querying, expiry, the deterministic extractor's lifecycle rules, and the
evidence-backed user model.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from uap.contracts import (
    Artifact,
    Domain,
    Memory,
    MemoryCategory,
    Observation,
    TaskSpec,
    UserModel,
    WorkflowResult,
    WorkflowStatus,
    utc_now,
)
from uap.memory import MemoryExtractor, MemoryStore, UserModelStore


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _memory(**overrides: object) -> Memory:
    base: dict[str, object] = {
        "category": MemoryCategory.TASK_HISTORY,
        "content": "did a thing",
        "relevance": 0.8,
    }
    base.update(overrides)
    return Memory(**base)  # type: ignore[arg-type]


def _task(**overrides: object) -> TaskSpec:
    base: dict[str, object] = {"domain": Domain.RESEARCH, "goal": "find X"}
    base.update(overrides)
    return TaskSpec(**base)  # type: ignore[arg-type]


def _result(
    status: WorkflowStatus, output: str = "", **overrides: object
) -> WorkflowResult:
    base: dict[str, object] = {
        "task_id": "task-1",
        "workflow": "ResearchWorkflow",
        "status": status,
        "output": output,
    }
    base.update(overrides)
    return WorkflowResult(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 1. add/get round-trip returns the exact model
# --------------------------------------------------------------------------- #


def test_add_get_round_trip_is_exact() -> None:
    store = MemoryStore()
    memory = _memory(metadata={"task_id": "t1", "workflow": "W"})
    assert store.add(memory) == memory

    fetched = store.get(memory.memory_id)
    assert fetched is not None
    # last_accessed_at is the only field get() is allowed to touch.
    assert fetched.model_copy(update={"last_accessed_at": None}) == memory
    store.close()


# --------------------------------------------------------------------------- #
# 2. get touches last_accessed_at
# --------------------------------------------------------------------------- #


def test_get_touches_last_accessed_at() -> None:
    store = MemoryStore()
    memory = _memory()
    assert memory.last_accessed_at is None
    store.add(memory)

    first = store.get(memory.memory_id)
    assert first is not None and first.last_accessed_at is not None

    second = store.get(memory.memory_id)
    assert second is not None and second.last_accessed_at is not None
    assert second.last_accessed_at > first.last_accessed_at
    store.close()


# --------------------------------------------------------------------------- #
# 3. get unknown -> None
# --------------------------------------------------------------------------- #


def test_get_unknown_returns_none() -> None:
    store = MemoryStore()
    assert store.get("does-not-exist") is None
    store.close()


# --------------------------------------------------------------------------- #
# 4. query by category filters
# --------------------------------------------------------------------------- #


def test_query_by_category_filters() -> None:
    store = MemoryStore()
    store.add(_memory(category=MemoryCategory.TASK_HISTORY))
    store.add(_memory(category=MemoryCategory.LONG_TERM_FACTS))
    store.add(_memory(category=MemoryCategory.LONG_TERM_FACTS))

    history = store.query(category=MemoryCategory.TASK_HISTORY)
    assert len(history) == 1
    assert history[0].category == MemoryCategory.TASK_HISTORY

    facts = store.query(category="long_term_facts")
    assert len(facts) == 2
    assert all(m.category == MemoryCategory.LONG_TERM_FACTS for m in facts)
    store.close()


# --------------------------------------------------------------------------- #
# 5. query min_relevance filters
# --------------------------------------------------------------------------- #


def test_query_min_relevance_filters() -> None:
    store = MemoryStore()
    store.add(_memory(relevance=0.2))
    store.add(_memory(relevance=0.5))
    store.add(_memory(relevance=0.9))

    kept = store.query(min_relevance=0.5)
    assert sorted(m.relevance for m in kept) == [0.5, 0.9]
    store.close()


# --------------------------------------------------------------------------- #
# 6. query limit respected
# --------------------------------------------------------------------------- #


def test_query_limit_respected() -> None:
    store = MemoryStore()
    for index in range(5):
        store.add(_memory(relevance=index / 10))

    assert len(store.query(limit=3)) == 3
    assert len(store.query(limit=100)) == 5
    store.close()


# --------------------------------------------------------------------------- #
# 7. update_relevance works and persists
# --------------------------------------------------------------------------- #


def test_update_relevance_persists() -> None:
    store = MemoryStore()
    memory = _memory(relevance=0.8)
    store.add(memory)

    assert store.update_relevance(memory.memory_id, 0.3) is True
    reloaded = store.get(memory.memory_id)
    assert reloaded is not None and reloaded.relevance == 0.3
    store.close()


def test_update_relevance_unknown_returns_false() -> None:
    store = MemoryStore()
    assert store.update_relevance("nope", 0.5) is False
    store.close()


def test_update_relevance_out_of_range_raises() -> None:
    store = MemoryStore()
    memory = store.add(_memory())
    with pytest.raises(ValueError):
        store.update_relevance(memory.memory_id, 1.5)
    store.close()


# --------------------------------------------------------------------------- #
# 8. delete removes + returns True; second delete False
# --------------------------------------------------------------------------- #


def test_delete_removes_and_second_delete_is_false() -> None:
    store = MemoryStore()
    memory = store.add(_memory())

    assert store.delete(memory.memory_id) is True
    assert store.get(memory.memory_id) is None
    assert store.delete(memory.memory_id) is False
    store.close()


# --------------------------------------------------------------------------- #
# 9. purge_expired removes only expired; expires_at None survives
# --------------------------------------------------------------------------- #


def test_purge_expired_removes_only_expired() -> None:
    store = MemoryStore()
    now = utc_now()

    expired = store.add(_memory(content="old", expires_at=now - timedelta(hours=1)))
    live = store.add(_memory(content="live", expires_at=now + timedelta(hours=1)))
    permanent = store.add(_memory(content="forever", expires_at=None))

    removed = store.purge_expired(now)
    assert removed == 1
    assert store.get(expired.memory_id) is None
    assert store.get(live.memory_id) is not None
    assert store.get(permanent.memory_id) is not None
    assert store.count() == 2
    store.close()


# --------------------------------------------------------------------------- #
# 10. count matches
# --------------------------------------------------------------------------- #


def test_count_matches_added_rows() -> None:
    store = MemoryStore()
    assert store.count() == 0
    for _ in range(4):
        store.add(_memory())
    assert store.count() == 4
    store.close()


# --------------------------------------------------------------------------- #
# 11. Extractor: completed substantial task -> 1 memory
# --------------------------------------------------------------------------- #


def test_extractor_completed_substantial_task() -> None:
    task = _task(task_id="task-42", domain=Domain.RESEARCH, goal="survey topic")
    result = _result(
        WorkflowStatus.COMPLETED,
        output="A" * 200,
        task_id="task-42",
        workflow="ResearchWorkflow",
        artifacts=[Artifact(task_id="task-42", type="report", source="workflow")],
    )

    memories = MemoryExtractor().extract(task, result)

    assert len(memories) == 1
    memory = memories[0]
    assert memory.category == MemoryCategory.TASK_HISTORY
    assert memory.metadata["task_id"] == "task-42"
    assert memory.metadata["workflow"] == "ResearchWorkflow"
    assert "research" in memory.content
    assert "report" in memory.content
    assert memory.relevance == pytest.approx(0.8)
    assert memory.expires_at is not None


# --------------------------------------------------------------------------- #
# 12. Extractor: failed task -> relevance 0.4
# --------------------------------------------------------------------------- #


def test_extractor_failed_task_has_low_relevance() -> None:
    task = _task(task_id="task-7")
    result = _result(
        WorkflowStatus.FAILED, output="boom", task_id="task-7", error="boom"
    )

    memories = MemoryExtractor().extract(task, result)

    assert len(memories) == 1
    assert memories[0].category == MemoryCategory.TASK_HISTORY
    assert memories[0].relevance == pytest.approx(0.4)


# --------------------------------------------------------------------------- #
# 13. Extractor: short output -> []
# --------------------------------------------------------------------------- #


def test_extractor_short_output_returns_empty() -> None:
    task = _task()
    assert MemoryExtractor().extract(task, _result(WorkflowStatus.COMPLETED, "hi")) == []
    assert MemoryExtractor().extract(task, _result(WorkflowStatus.COMPLETED, "")) == []


def test_extractor_non_terminal_status_returns_empty() -> None:
    task = _task()
    running = _result(WorkflowStatus.RUNNING, output="x" * 200)
    assert MemoryExtractor().extract(task, running) == []


def test_extractor_never_exceeds_two_candidates() -> None:
    task = _task()
    result = _result(WorkflowStatus.COMPLETED, output="x" * 200)
    assert len(MemoryExtractor().extract(task, result)) <= 2


# --------------------------------------------------------------------------- #
# 14. UserModelStore default + observations persist across store instances
# --------------------------------------------------------------------------- #


def test_user_model_default_is_empty() -> None:
    store = UserModelStore()
    model = store.get("alice")
    assert isinstance(model, UserModel)
    assert model.user_id == "alice"
    assert model.preferences == {}
    assert model.domain_skill_levels == {}
    assert model.observations == []
    store.close()


def test_observations_persist_across_store_instances(tmp_path) -> None:
    db = tmp_path / "memory.db"
    observation = Observation(
        domain="learning", note="solved 3 subnetting drills", evidence="quiz #12"
    )

    first = UserModelStore(db)
    returned = first.add_observation("alice", observation)
    assert returned.observations == [observation]
    first.close()

    second = UserModelStore(db)
    reloaded = second.get("alice")
    assert len(reloaded.observations) == 1
    assert reloaded.observations[0].note == "solved 3 subnetting drills"
    assert reloaded.observations[0].evidence == "quiz #12"
    second.close()


# --------------------------------------------------------------------------- #
# 15. set_skill_level + set_preference persist
# --------------------------------------------------------------------------- #


def test_skill_level_and_preference_persist(tmp_path) -> None:
    db = tmp_path / "user.db"

    store = UserModelStore(db)
    store.set_skill_level("bob", "coding", "intermediate")
    store.set_preference("bob", "language", "id")
    store.close()

    reopened = UserModelStore(db)
    model = reopened.get("bob")
    assert model.domain_skill_levels["coding"] == "intermediate"
    assert model.preferences["language"] == "id"
    reopened.close()


def test_set_calls_return_updated_model() -> None:
    store = UserModelStore()
    model = store.set_preference("carol", "theme", "dark")
    assert model.preferences["theme"] == "dark"
    model = store.set_skill_level("carol", "research", "advanced")
    assert model.domain_skill_levels["research"] == "advanced"
    assert model.preferences["theme"] == "dark"  # prior write preserved
    store.close()


# --------------------------------------------------------------------------- #
# 16. Memory model JSON round-trip from the store row
# --------------------------------------------------------------------------- #


def test_memory_json_round_trip_from_row(tmp_path) -> None:
    db = tmp_path / "mem.db"
    store = MemoryStore(db)
    memory = _memory(metadata={"k": [1, 2, 3]}, expires_at=utc_now() + timedelta(days=1))
    store.add(memory)
    store.close()

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT model_json, category, relevance, created_at FROM memories "
            "WHERE memory_id = ?",
            (memory.memory_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    raw_json, category, relevance, created_at = row
    restored = Memory.model_validate(json.loads(raw_json))
    assert restored == memory
    assert category == MemoryCategory.TASK_HISTORY.value
    assert relevance == pytest.approx(0.8)
    assert created_at == memory.created_at.isoformat()
