"""Deterministic closed-loop learner (Master section 48).

Implements the section 48 loop's safe half:

```text
execute -> observe -> evaluate -> propose -> VALIDATE/POLICY -> new version
```

The runtime does the executing and evaluating (producing
:class:`~uap.evaluators.model.EvaluationRecord` objects); this loop *observes*
those records, *proposes* improvements, and gates every proposal behind explicit
approval before anything is applied. Proposals are NEVER auto-applied: applying
calls an injected ``applier`` that performs the real versioning (e.g. through
:class:`~uap.library.service.LibraryService`), and only when the proposal has
been approved (section 48: permanent changes require validation + policy +
versioning; section 69: "propose before mutate").
"""

from __future__ import annotations

from typing import Callable

from uap.contracts.models import utc_now

from .proposal import ProposalKind, ProposalStatus, StrategyProposal

__all__ = ["LearningLoop"]


class LearningLoop:
    """Observe evaluation records, propose fixes, gate them behind approval."""

    def __init__(
        self,
        evaluator_runner: object,
        *,
        min_failures_for_proposal: int = 2,
    ) -> None:
        if min_failures_for_proposal < 1:
            raise ValueError("min_failures_for_proposal must be >= 1")
        self.evaluator_runner = evaluator_runner
        self.min_failures_for_proposal = min_failures_for_proposal

    # ------------------------------------------------------------------ #
    # Observe
    # ------------------------------------------------------------------ #

    def observe(self, records: list) -> dict:
        """Aggregate pass/fail counts per evaluator ref and failing criteria.

        Returns::

            {
              "evaluators": {ref: {"passed": int, "failed": int}},
              "failing_criteria": {criterion_name: int},
              "total": int,
            }

        Counts are deterministic; ``failing_criteria`` tallies every failing
        criterion across all records, keyed by its name.
        """
        evaluators: dict[str, dict[str, int]] = {}
        failing_criteria: dict[str, int] = {}
        for record in records:
            ref = record.evaluator_ref
            bucket = evaluators.setdefault(ref, {"passed": 0, "failed": 0})
            if record.result.passed:
                bucket["passed"] += 1
            else:
                bucket["failed"] += 1
            for crit in record.result.criteria:
                if not crit.passed:
                    failing_criteria[crit.criterion] = (
                        failing_criteria.get(crit.criterion, 0) + 1
                    )
        return {
            "evaluators": evaluators,
            "failing_criteria": failing_criteria,
            "total": len(records),
        }

    # ------------------------------------------------------------------ #
    # Propose
    # ------------------------------------------------------------------ #

    def propose(self, observation: dict, *, target_ref: str) -> list[StrategyProposal]:
        """One proposal per criterion that failed at least the threshold times.

        Ordering is deterministic (criterion name ascending). Proposal ``kind``:

        * criterion not covered by any registered evaluator -> ``EVALUATOR_ADD``
        * ``target_ref`` starts ``workflow:`` -> ``WORKFLOW_CHANGE``
        * ``target_ref`` starts ``agent:``   -> ``PROMPT_TWEAK``
        * otherwise                           -> ``WORKFLOW_CHANGE``
        """
        failing = observation.get("failing_criteria", {})
        known = self._known_criterion_names()
        proposals: list[StrategyProposal] = []
        for name in sorted(failing):
            count = failing[name]
            if count < self.min_failures_for_proposal:
                continue
            kind = self._kind_for(name, target_ref, known)
            proposals.append(
                StrategyProposal(
                    kind=kind,
                    target_ref=target_ref,
                    rationale=(
                        f"criterion {name!r} failed {count} times "
                        f"(>= {self.min_failures_for_proposal})"
                    ),
                    proposed_change={"criterion": name, "failures": count},
                )
            )
        return proposals

    def _kind_for(
        self, criterion: str, target_ref: str, known: set[str]
    ) -> ProposalKind:
        if criterion not in known:
            return ProposalKind.EVALUATOR_ADD
        if target_ref.startswith("workflow:"):
            return ProposalKind.WORKFLOW_CHANGE
        if target_ref.startswith("agent:"):
            return ProposalKind.PROMPT_TWEAK
        return ProposalKind.WORKFLOW_CHANGE

    def _known_criterion_names(self) -> set[str]:
        names: set[str] = set()
        registry = getattr(self.evaluator_runner, "registry", None)
        if registry is None:
            return names
        for spec in registry.all():
            for crit in spec.criteria:
                name = crit.get("name") or crit.get("kind")
                if name:
                    names.add(str(name))
        return names

    # ------------------------------------------------------------------ #
    # Decide
    # ------------------------------------------------------------------ #

    def approve(
        self, proposal: StrategyProposal, *, decided_by: str
    ) -> StrategyProposal:
        """Return an approved copy of ``proposal`` (immutable; policy gate)."""
        return proposal.model_copy(
            update={
                "status": ProposalStatus.APPROVED,
                "decided_at": utc_now(),
                "decided_by": decided_by,
            }
        )

    def reject(
        self, proposal: StrategyProposal, *, decided_by: str, reason: str
    ) -> StrategyProposal:
        """Return a rejected copy; ``reason`` is recorded in ``proposed_change``."""
        change = dict(proposal.proposed_change)
        change["rejection_reason"] = reason
        return proposal.model_copy(
            update={
                "status": ProposalStatus.REJECTED,
                "decided_at": utc_now(),
                "decided_by": decided_by,
                "proposed_change": change,
            }
        )

    def apply(
        self,
        proposal: StrategyProposal,
        *,
        applier: Callable[[StrategyProposal], dict],
    ) -> StrategyProposal:
        """Apply an APPROVED proposal via ``applier``; set status ``applied``.

        ``applier`` does the real versioning (e.g. ``LibraryService.new_version``)
        and returns a result dict recorded under ``proposed_change["applied"]``.
        """
        if proposal.status != ProposalStatus.APPROVED:
            raise ValueError("proposal not approved")
        result = applier(proposal)
        change = dict(proposal.proposed_change)
        change["applied"] = result
        return proposal.model_copy(
            update={
                "status": ProposalStatus.APPLIED,
                "proposed_change": change,
            }
        )
