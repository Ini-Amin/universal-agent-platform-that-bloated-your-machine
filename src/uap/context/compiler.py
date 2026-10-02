"""Context Compiler: selects only relevant context (Master section 15).

    Memory + User Model + Knowledge + Task State
                    │
                    ▼
              Context Compiler
                    │
                    ▼
               AgentContext
                    │
                    ▼
                  Agent

The compiler is a pure function of its inputs: no IO, no network, no clock
reads, no mutation of the caller's objects. Given identical inputs it returns an
identical ``AgentContext``. It never dumps the whole database, memory, or
knowledge base into the prompt (Master section 29, rule 5): it scores every
candidate, keeps the best few, and hard-caps sections and characters.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts.models import (
    AgentContext,
    ContextSection,
    Memory,
    TaskSpec,
    UserModel,
    WorkflowState,
)
from uap.context.scoring import Candidate, build_candidates

__all__ = ["ContextBudget", "ContextCompiler"]


class ContextBudget(BaseModel):
    """Hard limits on how much context one agent may receive.

    These are caps, not targets: the compiler must never exceed any of them
    (Master section 15).
    """

    model_config = ConfigDict(extra="forbid")

    max_sections: int = 8
    max_chars_per_section: int = 2000
    max_total_chars: int = 8000


class ContextCompiler:
    """Turns raw memory/knowledge/state into a small, relevant AgentContext."""

    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget if budget is not None else ContextBudget()
        self.last_selected: list[Candidate] = []
    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def compile(
        self,
        task: TaskSpec,
        *,
        memory: list[Memory] | None = None,
        user_model: UserModel | None = None,
        knowledge: list[dict] | None = None,
        task_state: WorkflowState | None = None,
        prior_outputs: dict[str, Any] | None = None,
    ) -> AgentContext:
        """Select the most relevant context for ``task`` and return it.

        Never raises for well-formed inputs; empty or absent inputs simply yield
        fewer (possibly zero) sections.
        """
        candidates = build_candidates(
            task,
            memory=memory,
            user_model=user_model,
            knowledge=knowledge,
            task_state=task_state,
            prior_outputs=prior_outputs,
        )
        selected, dropped = self._select(candidates)
        self.last_selected = selected
        return AgentContext(
            task=task,
            context_sections=[
                ContextSection(key=c.key, content=c.content) for c in selected
            ],
            available_tools=[],
            available_skills=[],
            budget={
                "max_sections": self.budget.max_sections,
                "max_total_chars": self.budget.max_total_chars,
            },
            extras={
                "selected_count": len(selected),
                "dropped_count": dropped,
            },
        )

    # ------------------------------------------------------------------ #
    # Selection
    # ------------------------------------------------------------------ #

    def _select(self, candidates: list[Candidate]) -> tuple[list[Candidate], int]:
        """Rank, cap, truncate, and pack candidates into a section list.

        Order: score descending, ties broken by key (stable, deterministic).
        Only candidates with a positive score survive (except ``task_state``,
        which is the forced workflow anchor). Then: at most ``max_sections``,
        each truncated to ``max_chars_per_section``, and packing stops as soon
        as the next section would push the total past ``max_total_chars``.
        """
        max_sections = max(0, self.budget.max_sections)
        max_chars_per_section = max(0, self.budget.max_chars_per_section)
        max_total_chars = max(0, self.budget.max_total_chars)

        eligible = [
            c for c in candidates if c.score > 0 or c.source in ("task_state", "prior_output")
        ]
        ranked = sorted(eligible, key=lambda c: (-c.score, c.key))

        selected: list[Candidate] = []
        total_chars = 0
        for candidate in ranked:
            if len(selected) >= max_sections:
                break
            content = candidate.content[:max_chars_per_section]
            if total_chars + len(content) > max_total_chars:
                break
            selected.append(
                Candidate(
                    key=candidate.key,
                    content=content,
                    source=candidate.source,
                    score=candidate.score,
                    source_ref=candidate.source_ref,
                )
            )
            total_chars += len(content)

        dropped = len(candidates) - len(selected)
        return selected, dropped
