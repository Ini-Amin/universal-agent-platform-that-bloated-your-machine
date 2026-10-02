"""Entry Workflow: the universal front door (Master section 3).

The Entry Workflow is deliberately lightweight. It classifies the request,
checks whether it is clear enough, asks for clarification when it is not, and
compiles a ``TaskSpec``. It performs no domain work, makes no tool calls and
never invokes an agent -- that is the Router's and the domain workflows' job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..contracts.models import Domain, TaskSpec, UserRequest
from .stages import (
    DeterministicIntentAnalyzer,
    IntentResult,
    best_effort_goal,
    check_constraints,
    check_sufficiency,
    clarification_question,
    compile_task,
    intake,
    session_key,
)


@runtime_checkable
class IntentAnalyzer(Protocol):
    """Anything that can turn a request into an :class:`IntentResult`.

    The default implementation is deterministic and keyword based; an
    LLM-backed analyzer can be injected through ``EntryWorkflow`` as long as it
    matches this single-method contract.
    """

    def analyze(self, request: UserRequest) -> IntentResult: ...


@dataclass(frozen=True)
class EntryOutcome:
    """Result of one Entry Workflow run."""

    spec: TaskSpec | None = None
    needs_clarification: bool = False
    question: str | None = None
    stage_log: list[str] = field(default_factory=list)


class EntryWorkflow:
    """Deterministic front door: request in, ``TaskSpec`` (or a question) out."""

    def __init__(
        self,
        intent_analyzer: IntentAnalyzer | None = None,
        max_clarification_rounds: int = 3,
    ):
        self._intent_analyzer = intent_analyzer or DeterministicIntentAnalyzer()
        self.max_clarification_rounds = max_clarification_rounds
        # Clarification rounds already spent, keyed by session/user/request.
        self._rounds: dict[str, int] = {}

    def run(
        self, request: UserRequest, clarification: str | None = None
    ) -> EntryOutcome:
        stage_log: list[str] = ["intake"]
        working = intake(request, clarification)

        stage_log.append("intent_analysis")
        intent = self._intent_analyzer.analyze(working)

        stage_log.append("context_sufficiency")
        sufficient, _reason = check_sufficiency(intent)
        if not sufficient:
            return self._request_clarification(request, working, intent, stage_log)

        stage_log.append("constraint_check")
        constraints, mode, verification = check_constraints(intent, request)

        stage_log.append("task_compiler")
        spec = compile_task(request, intent, constraints, mode, verification)
        return EntryOutcome(spec=spec, needs_clarification=False, stage_log=stage_log)

    # ------------------------------------------------------------------ #

    def _request_clarification(
        self,
        request: UserRequest,
        working: UserRequest,
        intent: IntentResult,
        stage_log: list[str],
    ) -> EntryOutcome:
        """Ask the user once more, or fall back to a best-effort spec.

        Once ``max_clarification_rounds`` is exhausted the workflow stops asking
        and compiles a best-effort ``TaskSpec`` with ``Domain.UNKNOWN`` rather
        than looping forever (Master section 3).
        """
        key = session_key(request)
        rounds = self._rounds.get(key, 0)
        if rounds < self.max_clarification_rounds:
            self._rounds[key] = rounds + 1
            stage_log.append("clarification")
            language = intent.constraints.get("language", "en")
            return EntryOutcome(
                spec=None,
                needs_clarification=True,
                question=clarification_question(intent, language),
                stage_log=stage_log,
            )

        # Cap reached: best-effort compilation.
        best_effort = IntentResult(
            domain=Domain.UNKNOWN,
            goal=intent.goal.strip() or best_effort_goal(working),
            confidence=0.0,
            needs_clarification=False,
        )
        stage_log.append("constraint_check")
        constraints, mode, verification = check_constraints(best_effort, request)
        stage_log.append("task_compiler")
        spec = compile_task(request, best_effort, constraints, mode, verification)
        return EntryOutcome(spec=spec, needs_clarification=False, stage_log=stage_log)
