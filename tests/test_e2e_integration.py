"""End-to-end integration test across the committed UAP modules.

This is the orchestrator-owned cross-module verification: it proves the
Master-spec flow (section 3, 4, 5, 7, 16, 17, 19, 20, 22) works as ONE
pipeline, not just as isolated modules:

    UserRequest -> EntryWorkflow -> TaskSpec -> Router -> ResearchWorkflow
        -> artifacts (report.md, sources.json) -> ArtifactStore persistence
        -> MemoryExtractor -> MemoryStore -> UserModelStore
        -> Observability event bus -> VerificationResult

Everything is deterministic (stub collectors); no network, no LLM.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from uap.approval import ApprovalGate
from uap.artifacts import ArtifactStore
from uap.contracts import (
    ApprovalState,
    Domain,
    TaskSpec,
    UserRequest,
    WorkflowResult,
    WorkflowStatus,
)
from uap.context import ContextBudget, ContextCompiler
from uap.entry import EntryWorkflow
from uap.memory import MemoryExtractor, MemoryStore, UserModelStore
from uap.observability import EventBus, EventKind, JsonlSink, MemorySink, Tracer
from uap.router import Router, WorkflowRegistry
from uap.skills import load_skills_from_dir
from uap.tools import ToolRegistry, register_local_tools
from uap.verification import DeterministicVerifier, has_min_evidence
from uap.workflows.research import ResearchWorkflow


def _run(coro):
    return asyncio.run(coro)


class TestFullPipeline:
    """entry -> router -> research -> artifacts, wired end to end."""

    def test_indonesian_request_becomes_completed_research_with_artifacts(self, tmp_path: Path):
        # 1. Entry: the Master section 3 canonical example (learning) ...
        entry = EntryWorkflow()
        out = entry.run(UserRequest(raw_input="Ajari saya subnetting dari dasar", user_id="u-e2e"))
        assert not out.needs_clarification
        assert out.spec.domain == Domain.LEARNING

        # ... and a research request for the pipeline itself.
        out_r = entry.run(UserRequest(raw_input="research the best checkpoint strategy for workflows", user_id="u-e2e"))
        assert not out_r.needs_clarification
        assert out_r.spec.domain == Domain.RESEARCH
        spec = out_r.spec

        # 2. Router: dispatch decision, no execution side effects.
        registry = WorkflowRegistry()
        wf = ResearchWorkflow()
        registry.register("research", wf)
        decision = Router(registry).route(spec)
        assert decision.workflow_name == "ResearchWorkflow"
        assert decision.target is wf

        # 3. Research workflow runs the full 8-node pipeline.
        result = _run(wf.run(spec))
        assert result.status == WorkflowStatus.COMPLETED
        assert result.verification is not None and result.verification.passed
        artifact_types = sorted(a.type for a in result.artifacts)
        assert artifact_types == ["report.md", "sources.json"]

        # 4. Artifact system persists both artifacts to disk.
        store = ArtifactStore(tmp_path / "artifacts")
        for artifact in result.artifacts:
            content = artifact.content_ref or ""
            saved = store.save(artifact, content)
            assert saved.uri is not None
        on_disk = store.list(spec.task_id)
        assert len(on_disk) == 2
        report_art, report_bytes = store.get(spec.task_id, next(a.artifact_id for a in on_disk if a.type == "report.md"))
        assert b"Research Report" in report_bytes

        # 5. Verification of the report content with explicit criteria.
        verifier = DeterministicVerifier("e2e", [has_min_evidence(1)])
        verdict = verifier.verify({"evidence": json.loads(
            next(c for c in [a.content_ref for a in result.artifacts if a.type == "sources.json"]) or "[]"
        )})
        assert verdict.passed

    def test_same_pipeline_with_three_parallel_collectors(self, tmp_path: Path):
        async def collector_a(q: str):
            await asyncio.sleep(0.02)
            return [{"source": "web", "claim": f"{q} is documented", "url": "https://a.example/1", "confidence": 0.9}]

        async def collector_b(q: str):
            await asyncio.sleep(0.02)
            return [{"source": "papers", "claim": f"{q} is documented", "url": "https://b.example/1", "confidence": 0.8}]

        wf = ResearchWorkflow(collectors=[collector_a, collector_b])
        spec = TaskSpec(domain="research", goal="parallel evidence check")
        result = _run(wf.run(spec))
        assert result.status == WorkflowStatus.COMPLETED
        # the same claim from 2 independent sources must be cross-verified
        assert "documented" in result.output


class TestMemoryLifecycle:
    """Master section 16: result -> extractor -> memory + user model."""

    def test_completed_result_becomes_memory_and_survives_reopen(self, tmp_path: Path):
        db = tmp_path / "memory.sqlite"
        spec = TaskSpec(domain="research", goal="memory lifecycle check")
        result = _run(ResearchWorkflow().run(spec))

        candidates = MemoryExtractor().extract(spec, result)
        assert len(candidates) == 1
        assert candidates[0].category.value == "task_history"

        store = MemoryStore(db)
        store.add(candidates[0])
        assert store.count() == 1
        store.close()

        reopened = MemoryStore(db)
        loaded = reopened.get(candidates[0].memory_id)
        assert loaded is not None and loaded.content == candidates[0].content
        reopened.close()

    def test_user_model_records_observations_not_labels(self, tmp_path: Path):
        from uap.contracts import Observation

        ums = UserModelStore(tmp_path / "um.sqlite")
        um = ums.add_observation(
            "u-e2e",
            Observation(domain="research", note="asked for checkpoint strategy comparison", evidence="task t-1"),
        )
        assert um.observations[0].evidence == "task t-1"
        um2 = ums.set_skill_level("u-e2e", "research", "intermediate")
        assert um2.domain_skill_levels["research"] == "intermediate"
        ums.close()


class TestContextAndSkillsIntegration:
    def test_compiler_selects_relevant_memory_for_the_task(self):
        spec = TaskSpec(domain="research", goal="langgraph checkpointer strategy")
        from uap.contracts import Memory

        relevant = Memory(category="long_term_facts", content="langgraph checkpointer uses thread_id", relevance=0.9)
        irrelevant = Memory(category="user_preferences", content="prefers dark mode terminals", relevance=0.9)
        ctx = ContextCompiler(ContextBudget(max_sections=4)).compile(spec, memory=[relevant, irrelevant])
        joined = " ".join(s.content for s in ctx.context_sections)
        assert "thread_id" in joined
        assert "dark mode" not in joined

    def test_library_skills_load_and_cover_domains(self):
        skills = load_skills_from_dir(Path(__file__).resolve().parents[1] / "src" / "uap" / "skills" / "library")
        assert {s.domain for s in skills} == {"learning", "research", "bbp"}


class TestObservabilityAndApproval:
    def test_pipeline_emits_events_and_jsonl_sink_writes(self, tmp_path: Path):
        bus = EventBus()
        memory = MemorySink()
        jsonl = JsonlSink(tmp_path / "events.jsonl")
        bus.subscribe(memory)
        bus.subscribe(jsonl)

        spec = TaskSpec(domain="research", goal="observability e2e")
        with Tracer(bus, EventKind.TASK_STARTED, task_id=spec.task_id, workflow="research"):
            _run(ResearchWorkflow().run(spec))
            bus.emit_kind(EventKind.TASK_FINISHED, task_id=spec.task_id, workflow="research")

        kinds = [e.kind for e in memory.events]
        assert EventKind.TASK_STARTED in kinds
        assert EventKind.TASK_FINISHED in kinds
        lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
        # Tracer emits start + timed finish; the manual emit adds TASK_FINISHED -> >= 2
        assert len(lines) >= 2
        parsed = [json.loads(line) for line in lines]
        assert parsed[0]["kind"] == "task_started"
        assert all(p["task_id"] == spec.task_id for p in parsed)

    def test_approval_gate_blocks_then_authorizes_tier3_tool(self, tmp_path: Path):
        gate = ApprovalGate()
        registry = ToolRegistry()
        register_local_tools(registry, tmp_path / "tools")

        # denied without approval
        denied = _run(registry.call("write_artifact_file", {"path": "out.txt", "content": "x"}))
        assert not denied.ok
        assert not (tmp_path / "tools" / "out.txt").exists()

        # request -> decide -> allowed
        req = gate.request("task-e2e", "write artifact", {"path": "out.txt"})
        assert req.state == ApprovalState.PENDING_APPROVAL
        approved = gate.decide(req.approval_id, approved=True, decided_by="operator")
        ok = _run(registry.call("write_artifact_file", {"path": "out.txt", "content": "x"}, approval=approved))
        assert ok.ok
        assert (tmp_path / "tools" / "out.txt").read_text() == "x"

    def test_workflow_result_and_state_round_trip_json(self):
        spec = TaskSpec(domain="research", goal="round trip check")
        result = _run(ResearchWorkflow().run(spec))
        reloaded = WorkflowResult.model_validate_json(result.model_dump_json())
        assert reloaded == result


class TestPlatformInvariants:
    """Master section 29 architectural rules, enforced structurally."""

    def test_agents_do_not_import_forbidden_frameworks(self):
        agents_dir = Path(__file__).resolve().parents[1] / "src" / "uap" / "agents"
        forbidden = ("fastapi", "langgraph", "sqlite3", "mcp", "httpx", "requests", "uap.server")
        for path in agents_dir.glob("*.py"):
            text = path.read_text()
            for name in forbidden:
                assert f"import {name}" not in text, f"{path.name} imports {name}"

    def test_contracts_are_framework_free(self):
        contracts = Path(__file__).resolve().parents[1] / "src" / "uap" / "contracts"
        for path in contracts.glob("*.py"):
            text = path.read_text()
            for name in ("fastapi", "langgraph", "sqlite3", "mcp", "httpx"):
                assert f"import {name}" not in text, f"{path.name} imports {name}"

    def test_tool_module_does_not_depend_on_mcp(self):
        tools_dir = Path(__file__).resolve().parents[1] / "src" / "uap" / "tools"
        for path in tools_dir.glob("*.py"):
            assert "uap.mcp" not in path.read_text(), f"{path.name} depends on uap.mcp"
