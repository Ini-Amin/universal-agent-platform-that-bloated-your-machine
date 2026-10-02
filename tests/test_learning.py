"""Tests for the deterministic closed-loop learner (Master section 48)."""

from __future__ import annotations

import pytest

from uap.contracts.models import VerificationCriterion, VerificationResult
from uap.evaluators import EvaluationRecord, EvaluatorRegistry, EvaluatorRunner
from uap.learning import LearningLoop, ProposalKind, StrategyProposal
from uap.learning.proposal import ProposalStatus


def _record(ref: str, subject: str, criteria: list[tuple[str, bool]]) -> EvaluationRecord:
    crits = [VerificationCriterion(criterion=n, passed=p) for n, p in criteria]
    return EvaluationRecord(
        evaluator_ref=ref,
        subject_ref=subject,
        result=VerificationResult(
            verifier=ref,
            passed=all(p for _, p in criteria),
            criteria=crits,
        ),
    )


def _loop(min_failures: int = 2) -> LearningLoop:
    reg = EvaluatorRegistry()
    reg.add("q", [{"name": "has-sources", "kind": "min_count", "params": {"field": "s", "n": 2}}])
    return LearningLoop(EvaluatorRunner(reg), min_failures_for_proposal=min_failures)


def test_observe_aggregates_pass_fail_and_failing_criteria() -> None:
    loop = _loop()
    records = [
        _record("q@v1", "a", [("has-sources", False), ("non-empty", True)]),
        _record("q@v1", "b", [("has-sources", False), ("non-empty", True)]),
        _record("q@v1", "c", [("has-sources", True), ("non-empty", True)]),
    ]
    obs = loop.observe(records)
    assert obs["evaluators"]["q@v1"] == {"passed": 1, "failed": 2}
    assert obs["failing_criteria"]["has-sources"] == 2
    assert "non-empty" not in obs["failing_criteria"]
    assert obs["total"] == 3


def test_below_min_failures_yields_no_proposal() -> None:
    loop = _loop(min_failures=2)
    records = [_record("q@v1", "a", [("has-sources", False)])]
    obs = loop.observe(records)
    assert loop.propose(obs, target_ref="workflow:recon@v1") == []


def test_at_threshold_yields_exactly_one_proposal() -> None:
    loop = _loop(min_failures=2)
    records = [
        _record("q@v1", "a", [("has-sources", False)]),
        _record("q@v1", "b", [("has-sources", False)]),
    ]
    obs = loop.observe(records)
    proposals = loop.propose(obs, target_ref="workflow:recon@v1")
    assert len(proposals) == 1
    assert proposals[0].proposed_change["criterion"] == "has-sources"
    assert proposals[0].proposed_change["failures"] == 2


def test_two_failing_criteria_two_proposals_deterministic_order() -> None:
    loop = _loop(min_failures=2)
    records = [
        _record("q@v1", "a", [("zeta", False), ("alpha", False)]),
        _record("q@v1", "b", [("zeta", False), ("alpha", False)]),
    ]
    obs = loop.observe(records)
    proposals = loop.propose(obs, target_ref="workflow:recon@v1")
    assert [p.proposed_change["criterion"] for p in proposals] == ["alpha", "zeta"]


def test_proposal_kind_mapping() -> None:
    loop = _loop(min_failures=2)
    # "has-sources" IS a registered criterion -> workflow/agent mapping.
    known = [_record("q@v1", str(i), [("has-sources", False)]) for i in range(2)]
    obs = loop.observe(known)
    wf = loop.propose(obs, target_ref="workflow:recon@v1")[0]
    assert wf.kind == ProposalKind.WORKFLOW_CHANGE
    ag = loop.propose(obs, target_ref="agent:scout@v1")[0]
    assert ag.kind == ProposalKind.PROMPT_TWEAK
    # An unregistered criterion -> EVALUATOR_ADD regardless of target prefix.
    unknown = [_record("q@v1", str(i), [("brand-new", False)]) for i in range(2)]
    obs2 = loop.observe(unknown)
    ev = loop.propose(obs2, target_ref="workflow:recon@v1")[0]
    assert ev.kind == ProposalKind.EVALUATOR_ADD


def test_approve_sets_status_and_decided_by() -> None:
    loop = _loop()
    prop = StrategyProposal(
        kind=ProposalKind.WORKFLOW_CHANGE, target_ref="workflow:recon@v1", rationale="x"
    )
    approved = loop.approve(prop, decided_by="alice")
    assert approved.status == ProposalStatus.APPROVED
    assert approved.decided_by == "alice"
    assert approved.decided_at is not None
    # Original is untouched (immutable).
    assert prop.status == ProposalStatus.PROPOSED


def test_reject_records_reason_and_decided_at() -> None:
    loop = _loop()
    prop = StrategyProposal(
        kind=ProposalKind.WORKFLOW_CHANGE, target_ref="workflow:recon@v1", rationale="x"
    )
    rejected = loop.reject(prop, decided_by="bob", reason="too risky")
    assert rejected.status == ProposalStatus.REJECTED
    assert rejected.decided_by == "bob"
    assert rejected.decided_at is not None
    assert rejected.proposed_change["rejection_reason"] == "too risky"


def test_apply_unapproved_raises_valueerror() -> None:
    loop = _loop()
    prop = StrategyProposal(
        kind=ProposalKind.WORKFLOW_CHANGE, target_ref="workflow:recon@v1", rationale="x"
    )
    with pytest.raises(ValueError, match="not approved"):
        loop.apply(prop, applier=lambda p: {})


def test_apply_approved_calls_applier_and_sets_applied() -> None:
    loop = _loop()
    prop = StrategyProposal(
        kind=ProposalKind.WORKFLOW_CHANGE, target_ref="workflow:recon@v1", rationale="x"
    )
    approved = loop.approve(prop, decided_by="alice")
    calls: list[StrategyProposal] = []

    def applier(p: StrategyProposal) -> dict:
        calls.append(p)
        return {"new_version": "recon@v2"}

    applied = loop.apply(approved, applier=applier)
    assert len(calls) == 1
    assert applied.status == ProposalStatus.APPLIED
    assert applied.proposed_change["applied"] == {"new_version": "recon@v2"}


def test_strategy_proposal_json_round_trip() -> None:
    prop = StrategyProposal(
        kind=ProposalKind.EVALUATOR_ADD,
        target_ref="evaluator:quality",
        rationale="criterion failed",
        evidence_refs=["rec-1", "rec-2"],
        proposed_change={"criterion": "has-sources"},
    )
    restored = StrategyProposal.model_validate_json(prop.model_dump_json())
    assert restored == prop
