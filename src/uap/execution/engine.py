"""Deterministic graph execution engine (Master sections 18-21, 35-37).

The engine schedules a :class:`~uap.graph.model.WorkflowGraph` and delegates the
*actual* work of a node to an injected :class:`~uap.execution.context.NodeRuntime`.
It owns every state transition, the fan-in policies, the recursion guard, the
step budget and the event/decision stream; it knows nothing about agents, tools
or any framework.

Scheduling semantics
--------------------
* A node becomes *runnable* when every incoming edge it depends on is resolved
  (its source has settled) and at least one required incoming edge is satisfied.
  Nodes whose required inputs can never arrive are marked ``skipped``.
* Independent runnable nodes are launched concurrently (``asyncio``) up to
  ``max_parallel``; with ``max_parallel == 1`` they run one at a time.
* INPUT nodes are graph sources: their result *is* the run ``inputs`` dict.
* CONDITION nodes evaluate their CONTROL-out edge expressions (see
  :mod:`uap.execution.conditions`) and activate exactly the matching branches;
  every other branch is skipped. If nothing matches, a default edge
  (``condition is None`` or one of ``"default"``/``"else"``/``"*"``) is taken;
  otherwise the condition node fails.
* JOIN nodes wait for all incoming branches to settle and then apply
  ``graph.fan_in_policy`` (see below).
* SUBWORKFLOW nodes resolve ``config['workflow_ref']`` through the injected
  resolver and execute the nested graph recursively, guarded by
  ``max_subworkflow_depth`` (section 21).
* APPROVAL nodes are ordinary runtime nodes; a runtime that raises
  :class:`~uap.execution.context.PauseExecution` pauses the run.

Fan-in policy decisions (section 36)
------------------------------------
* ``REQUIRE_ALL``   - every incoming branch must succeed, otherwise the join
  fails.
* ``ALLOW_PARTIAL`` - at least one branch must succeed; the join proceeds with
  the successes.
* ``MIN_SUCCESS``   - at least ``graph.min_success`` branches must succeed
  (default 1).
* ``TIMEOUT``       - wait up to ``graph.timeout_s`` seconds for the branches.
  **Chosen semantics:** once the deadline passes the join *proceeds with the
  branches that arrived* (best-effort) and any still-running branch is abandoned
  (cancelled and marked ``skipped``). If zero branches arrived by the deadline
  the join fails.

Approval resume contract (section 20/23)
----------------------------------------
The simpler contract is implemented: when a run pauses on an APPROVAL node and
is resumed with ``resume_state``, the engine marks that approval node
``completed`` with the approval payload **without re-running its
``node_runtime``**. Therefore approval nodes must be idempotent *or* their
side-effectful action must be performed after the approval is granted, outside
the node body. If an ``approval_gate`` is supplied on resume and the stored
request is ``REJECTED``, the execution fails.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from uap.contracts.models import ApprovalRequest, ApprovalState
from uap.graph import (
    EdgeKind,
    FanInPolicy,
    GraphEdge,
    GraphNode,
    NodeKind,
    WorkflowGraph,
    validate_graph,
)
from uap.graph.validation import ValidationIssue

from .conditions import evaluate
from .context import (
    ExecutionContext,
    ExecutionResult,
    NodeExecutionError,
    NodeRuntime,
    PauseExecution,
)

__all__ = ["GraphExecutor"]

_DEFAULT_CONDITIONS = frozenset({"default", "else", "*"})
_DEPTH_KEY = "_uap_subworkflow_depth"


def _is_default_condition(expression: str | None) -> bool:
    if expression is None:
        return True
    return expression.strip().lower() in _DEFAULT_CONDITIONS


class GraphExecutor:
    """Schedules and runs a :class:`WorkflowGraph` against a node runtime."""

    def __init__(
        self,
        node_runtime: NodeRuntime,
        *,
        max_parallel: int = 8,
        max_steps: int = 1000,
        max_subworkflow_depth: int = 3,
        node_timeout_s: float | None = None,
    ) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be >= 1")
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if max_subworkflow_depth < 0:
            raise ValueError("max_subworkflow_depth must be >= 0")
        self.node_runtime = node_runtime
        self.max_parallel = max_parallel
        self.max_steps = max_steps
        self.max_subworkflow_depth = max_subworkflow_depth
        self.node_timeout_s = node_timeout_s

    async def execute(
        self,
        graph: WorkflowGraph,
        inputs: dict[str, Any],
        *,
        execution_id: str,
        metadata: dict | None = None,
        approval_gate: Any | None = None,
        subworkflow_resolver: Callable[[str], WorkflowGraph] | None = None,
        event_sink: Callable[[str, dict], None] | None = None,
        decision_sink: Callable[[dict], None] | None = None,
        resume_state: ExecutionResult | None = None,
    ) -> ExecutionResult:
        """Run ``graph``; return the terminal or paused :class:`ExecutionResult`.

        ``resume_state`` continues a previously paused run: nodes already
        ``completed``/``failed`` are not re-run and paused APPROVAL nodes are
        completed with the approval payload (see the module docstring).
        """
        run = _Run(
            executor=self,
            graph=graph,
            inputs=dict(inputs),
            execution_id=execution_id,
            metadata=dict(metadata) if metadata else {},
            approval_gate=approval_gate,
            subworkflow_resolver=subworkflow_resolver,
            event_sink=event_sink,
            decision_sink=decision_sink,
            resume_state=resume_state,
        )
        return await run.run()


class _Run:
    """Per-execution state. Kept off the executor so concurrent runs are safe."""

    def __init__(
        self,
        *,
        executor: GraphExecutor,
        graph: WorkflowGraph,
        inputs: dict[str, Any],
        execution_id: str,
        metadata: dict[str, Any],
        approval_gate: Any | None,
        subworkflow_resolver: Callable[[str], WorkflowGraph] | None,
        event_sink: Callable[[str, dict], None] | None,
        decision_sink: Callable[[dict], None] | None,
        resume_state: ExecutionResult | None,
    ) -> None:
        self.executor = executor
        self.runtime: NodeRuntime = executor.node_runtime
        self.graph = graph
        self.inputs = inputs
        self.max_parallel = executor.max_parallel
        self.max_steps = executor.max_steps
        self.max_subworkflow_depth = executor.max_subworkflow_depth
        self.node_timeout_s = executor.node_timeout_s
        self.depth = int(metadata.get(_DEPTH_KEY, 0))

        self.ctx = ExecutionContext(
            execution_id=execution_id,
            graph=graph,
            metadata=metadata,
            approval_gate=approval_gate,
            subworkflow_resolver=subworkflow_resolver,
            node_runtime=self.runtime,
            event_sink=event_sink,
            decision_sink=decision_sink,
        )

        self.nodes: dict[str, GraphNode] = {n.id: n for n in graph.nodes}
        self.node_ids: list[str] = [n.id for n in graph.nodes]
        self.incoming: dict[str, list[GraphEdge]] = {nid: [] for nid in self.node_ids}
        self.outgoing: dict[str, list[GraphEdge]] = {nid: [] for nid in self.node_ids}
        for edge in graph.edges:
            if edge.source in self.outgoing:
                self.outgoing[edge.source].append(edge)
            if edge.target in self.incoming:
                self.incoming[edge.target].append(edge)
        self.port_required: dict[tuple[str, str], bool] = {
            (n.id, p.name): p.required for n in graph.nodes for p in n.inputs
        }

        self.selected: set[str] = set()
        self.absorbed: set[str] = set()
        self.order: list[str] = []
        self.node_errors: dict[str, str] = {}
        self.join_start: dict[str, float] = {}
        self.steps = 0
        self.fatal_error: str | None = None
        self.pending_approval: ApprovalRequest | None = None
        self.paused = False
        self.running: dict[asyncio.Task, str] = {}
        self.resume_state = resume_state

    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #

    async def run(self) -> ExecutionResult:
        issues = validate_graph(self.graph)
        errors = [i for i in issues if i.severity == "error"]
        if errors:
            return self._validation_failure(errors)

        self._init_status()
        if self.resume_state is not None:
            self._restore(self.resume_state)

        self._run_input_nodes()
        if self.fatal_error or self.paused:
            return self._result()

        await self._schedule()
        return self._result()

    # ------------------------------------------------------------------ #
    # Status / resume
    # ------------------------------------------------------------------ #

    def _init_status(self) -> None:
        for nid in self.node_ids:
            self.ctx.node_status.setdefault(nid, "pending")

    def _restore(self, resume: ExecutionResult) -> None:
        for nid, status in resume.node_status.items():
            if nid not in self.nodes:
                continue
            self.ctx.node_status[nid] = status
        for nid, results in resume.node_results.items():
            self.ctx.node_results[nid] = dict(results)
        self.order = [nid for nid in resume.order if nid in self.nodes]

        for nid in self.node_ids:
            node = self.nodes[nid]
            status = self.ctx.node_status.get(nid, "pending")
            if status == "completed":
                if node.kind is NodeKind.CONDITION:
                    self._reselect_condition(node)
                else:
                    self._select_non_error(nid)
            elif status == "failed":
                for edge in self.outgoing[nid]:
                    if edge.kind is EdgeKind.ERROR:
                        self.selected.add(edge.id)
            elif status == "paused":
                self._resume_paused_node(node, resume)
            elif status == "running":
                self.ctx.node_status[nid] = "pending"

    def _resume_paused_node(self, node: GraphNode, resume: ExecutionResult) -> None:
        approval = resume.pending_approval
        if self.ctx.approval_gate is not None and approval is not None:
            try:
                state = self.ctx.approval_gate.state(approval.approval_id)
            except Exception:  # noqa: BLE001 - unknown id means "trust resume state"
                state = None
            if state is ApprovalState.REJECTED:
                self.fatal_error = f"approval {approval.approval_id} was rejected"
                self.ctx.node_status[node.id] = "failed"
                return

        payload: dict[str, Any] = {}
        if approval is not None:
            payload = {"approval": approval, **dict(approval.details or {})}
        self.ctx.node_results[node.id] = payload
        self.ctx.node_status[node.id] = "completed"
        if node.id not in self.order:
            self.order.append(node.id)
        self._select_non_error(node.id)
        self._emit("node_finished", {"node_id": node.id, "duration_ms": 0.0})

    # ------------------------------------------------------------------ #
    # INPUT nodes
    # ------------------------------------------------------------------ #

    def _run_input_nodes(self) -> None:
        for node in self.graph.nodes:
            if node.kind is not NodeKind.INPUT:
                continue
            if self.ctx.node_status.get(node.id) == "completed":
                continue
            missing = [
                p.name
                for p in node.inputs
                if p.required and p.name not in self.inputs
            ]
            if missing:
                self.fatal_error = (
                    f"INPUT node {node.id!r} is missing required input(s): "
                    f"{', '.join(missing)}"
                )
                return
            if not self._charge_step():
                return
            self._emit("node_started", {"node_id": node.id})
            self.ctx.node_results[node.id] = dict(self.inputs)
            self.ctx.node_status[node.id] = "completed"
            self.order.append(node.id)
            self._select_non_error(node.id)
            self._emit("node_finished", {"node_id": node.id, "duration_ms": 0.0})

    # ------------------------------------------------------------------ #
    # Scheduling loop
    # ------------------------------------------------------------------ #

    async def _schedule(self) -> None:
        while True:
            if self.fatal_error or self.paused:
                break
            launched = False
            for nid in self.node_ids:
                if self.fatal_error or self.paused:
                    break
                if self.ctx.node_status.get(nid, "pending") != "pending":
                    continue
                node = self.nodes[nid]
                if node.kind is NodeKind.CONDITION:
                    if self._all_incoming_resolved(nid):
                        self._run_condition(node)
                        launched = True
                    continue
                if node.kind is NodeKind.JOIN:
                    self._track_join_start(nid)
                    if self._join_ready(nid):
                        await self._run_join(node)
                        launched = True
                    continue
                if len(self.running) >= self.max_parallel:
                    continue
                if self._normal_ready(nid):
                    if not self._charge_step():
                        break
                    task = asyncio.create_task(self._invoke(node, self._gather_inputs(nid)))
                    self.running[task] = nid
                    launched = True

            self._mark_skips()
            if self.fatal_error or self.paused:
                break
            if not self.running:
                if not launched:
                    break
                continue

            timeout = self._join_wait_timeout()
            done, _ = await asyncio.wait(
                set(self.running), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                self.running.pop(task, None)

        if self.running:
            for task in list(self.running):
                task.cancel()
            await asyncio.gather(*self.running, return_exceptions=True)
            self.running.clear()

    # ------------------------------------------------------------------ #
    # Readiness
    # ------------------------------------------------------------------ #

    def _edge_state(self, edge: GraphEdge) -> str:
        status = self.ctx.node_status.get(edge.source, "pending")
        if status in ("completed", "failed"):
            return "satisfied" if edge.id in self.selected else "dead"
        if status == "skipped":
            return "dead"
        return "unresolved"

    def _incoming_states(self, nid: str) -> list[str]:
        return [self._edge_state(e) for e in self.incoming[nid]]

    def _all_incoming_resolved(self, nid: str) -> bool:
        return all(state != "unresolved" for state in self._incoming_states(nid))

    def _is_required(self, edge: GraphEdge) -> bool:
        return self.port_required.get((edge.target, edge.target_port), True)

    def _normal_ready(self, nid: str) -> bool:
        edges = self.incoming[nid]
        if not edges:
            return False
        states = [self._edge_state(e) for e in edges]
        if any(state == "unresolved" for state in states):
            return False
        for edge, state in zip(edges, states):
            if state == "dead" and self._is_required(edge):
                return False
        return any(state == "satisfied" for state in states)

    def _mark_skips(self) -> None:
        for nid in self.node_ids:
            if self.ctx.node_status.get(nid, "pending") != "pending":
                continue
            node = self.nodes[nid]
            if node.kind in (NodeKind.CONDITION, NodeKind.JOIN):
                continue
            if not self.incoming[nid]:
                continue
            if not self._all_incoming_resolved(nid):
                continue
            if self._normal_ready(nid):
                continue
            self.ctx.node_status[nid] = "skipped"

    def _gather_inputs(self, nid: str) -> dict[str, Any]:
        inputs: dict[str, Any] = {}
        for edge in self.incoming[nid]:
            if edge.kind is not EdgeKind.DATA:
                continue
            if self._edge_state(edge) != "satisfied":
                continue
            value = self.ctx.node_results.get(edge.source, {}).get(edge.source_port)
            if edge.target_port in inputs:
                existing = inputs[edge.target_port]
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    inputs[edge.target_port] = [existing, value]
            else:
                inputs[edge.target_port] = value
        return inputs

    def _select_non_error(self, nid: str) -> None:
        for edge in self.outgoing[nid]:
            if edge.kind is not EdgeKind.ERROR:
                self.selected.add(edge.id)

    # ------------------------------------------------------------------ #
    # CONDITION nodes
    # ------------------------------------------------------------------ #

    def _reselect_condition(self, node: GraphNode) -> None:
        """Deterministically replay a completed condition node on resume."""
        control_out = [e for e in self.outgoing[node.id] if e.kind is EdgeKind.CONTROL]
        scope = {"inputs": self.inputs, "node": self.ctx.node_results}
        default_edge: GraphEdge | None = None
        matched: list[GraphEdge] = []
        for edge in control_out:
            if _is_default_condition(edge.condition):
                default_edge = edge
                continue
            ok, _ = evaluate(edge.condition or "", scope)
            if ok:
                matched.append(edge)
        if matched:
            for edge in matched:
                self.selected.add(edge.id)
        elif default_edge is not None:
            self.selected.add(default_edge.id)

    def _run_condition(self, node: GraphNode) -> None:
        if not self._charge_step():
            return
        self._emit("node_started", {"node_id": node.id})
        control_out = [e for e in self.outgoing[node.id] if e.kind is EdgeKind.CONTROL]
        scope = {"inputs": self.inputs, "node": self.ctx.node_results}
        default_edge: GraphEdge | None = None
        matched: list[tuple[GraphEdge, str]] = []
        for edge in control_out:
            if _is_default_condition(edge.condition):
                default_edge = edge
                continue
            ok, reason = evaluate(edge.condition or "", scope)
            if ok:
                matched.append((edge, reason))

        if matched:
            for edge, _ in matched:
                self.selected.add(edge.id)
            chosen_edge, rationale = matched[0]
            chosen = chosen_edge.id
            alternatives = [e.id for e in control_out if e.id != chosen_edge.id]
        elif default_edge is not None:
            self.selected.add(default_edge.id)
            chosen = "default"
            rationale = "default branch"
            alternatives = [e.id for e in control_out if e.id != default_edge.id]
        else:
            self._fail_node(
                node,
                f"no branch matched (evaluated {len(control_out)} condition(s))",
            )
            self._decision(
                node,
                chosen=None,
                alternatives=[e.id for e in control_out],
                rationale="no branch matched",
            )
            return

        self.ctx.node_status[node.id] = "completed"
        self.ctx.node_results[node.id] = {}
        self.order.append(node.id)
        self._emit("node_finished", {"node_id": node.id, "duration_ms": 0.0})
        self._decision(node, chosen=chosen, alternatives=alternatives, rationale=rationale)

    # ------------------------------------------------------------------ #
    # JOIN nodes
    # ------------------------------------------------------------------ #

    def _track_join_start(self, nid: str) -> None:
        if nid in self.join_start:
            return
        if any(
            self.ctx.node_status.get(e.source, "pending") != "pending"
            for e in self.incoming[nid]
        ):
            self.join_start[nid] = time.monotonic()

    def _join_ready(self, nid: str) -> bool:
        if not self.incoming[nid]:
            return True
        if self._all_incoming_resolved(nid):
            return True
        if (
            self.graph.fan_in_policy is FanInPolicy.TIMEOUT
            and self.graph.timeout_s is not None
            and nid in self.join_start
            and time.monotonic() - self.join_start[nid] >= self.graph.timeout_s
        ):
            return True
        return False

    def _join_wait_timeout(self) -> float | None:
        if self.graph.fan_in_policy is not FanInPolicy.TIMEOUT:
            return None
        if self.graph.timeout_s is None:
            return None
        remaining: list[float] = []
        for nid, node in self.nodes.items():
            if node.kind is not NodeKind.JOIN:
                continue
            if self.ctx.node_status.get(nid, "pending") != "pending":
                continue
            if nid not in self.join_start:
                continue
            remaining.append(self.graph.timeout_s - (time.monotonic() - self.join_start[nid]))
        if not remaining:
            return None
        return max(0.0, min(remaining))

    async def _run_join(self, node: GraphNode) -> None:
        if not self._charge_step():
            return
        edges = self.incoming[node.id]
        arrived = [e for e in edges if self._edge_state(e) == "satisfied"]
        total = len(edges)
        policy = self.graph.fan_in_policy or FanInPolicy.REQUIRE_ALL

        # The JOIN consumes branch failures: once it has applied its policy the
        # failed branches are no longer surfaced as the run error (the JOIN's own
        # verdict is), and a successful join lets the run continue.
        for edge in edges:
            if self.ctx.node_status.get(edge.source) == "failed":
                self.absorbed.add(edge.source)

        if policy is FanInPolicy.REQUIRE_ALL:
            ok = len(arrived) == total
            reason = f"require_all: {len(arrived)}/{total} branches succeeded"
        elif policy is FanInPolicy.ALLOW_PARTIAL:
            ok = len(arrived) >= 1
            reason = f"allow_partial: {len(arrived)}/{total} branches succeeded"
        elif policy is FanInPolicy.MIN_SUCCESS:
            need = self.graph.min_success if self.graph.min_success is not None else 1
            ok = len(arrived) >= need
            reason = f"min_success: {len(arrived)}/{need} required successes"
        else:  # TIMEOUT - best-effort: proceed with whatever arrived.
            ok = len(arrived) >= 1
            reason = (
                f"timeout: proceeding with {len(arrived)}/{total} arrived branch(es) "
                "(best-effort)"
            )

        if not ok:
            self._emit("node_started", {"node_id": node.id})
            self._fail_node(node, f"fan-in policy {policy.value} not satisfied ({reason})")
            return

        if policy is FanInPolicy.TIMEOUT:
            self._abandon_running_branches(edges)

        inputs = self._gather_join_inputs(arrived)
        await self._invoke(node, inputs)

    def _gather_join_inputs(self, arrived: list[GraphEdge]) -> dict[str, Any]:
        inputs: dict[str, Any] = {}
        for edge in arrived:
            value = self.ctx.node_results.get(edge.source, {}).get(edge.source_port)
            if edge.target_port in inputs:
                existing = inputs[edge.target_port]
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    inputs[edge.target_port] = [existing, value]
            else:
                inputs[edge.target_port] = value
        return inputs

    def _abandon_running_branches(self, edges: list[GraphEdge]) -> None:
        for edge in edges:
            source = edge.source
            if self.ctx.node_status.get(source) != "running":
                continue
            self.ctx.node_status[source] = "skipped"
            for task, nid in list(self.running.items()):
                if nid == source:
                    # Cancel but keep the task in ``running`` so the scheduler
                    # still awaits it (no orphaned-task warnings).
                    task.cancel()

    # ------------------------------------------------------------------ #
    # Node invocation
    # ------------------------------------------------------------------ #

    async def _invoke(self, node: GraphNode, inputs: dict[str, Any]) -> None:
        self._emit("node_started", {"node_id": node.id})
        self.ctx.node_status[node.id] = "running"
        started = time.perf_counter()
        try:
            if node.kind is NodeKind.SUBWORKFLOW:
                outputs = await self._execute_subworkflow(node, inputs)
            else:
                call = self.runtime.run_node(node, inputs, self.ctx)
                if self.node_timeout_s is not None:
                    outputs = await asyncio.wait_for(call, timeout=self.node_timeout_s)
                else:
                    outputs = await call
        except PauseExecution as pause:
            self.ctx.node_status[node.id] = "paused"
            self.pending_approval = pause.approval
            self.paused = True
            return
        except asyncio.TimeoutError:
            self._fail_node(node, f"node timed out after {self.node_timeout_s}s")
            return
        except NodeExecutionError as exc:
            self._fail_node(node, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - node failures are data
            self._fail_node(node, f"{type(exc).__name__}: {exc}")
            return

        result = dict(outputs or {})
        if node.kind is NodeKind.OUTPUT and not result:
            result = dict(inputs)
        self.ctx.node_results[node.id] = result
        self.ctx.node_status[node.id] = "completed"
        self.order.append(node.id)
        self._emit(
            "node_finished",
            {"node_id": node.id, "duration_ms": round((time.perf_counter() - started) * 1000, 3)},
        )
        self._select_non_error(node.id)

    def _fail_node(self, node: GraphNode, message: str) -> None:
        self.ctx.node_status[node.id] = "failed"
        self.ctx.node_results.setdefault(node.id, {})
        self.order.append(node.id)
        self.node_errors[node.id] = message
        self._emit("node_failed", {"node_id": node.id, "error": message})
        error_edges = [e for e in self.outgoing[node.id] if e.kind is EdgeKind.ERROR]
        if error_edges:
            for edge in error_edges:
                self.selected.add(edge.id)
            self.absorbed.add(node.id)

    # ------------------------------------------------------------------ #
    # SUBWORKFLOW nodes
    # ------------------------------------------------------------------ #

    async def _execute_subworkflow(
        self, node: GraphNode, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        ref = node.config.get("workflow_ref")
        if not isinstance(ref, str) or not ref:
            raise NodeExecutionError(
                f"subworkflow node {node.id!r} is missing config['workflow_ref']"
            )
        if self.ctx.subworkflow_resolver is None:
            raise NodeExecutionError(
                f"subworkflow node {node.id!r}: no subworkflow_resolver configured"
            )
        try:
            nested = self.ctx.subworkflow_resolver(ref)
        except Exception as exc:  # noqa: BLE001 - resolver errors fail the node
            raise NodeExecutionError(f"subworkflow resolver failed for {ref!r}: {exc}") from exc

        child_depth = self.depth + 1
        if child_depth > self.max_subworkflow_depth:
            raise NodeExecutionError(
                f"subworkflow depth exceeded: {child_depth} > max "
                f"{self.max_subworkflow_depth} (recursion guard, section 21)"
            )

        child_metadata = dict(self.ctx.metadata)
        child_metadata[_DEPTH_KEY] = child_depth
        child = await self.executor.execute(
            nested,
            inputs,
            execution_id=f"{self.ctx.execution_id}/{node.id}",
            metadata=child_metadata,
            approval_gate=self.ctx.approval_gate,
            subworkflow_resolver=self.ctx.subworkflow_resolver,
            event_sink=self.ctx.event_sink,
            decision_sink=self.ctx.decision_sink,
        )
        if child.status == "paused":
            if child.pending_approval is not None:
                raise PauseExecution(child.pending_approval)
            raise NodeExecutionError(f"subworkflow {ref!r} paused without an approval")
        if child.status != "completed":
            raise NodeExecutionError(f"subworkflow {ref!r} {child.status}: {child.error}")

        values = list(child.outputs.values())
        ports = [p.name for p in node.outputs]
        if ports and len(ports) == len(values):
            return dict(zip(ports, values))
        if len(ports) == 1:
            return {ports[0]: child.outputs}
        result: dict[str, Any] = {"out": child.outputs}
        for port in ports:
            result.setdefault(port, child.outputs)
        return result

    # ------------------------------------------------------------------ #
    # Result / sinks / budget
    # ------------------------------------------------------------------ #

    def _charge_step(self) -> bool:
        self.steps += 1
        if self.steps > self.max_steps:
            self.fatal_error = "step budget exhausted"
            return False
        return True

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        sink = self.ctx.event_sink
        if sink is None:
            return
        try:
            sink(kind, payload)
        except Exception:  # noqa: BLE001 - a broken sink must not abort a run
            pass

    def _decision(
        self,
        node: GraphNode,
        *,
        chosen: str | None,
        alternatives: list[str],
        rationale: str,
    ) -> None:
        sink = self.ctx.decision_sink
        if sink is None:
            return
        record = {
            "node_id": node.id,
            "decision_type": "condition",
            "chosen": chosen,
            "alternatives": list(alternatives),
            "rationale": rationale,
        }
        try:
            sink(record)
        except Exception:  # noqa: BLE001 - a broken sink must not abort a run
            pass

    def _validation_failure(self, errors: list[ValidationIssue]) -> ExecutionResult:
        codes = ", ".join(issue.code for issue in errors)
        message = f"graph validation failed: {codes}"
        self.fatal_error = message
        self._init_status()
        result = self._result(emit=False)
        self._emit("execution_failed", {"execution_id": self.ctx.execution_id, "error": message})
        return result

    def _result(self, *, emit: bool = True) -> ExecutionResult:
        if self.paused:
            status = "paused"
        else:
            unabsorbed = [
                nid
                for nid, st in self.ctx.node_status.items()
                if st == "failed" and nid not in self.absorbed
            ]
            if self.fatal_error is None and unabsorbed:
                self.fatal_error = self.node_errors.get(unabsorbed[0], "node failed")
            status = "failed" if self.fatal_error else "completed"

        outputs: dict[str, Any] = {}
        for node in self.graph.nodes:
            if node.kind is NodeKind.OUTPUT and self.ctx.node_status.get(node.id) == "completed":
                outputs[node.id] = self.ctx.node_results.get(node.id, {})

        if emit:
            kind = {
                "paused": "execution_paused",
                "failed": "execution_failed",
                "completed": "execution_completed",
            }[status]
            self._emit(
                kind,
                {
                    "execution_id": self.ctx.execution_id,
                    "status": status,
                    "error": self.fatal_error,
                },
            )

        return ExecutionResult(
            status=status,
            outputs=outputs,
            node_results=dict(self.ctx.node_results),
            node_status=dict(self.ctx.node_status),
            error=self.fatal_error,
            pending_approval=self.pending_approval,
            order=list(self.order),
        )
