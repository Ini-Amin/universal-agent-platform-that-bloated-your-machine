"""Tests for governance enforcement and approval gate wiring (Master sections 9, 20, 29, 31).

Verifies:
1. ToolRegistry with a PolicyEngine that DENIES -> ok=False with policy reason, tool fn never ran (spy).
2. ToolRegistry with a PolicyEngine that ALLOWS -> call succeeds.
3. Registry WITHOUT policy/capability configured -> behavior identical to today (regression guard).
4. CapabilityResolver denies -> failed result with reason; allows -> succeeds.
5. Tier-3 tool with no approval -> needs_approval result (existing behavior, keep).
6. Tier-3 tool with an APPROVED request -> executes.
7. ApprovalGate integration: a blocked tier-3 call creates a pending ApprovalRequest; after .decide(approved=True) the retried call succeeds.
8. The pending approval is visible in GET /tasks/{id} for the owning task.
9. An EventKind.APPROVAL event is emitted on the approval request (assert via the bus/sink).
10. Engine-level: an APPROVAL node pauses execution and resumes after the gate is decided (use GraphExecutor directly with a tiny 2-node graph).
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from uap.approval import ApprovalGate
from uap.capability import CapabilityResolver
from uap.contracts import ApprovalRequest, ApprovalState
from uap.execution import ExecutionResult, GraphExecutor, NodeRuntime, PauseExecution
from uap.graph import EdgeKind, GraphEdge, GraphNode, NodeKind, Port, PortType, WorkflowGraph
from uap.observability import EventBus, EventKind, MemorySink
from uap.policy import PolicyEffect, PolicyEngine, PolicyRule
from uap.server import create_app
from uap.tools import ToolRegistry, ToolSpec


# --------------------------------------------------------------------------- #
# Helpers & Spies
# --------------------------------------------------------------------------- #


class SpyTool:
    """Spy callable to confirm whether a tool function executed."""

    def __init__(self, return_value: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.return_value = return_value or {"status": "executed"}

    async def __call__(self, **kwargs) -> dict:
        self.calls.append(dict(kwargs))
        return self.return_value

    @property
    def called(self) -> bool:
        return len(self.calls) > 0


# --------------------------------------------------------------------------- #
# Test 1: PolicyEngine DENIES -> call returns ok=False with policy reason (spy not run)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_policy_engine_denies_blocks_tool_call() -> None:
    spy = SpyTool()
    engine = PolicyEngine(
        [
            PolicyRule(
                id="deny-all-tools",
                effect=PolicyEffect.DENY,
                priority=100,
                action_pattern="*",
                reason="policy engine forbids tool execution",
            )
        ]
    )
    registry = ToolRegistry(policy_engine=engine)
    registry.register(ToolSpec(name="spy_tool", description="test spy", risk_tier=0), spy)

    result = await registry.call("spy_tool", {"arg": "val"})

    assert result.ok is False
    assert "policy engine forbids tool execution" in result.error
    assert not spy.called


# --------------------------------------------------------------------------- #
# Test 2: PolicyEngine ALLOWS -> call succeeds
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_policy_engine_allows_tool_call() -> None:
    spy = SpyTool({"data": 42})
    engine = PolicyEngine(
        [
            PolicyRule(
                id="allow-all-tools",
                effect=PolicyEffect.ALLOW,
                priority=100,
                action_pattern="*",
                reason="policy engine permits tool execution",
            )
        ]
    )
    registry = ToolRegistry(policy_engine=engine)
    registry.register(ToolSpec(name="allowed_tool", description="test allowed", risk_tier=0), spy)

    result = await registry.call("allowed_tool", {"arg": "val"})

    assert result.ok is True
    assert result.result == {"data": 42}
    assert spy.called
    assert len(spy.calls) == 1


# --------------------------------------------------------------------------- #
# Test 3: Registry WITHOUT policy/capability configured -> behavior identical to today
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_registry_unconfigured_behavior_identical() -> None:
    spy = SpyTool({"legacy": True})
    registry = ToolRegistry()

    assert registry.policy_engine is None
    assert registry.capability_resolver is None
    assert registry.approval_gate is None

    registry.register(ToolSpec(name="default_tool", description="test unconfigured", risk_tier=0), spy)

    result = await registry.call("default_tool", {"x": 1})

    assert result.ok is True
    assert result.result == {"legacy": True}
    assert spy.called


# --------------------------------------------------------------------------- #
# Test 4: CapabilityResolver denies -> failed result with reason; allows -> succeeds
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_capability_resolver_denies_and_allows() -> None:
    spy = SpyTool({"network_probe": "done"})
    resolver = CapabilityResolver()
    registry = ToolRegistry(capability_resolver=resolver)

    # Risk tier 2 tool derives "network" capability requirement
    registry.register(ToolSpec(name="probe_http", description="http probe", risk_tier=2), spy)

    # 1. Deny case: no grant issued yet
    denied = await registry.call("probe_http", {"url": "https://example.com"})
    assert denied.ok is False
    assert "no grant" in denied.error
    assert not spy.called

    # 2. Allow case: issue capability grant for network
    resolver.grant("agent", "network")
    allowed = await registry.call("probe_http", {"url": "https://example.com"})
    assert allowed.ok is True
    assert allowed.result == {"network_probe": "done"}
    assert spy.called


# --------------------------------------------------------------------------- #
# Test 5: Tier-3 tool with no approval -> needs_approval result (existing behavior, keep)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tier3_tool_without_approval_needs_approval() -> None:
    spy = SpyTool()
    registry = ToolRegistry()
    registry.register(ToolSpec(name="side_effect", description="tier-3 side-effectful", risk_tier=3), spy)

    result = await registry.call("side_effect", {"action": "delete"})

    assert result.ok is False
    assert "requires an approved request" in result.error
    assert not spy.called


# --------------------------------------------------------------------------- #
# Test 6: Tier-3 tool with an APPROVED request -> executes
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tier3_tool_with_approved_request_executes() -> None:
    spy = SpyTool({"executed": True})
    gate = ApprovalGate()
    req = gate.request(task_id="task-approved", action="side_effect")
    approved = gate.decide(req.approval_id, approved=True, decided_by="sec_admin")

    registry = ToolRegistry()
    registry.register(ToolSpec(name="side_effect", description="tier-3 side-effectful", risk_tier=3), spy)

    result = await registry.call("side_effect", {"action": "delete"}, approval=approved)

    assert result.ok is True
    assert result.result == {"executed": True}
    assert spy.called


# --------------------------------------------------------------------------- #
# Test 7: ApprovalGate integration: blocked tier-3 call creates pending request; decide(approved=True) -> retry succeeds
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_approval_gate_integration_blocked_call_creates_request_and_retry_succeeds() -> None:
    spy = SpyTool({"deployed": True})
    gate = ApprovalGate()
    registry = ToolRegistry(approval_gate=gate)
    registry.register(ToolSpec(name="deploy_patch", description="deploy patch", risk_tier=3), spy)

    # Call blocked needing approval
    result = await registry.call("deploy_patch", {"version": "v1.2"}, task_id="task-deploy-7")

    assert result.ok is False
    assert result.approval_id is not None
    assert not spy.called

    # Verify ApprovalRequest created in the gate
    pending = gate.pending()
    assert len(pending) == 1
    req = pending[0]
    assert req.approval_id == result.approval_id
    assert req.task_id == "task-deploy-7"
    assert req.action == "deploy_patch"
    assert req.details["tier"] == 3
    assert req.details["args"] == {"version": "v1.2"}
    assert req.state == ApprovalState.PENDING_APPROVAL

    # Decide approval
    decided = gate.decide(req.approval_id, approved=True, decided_by="incident_commander")
    assert decided.state == ApprovalState.APPROVED

    # Retry tool call with the approved request
    retried = await registry.call("deploy_patch", {"version": "v1.2"}, approval=decided)
    assert retried.ok is True
    assert retried.result == {"deployed": True}
    assert spy.called


# --------------------------------------------------------------------------- #
# Test 8: The pending approval is visible in GET /tasks/{id} for the owning task
# --------------------------------------------------------------------------- #


def test_pending_approval_visible_in_get_task() -> None:
    app = create_app(run_inline=True)
    client = TestClient(app)

    # Create task
    res = client.post("/tasks", json={"input": "research database indexing strategies"})
    assert res.status_code == 200
    task_id = res.json()["task_id"]

    # Before pending approval: pending_approvals is empty
    detail_before = client.get(f"/tasks/{task_id}").json()
    assert "pending_approvals" in detail_before
    assert detail_before["pending_approvals"] == []

    # Create an approval request for this task via the app gate
    req = app.state.gate.request(
        task_id=task_id,
        action="execute_exploit",
        details={"tier": 3, "args": {"target": "10.0.0.1"}},
    )

    # GET /tasks/{id} now reveals the pending approval
    detail_pending = client.get(f"/tasks/{task_id}").json()
    assert "pending_approvals" in detail_pending
    assert len(detail_pending["pending_approvals"]) == 1
    approval_payload = detail_pending["pending_approvals"][0]
    assert approval_payload["approval_id"] == req.approval_id
    assert approval_payload["task_id"] == task_id
    assert approval_payload["action"] == "execute_exploit"
    assert approval_payload["state"] == "pending_approval"

    # Resolve via POST /approvals/{id}/decide
    decide_res = client.post(
        f"/approvals/{req.approval_id}/decide",
        json={"approved": True, "decided_by": "security_officer"},
    )
    assert decide_res.status_code == 200
    assert decide_res.json()["state"] == "approved"

    # GET /tasks/{id} now shows no pending approvals
    detail_after = client.get(f"/tasks/{task_id}").json()
    assert detail_after["pending_approvals"] == []


# --------------------------------------------------------------------------- #
# Test 9: An EventKind.APPROVAL event is emitted on the approval request
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_event_kind_approval_emitted_on_approval_request() -> None:
    bus = EventBus()
    sink = MemorySink()
    bus.subscribe(sink)

    gate = ApprovalGate()
    registry = ToolRegistry(approval_gate=gate, event_bus=bus)
    registry.register(ToolSpec(name="privileged_op", description="tier-3 op", risk_tier=3), SpyTool())

    result = await registry.call("privileged_op", {"scope": "prod"}, task_id="task-event-9")

    assert result.ok is False
    assert result.approval_id is not None

    approval_events = sink.query(kind=EventKind.APPROVAL)
    assert len(approval_events) == 1
    event = approval_events[0]
    assert event.task_id == "task-event-9"
    assert event.data["approval_id"] == result.approval_id
    assert event.data["action"] == "privileged_op"
    assert event.data["tier"] == 3
    assert event.data["args"] == {"scope": "prod"}


# --------------------------------------------------------------------------- #
# Test 10: Engine-level: APPROVAL node pauses execution and resumes after gate is decided
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_engine_approval_node_pauses_and_resumes_after_gate_decided() -> None:
    # Build a tiny 2-node graph: start (INPUT) -> approve (APPROVAL)
    port = Port(name="data", type=PortType.ANY, required=True)
    input_node = GraphNode(id="start", kind=NodeKind.INPUT, outputs=[port])
    approval_node = GraphNode(id="approve", kind=NodeKind.APPROVAL, inputs=[port])
    edge = GraphEdge(
        id="e1",
        source="start",
        source_port="data",
        target="approve",
        target_port="data",
        kind=EdgeKind.DATA,
    )
    graph = WorkflowGraph(
        id="tiny-approval-graph",
        name="tiny_approval",
        nodes=[input_node, approval_node],
        edges=[edge],
    )

    gate = ApprovalGate()
    request = gate.request("task-engine-10", "release_pipeline")

    class ApprovalRuntime(NodeRuntime):
        def __init__(self) -> None:
            self.approval_calls = 0

        async def run_node(self, node: GraphNode, inputs: dict, ctx) -> dict:
            if node.kind is NodeKind.APPROVAL:
                self.approval_calls += 1
                raise PauseExecution(request)
            return {"data": "input_ok"}

    runtime = ApprovalRuntime()
    executor = GraphExecutor(runtime)

    # 1. First execution pauses on APPROVAL node
    paused: ExecutionResult = await executor.execute(
        graph,
        {"data": "initial"},
        execution_id="exec-tiny-10",
        approval_gate=gate,
    )

    assert paused.status == "paused"
    assert paused.pending_approval is not None
    assert paused.pending_approval.approval_id == request.approval_id
    assert paused.node_status["start"] == "completed"
    assert paused.node_status["approve"] == "paused"
    assert runtime.approval_calls == 1

    # 2. Gate decides approval
    gate.decide(request.approval_id, approved=True, decided_by="qa_lead")

    # 3. Resume with approval: approval node completes without re-invoking node body
    resumed: ExecutionResult = await executor.execute(
        graph,
        {"data": "initial"},
        execution_id="exec-tiny-10",
        approval_gate=gate,
        resume_state=paused,
    )

    assert resumed.status == "completed"
    assert resumed.node_status["start"] == "completed"
    assert resumed.node_status["approve"] == "completed"
    assert runtime.approval_calls == 1  # Not re-run on resume
    assert resumed.node_results["approve"]["approval"].approval_id == request.approval_id
