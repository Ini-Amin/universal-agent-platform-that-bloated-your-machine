"""Slice evaluation gate (§71).

A deterministic verifier over the *whole* slice run — not one node — that asks
the four questions the vertical slice must answer affirmatively to be "done":

* ``execution_completed`` — the durable execution reached ``completed``.
* ``artifacts_present``   — at least one artifact was produced and stored.
* ``trace_coverage``      — at least one decision trace was recorded (auditable).
* ``knowledge_eligible``  — at least one knowledge claim was proposed/promoted.

It reuses :class:`~uap.verification.verifier.DeterministicVerifier` so the result
is an ordinary :class:`~uap.contracts.models.VerificationResult` with per-criterion
evidence, exactly like every other verifier in the platform.
"""

from __future__ import annotations

from typing import Any

from uap.contracts import VerificationResult
from uap.verification.verifier import Criterion, DeterministicVerifier

__all__ = ["SliceEvaluator"]


class _Subject:
    """Plain carrier of the four evidence inputs for the criteria."""

    def __init__(
        self,
        *,
        execution_status: str,
        node_results: dict[str, Any],
        artifacts: list,
        traces: list,
        knowledge_ids: list,
    ) -> None:
        self.execution_status = execution_status
        self.node_results = node_results
        self.artifacts = artifacts
        self.traces = traces
        self.knowledge_ids = knowledge_ids


class SliceEvaluator:
    """Evaluates one slice run against the four end-to-end criteria."""

    name = "slice-evaluator"

    def __init__(self) -> None:
        self._verifier = DeterministicVerifier(
            self.name,
            [
                Criterion(
                    name="execution_completed",
                    check=lambda s: s.execution_status == "completed",
                    evidence=lambda s: f"execution_status={s.execution_status!r}",
                ),
                Criterion(
                    name="artifacts_present",
                    check=lambda s: len(s.artifacts) > 0,
                    evidence=lambda s: f"{len(s.artifacts)} artifact(s)",
                ),
                Criterion(
                    name="trace_coverage",
                    check=lambda s: len(s.traces) > 0,
                    evidence=lambda s: f"{len(s.traces)} decision trace(s)",
                ),
                Criterion(
                    name="knowledge_eligible",
                    check=lambda s: len(s.knowledge_ids) > 0,
                    evidence=lambda s: f"{len(s.knowledge_ids)} knowledge item(s)",
                ),
            ],
        )

    def evaluate(
        self,
        *,
        execution_id: str,
        execution_status: str = "",
        node_results: dict[str, Any] | None = None,
        artifacts: list | None = None,
        traces: list | None = None,
        knowledge_ids: list | None = None,
    ) -> VerificationResult:
        """Return a scored :class:`VerificationResult` for the slice run."""
        subject = _Subject(
            execution_status=execution_status,
            node_results=dict(node_results or {}),
            artifacts=list(artifacts or []),
            traces=list(traces or []),
            knowledge_ids=list(knowledge_ids or []),
        )
        result = self._verifier.verify(subject)
        return result.model_copy(
            update={"notes": f"slice evaluation for execution {execution_id}"}
        )
