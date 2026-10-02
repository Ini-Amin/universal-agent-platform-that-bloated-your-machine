"""Deep Agent integration (Master section 14) - build Step 17.

A Deep Agent is a *bounded executor for one open-ended subtask*, invoked by a
workflow as a single node -- it is not the application. LangGraph (or the
plain-asyncio runner) orchestrates; Deep Agents execute.

Public surface:

* :class:`DeepAgent` - plan/act/observe/replan loop with hard iteration and
  tool-call budgets, registry-only delegation, per-iteration checkpoints,
  resume, and full observability.
* :class:`Planner` - the protocol every planner satisfies.
* :class:`ScriptedPlanner` - deterministic planner for tests and offline runs.
* :class:`PlanStep` - one closed-shape step of one plan round.
"""

from uap.deep.agent import DeepAgent
from uap.deep.planner import PlanStep, Planner, ScriptedPlanner

__all__ = ["DeepAgent", "PlanStep", "Planner", "ScriptedPlanner"]
