"""Deep Agent: a bounded executor for open-ended subtasks (Master section 14).

A Deep Agent is *one node* in a workflow -- never the whole application. The
orchestration layer (LangGraph, or the plain-asyncio runner in
:mod:`uap.workflows.runner`) owns sequencing; a Deep Agent owns the messy,
open-ended middle of a single subtask, where the number of steps is not known
up front. It runs a small state machine::

    plan -> act -> observe -> replan -> ... -> finish

Everything that makes that safe lives here, and the safety invariants below are
the point of build Step 17 (Master section 29):

1. **Bounded.** At most ``max_iterations`` replan rounds and ``max_tool_calls``
   tool calls. Exceeding either stops the loop with ``status=partial`` and an
   explanation -- there is no path to an unbounded loop.
2. **Delegation via registries only.** ``agent`` steps resolve through
   :class:`~uap.agents.registry.AgentRegistry`; ``tool`` steps go through
   :meth:`~uap.tools.registry.ToolRegistry.call`, so the deterministic tool
   policy (including the tier-3 human-approval gate) always applies. A denied
   call is recorded as an observation, never bypassed.
3. **Checkpointed.** After every iteration a :class:`WorkflowState` is saved
   (``workflow="deep:<name>"``) holding the observations and iteration count.
   :meth:`DeepAgent.resume` continues from the last committed iteration without
   re-running committed steps (Master section 21).
4. **Observable.** Every plan round, tool call, agent call and error is emitted
   to the :class:`~uap.observability.events.EventBus` with the task id.
5. **Artifacts.** Observations and the final output are assembled into an
   :class:`AgentResult`; optionally a single ``deep-report.md`` artifact is
   referenced via ``content_ref``. The agent itself never touches the
   filesystem.
6. **Total.** Any unexpected exception becomes ``status=failed`` with the error
   text; :meth:`solve` / :meth:`resume` never raise.

The module deliberately imports neither LangGraph nor the server/HTTP layers
(Master section 29 rule 20): the Deep Agent is orchestrated, it does not
orchestrate.
"""

from __future__ import annotations

from typing import Any

from uap.agents.registry import AgentRegistry
from uap.contracts import (
    AgentContext,
    AgentResult,
    AgentStatus,
    ApprovalRequest,
    ApprovalState,
    Artifact,
    TaskSpec,
    ToolCall,
    WorkflowState,
    WorkflowStatus,
)
from uap.deep.planner import PlanStep, Planner, ScriptedPlanner
from uap.observability.events import EventBus, EventKind
from uap.tools.registry import ToolRegistry
from uap.workflows.runner import StateStore

__all__ = ["DeepAgent"]

_WORKFLOW_PREFIX = "deep:"
_REPORT_TYPE = "deep-report.md"
_SOURCE = "deep_agent"
_TERMINAL = (
    WorkflowStatus.COMPLETED,
    WorkflowStatus.FAILED,
    WorkflowStatus.CANCELLED,
)


class DeepAgent:
    """A bounded, checkpointed, observable plan/act/observe loop."""

    def __init__(
        self,
        name: str,
        planner: Planner,
        tools: ToolRegistry,
        agents: AgentRegistry,
        bus: EventBus | None = None,
        state_store: StateStore | None = None,
        max_iterations: int = 8,
        max_tool_calls: int = 20,
        require_approval: bool = True,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("deep agent name must be a non-empty string")
        if max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if max_tool_calls < 0:
            raise ValueError("max_tool_calls must be >= 0")
        self.name = name
        self.planner = planner
        self.tools = tools
        self.agents = agents
        self.bus = bus
        self.state_store = state_store
        self.max_iterations = max_iterations
        self.max_tool_calls = max_tool_calls
        #: When True, only *approved* requests are forwarded to the registry;
        #: anything else is passed as ``None`` so the deterministic tool policy
        #: denies it. When False, the request is forwarded as-is and the
        #: registry policy still decides -- the policy is never bypassed either
        #: way (Master section 29 rules 6 and 17).
        self.require_approval = require_approval
        self.workflow = f"{_WORKFLOW_PREFIX}{name}"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def solve(self, task: TaskSpec, context: AgentContext) -> AgentResult:
        """Run a fresh plan/act/observe loop for ``task``. Never raises."""
        try:
            return await self._drive(
                task,
                context,
                start_iteration=0,
                observations=[],
                tool_calls=0,
            )
        except Exception as exc:  # pragma: no cover - defensive total function
            return self._failed(task, exc)

    async def resume(self, task_id: str) -> AgentResult:
        """Continue a checkpointed run. Never raises.

        Loads the checkpoint written by the previous run and continues from the
        next iteration, re-using the stored observations so committed steps are
        not replayed. A missing checkpoint or a terminal checkpoint yields a
        failed result instead of an exception.
        """
        try:
            return await self._resume_inner(task_id)
        except Exception as exc:  # pragma: no cover - defensive total function
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.FAILED,
                output="",
                error=f"{type(exc).__name__}: {exc}",
            )

    # ------------------------------------------------------------------ #
    # Resume internals
    # ------------------------------------------------------------------ #

    async def _resume_inner(self, task_id: str) -> AgentResult:
        if self.state_store is None:
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.FAILED,
                output="",
                error="cannot resume without a state_store",
            )
        state = self.state_store.load(task_id)
        if state is None:
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.FAILED,
                output="",
                error=f"no checkpoint for task {task_id!r}",
            )
        if state.status in _TERMINAL:
            return AgentResult(
                agent_name=self.name,
                status=AgentStatus.FAILED,
                output="",
                error=f"task {task_id!r} already {state.status.value}",
            )

        data = state.data if isinstance(state.data, dict) else {}
        start_iteration = int(data.get("iteration", 0))
        observations = list(data.get("observations") or [])
        tool_calls = int(data.get("tool_calls", 0))

        # Keep a scripted planner aligned with the committed rounds so it does
        # not hand back plans that were already executed.
        if isinstance(self.planner, ScriptedPlanner):
            self.planner.set_round(start_iteration)

        task = self._task_from_state(state, data)
        context = self._context_from_state(data, task)

        self._emit(
            EventKind.NODE_STARTED,
            task.task_id,
            node=f"iteration-{start_iteration}",
            data={"iteration": start_iteration, "resumed": True},
        )
        return await self._drive(
            task,
            context,
            start_iteration=start_iteration,
            observations=observations,
            tool_calls=tool_calls,
        )

    # ------------------------------------------------------------------ #
    # The loop
    # ------------------------------------------------------------------ #

    async def _drive(
        self,
        task: TaskSpec,
        context: AgentContext,
        *,
        start_iteration: int,
        observations: list[dict],
        tool_calls: int,
    ) -> AgentResult:
        output = ""
        status = AgentStatus.PARTIAL
        error: str | None = None
        iteration = start_iteration

        while True:
            # -- Budget gate: iterations ---------------------------------- #
            if iteration >= self.max_iterations:
                error = (
                    f"iteration budget exhausted after {iteration} of "
                    f"{self.max_iterations} iterations"
                )
                self._emit(
                    EventKind.ERROR,
                    task.task_id,
                    node=f"iteration-{iteration}",
                    error=error,
                    data={"iteration": iteration},
                )
                break

            self._emit(
                EventKind.NODE_STARTED,
                task.task_id,
                node=f"iteration-{iteration}",
                data={"iteration": iteration},
            )

            # -- Plan ------------------------------------------------------ #
            try:
                steps = await self.planner.plan(task, context, observations)
            except Exception as exc:  # a planner failure is data, not a crash
                error = f"planner failed: {type(exc).__name__}: {exc}"
                observations.append(
                    self._observation(
                        iteration, None, "plan", None, "error", error, error=error
                    )
                )
                self._emit(
                    EventKind.ERROR,
                    task.task_id,
                    error=error,
                    data={"iteration": iteration},
                )
                status = AgentStatus.FAILED
                break

            observations.append(
                self._observation(
                    iteration,
                    None,
                    "plan",
                    None,
                    "ok",
                    f"planned {len(steps)} step(s)",
                )
            )

            if not steps:
                # No plan means no more work. Without an explicit ``finish`` we
                # only claim success if nothing failed; otherwise the run is
                # partial (invariant 1/6: finish is the only success signal).
                output = self._summary(observations)
                status = (
                    AgentStatus.PARTIAL
                    if self._has_error(observations)
                    else AgentStatus.SUCCESS
                )
                if status is AgentStatus.PARTIAL:
                    error = "plan exhausted without a finish step after errors"
                break

            # -- Act ------------------------------------------------------- #
            finish_output: str | None = None
            budget_hit: str | None = None
            for step in steps:
                if step.action == "finish":
                    finish_output = self._finish_output(step, observations)
                    break

                if step.action == "tool":
                    if tool_calls >= self.max_tool_calls:
                        budget_hit = (
                            f"tool budget exhausted after {tool_calls} of "
                            f"{self.max_tool_calls} tool calls"
                        )
                        break
                    tool_calls += 1
                    await self._run_tool_step(
                        task, context, step, iteration, observations
                    )

                elif step.action == "agent":
                    await self._run_agent_step(
                        task, context, step, iteration, observations
                    )

                else:  # pragma: no cover - the Literal keeps this unreachable
                    observations.append(
                        self._observation(
                            iteration,
                            step.step_id,
                            step.action,
                            step.target,
                            "error",
                            f"unknown action {step.action!r}",
                            error=f"unknown action {step.action!r}",
                        )
                    )

            iteration += 1
            self._checkpoint(task, iteration, observations, tool_calls, context)

            if finish_output is not None:
                output = finish_output
                status = AgentStatus.SUCCESS
                break
            if budget_hit is not None:
                error = budget_hit
                self._emit(
                    EventKind.ERROR,
                    task.task_id,
                    error=budget_hit,
                    data={"iteration": iteration, "tool_calls": tool_calls},
                )
                break

        if not output and status in (AgentStatus.PARTIAL, AgentStatus.FAILED):
            output = self._summary(observations)

        return self._result(
            task, status, output, observations, tool_calls, error=error
        )

    # ------------------------------------------------------------------ #
    # Step execution
    # ------------------------------------------------------------------ #

    async def _run_tool_step(
        self,
        task: TaskSpec,
        context: AgentContext,
        step: PlanStep,
        iteration: int,
        observations: list[dict],
    ) -> None:
        name = step.target or ""
        approval = self._approval_for(context, name)

        self._emit(
            EventKind.TOOL_CALL,
            task.task_id,
            node=f"iteration-{iteration}",
            data={"tool": name, "step_id": step.step_id, "args": step.args},
        )

        # Delegation goes through the registry only: policy always applies.
        result = await self.tools.call(name, step.args, approval=approval)

        if result.ok:
            observations.append(
                self._observation(
                    iteration,
                    step.step_id,
                    "tool",
                    name,
                    "ok",
                    f"{name} ok",
                    result=result.result,
                )
            )
            self._emit(
                EventKind.TOOL_CALL,
                task.task_id,
                node=f"iteration-{iteration}",
                duration_ms=result.duration_ms,
                data={"tool": name, "ok": True, "step_id": step.step_id},
            )
            return

        # A denial and a failure are both non-fatal: record and continue.
        detail = result.error or "tool call failed"
        kind = "denied" if self._looks_like_denial(detail) else "error"
        observations.append(
            self._observation(
                iteration, step.step_id, "tool", name, kind, detail, error=detail
            )
        )
        self._emit(
            EventKind.ERROR,
            task.task_id,
            node=f"iteration-{iteration}",
            error=detail,
            data={"tool": name, "ok": False, "step_id": step.step_id},
        )

    async def _run_agent_step(
        self,
        task: TaskSpec,
        context: AgentContext,
        step: PlanStep,
        iteration: int,
        observations: list[dict],
    ) -> None:
        target = step.target or ""
        # Resolution happens through the registry: name first, then capability.
        agent = self.agents.get(target)
        if agent is None:
            matches = self.agents.select(target)
            agent = matches[0] if matches else None

        self._emit(
            EventKind.AGENT_RUN,
            task.task_id,
            node=f"iteration-{iteration}",
            agent=target,
            data={"step_id": step.step_id, "args": step.args},
        )

        if agent is None:
            detail = f"unknown agent: {target}"
            observations.append(
                self._observation(
                    iteration,
                    step.step_id,
                    "agent",
                    target,
                    "error",
                    detail,
                    error=detail,
                )
            )
            self._emit(EventKind.ERROR, task.task_id, error=detail)
            return

        sub_context = self._sub_context(context, step)
        try:
            result = await agent.run(sub_context)
        except Exception as exc:  # agents never raise, but stay defensive
            detail = f"{target} raised: {type(exc).__name__}: {exc}"
            observations.append(
                self._observation(
                    iteration,
                    step.step_id,
                    "agent",
                    target,
                    "error",
                    detail,
                    error=detail,
                )
            )
            self._emit(EventKind.ERROR, task.task_id, agent=target, error=detail)
            return

        ok = result.status is AgentStatus.SUCCESS
        observations.append(
            self._observation(
                iteration,
                step.step_id,
                "agent",
                target,
                "ok" if ok else "error",
                f"{target} -> {result.status.value}",
                result=result.output,
                error=result.error,
            )
        )

    # ------------------------------------------------------------------ #
    # Approval plumbing
    # ------------------------------------------------------------------ #

    def _approval_for(
        self, context: AgentContext, tool_name: str
    ) -> ApprovalRequest | None:
        extras = context.extras if isinstance(context.extras, dict) else {}
        approval = None
        approvals = extras.get("approvals")
        if isinstance(approvals, dict):
            approval = self._coerce_approval(approvals.get(tool_name))
        if approval is None:
            approval = self._coerce_approval(extras.get("approval"))
        if (
            approval is not None
            and self.require_approval
            and approval.state is not ApprovalState.APPROVED
        ):
            # Never forward a non-approved request when the gate is on; the
            # registry policy then denies and the denial is observed.
            return None
        return approval

    @staticmethod
    def _coerce_approval(value: Any) -> ApprovalRequest | None:
        if value is None:
            return None
        if isinstance(value, ApprovalRequest):
            return value
        if isinstance(value, dict):
            try:
                return ApprovalRequest.model_validate(value)
            except Exception:
                return None
        return None

    @staticmethod
    def _looks_like_denial(detail: str) -> bool:
        lowered = detail.lower()
        return "approv" in lowered or "outside the allowed tiers" in lowered

    # ------------------------------------------------------------------ #
    # Checkpointing
    # ------------------------------------------------------------------ #

    def _checkpoint(
        self,
        task: TaskSpec,
        iteration: int,
        observations: list[dict],
        tool_calls: int,
        context: AgentContext | None = None,
    ) -> None:
        if self.state_store is None:
            return
        data: dict[str, Any] = {
            "iteration": iteration,
            "tool_calls": tool_calls,
            "observations": observations,
            "task": task.model_dump(mode="json"),
        }
        if context is not None:
            # Persist extras (including approvals) so resume keeps the same
            # delegation surface instead of silently losing it.
            data["context_extras"] = _jsonable(context.extras)
        state = WorkflowState(
            task_id=task.task_id,
            workflow=self.workflow,
            status=WorkflowStatus.CHECKPOINTED,
            current_node=f"iteration-{iteration}",
            data=data,
        )
        self.state_store.save(state)

    @staticmethod
    def _task_from_state(state: WorkflowState, data: dict) -> TaskSpec:
        raw = data.get("task")
        if isinstance(raw, dict):
            return TaskSpec.model_validate(raw)
        return TaskSpec(domain="unknown", goal="", task_id=state.task_id)

    @staticmethod
    def _context_from_state(data: dict, task: TaskSpec) -> AgentContext:
        extras = data.get("context_extras")
        if not isinstance(extras, dict):
            extras = {}
        return AgentContext(task=task, extras=extras)

    # ------------------------------------------------------------------ #
    # Result assembly
    # ------------------------------------------------------------------ #

    def _result(
        self,
        task: TaskSpec,
        status: AgentStatus,
        output: str,
        observations: list[dict],
        tool_calls: int,
        *,
        error: str | None,
    ) -> AgentResult:
        calls = [
            ToolCall(tool=obs["target"], args=obs.get("args") or {})
            for obs in observations
            if obs.get("action") == "tool" and obs.get("target")
        ]
        artifacts: list[Artifact] = []
        if observations:
            artifacts.append(
                Artifact(
                    task_id=task.task_id,
                    type=_REPORT_TYPE,
                    source=_SOURCE,
                    content_ref=f"deep:{self.name}/{task.task_id}/deep-report.md",
                )
            )
        return AgentResult(
            agent_name=self.name,
            status=status,
            output=output,
            artifacts=artifacts,
            tool_calls=calls,
            error=error,
        )

    def _failed(self, task: TaskSpec, exc: Exception) -> AgentResult:
        message = f"{type(exc).__name__}: {exc}"
        self._emit(EventKind.ERROR, task.task_id, error=message)
        return AgentResult(
            agent_name=self.name,
            status=AgentStatus.FAILED,
            output="",
            error=message,
        )

    @staticmethod
    def _finish_output(step: PlanStep, observations: list[dict]) -> str:
        summary = step.args.get("summary")
        if summary is not None:
            return str(summary)
        return DeepAgent._summary(observations)

    @staticmethod
    def _summary(observations: list[dict]) -> str:
        lines: list[str] = []
        for obs in observations:
            if obs.get("action") == "plan":
                continue
            target = obs.get("target") or ""
            status = obs.get("status")
            result = obs.get("result")
            if status == "ok" and result is not None:
                lines.append(f"{target}: {result}")
            else:
                detail = obs.get("detail") or ""
                if detail:
                    lines.append(detail)
        return "\n".join(lines)

    @staticmethod
    def _has_error(observations: list[dict]) -> bool:
        return any(obs.get("status") == "error" for obs in observations)

    @staticmethod
    def _observation(
        iteration: int,
        step_id: int | None,
        action: str,
        target: str | None,
        status: str,
        detail: str,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> dict:
        return {
            "iteration": iteration,
            "step_id": step_id,
            "action": action,
            "target": target,
            "status": status,
            "detail": detail,
            "result": result,
            "error": error,
        }

    @staticmethod
    def _sub_context(context: AgentContext, step: PlanStep) -> AgentContext:
        extras = dict(context.extras)
        extras["message"] = step.args.get("message", step.description)
        extras["deep_args"] = dict(step.args)
        return context.model_copy(update={"extras": extras})

    # ------------------------------------------------------------------ #
    # Observability
    # ------------------------------------------------------------------ #

    def _emit(
        self,
        kind: EventKind,
        task_id: str,
        *,
        node: str | None = None,
        agent: str | None = None,
        duration_ms: float | None = None,
        error: str | None = None,
        data: dict | None = None,
    ) -> None:
        if self.bus is None:
            return
        self.bus.emit_kind(
            kind,
            task_id=task_id,
            workflow=self.workflow,
            node=node,
            agent=agent,
            duration_ms=duration_ms,
            error=error,
            data=data or {},
        )


def _jsonable(value: Any) -> Any:
    """Best-effort conversion to checkpoint-safe JSON (drops non-serializable)."""
    import json

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {k: _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        return str(value)
