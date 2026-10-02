"""Checklist-based review of a domain result (Master section 18).

After a domain workflow executes, a Reviewer applies a *defined schema* of
measurable criteria and returns an explicit PASS/FAIL decision. The spec is
explicit that "does this look good?" is not a criterion (Master section 18);
reviewers therefore take a list of ``Criterion`` objects, the same explicit,
evidence-backed checks used by verifiers.
"""

from __future__ import annotations

from enum import StrEnum

from uap.contracts.models import VerificationResult
from uap.verification.verifier import Criterion, _run_criteria, _score

__all__ = ["ReviewDecision", "Reviewer"]

class ReviewDecision(StrEnum):
    """The two terminal outcomes of a review."""

    PASS = "pass"
    FAIL = "fail"

class Reviewer:
    """Applies an explicit checklist and returns a decision + evidence."""

    def __init__(self, name: str, checklist: list[Criterion]) -> None:
        if not checklist:
            raise ValueError("a reviewer requires at least one measurable criterion")
        self.name = name
        self.checklist = list(checklist)

    def review(self, subject: object) -> tuple[ReviewDecision, VerificationResult]:
        """Run the checklist. Never raises; a raising check counts as FAILED."""
        results = _run_criteria(self.checklist, subject)
        failed = [item.criterion for item in results if not item.passed]
        decision = ReviewDecision.FAIL if failed else ReviewDecision.PASS
        notes: str | None = None
        if failed:
            notes = f"FAILED: {', '.join(failed)}"
        result = VerificationResult(
            verifier=self.name,
            passed=decision is ReviewDecision.PASS,
            criteria=results,
            score=_score(results),
            notes=notes,
        )
        return decision, result
