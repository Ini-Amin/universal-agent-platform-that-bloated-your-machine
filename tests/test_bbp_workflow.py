"""Tests for the BBP workflow + deterministic scope gate (Master section 9)."""

import json

import pytest

from uap.contracts.models import (
    Artifact,
    Domain,
    TaskSpec,
    WorkflowResult,
    WorkflowStatus,
)
from uap.tools import ToolPolicy, ToolRegistry, ToolSpec
from uap.workflows.bbp import NODES, WORKFLOW_NAME, BBPWorkflow, classify_severity
from uap.workflows.runner import InMemoryStateStore
from uap.workflows.scope import ScopeGate, ScopeRuleError, normalize_target

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def make_task(targets=None, *, constraints=False) -> TaskSpec:
    input_data = {} if targets is None else {"targets": targets}
    constraints_data = {}
    if constraints:
        constraints_data["targets"] = targets or []
        input_data = {}
    return TaskSpec(
        domain=Domain.BBP,
        goal="bbp: assess the target",
        input=input_data,
        constraints=constraints_data,
    )


def default_gate() -> ScopeGate:
    return ScopeGate(in_scope=["*.example.com"])


class CountingStep:
    """Recon step that records every target it is invoked with."""

    def __init__(self):
        self.seen: list[str] = []

    async def __call__(self, target: str, context: dict) -> dict:
        self.seen.append(target)
        return {
            "findings": [
                {
                    "title": f"Subdomain takeover on {target}",
                    "target": target,
                    "evidence": {"detail": "dangling CNAME", "status": 404},
                }
            ]
        }


class SpyGate(ScopeGate):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    def is_allowed(self, target: str):
        self.calls += 1
        return super().is_allowed(target)


# --------------------------------------------------------------------------- #
# 1-6: ScopeGate semantics
# --------------------------------------------------------------------------- #


def test_scope_wildcard_matches_root_and_subdomains():
    gate = ScopeGate(in_scope=["*.example.com"])
    assert gate.is_allowed("sub.example.com") == (
        True,
        "allowed: matched in-scope rule *.example.com",
    )
    assert gate.is_allowed("example.com")[0] is True
    assert gate.is_allowed("deep.sub.example.com")[0] is True
    # a sibling domain that merely ends in the same text is not matched
    assert gate.is_allowed("notexample.com")[0] is False


def test_scope_out_of_scope_beats_in_scope():
    gate = ScopeGate(in_scope=["*.example.com"], out_of_scope=["admin.example.com"])
    allowed, reason = gate.is_allowed("admin.example.com")
    assert allowed is False
    assert reason == "blocked: matched out-of-scope rule admin.example.com"
    assert gate.is_allowed("api.example.com")[0] is True


def test_scope_empty_in_scope_blocks_everything():
    gate = ScopeGate(in_scope=[])
    allowed, reason = gate.is_allowed("example.com")
    assert allowed is False
    assert reason == "blocked: no in-scope rules defined"


def test_scope_exact_host_match():
    gate = ScopeGate(in_scope=["api.example.com"])
    assert gate.is_allowed("api.example.com")[0] is True
    assert gate.is_allowed("www.example.com")[1] == "blocked: target not in scope"


def test_scope_normalizes_scheme_port_and_path():
    assert normalize_target("https://a.example.com:8443/x?y=1") == "a.example.com"
    gate = ScopeGate(in_scope=["a.example.com"])
    assert gate.is_allowed("https://a.example.com:8443/x")[0] is True
    assert gate.is_allowed("http://A.EXAMPLE.COM/path")[0] is True


def test_scope_invalid_pattern_raises_at_construction():
    with pytest.raises(ScopeRuleError):
        ScopeGate(in_scope=["http://x.com"])
    with pytest.raises(ScopeRuleError):
        ScopeGate(in_scope=["Example.com"])
    with pytest.raises(ScopeRuleError):
        ScopeGate(in_scope=[""])
    with pytest.raises(ScopeRuleError):
        ScopeGate(in_scope=["a.com"], out_of_scope=["*.bad.*.com"])


# --------------------------------------------------------------------------- #
# 7-9: happy path, fail-closed, mixed targets
# --------------------------------------------------------------------------- #


async def test_run_with_in_scope_target_completes_with_exact_artifacts():
    workflow = BBPWorkflow(scope_gate=default_gate())
    result = await workflow.run(make_task(["api.example.com"]))

    assert result.status == WorkflowStatus.COMPLETED
    assert result.workflow == WORKFLOW_NAME
    assert [a.type for a in result.artifacts] == ["findings.json", "report.md"]
    assert {a.source for a in result.artifacts} == {"bbp_workflow"}
    assert {a.status for a in result.artifacts} == {"final"}
    assert result.verification is not None
    assert result.verification.passed is True
    assert [c.criterion for c in result.verification.criteria] == [
        "scope_gate_ran",
        "all_findings_have_evidence",
        "report_non_empty",
    ]


async def test_out_of_scope_only_targets_fail_without_probing():
    step = CountingStep()
    workflow = BBPWorkflow(scope_gate=default_gate(), recon_steps=[step])
    result = await workflow.run(make_task(["evil.test"]))

    assert result.status == WorkflowStatus.FAILED
    assert "blocked" in (result.error or "")
    assert step.seen == []  # no recon step ever ran
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert state.data["targets"] == []
    assert state.data["blocked_targets"][0]["target"] == "evil.test"


async def test_mixed_targets_only_probe_allowed_and_record_blocked():
    step = CountingStep()
    workflow = BBPWorkflow(scope_gate=default_gate(), recon_steps=[step])
    result = await workflow.run(make_task(["good.example.com", "evil.test"]))

    assert result.status == WorkflowStatus.COMPLETED
    assert step.seen == ["good.example.com"]
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert state.data["targets"] == ["good.example.com"]
    assert [b["target"] for b in state.data["blocked_targets"]] == ["evil.test"]


# --------------------------------------------------------------------------- #
# 10-11: hard gate and tool denial
# --------------------------------------------------------------------------- #


async def test_no_scope_gate_fails_closed():
    workflow = BBPWorkflow(scope_gate=None, recon_steps=[CountingStep()])
    result = await workflow.run(make_task(["api.example.com"]))

    assert result.status == WorkflowStatus.FAILED
    assert "no scope gate configured" in (result.error or "")


async def test_tool_denial_is_recorded_and_step_is_skipped():
    registry = ToolRegistry(policy=ToolPolicy(allowed_tiers=set(), require_approval_tiers=set()))
    for name in ("stub_subdomain_recon", "stub_endpoint_recon", "stub_http_recon"):
        registry.register(
            ToolSpec(name=name, description="fixture", risk_tier=2), _noop_tool
        )

    workflow = BBPWorkflow(scope_gate=default_gate(), tools=registry)
    result = await workflow.run(make_task(["api.example.com"]))

    # Documented choice: a denied tool call is recorded, the step is skipped,
    # and the workflow still completes (no crash, no probe).
    assert result.status == WorkflowStatus.COMPLETED
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert len(state.data["tool_denials"]) == 3
    assert state.data["plan"]["skipped_steps"] == [
        "stub_subdomain_recon",
        "stub_endpoint_recon",
        "stub_http_recon",
    ]
    assert state.data["validated_findings"] == []


async def _noop_tool(**kwargs):  # pragma: no cover - never reached when denied
    return {}


# --------------------------------------------------------------------------- #
# 12-13: classification and validation
# --------------------------------------------------------------------------- #


def test_classification_keywords_map_to_severity():
    assert classify_severity({"title": "Subdomain takeover candidate"}) == "high"
    assert classify_severity({"title": "Remote Code Execution in parser"}) == "high"
    assert classify_severity({"title": "Reflected XSS on /search"}) == "medium"
    assert classify_severity({"title": "Missing security header"}) == "low"
    assert classify_severity({"title": "Unexplained observation"}) == "info"


async def test_validation_excludes_finding_without_evidence_into_appendix():
    async def step(target: str, context: dict) -> dict:
        return {
            "findings": [
                {
                    "title": f"Reflected XSS on {target}",
                    "target": target,
                    "evidence": {"detail": "unescaped q param", "status": 200},
                },
                {"title": "Speculative issue", "target": target, "evidence": None},
            ]
        }

    workflow = BBPWorkflow(scope_gate=default_gate(), recon_steps=[step])
    result = await workflow.run(make_task(["api.example.com"]))

    assert result.status == WorkflowStatus.COMPLETED
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert len(state.data["validated_findings"]) == 1
    assert len(state.data["unvalidated"]) == 1
    assert state.data["unvalidated"][0]["validated"] is False

    findings = json.loads(
        next(a for a in result.artifacts if a.type == "findings.json").content_ref
    )
    assert len(findings) == 1
    report = next(a for a in result.artifacts if a.type == "report.md").content_ref
    assert "Speculative issue" in report
    assert "## Unvalidated (appendix)" in report


# --------------------------------------------------------------------------- #
# 14-15: checkpoint and resume
# --------------------------------------------------------------------------- #


async def test_checkpoint_records_eight_nodes_in_order():
    store = InMemoryStateStore()
    workflow = BBPWorkflow(scope_gate=default_gate(), state_store=store)
    result = await workflow.run(make_task(["api.example.com"]))

    state = store.load(result.task_id)
    assert state is not None
    assert state.status == WorkflowStatus.COMPLETED
    assert state.node_history == list(NODES)
    assert len(NODES) == 8
    assert state.retries == 0


async def test_resume_does_not_rerun_scope_validation():
    store = InMemoryStateStore()
    gate = SpyGate(in_scope=["*.example.com"])
    workflow = BBPWorkflow(scope_gate=gate, state_store=store)
    completed = await workflow.run(make_task(["api.example.com"]))
    assert completed.status == WorkflowStatus.COMPLETED
    calls_after_run = gate.calls
    assert calls_after_run > 0

    # Simulate a crash: rewind to the checkpoint right after recon_planning.
    state = store.load(completed.task_id)
    assert state is not None
    mid = NODES.index("asset_discovery")
    state.status = WorkflowStatus.CHECKPOINTED
    state.current_node = NODES[mid]
    state.node_history = list(NODES[:mid])
    state.artifacts = []
    for key in ("assets", "raw_endpoints", "raw_findings", "endpoints", "findings",
                "classified", "validated_findings", "unvalidated", "report", "output",
                "verification", "artifacts"):
        state.data.pop(key, None)
    store.save(state)

    resumed = await workflow.resume(completed.task_id)
    assert resumed.status == WorkflowStatus.COMPLETED
    assert gate.calls == calls_after_run  # scope_validation was not re-run

    after = store.load(completed.task_id)
    assert after is not None
    assert after.node_history == list(NODES)
    assert [a.type for a in resumed.artifacts] == ["findings.json", "report.md"]


# --------------------------------------------------------------------------- #
# 16-18: artifact contents and serialisation
# --------------------------------------------------------------------------- #


async def test_findings_json_is_a_list_with_target_evidence_severity():
    workflow = BBPWorkflow(scope_gate=default_gate())
    result = await workflow.run(make_task(["api.example.com"]))

    raw = next(a for a in result.artifacts if a.type == "findings.json").content_ref
    payload = json.loads(raw)
    # Master spec section 49: stub runs must carry the simulation warning and
    # wrap the findings; real (non-stub) runs keep the bare-list shape.
    assert isinstance(payload, dict), "stub findings.json must be labelled"
    assert "_SIMULATION" in payload
    findings = payload["findings"]
    assert isinstance(findings, list) and findings
    for entry in findings:
        assert entry["target"]
        assert entry["evidence"]
        assert entry["severity"] in {"high", "medium", "low", "info"}


async def test_report_has_blocked_section_when_targets_blocked():
    workflow = BBPWorkflow(scope_gate=default_gate())
    result = await workflow.run(make_task(["api.example.com", "evil.test"]))

    report = next(a for a in result.artifacts if a.type == "report.md").content_ref
    assert "## Blocked" in report
    assert "evil.test" in report
    assert "## Targets" in report
    assert "api.example.com" in report


async def test_workflow_result_round_trips_json():
    workflow = BBPWorkflow(scope_gate=default_gate())
    result = await workflow.run(make_task(["api.example.com"]))

    restored = WorkflowResult.model_validate_json(result.model_dump_json())
    assert restored.model_dump(mode="json") == result.model_dump(mode="json")

    artifact = Artifact(
        task_id=result.task_id,
        type="report.md",
        source="bbp_workflow",
        content_ref="# hi",
    )
    assert Artifact.model_validate_json(artifact.model_dump_json()) == artifact


async def test_targets_can_come_from_constraints():
    workflow = BBPWorkflow(scope_gate=default_gate())
    result = await workflow.run(make_task(["api.example.com"], constraints=True))

    assert result.status == WorkflowStatus.COMPLETED
    state = workflow.runner.state_store.load(result.task_id)
    assert state is not None
    assert state.data["targets"] == ["api.example.com"]
