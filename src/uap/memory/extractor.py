"""Deterministic memory extractor (Master section 16).

After a workflow finishes, the extractor *proposes* memory candidates. It never
writes anything itself: the caller (or a lifecycle policy) decides whether a
candidate is stored. Extraction is deliberately deterministic -- no LLM is
consulted, because "prefer deterministic logic wherever deterministic logic is
sufficient" (Master section 29, rule 11).

Lifecycle rules (see the module constants below):

* COMPLETED task with substantial output -> one ``task_history`` memory,
  relevance 0.8, expiring after 90 days.
* FAILED task -> one ``task_history`` memory, relevance 0.4, expiring after
  30 days. Failures are worth remembering even when output is short.
* COMPLETED task with short/empty output -> nothing (no signal to keep).
* Any non-terminal status (pending, running, awaiting approval, checkpointed,
  cancelled) -> nothing: runtime state is not memory (Master section 16).
* At most 2 candidates are ever returned for a single run.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from uap.contracts import (
    Memory,
    MemoryCategory,
    TaskSpec,
    WorkflowResult,
    WorkflowStatus,
    utc_now,
)

__all__ = ["MemoryExtractor"]

# -- lifecycle constants ---------------------------------------------------- #

SUCCESS_RELEVANCE = 0.8
FAILURE_RELEVANCE = 0.4
SUCCESS_TTL = timedelta(days=90)
FAILURE_TTL = timedelta(days=30)

#: Hard cap: one run never proposes more than this many candidates.
MAX_CANDIDATES_PER_RUN = 2

#: Statuses that mean "the task is still in flight" -- never extracted.
_NON_TERMINAL = frozenset(
    {
        WorkflowStatus.PENDING,
        WorkflowStatus.RUNNING,
        WorkflowStatus.AWAITING_APPROVAL,
        WorkflowStatus.CHECKPOINTED,
        WorkflowStatus.CANCELLED,
    }
)


def _artifact_summary(result: WorkflowResult) -> str:
    """Comma-separated, de-duplicated artifact types, or ``none``."""
    types = sorted({artifact.type for artifact in result.artifacts})
    return ", ".join(types) if types else "none"


class MemoryExtractor:
    """Turns a finished ``WorkflowResult`` into zero or more memory candidates."""

    def __init__(self, min_output_chars: int = 40) -> None:
        if min_output_chars < 0:
            raise ValueError("min_output_chars must not be negative")
        self.min_output_chars = min_output_chars

    @staticmethod
    def _summary(task: TaskSpec, result: WorkflowResult) -> str:
        """One deterministic line describing the run."""
        return (
            f"{task.domain.value} | {task.goal} | {result.status.value} | "
            f"artifacts: {_artifact_summary(result)}"
        )

    def extract(self, task: TaskSpec, result: WorkflowResult) -> list[Memory]:
        """Propose memory candidates for a finished run; [] when nothing is worth keeping."""
        if result.status in _NON_TERMINAL:
            return []

        now = utc_now()
        candidates: list[Memory] = []

        if result.status == WorkflowStatus.COMPLETED:
            if len(result.output.strip()) < self.min_output_chars:
                return []
            candidates.append(
                self._candidate(task, result, now, SUCCESS_RELEVANCE, SUCCESS_TTL)
            )
        elif result.status == WorkflowStatus.FAILED:
            # A failure is worth remembering even with little output: the error
            # itself is the signal.
            candidates.append(
                self._candidate(task, result, now, FAILURE_RELEVANCE, FAILURE_TTL)
            )
        else:  # pragma: no cover - defensive: unknown terminal status
            return []

        return candidates[:MAX_CANDIDATES_PER_RUN]

    @staticmethod
    def _candidate(
        task: TaskSpec,
        result: WorkflowResult,
        now: datetime,
        relevance: float,
        ttl: timedelta,
    ) -> Memory:
        return Memory(
            category=MemoryCategory.TASK_HISTORY,
            content=MemoryExtractor._summary(task, result),
            metadata={"task_id": task.task_id, "workflow": result.workflow},
            relevance=relevance,
            created_at=now,
            expires_at=now + ttl,
        )
