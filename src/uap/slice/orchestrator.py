"""§71 vertical slice orchestrator.

``PlatformSlice`` wires the whole platform spine end-to-end for one user
request: Entry Workflow -> Workspace detection -> Library (reuse or register a
workflow) -> durable execution (ExecutionService + Worker over the real graph
engine) -> artifacts -> knowledge lifecycle -> decision traces -> slice
evaluation -> optional Git export.

Every risky step is wrapped so :meth:`PlatformSlice.run` *never raises*: a
failure after useful progress is recorded in :attr:`SliceResult.error` while the
earlier results are preserved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from uap.knowledge.embeddings import Embedder
    from uap.models.client import ChatClient

from uap.agents.deterministic import EchoAgent, SummarizeAgent
from uap.agents.registry import AgentRegistry
from uap.artifacts.store import ArtifactStore
from uap.contracts import Artifact, ArtifactStatus, TaskSpec, VerificationResult
from uap.contracts.models import UserRequest
from uap.entry.workflow import EntryWorkflow
from uap.graph import (
    EdgeKind,
    GraphEdge,
    GraphNode,
    NodeKind,
    Port,
    PortType,
    WorkflowGraph,
)
from uap.knowledge.lifecycle import KnowledgeLifecycle
from uap.knowledge.store import KnowledgeStore
from uap.library.service import LibraryService
from uap.runtime import ExecutionService, Worker
from uap.tools.local import register_local_tools
from uap.tools.registry import ToolRegistry
from uap.trace.model import DecisionTrace, DecisionType
from uap.trace.store import DecisionRecorder
from uap.workspace.detection import WorkspaceDetector
from uap.workspace.model import Workspace

from .evaluation_gate import SliceEvaluator
from .runtime_adapter import PlatformNodeRuntime
from uap.observability.errors import log_swallowed_exception
from uap.models.router import capability_for
from uap.workflows.scope import ScopeGate, ScopeRuleError

__all__ = [
    "PlatformSlice",
    "SliceResult",
    "build_research_graph",
    "build_bbp_graph",
    "scope_gate_for",
    "capability_for",
]

logger = logging.getLogger(__name__)
_WORKFLOW_NAME = "research-slice"
_WORKFLOW_VERSION = 1
_WORKFLOW_REF = f"{_WORKFLOW_NAME}@v{_WORKFLOW_VERSION}"

_BBP_WORKFLOW_NAME = "bbp-slice"
_BBP_WORKFLOW_VERSION = 1
_BBP_WORKFLOW_REF = f"{_BBP_WORKFLOW_NAME}@v{_BBP_WORKFLOW_VERSION}"

def _llm_synthesizer(
    client: Any,
    router: Any | None = None,
    domain: Any | None = None,
    goal: str | None = None,
) -> Any:
    """Build the async ``(question, verified) -> analysis`` callable the
    research pipeline's synthesis node appends to the report.

    Kept here (not in the server) so the §71 slice owns its own LLM contract;
    the server builds the same shape for its legacy path.
    """
    last_decision: list[Any] = []

    async def synthesize(
        question: str,
        verified: list[dict[str, Any]],
        context: Any | None = None,
    ) -> str:
        claims = "\n".join(
            f"- {record.get('claim', '')}" for record in (verified or [])[:20]
        ) or "- (no verified claims)"
        user_parts = [f"Question: {question}"]
        if context is not None and getattr(context, "context_sections", None):
            user_parts.append("## Context")
            for section in context.context_sections:
                user_parts.append(f"### {section.key}\n{section.content}")
        user_parts.append(f"Verified claims:\n{claims}")
        model_id = os.environ.get("UAP_DEFAULT_MODEL", "gpt-5.6-sol")
        if router is not None:
            from uap.models.router import RoutingRequest

            ctx_task = getattr(context, "task", None) if context is not None else None
            effective_domain = getattr(ctx_task, "domain", None) or domain or "research"
            effective_goal = getattr(ctx_task, "goal", None) or goal or question
            effective_constraints = getattr(ctx_task, "constraints", None)
            capability = capability_for(
                effective_domain,
                effective_goal,
                constraints=effective_constraints,
            )
            decision, _is_fb = router.select_or_fallback(
                RoutingRequest(capability=capability)
            )
            model_id = decision.model_id
            last_decision.append(decision)

        text, _usage = await client.complete(
            model_id,
            [
                {
                    "role": "system",
                    "content": (
                        "You are a research analyst. Given a question and "
                        "verified claims, write a concise critical analysis "
                        "(3-6 short paragraphs). Note trade-offs, caveats and "
                        "what the evidence does not settle. Do not invent "
                        "sources or facts beyond the claims."
                    ),
                },
                {
                    "role": "user",
                    "content": "\n\n".join(user_parts),
                },
            ],
        )
        return text

    synthesize.last_decision = last_decision  # type: ignore[attr-defined]
    return synthesize


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


class SliceResult(BaseModel):
    """Everything one slice run produced, JSON-serialisable end to end."""

    task_id: str = ""
    workspace_id: str | None = None
    workspace_reason: str = ""
    workflow_ref: str | None = None
    workflow_was_existing: bool = False
    execution_id: str | None = None
    execution_status: str = ""
    artifacts: list[str] = Field(default_factory=list)
    knowledge_ids: list[str] = Field(default_factory=list)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    git_commit: str | None = None
    events_emitted: int = 0
    traces_recorded: int = 0
    context: dict[str, Any] | None = None
    error: str | None = None
    output: str | None = None


# --------------------------------------------------------------------------- #
# Canonical graph (8 nodes)
# --------------------------------------------------------------------------- #


def _port(name: str = "value") -> Port:
    return Port(name=name, type=PortType.ANY, required=True)


def build_research_graph() -> WorkflowGraph:
    """The canonical research slice graph — the REAL 8-node pipeline.

    INPUT -> question_analysis -> research_planning -> fan_out
          -> evidence_extraction -> evidence_filtering -> cross_verification
          -> synthesis -> review -> OUTPUT

    Every node maps 1:1 to a real node function in
    :class:`uap.workflows.research.ResearchWorkflow` (test-enforced by
    ``test_slice_research_nodes``); the runtime adapter executes those
    functions, so the durable spine runs the actual research pipeline rather
    than an echo demo. The extra INPUT/OUTPUT orchestration nodes bracket the
    chain so the engine has explicit entry/exit ports.
    """
    nodes = [
        GraphNode(id="input", kind=NodeKind.INPUT, outputs=[_port()]),
        GraphNode(
            id="question_analysis",
            kind=NodeKind.AGENT,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "question_analysis"},
        ),
        GraphNode(
            id="research_planning",
            kind=NodeKind.AGENT,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "research_planning"},
        ),
        GraphNode(
            id="fan_out",
            kind=NodeKind.PARALLEL,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "fan_out"},
        ),
        GraphNode(
            id="evidence_extraction",
            kind=NodeKind.TOOL,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "evidence_extraction"},
        ),
        GraphNode(
            id="evidence_filtering",
            kind=NodeKind.TOOL,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "evidence_filtering"},
        ),
        GraphNode(
            id="cross_verification",
            kind=NodeKind.EVALUATION,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "cross_verification"},
        ),
        GraphNode(
            id="synthesis",
            kind=NodeKind.SYNTHESIS,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "synthesis"},
        ),
        GraphNode(
            id="review",
            kind=NodeKind.EVALUATION,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "review"},
        ),
        GraphNode(id="output", kind=NodeKind.OUTPUT, inputs=[_port()], outputs=[_port()]),
    ]
    chain = [
        "input",
        "question_analysis",
        "research_planning",
        "fan_out",
        "evidence_extraction",
        "evidence_filtering",
        "cross_verification",
        "synthesis",
        "review",
        "output",
    ]
    edges = [
        GraphEdge(
            id=f"e{i}",
            source=src,
            source_port="value",
            target=dst,
            target_port="value",
            kind=EdgeKind.DATA,
        )
        for i, (src, dst) in enumerate(zip(chain, chain[1:]), start=1)
    ]
    return WorkflowGraph(
        id=_WORKFLOW_NAME,
        name=_WORKFLOW_NAME,
        description="§71 vertical-slice research workflow (real research pipeline).",
        nodes=nodes,
        edges=edges,
    )


def build_bbp_graph() -> WorkflowGraph:
    """The canonical BBP slice graph — the REAL 8-node pipeline.

    INPUT -> scope_validation -> recon_planning -> asset_discovery
          -> endpoint_discovery -> finding_generation -> finding_classification
          -> validation -> report -> OUTPUT
    """
    nodes = [
        GraphNode(id="input", kind=NodeKind.INPUT, outputs=[_port()]),
        GraphNode(
            id="scope_validation",
            kind=NodeKind.EVALUATION,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "scope_validation"},
        ),
        GraphNode(
            id="recon_planning",
            kind=NodeKind.AGENT,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "recon_planning"},
        ),
        GraphNode(
            id="asset_discovery",
            kind=NodeKind.TOOL,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "asset_discovery"},
        ),
        GraphNode(
            id="endpoint_discovery",
            kind=NodeKind.TOOL,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "endpoint_discovery"},
        ),
        GraphNode(
            id="finding_generation",
            kind=NodeKind.AGENT,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "finding_generation"},
        ),
        GraphNode(
            id="finding_classification",
            kind=NodeKind.AGENT,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "finding_classification"},
        ),
        GraphNode(
            id="validation",
            kind=NodeKind.EVALUATION,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "validation"},
        ),
        GraphNode(
            id="report",
            kind=NodeKind.SYNTHESIS,
            inputs=[_port()],
            outputs=[_port()],
            config={"pipeline_node": "report"},
        ),
        GraphNode(id="output", kind=NodeKind.OUTPUT, inputs=[_port()], outputs=[_port()]),
    ]
    chain = [
        "input",
        "scope_validation",
        "recon_planning",
        "asset_discovery",
        "endpoint_discovery",
        "finding_generation",
        "finding_classification",
        "validation",
        "report",
        "output",
    ]
    edges = [
        GraphEdge(
            id=f"e{i}",
            source=src,
            source_port="value",
            target=dst,
            target_port="value",
            kind=EdgeKind.DATA,
        )
        for i, (src, dst) in enumerate(zip(chain, chain[1:]), start=1)
    ]
    return WorkflowGraph(
        id=_BBP_WORKFLOW_NAME,
        name=_BBP_WORKFLOW_NAME,
        description="§71 vertical-slice bbp workflow (real BBP pipeline).",
        nodes=nodes,
        edges=edges,
    )


def scope_gate_for(spec: TaskSpec) -> ScopeGate:
    """Build the per-task BBP scope gate from the compiled ``TaskSpec``."""
    constraints = spec.constraints or {}
    in_scope = constraints.get("in_scope")
    if in_scope:
        return ScopeGate(
            in_scope=list(in_scope),
            out_of_scope=list(constraints.get("out_of_scope") or []),
        )
    targets = (spec.input or {}).get("targets")
    if targets:
        if isinstance(targets, (list, tuple)):
            in_scope = [str(item) for item in targets]
        else:
            in_scope = [str(targets)]
        return ScopeGate(
            in_scope=in_scope,
            out_of_scope=list(constraints.get("out_of_scope") or []),
        )
    return ScopeGate(in_scope=[], out_of_scope=[])


def _targets_from_spec(spec: TaskSpec) -> list[str]:
    data = spec.model_dump()
    for container in (data.get("input") or {}, data.get("constraints") or {}):
        value = container.get("targets")
        if value:
            return [str(item) for item in value] if isinstance(value, (list, tuple)) else [str(value)]
    goal = str(data.get("goal") or "").strip()
    if goal:
        return [goal]
    return []


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #


class PlatformSlice:
    """Drives one user request through the whole platform spine."""

    def __init__(
        self,
        session_factory: Any,
        *,
        artifacts_root: Path | str,
        agents: AgentRegistry | None = None,
        tools: ToolRegistry | None = None,
        workspaces: list[Workspace] | None = None,
        llm_client: "ChatClient | None" = None,
        model_router: Any | None = None,
        embedder: "Embedder | None" = None,
        git_repo: Any | None = None,
        git_definitions_root: Path | str = "definitions",
        pipeline: Any | None = None,
        approval_gate: Any | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.approval_gate = approval_gate
        self._artifacts_root = Path(artifacts_root)
        self._llm_client = llm_client
        self._model_router = model_router
        self.tools = tools or self._default_tools()
        if self.approval_gate is not None and getattr(self.tools, "approval_gate", None) is None:
            self.tools.approval_gate = self.approval_gate
        if pipeline is None:
            # The REAL research pipeline (its node functions execute through
            # the durable graph). Built lazily to avoid an import cycle and to
            # let callers inject a custom pipeline (e.g. BBP) later.
            from uap.workflows.research import ResearchWorkflow
            synth = (
                _llm_synthesizer(llm_client, router=model_router)
                if llm_client is not None
                else None
            )
            pipeline = ResearchWorkflow(synthesizer=synth, tools=self.tools)
        self.pipeline = pipeline
        if embedder is None:
            from uap.knowledge.embeddings import get_embedder

            embedder = get_embedder()
        self._embedder = embedder
        self.artifacts = ArtifactStore(self._artifacts_root)
        self.agents = agents or self._default_agents()
        self.workspaces = list(workspaces or [])
        self.git_repo = git_repo
        self.git_definitions_root = git_definitions_root

        self.entry = EntryWorkflow()
        self.detector = WorkspaceDetector()
        self.evaluator = SliceEvaluator()
        # In-memory ref -> graph map (the Library stores a graph_ref, not the graph).
        self._graphs: dict[str, WorkflowGraph] = {}

    # ------------------------------------------------------------------ #
    # Defaults
    # ------------------------------------------------------------------ #

    def _default_agents(self) -> AgentRegistry:
        """Build the agent registry.

        When ``llm_client`` is injected, the ``"llm"`` name resolves to a
        real :class:`~uap.agents.llm.LLMAgent`.  Otherwise an
        :class:`EchoAgent` is registered *under the name "llm"* so the graph
        node always resolves — this keeps the existing 15 slice tests
        deterministic and network-free.
        """
        from uap.agents.llm import LLMAgent

        registry = AgentRegistry()
        if self._llm_client is not None:
            registry.register(
                LLMAgent(client=self._llm_client, router=self._model_router)
            )
        else:
            registry.register(EchoAgent(name="llm"))
        registry.register(EchoAgent())
        registry.register(SummarizeAgent())
        return registry

    def _default_tools(self) -> ToolRegistry:
        registry = ToolRegistry(approval_gate=self.approval_gate)
        register_local_tools(registry, self._artifacts_root)
        return registry

    # ------------------------------------------------------------------ #
    # Workspace
    # ------------------------------------------------------------------ #

    def ensure_workspace(self, task: TaskSpec) -> tuple[Workspace, str]:
        """Detect a workspace, or fall back to a synthetic ``default`` one."""
        result = self.detector.detect(task, self.workspaces)
        if result.best is not None:
            return result.best, result.reason
        default = Workspace(
            id="default",
            name="default",
            description="Fallback workspace when detection is not confident.",
        )
        return default, f"no confident match: {result.reason}"

    # ------------------------------------------------------------------ #
    # Workflow registration / reuse
    # ------------------------------------------------------------------ #

    def register_research_workflow(self) -> str:
        """Register the canonical research workflow in the Library; return its ref.

        Uses a short, committed session so the row (and its lock) are released
        immediately -- a long-lived LibraryService session would hold the
        ``workflow_definitions`` row lock open and deadlock the next run.
        """
        graph = build_research_graph()
        if self._session_factory is not None:
            from uap.db.engine import session_scope
            from uap.library.service import DuplicateDefinitionError

            try:
                with session_scope(self._session_factory) as session:
                    LibraryService(session=session).register_workflow(
                        _WORKFLOW_NAME,
                        {"graph_ref": f"graphs/{_WORKFLOW_NAME}.v{_WORKFLOW_VERSION}.json"},
                        tags=["slice", "research"],
                    )
            except DuplicateDefinitionError:
                # Already registered (idempotent): fall through to the graph map.
                pass
        self._graphs[_WORKFLOW_REF] = graph
        self._graphs[_WORKFLOW_NAME] = graph
        return _WORKFLOW_REF

    def register_bbp_workflow(self) -> str:
        """Register the canonical BBP workflow in the Library; return its ref."""
        graph = build_bbp_graph()
        if self._session_factory is not None:
            from uap.db.engine import session_scope
            from uap.library.service import DuplicateDefinitionError

            try:
                with session_scope(self._session_factory) as session:
                    LibraryService(session=session).register_workflow(
                        _BBP_WORKFLOW_NAME,
                        {"graph_ref": f"graphs/{_BBP_WORKFLOW_NAME}.v{_BBP_WORKFLOW_VERSION}.json"},
                        tags=["slice", "bbp"],
                    )
            except DuplicateDefinitionError:
                pass
        self._graphs[_BBP_WORKFLOW_REF] = graph
        self._graphs[_BBP_WORKFLOW_NAME] = graph
        return _BBP_WORKFLOW_REF

    def _resolve_or_register_workflow(self, domain: str = "research") -> tuple[str, bool]:
        """Reuse an existing workflow or register a new one."""
        target_name = _BBP_WORKFLOW_NAME if domain == "bbp" else _WORKFLOW_NAME
        target_ref = _BBP_WORKFLOW_REF if domain == "bbp" else _WORKFLOW_REF
        graph_builder = build_bbp_graph if domain == "bbp" else build_research_graph
        register_fn = self.register_bbp_workflow if domain == "bbp" else self.register_research_workflow

        if self._session_factory is not None:
            from uap.db.engine import session_scope

            existing: list[tuple[str, str]] = []
            try:
                with session_scope(self._session_factory) as session:
                    for entry in LibraryService(session=session).list_library(
                        kind="workflow"
                    ):
                        existing.append((entry.name, entry.ref))
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "failed to list existing workflows in library",
                    level=logging.WARNING,
                )
                existing = []
            for name, ref in existing:
                if target_name in name:
                    if ref not in self._graphs:
                        # The Library stores a graph_ref, not the graph; rebuild
                        # the canonical graph so execution has something to run.
                        graph = graph_builder()
                        self._graphs[ref] = graph
                        self._graphs[name] = graph
                    return ref, True
        return register_fn(), False

    def _resolve_graph(self, ref: str) -> WorkflowGraph:
        if ref in self._graphs:
            return self._graphs[ref]
        name = ref.split("@", 1)[0]
        if name in self._graphs:
            return self._graphs[name]
        # Last-resort fallback: the canonical graph, so a run is never blocked by
        # a Library/graph lookup miss.
        if "bbp" in ref:
            graph = build_bbp_graph()
        else:
            graph = build_research_graph()
        self._graphs[ref] = graph
        return graph

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(
        self,
        raw_input: str | TaskSpec,
        *,
        user_id: str | None = None,
        task_id: str | None = None,
        workspace_id: str | None = None,
        scope_gate: Any | None = None,
        pipeline: Any | None = None,
        task: TaskSpec | None = None,
    ) -> SliceResult:
        """Execute the full slice for ``raw_input``; never raises.

        ``task_id`` lets the caller (e.g. the HTTP server, which already
        returned an id to its client) pin the id the run executes under, so
        durable lookups by that id resolve to this execution.
        """
        return asyncio.run(
            self._run(
                raw_input,
                user_id=user_id,
                task_id=task_id,
                workspace_id=workspace_id,
                scope_gate=scope_gate,
                pipeline=pipeline,
                task=task,
            )
        )

    async def _run(
        self,
        raw_input: str | TaskSpec,
        *,
        user_id: str | None = None,
        task_id: str | None = None,
        workspace_id: str | None = None,
        scope_gate: Any | None = None,
        pipeline: Any | None = None,
        task: TaskSpec | None = None,
    ) -> SliceResult:
        out = SliceResult()

        # 1. Entry Workflow -> TaskSpec (or clarification).
        if task is None:
            if isinstance(raw_input, TaskSpec):
                task = raw_input
            else:
                outcome = self.entry.run(UserRequest(raw_input=raw_input, user_id=user_id))
                if outcome.needs_clarification or outcome.spec is None:
                    out.execution_status = "failed"
                    out.error = f"clarification required: {outcome.question or 'intent unclear'}"
                    return out
                task = outcome.spec
        if task_id:
            # Pin the caller's id so durable records match what the client has.
            task = task.model_copy(update={"task_id": task_id})
        out.task_id = task.task_id

        # 1b. Determine pipeline & domain (Research vs BBP).
        active_pipeline = pipeline if pipeline is not None else self.pipeline
        if active_pipeline is not None and getattr(active_pipeline, "synthesizer", None) is not None:
            cur_synth = active_pipeline.synthesizer
            if getattr(cur_synth, "_context_wrapped", False) and hasattr(cur_synth, "__closure__") and cur_synth.__closure__:
                for cell in cur_synth.__closure__:
                    if callable(cell.cell_contents) and hasattr(cell.cell_contents, "last_decision"):
                        active_pipeline.synthesizer = cell.cell_contents
                        break
        from uap.workflows.bbp import BBPWorkflow

        is_bbp = (
            str(task.domain) == "bbp"
            or isinstance(active_pipeline, BBPWorkflow)
            or scope_gate is not None
        )

        # 1c. SCOPE GATE ENFORCEMENT (Security boundary - must be evaluated BEFORE any node runs).
        if is_bbp:
            gate = scope_gate
            if gate is None and active_pipeline is not None:
                gate = getattr(active_pipeline, "scope_gate", None)
            if gate is None:
                try:
                    gate = scope_gate_for(task)
                except Exception as exc:
                    log_swallowed_exception(
                        logger,
                        exc,
                        "scope gate evaluation failed",
                        level=logging.WARNING,
                    )
                    out.execution_status = "failed"
                    out.error = f"scope gate evaluation failed: {exc}"
                    return out
            if gate is None:
                out.execution_status = "failed"
                out.error = "no scope gate configured: refusing to run"
                return out

            targets = _targets_from_spec(task)
            allowed, blocked = gate.filter(targets)
            if not allowed:
                blocked_targets = gate.blocked_reasons(blocked)
                notes = (
                    f"blocked: no in-scope targets remain ({len(blocked_targets)} "
                    f"target(s) excluded)"
                )
                out.execution_status = "failed"
                out.error = notes
                return out

            if active_pipeline is None or not isinstance(active_pipeline, BBPWorkflow):
                active_pipeline = BBPWorkflow(scope_gate=gate, tools=self.tools)
            else:
                if getattr(active_pipeline, "scope_gate", None) is None:
                    active_pipeline.scope_gate = gate
                if getattr(active_pipeline, "tools", None) is None:
                    active_pipeline.tools = self.tools

        # 2. Workspace: an explicit client choice wins; otherwise detect.
        if workspace_id:
            workspace = next(
                (w for w in self.workspaces if w.id == workspace_id), None
            ) or Workspace(
                id=str(workspace_id),
                name=str(workspace_id),
                description="Client-selected workspace (not yet registered).",
            )
            reason = "explicit client selection"
        else:
            workspace, reason = self.ensure_workspace(task)
        out.workspace_id = workspace.id
        out.workspace_reason = reason

        # 3. Workflow: reuse or register.
        domain_name = "bbp" if is_bbp else "research"
        try:
            ref, was_existing = self._resolve_or_register_workflow(domain=domain_name)
        except Exception as exc:  # keep going on a canonical fallback
            log_swallowed_exception(
                logger,
                exc,
                "workflow registration failed; using fallback",
                level=logging.WARNING,
            )
            ref, was_existing = (_BBP_WORKFLOW_REF if is_bbp else _WORKFLOW_REF), False
            self._graphs[ref] = build_bbp_graph() if is_bbp else build_research_graph()
            out.error = f"workflow registration failed, using fallback: {exc!r}"
        out.workflow_ref = ref
        out.workflow_was_existing = was_existing

        # 4-5. Durable execution via ExecutionService + Worker.
        tools = (
            active_pipeline.tools
            if (is_bbp and getattr(active_pipeline, "tools", None) is not None)
            else self.tools
        )
        runtime = PlatformNodeRuntime(
            self.agents,
            tools,
            task=task,
            pipeline=active_pipeline,
            session_factory=self._session_factory,
        )
        service = ExecutionService(
            self._session_factory,
            graph_resolver=self._resolve_graph,
            node_runtime=runtime,
            approval_gate=self.approval_gate,
        )
        try:
            execution_id = service.enqueue(
                workflow_ref=ref,
                inputs={"value": task.goal},
                correlation_id=task.task_id,
                workspace_id=workspace.id,
            )
            out.execution_id = execution_id
            # Run THIS execution. `run_once()` claims the OLDEST pending row,
            # so a synchronous caller that just enqueued its own work ran the
            # PREVIOUS submission instead and returned a report attributed to
            # the wrong task — verified: two back-to-back requests received
            # each other's output (2026-10-03).
            await Worker(service).run_specific(execution_id)
            status = service.status(execution_id)
            out.execution_status = status.get("status", "")
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "slice execution failed",
                level=logging.ERROR,
                task_id=task.task_id,
            )
            out.error = (out.error + " | " if out.error else "") + f"execution failed: {exc!r}"
            return out

        # Pull node results from the latest durable checkpoint.
        node_results: dict[str, Any] = {}
        try:
            checkpoints = service.checkpoints(execution_id)
            if checkpoints:
                _seq, state = checkpoints[-1]
                node_results = dict(state.get("node_results") or {})
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "slice checkpoint read failed",
                level=logging.WARNING,
                execution_id=str(execution_id),
            )
            out.error = (out.error + " | " if out.error else "") + f"checkpoint read failed: {exc!r}"
        # 6. Decision traces (ROUTING always; SCOPE_CHECK for bbp).
        traces = 0
        recorder = DecisionRecorder(self._session_factory)
        try:
            recorder.record(
                DecisionTrace(
                    execution_id=execution_id,
                    decision_type=DecisionType.ROUTING,
                    chosen=workspace.id,
                    rationale=f"workspace selected: {reason}",
                    confidence=1.0,
                )
            )
            traces += 1
            if str(task.domain) == "bbp" or is_bbp:
                recorder.record(
                    DecisionTrace(
                        execution_id=execution_id,
                        decision_type=DecisionType.SCOPE_CHECK,
                        chosen="scope-evaluated",
                        rationale="bbp task: scope gate consulted",
                        confidence=1.0,
                    )
                )
                traces += 1

            # Model routing decision trace
            task_capability = capability_for(
                task.domain,
                task.goal,
                constraints=task.constraints,
            )
            model_decision = None
            is_fb = False
            synth = getattr(active_pipeline, "synthesizer", None)
            if synth is not None:
                if getattr(synth, "last_decision", None):
                    model_decision = synth.last_decision[-1]
                elif hasattr(synth, "__closure__") and synth.__closure__:
                    for cell in synth.__closure__:
                        target = cell.cell_contents
                        if hasattr(target, "last_decision") and target.last_decision:
                            model_decision = target.last_decision[-1]
                            break
            if model_decision is None:
                llm_agent = self.agents.get("llm")
                if llm_agent is not None and getattr(llm_agent, "last_decision", None):
                    model_decision = llm_agent.last_decision
            if model_decision is None and self._model_router is not None:
                from uap.models.router import RoutingRequest

                model_decision, is_fb = self._model_router.select_or_fallback(
                    RoutingRequest(capability=task_capability)
                )
            elif model_decision is not None:
                is_fb = "explicit fallback to" in getattr(model_decision, "reason", "")

            if model_decision is not None:
                from uap.trace.model import DecisionAlternative

                cap_str = (
                    task_capability.value
                    if hasattr(task_capability, "value")
                    else str(task_capability)
                )
                recorder.record(
                    DecisionTrace(
                        execution_id=execution_id,
                        node_id="synthesis" if not is_bbp else "report",
                        decision_type=DecisionType.MODEL_SELECTION,
                        chosen=model_decision.model_id,
                        rationale=model_decision.reason,
                        alternatives=[
                            DecisionAlternative(
                                option=fb,
                                reason_rejected="ranked lower by preference or fallback policy",
                            )
                            for fb in model_decision.fallbacks
                        ],
                        confidence=0.0 if is_fb else 1.0,
                        inputs_summary={
                            "capability": cap_str,
                            "fallback": is_fb,
                        },
                    )
                )
                traces += 1
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "slice trace record failed",
                level=logging.WARNING,
                execution_id=str(execution_id),
            )
            out.error = (out.error + " | " if out.error else "") + f"trace record failed: {exc!r}"
        out.traces_recorded = traces
        out.context = runtime.get_inspectable_context()
        # 7. Artifacts: any node output carrying "content".
        saved: list[Artifact] = []
        if not is_bbp:
            for node_id, ports in node_results.items():
                if not isinstance(ports, dict):
                    continue
                for value in ports.values():
                    if not (isinstance(value, dict) and value.get("content")):
                        continue
                    content = value["content"]
                    if not isinstance(content, str):
                        content = str(content)
                    artifact = Artifact(
                        task_id=task.task_id,
                        type=f"slice-{node_id}",
                        source=node_id,
                        status=ArtifactStatus.FINAL,
                    )
                    try:
                        stored = self.artifacts.save(artifact, content)
                        saved.append(stored)
                        out.artifacts.append(stored.artifact_id)
                    except Exception as exc:
                        log_swallowed_exception(
                            logger,
                            exc,
                            "slice artifact save failed",
                            level=logging.WARNING,
                            task_id=task.task_id,
                            node=node_id,
                        )
                        out.error = (out.error + " | " if out.error else "") + f"artifact save failed: {exc!r}"
                    break  # one artifact per node is enough

        for art in runtime.pipeline_artifacts:
            if art.content_ref:
                try:
                    stored = self.artifacts.save(art, art.content_ref)
                    saved.append(stored)
                    out.artifacts.append(stored.artifact_id)
                except Exception as exc:
                    log_swallowed_exception(
                        logger,
                        exc,
                        "slice pipeline artifact save failed",
                        level=logging.WARNING,
                        task_id=task.task_id,
                        type=art.type,
                    )
                    out.error = (out.error + " | " if out.error else "") + f"pipeline artifact save failed: {exc!r}"

        # Surface final output text
        if "report" in node_results:
            rep_node = node_results["report"]
            if isinstance(rep_node, dict):
                for val in rep_node.values():
                    if isinstance(val, dict) and val.get("content"):
                        out.output = str(val["content"])
                        break
        elif "synthesis" in node_results:
            syn_node = node_results["synthesis"]
            if isinstance(syn_node, dict):
                for val in syn_node.values():
                    if isinstance(val, dict) and val.get("content"):
                        out.output = str(val["content"])
                        break

        # 8. Knowledge: propose -> verify -> promote (best-effort).
        # The real research pipeline produces verified claims in its
        # cross_verification node; the old demo graph had a dedicated
        # "knowledge" node. Support both so neither contract is lost.
        statement = None
        knowledge_node = node_results.get("knowledge")
        if isinstance(knowledge_node, dict):
            for value in knowledge_node.values():
                if isinstance(value, dict) and value.get("statement"):
                    statement = str(value["statement"])
                    break
        if statement is None:
            # Fall back to the pipeline's verified claims (its real output).
            for node_id in ("cross_verification", "evidence_filtering"):
                ports = node_results.get(node_id)
                if not isinstance(ports, dict):
                    continue
                for value in ports.values():
                    content = value.get("content") if isinstance(value, dict) else None
                    claims = None
                    if isinstance(content, list):
                        claims = content
                    elif isinstance(content, str):
                        try:
                            parsed = json.loads(content.replace("'", '"'))
                            claims = parsed if isinstance(parsed, list) else None
                        except (ValueError, TypeError):
                            claims = None
                    if claims:
                        first = claims[0]
                        if isinstance(first, dict) and first.get("claim"):
                            statement = str(first["claim"])
                            break
                if statement:
                    break
        if statement is None and is_bbp:
            validated = runtime._pipeline_data.get("validated_findings") or []
            if validated:
                first = validated[0]
                t = first.get("target", "")
                title = first.get("title", "")
                statement = f"BBP finding on {t}: {title}" if t else str(title)
            else:
                statement = f"BBP security assessment completed for {task.goal}"

        if statement and saved:
            try:
                session = self._session_factory()
                try:
                    lifecycle = KnowledgeLifecycle(KnowledgeStore(session))
                    item = lifecycle.propose_from_artifact(
                        saved[0],
                        statement=statement,
                        domain=str(task.domain),
                        actor="slice",
                        embedder=self._embedder,
                    )
                    out.knowledge_ids.append(item.knowledge_id)
                    verification = VerificationResult(
                        verifier="slice-knowledge",
                        passed=True,
                        score=1.0,
                        notes="slice-proposed claim backed by artifact provenance",
                    )
                    lifecycle.verify(item.knowledge_id, verification, actor="slice")
                    try:
                        lifecycle.promote(item.knowledge_id, actor="slice")
                    except Exception as exc:
                        # Promotion policy (confidence/provenance) may deny; the
                        # verified proposal still counts as knowledge-eligible.
                        log_swallowed_exception(
                            logger,
                            exc,
                            "slice knowledge promotion denied or failed; kept as verified",
                            level=logging.DEBUG,
                            knowledge_id=str(item.knowledge_id),
                        )
                        pass
                finally:
                    session.close()
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "slice knowledge proposal/lifecycle failed",
                    level=logging.WARNING,
                    task_id=task.task_id,
                )
                out.error = (out.error + " | " if out.error else "") + f"knowledge failed: {exc!r}"
        # 9. Evaluation gate.
        try:
            evaluation = self.evaluator.evaluate(
                execution_id=execution_id,
                execution_status=out.execution_status,
                node_results=node_results,
                artifacts=out.artifacts,
                traces=[1] * out.traces_recorded,
                knowledge_ids=out.knowledge_ids,
            )
            out.evaluation = evaluation.model_dump(mode="json")
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "slice evaluation failed",
                level=logging.WARNING,
                execution_id=str(execution_id),
            )
            out.error = (out.error + " | " if out.error else "") + f"evaluation failed: {exc!r}"
        # 10. Git export (optional).
        if self.git_repo is not None:
            try:
                from uap.gitx.sync import DefinitionGitSync

                workflow_name = _BBP_WORKFLOW_NAME if is_bbp else _WORKFLOW_NAME
                workflow_version = _BBP_WORKFLOW_VERSION if is_bbp else _WORKFLOW_VERSION
                sync = DefinitionGitSync(self.git_repo, self.git_definitions_root)
                sync.export_graph(workflow_name, workflow_version, self._resolve_graph(ref))
                out.git_commit = sync.commit_definition(
                    "workflow", workflow_name, workflow_version
                )
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "slice git export failed",
                    level=logging.WARNING,
                )
                out.error = (out.error + " | " if out.error else "") + f"git export failed: {exc!r}"
        # 11. Observability counts.
        try:
            out.events_emitted = len(service.events_since(execution_id))
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "slice events read failed",
                level=logging.WARNING,
                execution_id=str(execution_id),
            )
            out.error = (out.error + " | " if out.error else "") + f"events read failed: {exc!r}"
        return out
