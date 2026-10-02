"""Deep Agent planning contracts (Master section 14).

A Deep Agent is a bounded executor for open-ended subtasks: it plans, acts,
observes, and replans. This module owns only the *plan* vocabulary -- the
deterministic loop, the registries it delegates through, the checkpoints and
the observability live in :mod:`uap.deep.agent`.

Two pieces:

* :class:`PlanStep` - one immutable instruction. ``action`` is a closed
  three-value vocabulary so an LLM planner can never invent a new control
  primitive: it can only ask for a tool, ask for another agent, or finish.
* :class:`Planner` - the protocol every planner satisfies. A planner is a pure
  function of (task, context, observations): it never calls tools itself and
  never sees the registries, which keeps planning and acting separable.

:class:`ScriptedPlanner` is the deterministic implementation used by tests and
as an offline fallback. It replays a fixed list of plans, one per replan round.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from uap.contracts import AgentContext, TaskSpec

__all__ = ["PlanStep", "Planner", "ScriptedPlanner"]


class PlanStep(BaseModel):
    """One step of one plan round.

    ``extra="forbid"`` keeps the shape closed: producers may not smuggle
    behaviour in through undeclared keys (Master section 29 rule 17 -- the
    control surface stays explicit and auditable).
    """

    model_config = ConfigDict(extra="forbid")

    step_id: int
    description: str
    action: Literal["tool", "agent", "finish"]
    target: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class Planner(Protocol):
    """Produces one plan for the current observations.

    Implementations must be side-effect free with respect to the platform: a
    planner proposes, it never executes. Delegation happens in the executor
    through the registries, never through the planner.
    """

    async def plan(
        self,
        task: TaskSpec,
        context: AgentContext,
        observations: list[dict],
    ) -> list[PlanStep]:
        """Return the steps for the next replan round (empty list ends it)."""
        ...


class ScriptedPlanner:
    """Deterministic planner: one pre-baked plan per replan round.

    ``script[i]`` is returned on the ``i``-th call. Once the script is
    exhausted the planner returns an empty plan, which the executor treats as
    "no further plan" and stops. :meth:`set_round` lets a resumed run point the
    planner at the round it should continue from, so committed rounds are never
    replayed.
    """

    def __init__(self, script: list[list[PlanStep]]) -> None:
        self._script = [list(round_) for round_ in script]
        self._index = 0

    @property
    def round(self) -> int:
        """The zero-based round the next :meth:`plan` call will return."""
        return self._index

    def set_round(self, index: int) -> None:
        """Point the planner at ``index``; used by resume to skip committed rounds."""
        self._index = max(0, int(index))

    async def plan(
        self,
        task: TaskSpec,
        context: AgentContext,
        observations: list[dict],
    ) -> list[PlanStep]:
        if self._index >= len(self._script):
            return []
        round_ = self._script[self._index]
        self._index += 1
        # Hand out copies so a caller mutating a step cannot poison the script.
        return [step.model_copy(deep=True) for step in round_]
