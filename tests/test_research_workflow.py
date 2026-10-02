"""Tests for the Research Workflow and its plain-asyncio runner (build Step 4)."""

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from uap.contracts.models import (
    Artifact,
    Domain,
    TaskSpec,
    VerificationCriterion,
    VerificationResult,
    WorkflowResult,
    WorkflowState,
    WorkflowStatus,
)
from uap.workflows import (
    NODES,
    InMemoryStateStore,
    NodeResult,
    ResearchWorkflow,
    WorkflowRunner,
)

QUESTION = "Is Python a programming language"


def make_task(question: str = QUESTION) -> TaskSpec:
    return TaskSpec(
        domain=Domain.RESEARCH,
        goal=f"research: {question}",
        input={"question": question},
    )


# --------------------------------------------------------------------------- #
# 1-3: full run, artifacts, checkpoint
# --------------------------------------------------------------------------- #


async def test_full_run_with_three_collectors_completes():
    workflow = ResearchWorkflow()
    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    assert result.workflow == "research"
    assert QUESTION in result.output
    assert result.error is None
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert state.data["question"] == QUESTION
    assert state.data["plan"]["strategy"] == "parallel"


async def test_artifacts_are_report_and_sources():
    workflow = ResearchWorkflow()
    result = await workflow.run(make_task())

    assert [artifact.type for artifact in result.artifacts] == ["report.md", "sources.json"]
    assert {artifact.source for artifact in result.artifacts} == {"research_workflow"}
    assert {artifact.status for artifact in result.artifacts} == {"final"}
    assert {artifact.task_id for artifact in result.artifacts} == {result.task_id}

    report = next(a for a in result.artifacts if a.type == "report.md")
    sources = next(a for a in result.artifacts if a.type == "sources.json")
    assert report.uri is None and sources.uri is None
    assert report.content_ref is not None and QUESTION in report.content_ref

    evidence = json.loads(sources.content_ref or "[]")
    assert len(evidence) == 4
    assert {item["source"] for item in evidence} == {"web", "papers", "docs"}
    assert all({"source", "claim", "url", "confidence"} <= item.keys() for item in evidence)


async def test_checkpoint_after_run_records_full_node_history():
    store = InMemoryStateStore()
    workflow = ResearchWorkflow(state_store=store)
    result = await workflow.run(make_task())

    state = store.load(result.task_id)
    assert state is not None
    assert state.status == WorkflowStatus.COMPLETED
    assert state.current_node is None
    assert state.node_history == list(NODES)
    assert state.retries == 0


# --------------------------------------------------------------------------- #
# 4-6: resume and fan-out
# --------------------------------------------------------------------------- #


async def test_resume_continues_from_mid_pipeline_checkpoint():
    store = InMemoryStateStore()
    workflow = ResearchWorkflow(state_store=store)
    completed = await workflow.run(make_task())
    assert completed.status == WorkflowStatus.COMPLETED

    # Simulate a crash: rewind the persisted state to a mid-pipeline checkpoint.
    state = store.load(completed.task_id)
    assert state is not None
    mid = NODES.index("evidence_filtering")
    state.status = WorkflowStatus.CHECKPOINTED
    state.current_node = NODES[mid]
    state.node_history = list(NODES[:mid])
    state.artifacts = []
    for key in ("filtered_evidence", "verified_claims", "unverified_claims", "synthesis", "output", "verification", "error"):
        state.data.pop(key, None)
    store.save(state)

    resumed = await workflow.resume(completed.task_id)
    assert resumed.status == WorkflowStatus.COMPLETED
    assert QUESTION in resumed.output

    after = store.load(completed.task_id)
    assert after is not None
    assert after.node_history == list(NODES)  # earlier nodes were not re-run
    assert after.status == WorkflowStatus.COMPLETED
    assert [artifact.type for artifact in resumed.artifacts] == ["report.md", "sources.json"]


async def test_resume_rejects_unknown_and_terminal_tasks():
    workflow = ResearchWorkflow()

    with pytest.raises(KeyError):
        await workflow.resume("task-does-not-exist")

    result = await workflow.run(make_task())
    with pytest.raises(ValueError):
        await workflow.resume(result.task_id)


async def test_fan_out_runs_collectors_concurrently():
    timestamps: list[datetime] = []

    def collector(name: str):
        async def collect(question: str):
            await asyncio.sleep(0.05)
            timestamps.append(datetime.now(timezone.utc))
            return [{"source": name, "claim": f"{name} claim", "url": f"https://{name}", "confidence": 0.9}]

        return collect

    workflow = ResearchWorkflow(collectors=[collector("web"), collector("papers"), collector("docs")])

    start = time.perf_counter()
    result = await workflow.run(make_task())
    elapsed = time.perf_counter() - start

    assert result.status == WorkflowStatus.COMPLETED
    assert elapsed < 0.12  # 3 x 50ms sequentially would be ~150ms
    assert len(timestamps) == 3
    span = (max(timestamps) - min(timestamps)).total_seconds()
    assert span < 0.05  # the three collections overlapped in time


async def test_single_collector_completes_without_gather():
    async def solo(question: str):
        return [{"source": "web", "claim": "solo claim", "url": "https://solo", "confidence": 0.9}]

    workflow = ResearchWorkflow(collectors=[solo])
    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert state.data["plan"]["strategy"] == "sequential"
    assert len(state.data["evidence"]) == 1


# --------------------------------------------------------------------------- #
# 7-9: filtering, cross-verification, review
# --------------------------------------------------------------------------- #


async def test_filtering_drops_low_confidence_and_duplicate_urls():
    async def web(question: str):
        return [
            {"source": "web", "claim": "kept", "url": "https://dup", "confidence": 0.9},
            {"source": "web", "claim": "duplicate url", "url": "https://dup", "confidence": 0.8},
        ]

    async def papers(question: str):
        return [{"source": "papers", "claim": "too weak", "url": "https://weak", "confidence": 0.1}]

    workflow = ResearchWorkflow(collectors=[web, papers])
    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert [item["url"] for item in state.data["filtered_evidence"]] == ["https://dup"]
    assert all(item["confidence"] >= 0.3 for item in state.data["filtered_evidence"])


async def test_custom_confidence_threshold():
    async def web(question: str):
        return [{"source": "web", "claim": "mid", "url": "https://mid", "confidence": 0.4}]

    workflow = ResearchWorkflow(collectors=[web], confidence_threshold=0.5)
    result = await workflow.run(make_task())

    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert state.data["filtered_evidence"] == []
    assert result.verification is not None
    assert result.verification.passed is False


async def test_cross_verification_counts_independent_sources():
    async def web(question: str):
        return [
            {"source": "web", "claim": "Shared claim", "url": "https://a", "confidence": 0.9},
            {"source": "web", "claim": "Lonely claim", "url": "https://b", "confidence": 0.9},
        ]

    async def papers(question: str):
        return [{"source": "papers", "claim": "SHARED CLAIM", "url": "https://c", "confidence": 0.8}]

    workflow = ResearchWorkflow(collectors=[web, papers])
    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None

    verified = {claim["claim"]: claim for claim in state.data["verified_claims"]}
    unverified = {claim["claim"]: claim for claim in state.data["unverified_claims"]}
    assert "Shared claim" in verified
    assert sorted(verified["Shared claim"]["sources"]) == ["papers", "web"]
    assert verified["Shared claim"]["agreement"] == 2
    assert "Lonely claim" in unverified
    assert unverified["Lonely claim"]["agreement"] == 1


async def test_review_criteria_names_and_pass():
    workflow = ResearchWorkflow()
    result = await workflow.run(make_task())

    assert result.verification is not None
    assert result.verification.verifier == "research_review"
    assert result.verification.passed is True
    assert [criterion.criterion for criterion in result.verification.criteria] == [
        "evidence_count",
        "sources_have_urls",
        "synthesis_covers_all_verified",
    ]
    assert all(criterion.passed for criterion in result.verification.criteria)
    assert result.verification.score == 1.0


async def test_failed_review_marks_workflow_failed():
    async def urlless(question: str):
        return [{"source": "web", "claim": "no link", "url": "", "confidence": 0.9}]

    workflow = ResearchWorkflow(collectors=[urlless])
    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.FAILED
    assert result.verification is not None
    assert result.verification.passed is False
    assert "sources_have_urls" in (result.error or "")


# --------------------------------------------------------------------------- #
# 10-12: failures, versioning, serialisation
# --------------------------------------------------------------------------- #


async def test_collector_failure_retries_then_fails():
    attempts: list[float] = []

    async def boom(question: str):
        attempts.append(time.perf_counter())
        raise RuntimeError("collector exploded")

    workflow = ResearchWorkflow(collectors=[boom], max_retries=2)
    result = await workflow.run(make_task())

    assert result.status == WorkflowStatus.FAILED
    assert "collector exploded" in (result.error or "")
    assert "fan_out" in (result.error or "")
    assert len(attempts) == 3  # one attempt plus two retries

    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert state.status == WorkflowStatus.FAILED
    assert state.retries == 3
    assert "fan_out" in state.data["error"]
    assert state.node_history == ["question_analysis", "research_planning", "fan_out"]


async def test_version_increments_once_per_executed_node():
    store = InMemoryStateStore()
    workflow = ResearchWorkflow(state_store=store)
    result = await workflow.run(make_task())

    state = store.load(result.task_id)
    assert state is not None
    assert state.version == 1 + len(NODES)
    assert len(state.node_history) == len(NODES)


async def test_result_and_state_round_trip_json():
    workflow = ResearchWorkflow()
    result = await workflow.run(make_task())

    restored_result = WorkflowResult.model_validate_json(result.model_dump_json())
    assert restored_result.model_dump(mode="json") == result.model_dump(mode="json")

    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    restored_state = WorkflowState.model_validate_json(state.model_dump_json())
    assert restored_state.model_dump(mode="json") == state.model_dump(mode="json")
    assert restored_state.node_history == list(NODES)
    assert isinstance(restored_state.data["verification"], dict)

    artifact = Artifact(
        task_id=result.task_id,
        type="report.md",
        source="research_workflow",
        content_ref="# hello",
    )
    assert Artifact.model_validate_json(artifact.model_dump_json()) == artifact


# --------------------------------------------------------------------------- #
# 13-14: the runner, independent of research
# --------------------------------------------------------------------------- #


async def test_runner_drives_a_linear_workflow_and_checkpoints():
    async def first(state: WorkflowState) -> NodeResult:
        return NodeResult({"count": 1}, next_node="second")

    async def second(state: WorkflowState) -> NodeResult:
        return NodeResult({"count": state.data["count"] + 1, "output": "done"}, next_node=None)

    store = InMemoryStateStore()
    runner = WorkflowRunner("echo", {"first": first, "second": second}, entry="first", state_store=store)
    result = await runner.run(make_task())

    assert result.status == WorkflowStatus.COMPLETED
    assert result.workflow == "echo"
    assert result.output == "done"
    assert result.verification is None

    state = store.load(result.task_id)
    assert state is not None
    assert state.data["count"] == 2
    assert state.node_history == ["first", "second"]
    assert state.version == 1 + 2

    with pytest.raises(ValueError):
        WorkflowRunner("bad", {"first": first}, entry="missing")


async def test_runner_fails_on_failed_verification():
    async def review(state: WorkflowState) -> NodeResult:
        verdict = VerificationResult(
            verifier="unit",
            passed=False,
            criteria=[VerificationCriterion(criterion="sanity", passed=False, evidence="0/1")],
            notes="failed criteria: sanity",
        )
        return NodeResult({"verification": verdict}, next_node=None)

    runner = WorkflowRunner("checked", {"review": review}, entry="review")
    result = await runner.run(make_task())

    assert result.status == WorkflowStatus.FAILED
    assert result.verification is not None
    assert result.verification.passed is False
    assert (result.error or "").startswith("verification failed: failed criteria: sanity")
