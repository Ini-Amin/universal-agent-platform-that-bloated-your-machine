"""Tests for build Step 17 - Deep Agent Integration (Master sections 14, 29).

The Deep Agent is a *bounded executor for one open-ended subtask*, invoked by a
workflow as a single node. These tests pin the seven safety invariants:

1. BOUNDED          - iteration and tool-call budgets.
2. REGISTRIES ONLY  - tool/agent steps resolve through the registries, so the
                      deterministic tool policy (tier-3 approval gate) applies.
3. CHECKPOINTED     - a WorkflowState per iteration; resume continues without
                      re-running committed steps.
4. OBSERVABLE       - plan/tool/agent/error events on the EventBus.
5. ARTIFACTS        - observations + output assembled into an AgentResult.
6. FINISH           - the ``finish`` step ends the loop with success.
7. TOTAL            - unexpected exceptions never escape.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import uap.deep
from uap.agents import AgentRegistry, EchoAgent
from uap.contracts import (
    AgentContext,
    AgentResult,
    AgentStatus,
    ApprovalRequest,
    ApprovalState,
    Domain,
    TaskSpec,
    WorkflowState,
    WorkflowStatus,
)
from uap.deep import DeepAgent, PlanStep, ScriptedPlanner
from uap.observability import EventBus, EventKind, MemorySink
from uap.tools import ToolRegistry, ToolSpec, register_local_tools
from uap.workflows.runner import InMemoryStateStore

FORBIDDEN_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(langgraph|uap\.server|fastapi|httpx|requests)\b",
    flags=re.MULTILINE,
)

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def make_task(goal: str = "solve the open-ended subtask") -> TaskSpec:
    return TaskSpec(domain=Domain.UNKNOWN, goal=goal)


def make_context(task: TaskSpec, **extras: object) -> AgentContext:
    return AgentContext(task=task, extras=dict(extras))


def make_tools(root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_local_tools(registry, root)
    return registry


def make_agents() -> AgentRegistry:
    registry = AgentRegistry()
    registry.register(EchoAgent())
    return registry


def tool_step(step_id: int, target: str, **args: object) -> PlanStep:
    return PlanStep(
        step_id=step_id,
        description=f"call {target}",
        action="tool",
        target=target,
        args=dict(args),
    )


def finish_step(step_id: int, summary: str | None = None) -> PlanStep:
    args = {"summary": summary} if summary is not None else {}
    return PlanStep(
        step_id=step_id,
        description="finish",
        action="finish",
        args=args,
    )


def approved(task: TaskSpec, action: str = "write artifact") -> ApprovalRequest:
    return ApprovalRequest(
        task_id=task.task_id,
        action=action,
        state=ApprovalState.APPROVED,
        decided_by="reviewer",
    )


class BoomTool:
    """Registers a tier-0 tool whose callable raises on every invocation."""

    @staticmethod
    def register(registry: ToolRegistry) -> None:
        async def boom(**kwargs: object) -> object:
            raise RuntimeError("kaboom")

        registry.register(
            ToolSpec(name="boom", description="always raises", risk_tier=0), boom
        )


def make_agent(
    name: str,
    script: list[list[PlanStep]],
    tools: ToolRegistry,
    agents: AgentRegistry,
    *,
    bus: EventBus | None = None,
    store: InMemoryStateStore | None = None,
    **kwargs: object,
) -> DeepAgent:
    return DeepAgent(
        name=name,
        planner=ScriptedPlanner(script),
        tools=tools,
        agents=agents,
        bus=bus,
        state_store=store,
        **kwargs,  # type: ignore[arg-type]
    )


def observations_of(store: InMemoryStateStore, task_id: str) -> list[dict]:
    state = store.load(task_id)
    assert state is not None
    return list(state.data.get("observations") or [])


# --------------------------------------------------------------------------- #
# 1. Happy path: tool -> finish
# --------------------------------------------------------------------------- #


async def test_tool_then_finish_completes_success(tmp_path: Path) -> None:
    task = make_task()
    script = [[tool_step(1, "echo", hello="world")], [finish_step(2)]]
    agent = make_agent("solver", script, make_tools(tmp_path), make_agents())

    result = await agent.solve(task, make_context(task))

    assert result.status == AgentStatus.SUCCESS
    assert "hello" in result.output and "world" in result.output
    assert result.error is None


async def test_finish_with_summary_sets_output(tmp_path: Path) -> None:
    task = make_task()
    script = [[tool_step(1, "echo", x=1)], [finish_step(2, summary="ALL DONE")]]
    agent = make_agent("solver", script, make_tools(tmp_path), make_agents())

    result = await agent.solve(task, make_context(task))

    assert result.status == AgentStatus.SUCCESS
    assert result.output == "ALL DONE"


async def test_result_carries_one_deep_report_artifact_without_writing(
    tmp_path: Path,
) -> None:
    task = make_task()
    script = [[tool_step(1, "echo", x=1)], [finish_step(2)]]
    agent = make_agent("solver", script, make_tools(tmp_path), make_agents())

    result = await agent.solve(task, make_context(task))

    assert [a.type for a in result.artifacts] == ["deep-report.md"]
    assert result.artifacts[0].content_ref is not None
    # DeepAgent itself never writes to the filesystem.
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- #
# 2. Delegation via registries only
# --------------------------------------------------------------------------- #


async def test_agent_step_resolves_through_registry_and_is_observed(
    tmp_path: Path,
) -> None:
    task = make_task("explain subnetting")
    script = [
        [
            PlanStep(
                step_id=1,
                description="ask the echo agent",
                action="agent",
                target="echo",
                args={"message": "hello"},
            )
        ],
        [finish_step(2)],
    ]
    agent = make_agent("solver", script, make_tools(tmp_path), make_agents())

    result = await agent.solve(task, make_context(task))

    assert result.status == AgentStatus.SUCCESS
    assert "hello" in result.output


async def test_tier3_tool_without_approval_is_denied_and_loop_continues(
    tmp_path: Path,
) -> None:
    task = make_task()
    store = InMemoryStateStore()
    script = [
        [
            tool_step(
                1,
                "write_artifact_file",
                path="report.md",
                content="# hi\n",
            )
        ],
        [finish_step(2)],
    ]
    agent = make_agent(
        "solver", script, make_tools(tmp_path), make_agents(), store=store
    )

    result = await agent.solve(task, make_context(task))

    # The denial is an observation, not a crash, and the loop reaches finish.
    assert result.status == AgentStatus.SUCCESS
    obs = observations_of(store, task.task_id)
    denied = [o for o in obs if o["action"] == "tool" and o["status"] == "denied"]
    assert len(denied) == 1
    assert "approv" in denied[0]["error"].lower()
    # No side effect happened.
    assert (tmp_path / "report.md").exists() is False


async def test_tier3_tool_with_approved_request_executes(tmp_path: Path) -> None:
    task = make_task()
    script = [
        [
            tool_step(
                1,
                "write_artifact_file",
                path="report.md",
                content="# Findings\n",
            )
        ],
        [finish_step(2)],
    ]
    agent = make_agent("solver", script, make_tools(tmp_path), make_agents())

    result = await agent.solve(
        task,
        make_context(task, approvals={"write_artifact_file": approved(task)}),
    )

    assert result.status == AgentStatus.SUCCESS
    written = tmp_path / "report.md"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == "# Findings\n"


async def test_unknown_tool_records_observation_and_does_not_crash(
    tmp_path: Path,
) -> None:
    task = make_task()
    store = InMemoryStateStore()
    script = [[tool_step(1, "does_not_exist")], [finish_step(2)]]
    agent = make_agent(
        "solver", script, make_tools(tmp_path), make_agents(), store=store
    )

    result = await agent.solve(task, make_context(task))

    assert result.status == AgentStatus.SUCCESS
    obs = observations_of(store, task.task_id)
    assert any("unknown tool" in (o["error"] or "") for o in obs)


async def test_unknown_agent_records_observation_and_does_not_crash(
    tmp_path: Path,
) -> None:
    task = make_task()
    store = InMemoryStateStore()
    script = [
        [
            PlanStep(
                step_id=1,
                description="ghost",
                action="agent",
                target="ghost",
            )
        ],
        [finish_step(2)],
    ]
    agent = make_agent(
        "solver", script, make_tools(tmp_path), make_agents(), store=store
    )

    result = await agent.solve(task, make_context(task))

    assert result.status == AgentStatus.SUCCESS
    obs = observations_of(store, task.task_id)
    assert any("unknown agent" in (o["error"] or "") for o in obs)


# --------------------------------------------------------------------------- #
# 3. Bounded: iteration and tool-call budgets
# --------------------------------------------------------------------------- #


async def test_iteration_budget_exhaustion_is_partial(tmp_path: Path) -> None:
    task = make_task()
    # A planner that never emits a finish step: one tool round, forever.
    script = [[tool_step(1, "echo", i=n)] for n in range(10)]
    agent = make_agent(
        "solver", script, make_tools(tmp_path), make_agents(), max_iterations=2
    )

    result = await agent.solve(task, make_context(task))

    assert result.status == AgentStatus.PARTIAL
    assert result.error is not None
    assert "iteration" in result.error.lower()


async def test_tool_budget_exhaustion_is_partial(tmp_path: Path) -> None:
    task = make_task()
    steps = [tool_step(i, "echo", i=i) for i in (1, 2, 3)]
    script = [steps, [finish_step(9)]]
    tools = make_tools(tmp_path)
    agent = make_agent(
        "solver", script, tools, make_agents(), max_tool_calls=1
    )

    result = await agent.solve(task, make_context(task))

    assert result.status == AgentStatus.PARTIAL
    assert result.error is not None
    assert "tool" in result.error.lower()
    assert "budget" in result.error.lower()
    # Exactly the budget was spent, never more.
    assert len(tools.calls) == 1


async def test_scripted_planner_exhaustion_is_success_when_no_errors(
    tmp_path: Path,
) -> None:
    task = make_task()
    script = [[tool_step(1, "echo", i=1)]]
    agent = make_agent(
        "solver", script, make_tools(tmp_path), make_agents(), max_iterations=8
    )

    result = await agent.solve(task, make_context(task))

    # The plan ran out cleanly (every step ok): the loop ends successfully even
    # without an explicit finish step.
    assert result.status == AgentStatus.SUCCESS


async def test_scripted_planner_exhaustion_after_error_is_partial(
    tmp_path: Path,
) -> None:
    task = make_task()
    script = [[tool_step(1, "does_not_exist")]]
    agent = make_agent(
        "solver", script, make_tools(tmp_path), make_agents(), max_iterations=8
    )

    result = await agent.solve(task, make_context(task))

    # Plan ran out with an unresolved error and no finish: not a success.
    assert result.status == AgentStatus.PARTIAL


# --------------------------------------------------------------------------- #
# 4. Checkpoint and resume
# --------------------------------------------------------------------------- #


async def test_checkpoint_written_after_solve(tmp_path: Path) -> None:
    task = make_task()
    store = InMemoryStateStore()
    script = [[tool_step(1, "echo", i=1)], [finish_step(2)]]
    agent = make_agent(
        "solver", script, make_tools(tmp_path), make_agents(), store=store
    )

    await agent.solve(task, make_context(task))

    state = store.load(task.task_id)
    assert state is not None
    assert state.workflow == "deep:solver"
    assert state.status == WorkflowStatus.CHECKPOINTED
    assert state.data["iteration"] == 2
    assert isinstance(state.data["observations"], list)
    assert state.data["observations"]


async def test_resume_continues_without_rerunning_committed_step(
    tmp_path: Path,
) -> None:
    task = make_task()
    store = InMemoryStateStore()
    tools = make_tools(tmp_path)
    # Round 0 and 1 are tools; round 2 finishes. Step 1 (round 0) is already
    # committed in the checkpoint, so resume must start at round 1.
    script = [
        [tool_step(1, "echo", n=1)],
        [tool_step(2, "echo", n=2)],
        [finish_step(3, summary="DONE")],
    ]
    agent = make_agent("resumer", script, tools, make_agents(), store=store)

    committed = [
        {
            "iteration": 0,
            "step_id": 1,
            "action": "tool",
            "target": "echo",
            "status": "ok",
            "detail": "echo ok",
            "result": {"n": 1},
            "error": None,
        }
    ]
    store.save(
        WorkflowState(
            task_id=task.task_id,
            workflow="deep:resumer",
            status=WorkflowStatus.CHECKPOINTED,
            current_node="iteration-1",
            data={
                "iteration": 1,
                "tool_calls": 1,
                "observations": committed,
                "task": task.model_dump(mode="json"),
            },
        )
    )

    result = await agent.resume(task.task_id)

    assert result.status == AgentStatus.SUCCESS
    assert result.output == "DONE"
    # Step 1 was NOT re-run: only the round-1 echo call reached the registry.
    assert [call.args for call in tools.calls] == [{"n": 2}]


async def test_resume_without_checkpoint_fails_without_raising(
    tmp_path: Path,
) -> None:
    agent = make_agent(
        "solver",
        [[finish_step(1)]],
        make_tools(tmp_path),
        make_agents(),
        store=InMemoryStateStore(),
    )

    result = await agent.resume("missing-task")

    assert result.status == AgentStatus.FAILED
    assert result.error is not None
    assert "no checkpoint" in result.error


# --------------------------------------------------------------------------- #
# 5. Observability
# --------------------------------------------------------------------------- #


async def test_event_bus_receives_tool_and_agent_events(tmp_path: Path) -> None:
    task = make_task()
    bus = EventBus()
    sink = MemorySink()
    bus.subscribe(sink)
    script = [
        [
            PlanStep(
                step_id=1,
                description="ask echo",
                action="agent",
                target="echo",
                args={"message": "hi"},
            ),
            tool_step(2, "echo", x=1),
        ],
        [finish_step(3)],
    ]
    agent = make_agent(
        "solver", script, make_tools(tmp_path), make_agents(), bus=bus
    )

    await agent.solve(task, make_context(task))

    assert sink.query(EventKind.AGENT_RUN, task_id=task.task_id)
    assert sink.query(EventKind.TOOL_CALL, task_id=task.task_id)
    assert all(
        event.workflow == "deep:solver"
        for event in sink.query(task_id=task.task_id)
    )


async def test_denied_tool_emits_error_event(tmp_path: Path) -> None:
    task = make_task()
    bus = EventBus()
    sink = MemorySink()
    bus.subscribe(sink)
    script = [
        [tool_step(1, "write_artifact_file", path="a.txt", content="x")],
        [finish_step(2)],
    ]
    agent = make_agent(
        "solver", script, make_tools(tmp_path), make_agents(), bus=bus
    )

    await agent.solve(task, make_context(task))

    errors = sink.query(EventKind.ERROR, task_id=task.task_id)
    assert any("approv" in (event.error or "").lower() for event in errors)


# --------------------------------------------------------------------------- #
# 6. Totality: exceptions never escape
# --------------------------------------------------------------------------- #


async def test_tool_exception_is_observed_and_not_raised(tmp_path: Path) -> None:
    task = make_task()
    store = InMemoryStateStore()
    tools = make_tools(tmp_path)
    BoomTool.register(tools)
    agent = make_agent("solver", [[tool_step(1, "boom")]], tools, make_agents(), store=store)

    result = await agent.solve(task, make_context(task))

    assert result.status in (AgentStatus.PARTIAL, AgentStatus.FAILED)
    obs = observations_of(store, task.task_id)
    assert any("kaboom" in (o["error"] or "") for o in obs)


async def test_planner_exception_becomes_failed_result(tmp_path: Path) -> None:
    task = make_task()

    class ExplodingPlanner:
        async def plan(self, task, context, observations):  # noqa: ANN001
            raise RuntimeError("planner kaboom")

    agent = DeepAgent(
        name="solver",
        planner=ExplodingPlanner(),
        tools=make_tools(tmp_path),
        agents=make_agents(),
    )

    result = await agent.solve(task, make_context(task))

    assert result.status == AgentStatus.FAILED
    assert result.error is not None
    assert "kaboom" in result.error


# --------------------------------------------------------------------------- #
# 7. Hygiene and serialization
# --------------------------------------------------------------------------- #


def test_deep_package_imports_no_langgraph_or_server_layers() -> None:
    """Master section 29 rule 20: Deep Agents are orchestrated, not orchestrators."""
    deep_dir = Path(uap.deep.__file__).parent
    offenders = [
        f"{path.name}: {match.group(1)}"
        for path in sorted(deep_dir.glob("*.py"))
        for match in FORBIDDEN_IMPORT.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_plan_step_forbids_unknown_fields() -> None:
    with pytest.raises(Exception):
        PlanStep(step_id=1, description="x", action="tool", surprise=True)


async def test_scripted_planner_returns_empty_when_exhausted() -> None:
    planner = ScriptedPlanner([[finish_step(1)]])
    task = make_task()
    context = make_context(task)

    assert len(await planner.plan(task, context, [])) == 1
    assert await planner.plan(task, context, []) == []


async def test_agent_result_round_trips_json(tmp_path: Path) -> None:
    task = make_task()
    script = [[tool_step(1, "echo", x=1)], [finish_step(2)]]
    agent = make_agent("solver", script, make_tools(tmp_path), make_agents())

    result = await agent.solve(task, make_context(task))
    restored = AgentResult.model_validate_json(result.model_dump_json())

    assert restored == result
