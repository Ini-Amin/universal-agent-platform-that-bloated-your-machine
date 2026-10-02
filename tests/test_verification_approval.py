"""Tests for Step 12: Verification (Master section 17), Reviewer (18), and the
Human Approval gate (Master section 20).

The theme throughout: every check is explicit and measurable, and every failure
is recorded as evidence rather than raised into the caller.
"""

from __future__ import annotations

import json

import pytest

from uap.approval import ApprovalError, ApprovalGate
from uap.contracts.models import (
    ApprovalRequest,
    ApprovalState,
    VerificationResult,
    WorkflowState,
    WorkflowStatus,
)
from uap.verification import (
    Criterion,
    DeterministicVerifier,
    ReviewDecision,
    Reviewer,
    all_sources_have_urls,
    has_min_evidence,
    synthesis_non_empty,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _always(flag: bool, name: str = "c") -> Criterion:
    return Criterion(name=name, check=lambda _subject: flag)


class _Research:
    """Minimal stand-in for a research output."""

    def __init__(self, evidence=None, sources=None, synthesis="") -> None:
        self.evidence = evidence or []
        self.sources = sources or []
        self.synthesis = synthesis

# --------------------------------------------------------------------------- #
# 1. All criteria pass -> passed True, score 1.0
# --------------------------------------------------------------------------- #

def test_verifier_all_pass_scores_one() -> None:
    verifier = DeterministicVerifier(
        "research", [_always(True, "a"), _always(True, "b")]
    )
    result = verifier.verify(object())
    assert result.passed is True
    assert result.score == 1.0
    assert result.verifier == "research"
    assert [c.criterion for c in result.criteria] == ["a", "b"]
    assert all(c.passed for c in result.criteria)

# --------------------------------------------------------------------------- #
# 2. One criterion fails -> passed False, score 0.5, evidence recorded
# --------------------------------------------------------------------------- #

def test_verifier_one_failure_scores_half_and_records_evidence() -> None:
    def bad(_subject: object) -> bool:
        return False

    verifier = DeterministicVerifier(
        "research",
        [
            _always(True, "ok"),
            Criterion(
                name="bad",
                check=bad,
                evidence=lambda _s: "no url present",
            ),
        ],
    )
    result = verifier.verify(object())
    assert result.passed is False
    assert result.score == 0.5
    failed = [c for c in result.criteria if not c.passed]
    assert len(failed) == 1
    assert failed[0].criterion == "bad"
    assert failed[0].evidence == "no url present"

def test_failed_criterion_without_evidence_hook_still_has_evidence() -> None:
    verifier = DeterministicVerifier("v", [_always(False, "naked")])
    result = verifier.verify(object())
    assert result.criteria[0].evidence  # never blank on a failure

# --------------------------------------------------------------------------- #
# 3. A raising criterion -> failed with error evidence, no exception escapes
# --------------------------------------------------------------------------- #

def test_raising_criterion_is_contained_as_failure() -> None:
    def boom(_subject: object) -> bool:
        raise RuntimeError("kaboom")

    verifier = DeterministicVerifier(
        "research", [_always(True, "ok"), Criterion(name="boom", check=boom)]
    )
    result = verifier.verify(object())  # must not raise
    assert result.passed is False
    assert result.score == 0.5
    boom_record = next(c for c in result.criteria if c.criterion == "boom")
    assert boom_record.passed is False
    assert "kaboom" in (boom_record.evidence or "")
    assert "RuntimeError" in (boom_record.evidence or "")

def test_empty_criteria_list_is_vacuously_true() -> None:
    result = DeterministicVerifier("v", []).verify(object())
    assert result.passed is True
    assert result.score == 1.0
    assert result.criteria == []

# --------------------------------------------------------------------------- #
# 4. has_min_evidence factory
# --------------------------------------------------------------------------- #

def test_has_min_evidence_two_items_passes() -> None:
    verifier = DeterministicVerifier("research", [has_min_evidence(2)])
    result = verifier.verify(_Research(evidence=["a", "b"]))
    assert result.passed is True

def test_has_min_evidence_one_item_fails() -> None:
    verifier = DeterministicVerifier("research", [has_min_evidence(2)])
    result = verifier.verify(_Research(evidence=["a"]))
    assert result.passed is False
    assert result.criteria[0].evidence
    assert "1 evidence item" in (result.criteria[0].evidence or "")

def test_has_min_evidence_works_on_mappings() -> None:
    verifier = DeterministicVerifier("research", [has_min_evidence(1)])
    assert verifier.verify({"evidence": ["x"]}).passed is True
    assert verifier.verify({"evidence": []}).passed is False

# --------------------------------------------------------------------------- #
# 5. all_sources_have_urls factory
# --------------------------------------------------------------------------- #

def test_all_sources_have_urls_missing_url_fails() -> None:
    verifier = DeterministicVerifier("research", [all_sources_have_urls()])
    result = verifier.verify(
        _Research(sources=[{"url": "https://a"}, {"title": "no url"}])
    )
    assert result.passed is False
    assert "missing url" in (result.criteria[0].evidence or "")

def test_all_sources_have_urls_all_present_passes() -> None:
    verifier = DeterministicVerifier("research", [all_sources_have_urls()])
    result = verifier.verify(
        _Research(sources=[{"url": "https://a"}, {"uri": "https://b"}])
    )
    assert result.passed is True

def test_all_sources_have_urls_blank_url_fails() -> None:
    verifier = DeterministicVerifier("research", [all_sources_have_urls()])
    assert verifier.verify(_Research(sources=[{"url": "   "}])).passed is False

def test_synthesis_non_empty_factory() -> None:
    verifier = DeterministicVerifier("research", [synthesis_non_empty()])
    assert verifier.verify(_Research(synthesis="findings")).passed is True
    assert verifier.verify(_Research(synthesis="   ")).passed is False
    assert verifier.verify(_Research(synthesis="")).passed is False

# --------------------------------------------------------------------------- #
# 6. Reviewer PASS decision + result
# --------------------------------------------------------------------------- #

def test_reviewer_passes_when_all_criteria_met() -> None:
    reviewer = Reviewer("final", [_always(True, "has_summary")])
    decision, result = reviewer.review(object())
    assert decision is ReviewDecision.PASS
    assert result.passed is True
    assert result.notes is None
    assert result.verifier == "final"

# --------------------------------------------------------------------------- #
# 7. Reviewer FAIL puts "FAILED:" in notes
# --------------------------------------------------------------------------- #

def test_reviewer_fail_lists_failed_criteria_in_notes() -> None:
    reviewer = Reviewer(
        "final",
        [_always(True, "ok"), _always(False, "missing_citations")],
    )
    decision, result = reviewer.review(object())
    assert decision is ReviewDecision.FAIL
    assert result.passed is False
    assert result.notes is not None
    assert result.notes.startswith("FAILED:")
    assert "missing_citations" in result.notes
    assert "ok" not in result.notes

def test_reviewer_requires_at_least_one_criterion() -> None:
    with pytest.raises(ValueError):
        Reviewer("empty", [])

# --------------------------------------------------------------------------- #
# 8. Gate request -> PENDING, stored, pending() lists it
# --------------------------------------------------------------------------- #

def test_request_creates_pending_and_is_listed() -> None:
    gate = ApprovalGate()
    req = gate.request("task_1", "publish_report", {"channel": "public"})
    assert req.state is ApprovalState.PENDING_APPROVAL
    assert req.task_id == "task_1"
    assert req.action == "publish_report"
    assert req.details == {"channel": "public"}
    assert req.decided_at is None
    assert gate.get(req.approval_id) is not None
    assert req in gate.pending()

def test_request_without_details_defaults_to_empty_dict() -> None:
    gate = ApprovalGate()
    assert gate.request("t", "act").details == {}

def test_get_unknown_returns_none() -> None:
    assert ApprovalGate().get("nope") is None

# --------------------------------------------------------------------------- #
# 9. decide approved -> APPROVED + decided_at + decided_by
# --------------------------------------------------------------------------- #

def test_decide_approved_sets_terminal_fields() -> None:
    gate = ApprovalGate()
    req = gate.request("t", "send_email")
    decided = gate.decide(req.approval_id, approved=True, decided_by="operator")
    assert decided.state is ApprovalState.APPROVED
    assert decided.decided_at is not None
    assert decided.decided_by == "operator"
    assert decided.decided_at.tzinfo is not None
    # stored copy is updated, not just the returned one
    assert gate.state(req.approval_id) is ApprovalState.APPROVED
    assert gate.pending() == []

# --------------------------------------------------------------------------- #
# 10. decide rejected -> REJECTED
# --------------------------------------------------------------------------- #

def test_decide_rejected() -> None:
    gate = ApprovalGate()
    req = gate.request("t", "delete_prod")
    decided = gate.decide(req.approval_id, approved=False, decided_by="operator")
    assert decided.state is ApprovalState.REJECTED
    assert decided.decided_at is not None
    assert decided.decided_by == "operator"

# --------------------------------------------------------------------------- #
# 11. Double decide raises ApprovalError
# --------------------------------------------------------------------------- #

def test_double_decide_raises() -> None:
    gate = ApprovalGate()
    req = gate.request("t", "act")
    gate.decide(req.approval_id, approved=True, decided_by="operator")
    with pytest.raises(ApprovalError):
        gate.decide(req.approval_id, approved=False, decided_by="operator")

def test_decide_unknown_id_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        ApprovalGate().decide("ghost", approved=True, decided_by="operator")

# --------------------------------------------------------------------------- #
# 12. require(): pending raises, approved returns
# --------------------------------------------------------------------------- #

def test_require_pending_raises() -> None:
    gate = ApprovalGate()
    req = gate.request("t", "act")
    with pytest.raises(ApprovalError):
        gate.require(req.approval_id)

def test_require_rejected_raises() -> None:
    gate = ApprovalGate()
    req = gate.request("t", "act")
    gate.decide(req.approval_id, approved=False, decided_by="operator")
    with pytest.raises(ApprovalError):
        gate.require(req.approval_id)

def test_require_approved_returns_request() -> None:
    gate = ApprovalGate()
    req = gate.request("t", "act")
    gate.decide(req.approval_id, approved=True, decided_by="operator")
    returned = gate.require(req.approval_id)
    assert returned.approval_id == req.approval_id
    assert returned.state is ApprovalState.APPROVED

def test_require_unknown_raises_approval_error() -> None:
    with pytest.raises(ApprovalError):
        ApprovalGate().require("ghost")

# --------------------------------------------------------------------------- #
# 13. state() for unknown id raises KeyError (chosen behavior)
# --------------------------------------------------------------------------- #

def test_state_unknown_id_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        ApprovalGate().state("unknown-id")

def test_state_returns_current_state() -> None:
    gate = ApprovalGate()
    req = gate.request("t", "act")
    assert gate.state(req.approval_id) is ApprovalState.PENDING_APPROVAL
    gate.decide(req.approval_id, approved=True, decided_by="operator")
    assert gate.state(req.approval_id) is ApprovalState.APPROVED

# --------------------------------------------------------------------------- #
# 14. attach_to_state: sets fields, returns new object, original unchanged
# --------------------------------------------------------------------------- #

def test_attach_to_state_sets_pending_approval_and_status() -> None:
    gate = ApprovalGate()
    req = gate.request("task_9", "publish")
    original = WorkflowState(task_id="task_9", workflow="ResearchWorkflow")

    updated = gate.attach_to_state(original, req.approval_id)

    assert updated is not original
    assert updated.pending_approval is not None
    assert updated.pending_approval.approval_id == req.approval_id
    assert updated.status is WorkflowStatus.AWAITING_APPROVAL

def test_attach_to_state_leaves_original_unchanged() -> None:
    gate = ApprovalGate()
    req = gate.request("task_9", "publish")
    original = WorkflowState(task_id="task_9", workflow="ResearchWorkflow")

    gate.attach_to_state(original, req.approval_id)

    assert original.pending_approval is None
    assert original.status is WorkflowStatus.PENDING

def test_attach_to_state_unknown_id_raises_keyerror() -> None:
    gate = ApprovalGate()
    state = WorkflowState(task_id="t", workflow="w")
    with pytest.raises(KeyError):
        gate.attach_to_state(state, "ghost")

# --------------------------------------------------------------------------- #
# 15. JSON round-trips
# --------------------------------------------------------------------------- #

def test_verification_result_json_round_trip() -> None:
    verifier = DeterministicVerifier(
        "research", [has_min_evidence(2), all_sources_have_urls()]
    )
    result = verifier.verify(_Research(evidence=["a", "b"], sources=[]))
    restored = VerificationResult.model_validate_json(result.model_dump_json())
    assert restored == result

def test_approval_request_json_round_trip() -> None:
    gate = ApprovalGate()
    req = gate.request("task_7", "publish", {"target": "public"})
    gate.decide(req.approval_id, approved=True, decided_by="operator")
    decided = gate.require(req.approval_id)

    payload = json.loads(decided.model_dump_json())
    restored = ApprovalRequest.model_validate(payload)

    assert restored == decided
    assert restored.state is ApprovalState.APPROVED
    assert restored.decided_by == "operator"

def test_review_result_is_json_serialisable() -> None:
    reviewer = Reviewer("final", [_always(False, "missing_x")])
    _decision, result = reviewer.review(object())
    assert '"passed":false' in result.model_dump_json().replace(" ", "")
