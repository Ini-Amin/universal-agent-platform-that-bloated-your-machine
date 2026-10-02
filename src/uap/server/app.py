"""Minimal SSE-first FastAPI server (Master sections 25 and 28).

This module wires the *existing* platform modules into one HTTP surface:

    POST /tasks        -> EntryWorkflow -> Router -> domain workflow
    GET  /tasks        -> list of tracked runs
    GET  /tasks/{id}   -> full run detail (status / node_history / output / artifacts)
    GET  /events       -> Server-Sent Events stream of ObsEvents
    GET  /             -> the self-contained static UI

It adds no new dependencies: FastAPI, Starlette and asyncio only. The server is
a thin adapter -- it does not implement domain logic (Master section 29 rule 8),
it observes it through the :class:`EventBus` (Master section 22) and reports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from uap.approval import ApprovalError, ApprovalGate
from uap.contracts import (
    Artifact,
    Domain,
    TaskSpec,
    UserRequest,
    WorkflowResult,
    WorkflowStatus,
)
from uap.entry import EntryWorkflow
from uap.observability import EventBus, EventKind, JsonlSink, MemorySink, ObsEvent, Tracer
from uap.observability.sinks import DEFAULT_MAX_EVENTS
from uap.router import Router, WorkflowRegistry
from uap.workflows.bbp import WORKFLOW_NAME as BBP_WORKFLOW_NAME
from uap.workflows.bbp import BBPWorkflow
from uap.workflows.research import WORKFLOW_NAME as RESEARCH_WORKFLOW_NAME
from uap.workflows.research import ResearchWorkflow
from uap.workflows.scope import ScopeGate, ScopeRuleError

from uap.mcp.config import BUG_BOUNTY_MCP_CONFIG, MCPServerConfig
from uap.server.mcp_lifecycle import start_mcp_tools, stop_mcp_tools

from starlette.exceptions import WebSocketException
from starlette.staticfiles import StaticFiles

from uap.server.auth import (
    close_unauthorized_websocket,
    require_token,
    scrub_token_from_logs,
)
from uap.server.ws import build_ws_router
from uap.server.run_control import drop_control, get_control, run_controls

__all__ = ["create_app", "EventStream"]

from uap.observability.errors import log_swallowed_exception, redact_text

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"
_UI_DIR = Path(__file__).resolve().parents[3] / "ui"
#: Domains the server can actually execute. Everything else is a clarification.
_EXECUTABLE = frozenset({Domain.RESEARCH, Domain.BBP})

#: Example of the executable requests, shown when a domain cannot be run yet.
_AVAILABLE_DOMAINS_EXAMPLE = (
    'Try a research request ("research ...") or a bug bounty request '
    '("bug bounty on example.com").'
)

DEFAULT_RUNS_DIR = Path("./data/runs")
DEFAULT_HEARTBEAT_SECONDS = 15.0
#: How many buffered events the SSE stream replays to a new subscriber.
#: Older history stays in ``MemorySink``/``JsonlSink``; the stream announces
#: the cap in its opening ``meta`` frame instead of pretending replay is
#: complete.
DEFAULT_MAX_REPLAY = 1_000
#: Retention cap for the in-memory run registry (FIFO by creation time).
#: Evicted runs return 404 honestly rather than serving stale entries.
DEFAULT_MAX_RUNS = 500

# --------------------------------------------------------------------------- #
# Request / response bodies
# --------------------------------------------------------------------------- #

class TaskRequest(BaseModel):
    """Body of ``POST /tasks``."""

    input: str = ""
    user_id: str | None = None
    workspace_id: str | None = None

class ProposalRequest(BaseModel):
    """Body of ``POST /api/proposals``."""

    input: str
    workspace_id: str | None = None

class WorkspaceCreateRequest(BaseModel):
    """Body of ``POST /api/workspaces``."""

    name: str
    description: str | None = None

class WorkspaceFromTemplateRequest(BaseModel):
    """Body of ``POST /api/workspaces/from-template``.

    ``template`` is a catalog template name; ``input`` is the user's request in
    the shape the template documents (see ``GET /api/templates/{name}``).
    """

    template: str
    input: str = ""
    user_id: str | None = None

class DecisionRequest(BaseModel):
    """Body of ``POST /approvals/{approval_id}/decide``."""

    approved: bool
    decided_by: str

class PruneRequest(BaseModel):
    """Body of ``POST /api/maintenance/prune``.

    Retention is strictly opt-in: this endpoint does nothing unless an
    operator explicitly calls it. There is no scheduled or default pruning.
    """

    older_than_days: int
    limit: int | None = None

# --------------------------------------------------------------------------- #
# Run bookkeeping
# --------------------------------------------------------------------------- #

@dataclass
class RunRecord:
    """In-memory state for one accepted task (the RUNS dict value)."""

    task_id: str
    domain: str
    workflow: str
    status: str = "accepted"
    input: str = ""
    question: str | None = None
    output: str = ""
    error: str | None = None
    node_history: list[str] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    workspace_id: str | None = None
    evidence_source: str = "deterministic-stubs"
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.monotonic)
    requested_by: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "domain": self.domain,
            "workflow": self.workflow,
            "status": self.status,
            "requested_by": self.requested_by,
        }

    def detail(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "input": self.input,
            "node_history": list(self.node_history),
            "output": self.output,
            "artifacts": [dict(item) for item in self.artifacts],
            "error": self.error,
            "workspace_id": self.workspace_id,
            "evidence_source": self.evidence_source,
            "pending_approvals": [dict(item) for item in self.pending_approvals],
        }

class BoundedRuns(dict[str, RunRecord]):
    """Dict that retains at most ``max_runs`` records, evicting oldest by ``created_at``.

    Evicted runs return 404 (honest) rather than stale data. All keys pointing
    to an evicted record (e.g. both task_id and execution_id) are removed.
    """

    def __init__(self, max_runs: int = DEFAULT_MAX_RUNS, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.max_runs = max_runs

    def __setitem__(self, key: str, value: RunRecord) -> None:
        super().__setitem__(key, value)
        self._enforce_limit()

    def _enforce_limit(self) -> None:
        if self.max_runs is None:
            return
        seen: dict[int, RunRecord] = {}
        for rec in self.values():
            seen[id(rec)] = rec
        if self.max_runs <= 0:
            overflow = len(seen)
        elif len(seen) > self.max_runs:
            overflow = len(seen) - self.max_runs
        else:
            return

        sorted_records = sorted(
            seen.values(), key=lambda r: getattr(r, "created_at", 0.0)
        )
        victims = {id(r) for r in sorted_records[:overflow]}

        keys_to_delete = [k for k, v in self.items() if id(v) in victims]
        for k in keys_to_delete:
            self.pop(k, None)
        for r in sorted_records[:overflow]:
            drop_control(r.task_id)

# --------------------------------------------------------------------------- #
# SSE fan-out
# --------------------------------------------------------------------------- #

def _format_sse(event: ObsEvent | dict[str, Any]) -> str:
    if isinstance(event, ObsEvent):
        payload = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, default=str)
    else:
        payload = json.dumps(event, ensure_ascii=False, default=str)
    return f"data: {payload}\n\n"

class EventStream:
    """A bus sink that buffers recent events and fans new ones out to live SSE
    subscribers.

    The buffer is bounded by ``max_events`` (FIFO eviction) so long-running
    servers cannot exhaust process memory. Replay to any single subscriber is
    capped at ``max_replay`` (newest-first slice of the matching buffered
    history) and announced via an opening ``meta`` event (``replayed: N of M``)
    so clients never mistake a capped replay for a complete history.
    """

    def __init__(
        self,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_replay: int = DEFAULT_MAX_REPLAY,
    ) -> None:
        self.max_events = max_events
        self.max_replay = max_replay
        self._events: deque[ObsEvent] = deque(maxlen=max_events)
        self._appended = 0
        self._subscribers: set[asyncio.Queue[ObsEvent]] = set()

    # -- EventBus sink -------------------------------------------------- #

    def __call__(self, event: ObsEvent) -> None:
        self._appended += 1
        self._events.append(event)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - queues are unbounded
                pass

    # -- Introspection -------------------------------------------------- #

    @property
    def events(self) -> list[ObsEvent]:
        """A snapshot of every buffered event, oldest first."""
        return list(self._events)

    @property
    def dropped(self) -> int:
        """How many events the buffer cap has evicted since construction."""
        return self._appended - len(self._events)

    # -- Streaming ------------------------------------------------------ #

    async def iterate(
        self, task_id: str | None, heartbeat: float
    ) -> AsyncIterator[str]:
        """Yield SSE frames for ``task_id`` (or every task when ``None``).

        Emits a leading ``meta`` frame (``replayed: N of M``), then the
        buffered history (clamped to ``max_replay`` newest events), then
        live events with a heartbeat comment every ``heartbeat`` seconds of
        silence. Closes cleanly once a ``task_finished`` event for the
        requested task has been delivered.
        """
        queue: asyncio.Queue[ObsEvent] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            matching = [
                ev for ev in self._events
                if task_id is None or ev.task_id == task_id
            ]
            total_matching = len(matching)
            replay_slice = (
                matching[-self.max_replay :]
                if self.max_replay > 0 and len(matching) > self.max_replay
                else matching
            )
            # Honest announcement: expose exact replay coverage.
            meta_payload = {
                "kind": "meta",
                "task_id": task_id,
                "replayed": len(replay_slice),
                "total_matching": total_matching,
                "buffer_dropped": self.dropped,
            }
            yield _format_sse(meta_payload)

            for event in replay_slice:
                yield _format_sse(event)
                if self._is_terminal(event, task_id):
                    return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if task_id is not None and event.task_id != task_id:
                    continue
                yield _format_sse(event)
                if self._is_terminal(event, task_id):
                    return
        finally:
            self._subscribers.discard(queue)

    @staticmethod
    def _is_terminal(event: ObsEvent, task_id: str | None) -> bool:
        # Only a task-scoped stream terminates; the "all tasks" stream stays open.
        return (
            task_id is not None
            and event.kind == EventKind.TASK_FINISHED
            and event.task_id == task_id
        )

# --------------------------------------------------------------------------- #
# The application
# --------------------------------------------------------------------------- #

def _build_llm_synthesizer(router: Any | None = None):
    """Build the async ``(question, verified) -> analysis`` callable for research.

    Returns ``None`` when no credentials are configured, so the server degrades
    to the deterministic report instead of failing per-request. The analysis is
    APPENDED to the evidence report (never replaces it), which keeps the review
    node's coverage criteria meaningful.
    """
    import os

    if not (
        os.environ.get("UAP_LLM_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        return None

    async def synthesize(question: str, verified: list[dict[str, Any]]) -> str:
        from uap.models.client import ChatClient

        claims = "\n".join(
            f"- {record.get('claim', '')}" for record in verified[:20]
        ) or "- (no verified claims)"
        client = ChatClient()
        model_id = os.environ.get("UAP_DEFAULT_MODEL", "gpt-5.6-sol")
        if router is not None:
            from uap.models.catalog import ModelCapability
            from uap.models.router import RoutingRequest

            decision, _is_fb = router.select_or_fallback(
                RoutingRequest(capability=ModelCapability.REASONING)
            )
            model_id = decision.model_id
        try:
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
                        "content": f"Question: {question}\n\nVerified claims:\n{claims}",
                    },
                ],
            )
        finally:
            await client.aclose()
        return text

    return synthesize


def create_app(
    *,
    bus: EventBus | None = None,
    runs_dir: Path | None = None,
    run_inline: bool = False,
    llm_enabled: bool | None = None,
    model_router: Any | None = None,
    heartbeat_interval: float = DEFAULT_HEARTBEAT_SECONDS,
    mcp_enabled: bool | None = None,
    mcp_config: MCPServerConfig | None = None,
    max_runs: int = DEFAULT_MAX_RUNS,
    max_events: int = DEFAULT_MAX_EVENTS,
    max_replay: int = DEFAULT_MAX_REPLAY,
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        bus: observability bus to reuse; a fresh :class:`EventBus` is created
            when omitted. A :class:`JsonlSink` and :class:`MemorySink` are always
            attached under ``runs_dir``.
        runs_dir: filesystem root for event logs and artifacts (default
            ``./data/runs``).
        run_inline: run workflows synchronously inside the request instead of a
            background task. Deterministic and required for ``TestClient``.
        heartbeat_interval: seconds between SSE heartbeat comments.
        mcp_enabled: when ``True`` start the MCP subprocess; when ``None``
            fall back to env ``UAP_MCP_ENABLED`` (``1``/``true``/``yes``).
        mcp_config: override the default :data:`BUG_BOUNTY_MCP_CONFIG`.
    """
    runs_root = Path(runs_dir) if runs_dir is not None else DEFAULT_RUNS_DIR
    event_bus = bus if bus is not None else EventBus()

    # MCP resolution: explicit param > env > AUTO-DETECT.
    #
    # Auto-detect was added because requiring UAP_MCP_ENABLED meant a user with
    # a perfectly good bugbounty-mcp install got a "bug bounty" workflow that
    # performed NO reconnaissance and quietly produced stub findings. Verified
    # 2026-10-03: the binary was present and working, the flag was not set, and
    # every BBP run was a no-op that still reported "completed".
    #
    # Rules:
    #   - an explicit param always wins;
    #   - UAP_MCP_ENABLED=0/false/no forces it OFF even when the binary exists;
    #   - otherwise, start it when the configured binary is present and runnable.
    _mcp_cfg = mcp_config if mcp_config is not None else BUG_BOUNTY_MCP_CONFIG
    _env_flag = os.environ.get("UAP_MCP_ENABLED", "").strip().lower()
    if mcp_enabled is not None:
        _mcp_wanted = mcp_enabled
    elif _env_flag in {"0", "false", "no"}:
        _mcp_wanted = False
    elif _env_flag in {"1", "true", "yes"}:
        _mcp_wanted = True
    else:
        _mcp_bin = Path(str(_mcp_cfg.command))
        _mcp_wanted = _mcp_bin.is_file() and os.access(_mcp_bin, os.X_OK)

    jsonl_sink = JsonlSink(runs_root / "events.jsonl")
    memory_sink = MemorySink(max_events=max_events)
    stream = EventStream(max_events=max_events, max_replay=max_replay)
    event_bus.subscribe(jsonl_sink)
    event_bus.subscribe(memory_sink)
    event_bus.subscribe(stream)

    # ModelRouter: construct ONE instance (ModelCatalog.default(), PolicyResolver()) if not injected
    if model_router is None:
        try:
            from uap.models.catalog import ModelCatalog
            from uap.models.policy import PolicyResolver
            from uap.models.router import ModelRouter

            catalog = ModelCatalog.default()
            model_router = ModelRouter(catalog, PolicyResolver())
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "ModelRouter construction failed; server will continue without router",
                level=logging.WARNING,
            )
            model_router = None

    entry = EntryWorkflow()
    # LLM synthesis: explicit param > env UAP_LLM_ENABLED > off. Off keeps the
    # suite deterministic (the synthesizer is simply absent).
    _llm_wanted = (
        llm_enabled
        if llm_enabled is not None
        else os.environ.get("UAP_LLM_ENABLED", "").lower() in {"1", "true", "yes"}
    )
    research = ResearchWorkflow(
        synthesizer=_build_llm_synthesizer(router=model_router) if _llm_wanted else None,
    )
    _instrument(research, event_bus)

    # The registry holds one BBP instance so routing resolves the name; each
    # request gets its own scope-gated instance (built in POST /tasks) because
    # the gate is per-task policy, not shared mutable state.
    bbp = BBPWorkflow()
    _instrument(bbp, event_bus)

    registry = WorkflowRegistry()
    registry.register(RESEARCH_WORKFLOW_NAME, research)
    registry.register(BBP_WORKFLOW_NAME, bbp)
    router = Router(registry)
    gate = ApprovalGate()

    runs: dict[str, RunRecord] = BoundedRuns(max_runs=max_runs)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: optionally start MCP tools.
        if _mcp_wanted:
            try:
                mcp_registry, mcp_client = await asyncio.to_thread(
                    start_mcp_tools, _mcp_cfg,
                )
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    f"MCP server {_mcp_cfg.name!r} failed to start",
                    level=logging.WARNING,
                )
                mcp_registry, mcp_client = None, None
            if mcp_registry is not None and mcp_client is not None:
                mcp_registry.approval_gate = gate
                mcp_registry.event_bus = event_bus
                app.state.mcp_tools = mcp_registry
                app.state.mcp_client = mcp_client
                research.tools = mcp_registry
                names = [s.name for s in mcp_registry.list()]
                event_bus.emit_kind(
                    EventKind.MCP_CALL,
                    data={"mcp_startup": "ok", "tools": names},
                )
            else:
                app.state.mcp_tools = None
                app.state.mcp_client = None
                event_bus.emit_kind(
                    EventKind.ERROR,
                    error=f"MCP server {_mcp_cfg.name!r} failed to start; continuing without MCP tools",
                )
        else:
            app.state.mcp_tools = None
            app.state.mcp_client = None
        yield
        # Shutdown: close the MCP client if it was started.
        client = getattr(app.state, "mcp_client", None)
        if client is not None:
            await asyncio.to_thread(stop_mcp_tools, client)
            app.state.mcp_client = None


    # Auth is opt-in: a no-op unless UAP_API_TOKEN is set (see server/auth.py).
    # The access log would otherwise print the WS handshake's ?token= value.
    scrub_token_from_logs()
    app = FastAPI(
        title="Universal Agent Platform",
        version="0.1.0",
        lifespan=lifespan,
        dependencies=[Depends(require_token)],
        exception_handlers={WebSocketException: close_unauthorized_websocket},
    )
    app.state.bus = event_bus
    app.state.memory_sink = memory_sink
    app.state.jsonl_sink = jsonl_sink
    app.state.stream = stream
    app.state.runs = runs
    app.state.max_runs = max_runs
    app.state.runs_dir = runs_root
    app.state.gate = gate
    app.state.entry = entry
    app.state.router = router
    app.state.model_router = model_router
    app.state.research = research
    app.state.bbp = bbp
    app.state.run_inline = run_inline
    app.state.heartbeat_interval = heartbeat_interval
    # Defaults so request handlers are safe even if lifespan has not run
    # (e.g. TestClient used without the context-manager form).
    app.state.mcp_tools = None
    app.state.mcp_client = None
    #: Strong references to in-flight background runs (see create_task above).
    app.state.background = set()

    # Build durable execution service and the §71 platform slice when available.
    try:
        from uap.db.engine import get_session_factory
        from uap.graph import WorkflowGraph
        from uap.runtime.service import ExecutionService
        from uap.slice import PlatformNodeRuntime, PlatformSlice

        session_factory = get_session_factory()

        def _default_graph_resolver(ref: str) -> Any:
            name = str(ref).split("@", 1)[0]
            if name == RESEARCH_WORKFLOW_NAME:
                wf = research
            elif name == BBP_WORKFLOW_NAME:
                wf = bbp
            else:
                raise KeyError(ref)
            return WorkflowGraph.from_dict(wf.graph_spec())

        # Build the slice FIRST: it owns the agent/tool registries, and the
        # node runtime must execute through exactly those registries.
        #
        # The LLM client must reach the slice too: /tasks runs through the
        # slice whenever PG is up, so passing None here silently disabled the
        # model on the PRIMARY path (audit finding, fixed 2026-10-02 — the
        # README's "real ## Analysis section" was false in the main config).
        slice_llm_client = None
        if _llm_wanted:
            try:
                from uap.models.client import ChatClient

                slice_llm_client = ChatClient()
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "LLM enabled but ChatClient could not be constructed; slice runs will use deterministic output",
                    level=logging.WARNING,
                )
        app.state.slice = PlatformSlice(
            session_factory,
            artifacts_root=runs_root / "artifacts",
            llm_client=slice_llm_client,
            model_router=model_router,
            approval_gate=gate,
        )

        try:
            node_runtime: Any = PlatformNodeRuntime(
                app.state.slice.agents, app.state.slice.tools
            )
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "PlatformNodeRuntime unavailable; durable executions will echo inputs",
                level=logging.WARNING,
            )

            class EchoNodeRuntime:
                async def run_node(self, node: Any, inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
                    return dict(inputs)

            node_runtime = EchoNodeRuntime()

        app.state.service = ExecutionService(
            session_factory,
            graph_resolver=_default_graph_resolver,
            node_runtime=node_runtime,
            approval_gate=gate,
        )
    except Exception as exc:
        # No PostgreSQL / slice stack: the platform still boots and serves via
        # the legacy in-memory path (hard requirement — covered by tests).
        log_swallowed_exception(
            logger,
            exc,
            "durable runtime unavailable; falling back to in-memory execution",
            level=logging.WARNING,
        )
        app.state.service = None
        app.state.slice = None
    # The WS layer must answer for BOTH durable executions (PostgreSQL) and
    # in-memory UI runs (the ``runs`` dict). Without the composite bridge a
    # UI-created task showed status "unknown" forever (canvas stuck at
    # "connecting" — reproduced in a real browser 2026-10-02).
    #
    # Built lazily per connection so `app.state.service` overrides (tests
    # inject fakes after create_app returns) are always respected.
    from uap.server.ws_bridge import CompositeRunService

    def _ws_service_factory() -> CompositeRunService:
        return CompositeRunService(
            durable=getattr(app.state, "service", None),
            runs_getter=lambda: runs,
            events_getter=lambda task_id: memory_sink.query(task_id=task_id),
        )

    app.include_router(build_ws_router(_ws_service_factory))

    if _UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
        # Canvas IDE is the primary UI: serve it at the root too.
        app.mount("/legacy", StaticFiles(directory=str(_STATIC_DIR), html=True), name="legacy")
    # -- helpers -------------------------------------------------------- #

    def persist_artifact(artifact: Artifact) -> str | None:
        if not artifact.content_ref:
            return artifact.uri
        target = runs_root / "artifacts" / artifact.task_id / artifact.type
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(artifact.content_ref), encoding="utf-8")
        return str(target)

    def scope_gate_for(spec: TaskSpec) -> ScopeGate:
        """Build the per-task BBP scope gate from the compiled ``TaskSpec``.

        Precedence (Master section 9: fail closed, policy outside the model):

        1. explicit ``constraints['in_scope']`` -> use it (plus any
           ``constraints['out_of_scope']``);
        2. otherwise the entry-extracted ``input['targets']`` -> those exact
           targets become the in-scope allow-list (the user named them; the
           entry workflow already asked for clarification when none were given);
        3. otherwise an empty in-scope list -> the gate blocks everything.

        Raises :class:`ScopeRuleError` when a pattern is malformed; the caller
        turns that into a clarification response instead of a 500.
        """
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
        # Defense in depth: the entry workflow asks for a target first, so this
        # should be unreachable; an empty gate still blocks everything.
        return ScopeGate(in_scope=[], out_of_scope=[])

    def workflow_for(spec: TaskSpec, decision: Any) -> Any:
        """Resolve the workflow instance that will execute ``spec``.

        Research may be a shared singleton; BBP is rebuilt per request so its
        scope gate is task-local and never shared across requests.
        """
        if decision.domain == Domain.BBP:
            tools = app.state.mcp_tools
            if tools is not None:
                tools.approval_gate = gate
                tools.event_bus = event_bus
            workflow = BBPWorkflow(scope_gate=scope_gate_for(spec), tools=tools)
            _instrument(workflow, event_bus)
            return workflow
        return decision.target

    async def execute_run(record: RunRecord, spec: TaskSpec, workflow: Any) -> None:
        """Run ``workflow`` for ``spec`` and fold the result back into ``record``."""
        event_bus.emit_kind(
            EventKind.TASK_STARTED,
            task_id=record.task_id,
            workflow=record.workflow,
            data={"domain": record.domain, "goal": spec.goal},
        )
        record.status = "running"
        try:
            result: WorkflowResult = await workflow.run(spec)
            state = workflow.runner.state_store.load(spec.task_id)
            if state is not None:
                record.node_history = list(state.node_history)
            record.output = result.output or ""
            record.error = result.error
            record.artifacts = [
                {"type": artifact.type, "uri": persist_artifact(artifact)}
                for artifact in result.artifacts
            ]
            record.status = (
                "completed"
                if result.status == WorkflowStatus.COMPLETED
                else "failed"
            )
        except Exception as exc:  # noqa: BLE001 - surfaced through the run record
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {redact_text(str(exc))}"
            log_swallowed_exception(
                logger,
                exc,
                "workflow execution failed",
                level=logging.ERROR,
                task_id=record.task_id,
                workflow=record.workflow,
            )
            event_bus.emit_kind(
                EventKind.ERROR,
                task_id=record.task_id,
                workflow=record.workflow,
                error=record.error,
            )
        finally:
            drop_control(record.task_id)
            if spec.task_id != record.task_id:
                drop_control(spec.task_id)
            gate_pending = [
                req.model_dump(mode="json")
                for req in gate.pending()
                if req.task_id == record.task_id
            ]
            if gate_pending:
                record.pending_approvals = gate_pending
            event_bus.emit_kind(
                EventKind.TASK_FINISHED,
                task_id=record.task_id,
                workflow=record.workflow,
                data={"status": record.status},
            )

    async def _execute_slice(
        record: RunRecord,
        raw_input: str,
        user_id: str | None,
        spec: TaskSpec | None = None,
        workflow: Any | None = None,
    ) -> None:
        # The SSE stream and the WS bridge both watch this bus; the slice runs
        # its own internal eventing, so mirror the task-level lifecycle here
        # (the same contract the legacy path fulfils).
        event_bus.emit_kind(
            EventKind.TASK_STARTED,
            task_id=record.task_id,
            workflow=record.workflow,
            data={"domain": record.domain, "goal": raw_input},
        )
        record.status = "running"
        try:
            # Pin the id the client already received so durable records
            # (executions/events/traces) resolve under the same id.
            gate_arg = getattr(workflow, "scope_gate", None) if workflow is not None else None
            result = await asyncio.to_thread(
                app.state.slice.run,
                raw_input,
                user_id=user_id,
                task_id=record.task_id,
                workspace_id=record.workspace_id,
                scope_gate=gate_arg,
                pipeline=workflow,
                task=spec,
            )
            if result.task_id and result.task_id != record.task_id:
                runs[result.task_id] = record
                record.task_id = result.task_id
            if result.execution_id:
                runs[result.execution_id] = record
            record.workspace_id = result.workspace_id or record.workspace_id
            record.context = result.context
            # Report the evidence source HONESTLY. This field was hardcoded to
            # "deterministic-stubs" and never updated, so a run that really
            # called bugbounty-mcp (crt.sh subdomain enumeration, live HTTP
            # probes) still told the user its findings were fake — and a run
            # that really used stubs claimed nothing at all. Verified
            # 2026-10-03: MCP tools were invoked, the subdomains matched crt.sh
            # exactly, and the label still said "deterministic-stubs".
            record.evidence_source = _evidence_source_for(record.task_id, app)
            # SliceResult.artifacts holds artifact IDS; the store writes files
            # named "{artifact_id}__v{n}__{source}" under artifacts_root/<task_id>/
            # with a sidecar .meta.json carrying the real artifact type.
            artifact_dir = runs_root / "artifacts" / record.task_id
            resolved: list[dict[str, Any]] = []
            for artifact_id in result.artifacts:
                matches = sorted(artifact_dir.glob(f"{artifact_id}__*"))
                matches = [m for m in matches if not m.name.endswith(".meta.json")]
                if not matches:
                    continue
                path = matches[0]
                artifact_type = path.name
                meta_path = path.with_name(path.name + ".meta.json")
                if meta_path.is_file():
                    try:
                        artifact_type = json.loads(meta_path.read_text(encoding="utf-8")).get(
                            "type", artifact_type
                        )
                    except (OSError, ValueError) as exc:
                        log_swallowed_exception(
                            logger,
                            exc,
                            "failed to read artifact sidecar metadata",
                            level=logging.DEBUG,
                            meta_path=str(meta_path),
                        )
                target_path = artifact_dir / str(artifact_type)
                if not target_path.exists() and path.is_file():
                    try:
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        target_path.write_bytes(path.read_bytes())
                    except OSError:
                        pass
                resolved.append({
                    "type": str(artifact_type),
                    "uri": str(target_path if target_path.exists() else path),
                })
            record.artifacts = resolved
            # The run's output is the synthesis artifact's text when available
            # (that is what the user asked for); fall back to any text file.
            output = ""
            for item in resolved:
                path = Path(item["uri"])
                if "synthesize" in path.name or "output" in path.name:
                    try:
                        if path.is_file():
                            output = path.read_text(encoding="utf-8")
                            break
                    except (OSError, UnicodeError) as exc:
                        log_swallowed_exception(
                            logger,
                            exc,
                            "failed to read artifact text content",
                            level=logging.DEBUG,
                            path=str(path),
                        )
            if not output:
                for item in resolved:
                    path = Path(item["uri"])
                    try:
                        if path.is_file():
                            output = path.read_text(encoding="utf-8")
                            break
                    except (OSError, UnicodeError) as exc:
                        log_swallowed_exception(
                            logger,
                            exc,
                            "failed to read artifact text content",
                            level=logging.DEBUG,
                            path=str(path),
                        )
            record.output = output
            record.error = result.error
            record.status = "failed" if result.error else "completed"
            # Node history comes from the durable execution events (the slice
            # runs its own graph: input/recon/fetch/summarize/synthesize/
            # evaluate/knowledge/output), so the UI can show real progress.
            if result.execution_id:
                try:
                    from uap.db.engine import session_scope
                    from uap.db.repositories import EventRepository

                    with session_scope(session_factory) as session:
                        rows = EventRepository(session).read_since(
                            uuid.UUID(str(result.execution_id)), 0
                        )
                    history = [
                        str(event.node)
                        for event in rows
                        if event.kind == "node_started" and event.node
                    ]
                    if record.domain == Domain.BBP:
                        history = [n for n in history if n not in ("input", "output")]
                    record.node_history = history
                except Exception as exc:
                    log_swallowed_exception(
                        logger,
                        exc,
                        "failed to read execution events for node_history",
                        level=logging.WARNING,
                        execution_id=str(result.execution_id),
                    )
        except Exception as exc:
            record.status = "failed"
            record.error = f"{type(exc).__name__}: {redact_text(str(exc))}"
            log_swallowed_exception(
                logger,
                exc,
                "slice execution failed",
                level=logging.ERROR,
                task_id=record.task_id,
            )
        finally:
            drop_control(record.task_id)
            gate_pending = [
                req.model_dump(mode="json")
                for req in gate.pending()
                if req.task_id == record.task_id
            ]
            if gate_pending:
                record.pending_approvals = gate_pending
            event_bus.emit_kind(
                EventKind.TASK_FINISHED,
                task_id=record.task_id,
                workflow=record.workflow,
                data={"status": record.status},
            )

    # -- routes --------------------------------------------------------- #

    @app.get("/legacy", include_in_schema=False)
    async def legacy_redirect() -> FileResponse:
        """Old single-page UI (canvas IDE is the primary UI at /)."""
        return FileResponse(_INDEX_HTML, media_type="text/html")

    async def _start_task(
        raw_input: str,
        *,
        user_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Entry -> Router -> workflow -> dispatch; return the task payload.

        Shared by ``POST /tasks`` and ``POST /api/workspaces/from-template`` so
        a template-started run passes through exactly the same gate as a manual
        one: no duplicated routing, no second execution path. Returns either
        ``{"status": "clarification", "question": ...}`` or the accepted-task
        payload.
        """
        outcome = entry.run(UserRequest(raw_input=raw_input, user_id=user_id))
        if outcome.needs_clarification or outcome.spec is None:
            return {"status": "clarification", "question": outcome.question}

        decision = router.route(outcome.spec)
        if decision.domain not in _EXECUTABLE or decision.target is None:
            return {
                "status": "clarification",
                "question": (
                    f"The '{decision.domain}' domain is not available yet. "
                    f"{_AVAILABLE_DOMAINS_EXAMPLE}"
                ),
            }

        spec = outcome.spec
        try:
            workflow = workflow_for(spec, decision)
        except ScopeRuleError as exc:
            return {
                "status": "clarification",
                "question": (
                    "The bug bounty scope could not be applied: "
                    f"{exc}. Provide scope as bare hosts, e.g. \"In scope: "
                    "*.example.com\"."
                ),
            }

        record = RunRecord(
            task_id=spec.task_id,
            domain=decision.domain,
            workflow=decision.workflow_name or RESEARCH_WORKFLOW_NAME,
            input=raw_input,
            workspace_id=workspace_id,
            requested_by=user_id,
        )
        runs[spec.task_id] = record
        get_control(spec.task_id)  # register run-control gate

        # The durable §71 slice executes the REAL research and BBP pipelines
        # (their node functions run through canonical graphs; verified equivalent
        # output to the legacy runner plus PG events/traces/knowledge).
        # BBP scope gate is strictly evaluated BEFORE any node executes.
        use_slice = (
            getattr(app.state, "slice", None) is not None
            and decision.domain in (Domain.RESEARCH, Domain.BBP)
        )
        if use_slice:
            if run_inline:
                await _execute_slice(record, raw_input, user_id, spec, workflow)
            else:
                # Hold a strong reference: the event loop only keeps a weak one,
                # so a bare create_task() can be garbage-collected mid-run and
                # the task would silently vanish (CPython asyncio docs).
                task = asyncio.create_task(
                    _execute_slice(record, raw_input, user_id, spec, workflow)
                )
                app.state.background.add(task)
                task.add_done_callback(app.state.background.discard)
        elif run_inline:
            await execute_run(record, spec, workflow)
        else:
            task = asyncio.create_task(execute_run(record, spec, workflow))
            app.state.background.add(task)
            task.add_done_callback(app.state.background.discard)

        return {
            "task_id": record.task_id,
            "domain": record.domain,
            "workflow": record.workflow,
            "status": "accepted",
            "workspace_id": record.workspace_id,
            "evidence_source": record.evidence_source,
        }

    @app.post("/tasks")
    async def create_task(body: TaskRequest) -> dict[str, Any]:
        return await _start_task(
            body.input, user_id=body.user_id, workspace_id=body.workspace_id
        )

    @app.get("/tasks")
    async def list_tasks(requested_by: str | None = None) -> list[dict[str, Any]]:
        """List tracked runs, optionally filtered by requester identity.

        NOTE: This provides attribution for audit purposes, not authentication.
        A caller can claim any user_id, so this is not a security boundary.
        """
        # A run may be registered under several keys (task_id, execution_id,
        # pre-rewrite task_id) pointing at the SAME RunRecord. Deduplicate by
        # object identity so each logical run is listed exactly once; the
        # aliases are kept in the registry for WS/route lookups.
        seen: dict[int, RunRecord] = {}
        for record in runs.values():
            seen.setdefault(id(record), record)
        results = [record.summary() for record in seen.values()]
        if requested_by is not None:
            results = [r for r in results if r.get("requested_by") == requested_by]
        return results

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, Any]:
        record = runs.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown task")
        record.pending_approvals = [
            req.model_dump(mode="json")
            for req in gate.pending()
            if req.task_id == task_id
        ]
        return record.detail()

    @app.get("/events")
    async def events(request: Request, task_id: str | None = None) -> StreamingResponse:
        return StreamingResponse(
            stream.iterate(task_id, heartbeat_interval),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/approvals/{approval_id}/decide")
    async def decide(approval_id: str, body: DecisionRequest) -> dict[str, Any]:
        try:
            updated = gate.decide(
                approval_id, approved=body.approved, decided_by=body.decided_by
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown approval")
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return updated.model_dump(mode="json")


    # -- helper: resolve workflow by name -------------------------------- #

    _workflow_map: dict[str, Any] = {
        RESEARCH_WORKFLOW_NAME: research,
        BBP_WORKFLOW_NAME: bbp,
        "ResearchWorkflow": research,
        "BBPWorkflow": bbp,
    }

    def _graph_for_workflow(wf_name: str) -> dict[str, Any] | None:
        wf = _workflow_map.get(wf_name)
        if wf is not None and hasattr(wf, "graph_spec"):
            return wf.graph_spec()
        return None

    def _evidence_source_for(task_id: str, app_ref: Any) -> str:
        """What actually produced this run's evidence.

        Derived from what the run DID, never assumed. The value is user-facing:
        a security researcher must be able to tell a real crt.sh result from a
        fixture, and the previous hardcoded label said "stubs" for both.
        """

        mcp_calls = 0
        for ev in memory_sink.query(task_id=task_id):
            kind = str(getattr(ev, "kind", "") or "")
            if "mcp" in kind.lower():
                mcp_calls += 1
        if mcp_calls:
            return f"mcp:{mcp_calls}-calls"
        if getattr(app_ref.state, "mcp_client", None) is not None:
            return "mcp-available-no-calls"
        return "deterministic-stubs"

    def _node_statuses(task_id: str) -> tuple[dict[str, str], bool]:
        """Derive per-node status from observability events.

        Two event sources exist: the server's in-memory sink (legacy path) and
        the durable PostgreSQL event store (slice path — it emits through the
        ExecutionService). Merge both so the canvas shows live state whichever
        path ran the task.

        Returns (statuses, degraded_flag).
        """
        statuses: dict[str, str] = {}
        degraded = False

        def apply(kind: str, node: str | None) -> None:
            if not node:
                return
            if kind == "node_started" or kind == EventKind.NODE_STARTED:
                statuses[str(node)] = "running"
            elif kind == "node_finished" or kind == EventKind.NODE_FINISHED:
                statuses[str(node)] = "completed"
            elif kind in {"node_failed", "error"} or kind == EventKind.ERROR:
                statuses[str(node)] = "failed"

        for ev in memory_sink.query(task_id=task_id):
            apply(ev.kind, ev.node)

        # Durable events (slice path): resolve the execution by id OR
        # correlation id, then replay its node lifecycle.
        try:
            import uuid as _uuid
            from uap.db.engine import session_scope
            from uap.db.repositories import EventRepository, ExecutionRepository

            with session_scope() as session:
                eid = _uuid.UUID(str(task_id))
                repo = ExecutionRepository(session)
                row = repo.get(eid) or repo.get_by_correlation_id(eid)
                if row is not None:
                    for event in EventRepository(session).read_since(row.id, 0):
                        apply(event.kind, event.node)
        except Exception as exc:
            degraded = True
            log_swallowed_exception(
                logger,
                exc,
                "failed to query durable node statuses from database",
                level=logging.WARNING,
                task_id=str(task_id),
            )

        return statuses, degraded

    # -- /api/workflows -------------------------------------------------- #

    @app.get("/api/workflows")
    async def list_workflows() -> list[dict[str, Any]]:
        result = []
        for wf_name, wf in _workflow_map.items():
            graph = wf.graph_spec() if hasattr(wf, "graph_spec") else None
            entry = getattr(wf.runner, "entry", None) if hasattr(wf, "runner") else None
            result.append({
                "name": wf_name,
                "domain": wf_name,
                "description": graph["description"] if graph else "",
                "entry": entry,
                "execution_mode": "deterministic-stubs",
                "graph": graph,
            })
        return result

    # -- /api/proposals -------------------------------------------------- #

    @app.post("/api/proposals")
    async def create_proposal(body: ProposalRequest) -> dict[str, Any]:
        outcome = entry.run(UserRequest(raw_input=body.input))
        if outcome.needs_clarification or outcome.spec is None:
            return {"status": "clarification", "question": outcome.question}

        decision = router.route(outcome.spec)
        if decision.domain not in _EXECUTABLE or decision.target is None:
            return {
                "status": "clarification",
                "question": (
                    f"The '{decision.domain}' domain is not available yet. "
                    f"{_AVAILABLE_DOMAINS_EXAMPLE}"
                ),
            }

        wf_name = decision.workflow_name or RESEARCH_WORKFLOW_NAME
        graph = _graph_for_workflow(wf_name)

        # Build resource inventory from graph node configs.
        agents: list[str] = []
        tools: list[str] = []
        skills: list[str] = []
        if graph:
            for node in graph.get("nodes", []):
                kind = node.get("kind", "")
                if kind == "agent":
                    agents.append(node["id"])
                elif kind == "tool":
                    tools.append(node["id"])

        # Reasoning from the intent analysis.
        intent = getattr(outcome, "_intent", None)
        reasoning = [decision.reason]
        if outcome.spec:
            reasoning.append(f"domain={decision.domain}, confidence=matched")

        return {
            "status": "proposal",
            "domain": decision.domain,
            "workflow": wf_name,
            "goal": outcome.spec.goal if outcome.spec else "",
            "graph": graph,
            "resources": {"agents": agents, "tools": tools, "skills": skills},
            "reasoning": reasoning,
        }

    # -- /api/executions/{id}/graph -------------------------------------- #

    @app.get("/api/executions/{execution_id}/graph")
    async def get_execution_graph(execution_id: str) -> dict[str, Any]:
        # Try durable DB first.
        found = False
        spec = None
        try:
            import uuid as _uuid
            from uap.db.engine import session_scope
            from uap.db.models.definitions import WorkflowVersion
            from uap.db.repositories import ExecutionRepository
            with session_scope() as session:
                try:
                    eid = _uuid.UUID(str(execution_id))
                    repo = ExecutionRepository(session)
                    row = repo.get(eid) or repo.get_by_correlation_id(eid)
                    if row is not None:
                        found = True
                        # Execution has no `workflow_version` relationship; the
                        # version row's spec is a LIBRARY entry
                        # ({inputs, outputs, graph_ref}) — the graph itself is
                        # rebuilt from code (the ref is a pointer, not a spec).
                        version = session.get(WorkflowVersion, row.workflow_version_id)
                        ref = None
                        if version is not None and isinstance(version.spec, dict):
                            ref = version.spec.get("graph_ref")
                        for wf in (research, bbp):
                            graph_ref = getattr(wf, "graph_ref", None)
                            if ref and graph_ref and graph_ref != ref:
                                continue
                            if hasattr(wf, "graph_spec"):
                                spec = wf.graph_spec()
                                break
                except (ValueError, TypeError):
                    pass
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "failed to read execution graph from database",
                level=logging.WARNING,
                execution_id=str(execution_id),
            )

        record = runs.get(execution_id)
        if not found and record is not None:
            found = True

        if not found:
            raise HTTPException(status_code=404, detail="unknown execution")

        # Durable spec from DB (the slice path): return the real graph WITH the
        # live node statuses merged from the durable event store.
        if spec and isinstance(spec, dict):
            try:
                from uap.graph import WorkflowGraph

                payload = WorkflowGraph.from_dict(spec).to_dict()
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "failed to parse workflow spec with WorkflowGraph; using raw spec",
                    level=logging.DEBUG,
                )
                payload = spec
            node_statuses, is_degraded = _node_statuses(execution_id)
            for node in payload.get("nodes", []):
                node["status"] = node_statuses.get(node["id"], "pending")
            execution_status = None
            if record is not None:
                execution_status = record.status
            else:
                try:
                    import uuid as _uuid2
                    from uap.db.engine import session_scope
                    from uap.db.repositories import ExecutionRepository

                    with session_scope() as session:
                        eid2 = _uuid2.UUID(str(execution_id))
                        repo2 = ExecutionRepository(session)
                        row2 = repo2.get(eid2) or repo2.get_by_correlation_id(eid2)
                        if row2 is not None:
                            execution_status = getattr(row2.status, "value", str(row2.status))
                except Exception as exc:
                    is_degraded = True
                    log_swallowed_exception(
                        logger,
                        exc,
                        "failed to query execution status from database",
                        level=logging.WARNING,
                        execution_id=str(execution_id),
                    )
            payload["execution"] = {"status": execution_status or "unknown"}
            if is_degraded:
                payload["degraded"] = True
            return payload
        # Real graph from the workflow's graph_spec.
        wf_name = record.workflow if record else None
        graph = _graph_for_workflow(wf_name) if wf_name else None
        if graph is None:
            raise HTTPException(status_code=404, detail="unknown execution")

        # Merge live node statuses.
        node_statuses, is_degraded = _node_statuses(execution_id)
        for node in graph.get("nodes", []):
            node["status"] = node_statuses.get(node["id"], "pending")

        exec_status = record.status if record else "unknown"
        graph["execution"] = {"status": exec_status}
        if is_degraded:
            graph["degraded"] = True
        return graph

    # -- /api/executions/{id}/pause & resume ----------------------------- #

    @app.post("/api/executions/{execution_id}/pause")
    async def pause_execution(execution_id: str) -> dict[str, str]:
        # Durable path (delegate to service).
        svc = getattr(app.state, "service", None)
        if svc is not None and hasattr(svc, "pause_request"):
            try:
                svc.pause_request(execution_id)
                return {"status": "paused"}
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "service.pause_request failed; falling back to in-memory control",
                    level=logging.WARNING,
                    execution_id=str(execution_id),
                )

        record = runs.get(execution_id)
        if record is None:
            return {"status": "unknown"}
        if record.status not in ("accepted", "running"):
            return {"status": "not_running"}
        ctrl = get_control(execution_id)
        result = ctrl.pause()
        record.status = result
        return {"status": result}

    @app.post("/api/executions/{execution_id}/resume")
    async def resume_execution(execution_id: str) -> dict[str, str]:
        svc = getattr(app.state, "service", None)
        if svc is not None and hasattr(svc, "resume"):
            try:
                svc.resume(execution_id)
                return {"status": "running"}
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "service.resume failed; falling back to in-memory control",
                    level=logging.WARNING,
                    execution_id=str(execution_id),
                )

        record = runs.get(execution_id)
        if record is None:
            return {"status": "unknown"}
        if record.status not in ("paused",):
            return {"status": "not_running"}
        ctrl = get_control(execution_id)
        result = ctrl.resume()
        record.status = result
        return {"status": result}

    # -- /api/executions/{id}/traces (unchanged) ------------------------- #

    @app.get("/api/executions/{execution_id}/traces")
    async def get_execution_traces(execution_id: str) -> list[dict[str, Any]]:
        found = False
        traces: list[dict[str, Any]] = []
        try:
            import uuid as _uuid
            from uap.db.engine import session_scope
            from uap.db.repositories import ExecutionRepository
            from uap.trace.store import TraceStore
            with session_scope() as session:
                try:
                    eid = _uuid.UUID(str(execution_id))
                    repo = ExecutionRepository(session)
                    row = repo.get(eid) or repo.get_by_correlation_id(eid)
                    if row is not None:
                        found = True
                        store = TraceStore(session)
                        # Traces are recorded against the execution row's own
                        # id, which may differ from the client-facing task id
                        # (that one is stored as correlation_id).
                        items = store.list_for_execution(str(row.id))
                        traces = [item.model_dump(mode="json") for item in items]
                except (ValueError, TypeError):
                    pass
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "failed to query execution traces from database",
                level=logging.WARNING,
                execution_id=str(execution_id),
            )

        if not found and execution_id in runs:
            found = True
            traces = []

        if not found:
            raise HTTPException(status_code=404, detail="unknown execution")

        return traces

    # -- /api/executions/{id}/views -------------------------------------- #

    @app.get("/api/executions/{execution_id}/views")
    async def get_execution_views(execution_id: str) -> list[dict[str, Any]]:
        """Return the views nodes published for this execution.

        Accepts EITHER the durable row id OR the client-facing correlation id
        (the id ``POST /tasks`` handed the UI), exactly like the events/context
        read APIs. Each item is ``{node_id, view, created_at}``.
        """
        found = False
        views: list[dict[str, Any]] = []
        svc = getattr(app.state, "service", None)
        slc = getattr(app.state, "slice", None)
        factory = getattr(slc, "_session_factory", None) or getattr(svc, "_session_factory", None)
        try:
            import uuid as _uuid
            from uap.db.engine import session_scope
            from uap.db.repositories import ExecutionRepository
            from uap.views.store import NodeViewStore

            with session_scope(factory) as session:
                try:
                    eid = _uuid.UUID(str(execution_id))
                    repo = ExecutionRepository(session)
                    row = repo.get(eid) or repo.get_by_correlation_id(eid)
                    if row is not None:
                        found = True
                        # Views are keyed on the execution row's own id, which may
                        # differ from the client-facing correlation id.
                        views = NodeViewStore(session).list_for_execution(str(row.id))
                except (ValueError, TypeError):
                    pass
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "failed to query execution views from database",
                level=logging.WARNING,
                execution_id=str(execution_id),
            )

        if not found and execution_id in runs:
            found = True
            views = []

        if not found:
            raise HTTPException(status_code=404, detail="unknown execution")

        return views

    # -- /api/executions/{id}/context ------------------------------------ #

    @app.get("/api/executions/{execution_id}/context")
    async def get_execution_context(execution_id: str) -> dict[str, Any]:
        found = False
        context_data: dict[str, Any] | None = None
        svc = getattr(app.state, "service", None)
        slc = getattr(app.state, "slice", None)
        factory = getattr(slc, "_session_factory", None) or getattr(svc, "_session_factory", None)
        try:
            import uuid as _uuid
            from uap.db.engine import session_scope
            from uap.db.repositories import ExecutionRepository
            from uap.runtime.checkpoints import CheckpointStore

            with session_scope(factory) as session:
                try:
                    eid = _uuid.UUID(str(execution_id))
                    repo = ExecutionRepository(session)
                    row = repo.get(eid) or repo.get_by_correlation_id(eid)
                    if row is not None:
                        found = True
                        store = CheckpointStore(session)
                        latest = store.latest(str(row.id))
                        if latest:
                            _seq, state = latest
                            node_results = state.get("node_results") or {}
                            for node_id in ("synthesis", "review", "research_planning", "question_analysis"):
                                nr = node_results.get(node_id)
                                if isinstance(nr, dict):
                                    for val in nr.values():
                                        if isinstance(val, dict) and "context" in val:
                                            context_data = val["context"]
                                            break
                                if context_data:
                                    break
                            if not context_data:
                                for nr in node_results.values():
                                    if isinstance(nr, dict):
                                        for val in nr.values():
                                            if isinstance(val, dict) and "context" in val:
                                                context_data = val["context"]
                                                break
                                    if context_data:
                                        break
                except (ValueError, TypeError):
                    pass
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "failed to query execution context from database",
                level=logging.WARNING,
                execution_id=str(execution_id),
            )

        record = runs.get(execution_id)
        if not found and record is not None:
            found = True
            if hasattr(record, "context") and record.context:
                context_data = record.context
            elif hasattr(record, "result") and getattr(record.result, "context", None):
                context_data = record.result.context

        if not found:
            raise HTTPException(status_code=404, detail="unknown execution")

        if context_data is None:
            return {
                "execution_id": execution_id,
                "sections": [],
                "total_chars": 0,
                "budget": {},
            }

        sensitive = ("secret", "token", "password", "credential", "api_key", "auth", "privkey")
        safe_sections = [
            s
            for s in context_data.get("sections", [])
            if not any(sens in str(s.get("key", "")).lower() for sens in sensitive)
        ]
        return {
            "execution_id": execution_id,
            "sections": safe_sections,
            "total_chars": sum(s.get("size", 0) for s in safe_sections),
            "budget": context_data.get("budget", {}),
            "extras": context_data.get("extras", {}),
        }
    # -- /api/maintenance/prune (opt-in retention) ------------------------ #

    @app.post("/api/maintenance/prune")
    async def prune_executions(body: PruneRequest) -> dict[str, Any]:
        """Delete old, terminal executions. Opt-in: nothing runs unless called.

        Behind the app-level token gate like every other state-changing route.
        """
        if body.older_than_days < 0:
            raise HTTPException(status_code=422, detail="older_than_days must be >= 0")
        if body.limit is not None and body.limit <= 0:
            raise HTTPException(status_code=422, detail="limit must be positive")
        svc = getattr(app.state, "service", None)
        if svc is None or not hasattr(svc, "prune_executions"):
            raise HTTPException(
                status_code=503, detail="durable execution service unavailable"
            )
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=body.older_than_days)
        deleted = svc.prune_executions(cutoff, limit=body.limit)
        return {"deleted": deleted, "older_than_days": body.older_than_days}

    # -- /api/resources/* ------------------------------------------------ #

    @app.get("/api/resources/agents")
    async def list_agents() -> list[dict[str, Any]]:
        # The platform has one built-in agent: the LLM agent.
        return [{"name": "llm", "capabilities": ["reasoning", "coding"]}]

    @app.get("/api/resources/tools")
    async def list_tools() -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        # Local tools from ToolRegistry.
        try:
            from uap.tools.registry import ToolRegistry
            from uap.tools.local import register_local_tools
            reg = ToolRegistry()
            register_local_tools(reg, runs_root / "artifacts")
            for spec in reg.list():
                result.append({
                    "name": spec.name,
                    "description": spec.description,
                    "risk_tier": spec.risk_tier,
                    "risk_label": spec.risk_label,
                    "source": "local",
                    "input_schema": spec.input_schema,
                })
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "failed to load local tools",
                level=logging.WARNING,
            )
        # MCP tools.
        mcp_tools = getattr(app.state, "mcp_tools", None)
        if mcp_tools is not None:
            try:
                for spec in mcp_tools.list():
                    result.append({
                        "name": spec.name,
                        "description": spec.description,
                        "risk_tier": spec.risk_tier,
                        "risk_label": spec.risk_label,
                        "source": "mcp",
                        "input_schema": spec.input_schema,
                    })
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "failed to list MCP tools",
                    level=logging.WARNING,
                )
        return result

    @app.get("/api/resources/skills")
    async def list_skills() -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        try:
            from uap.skills.registry import SkillRegistry
            from uap.skills.loader import load_skills_lenient
            reg = SkillRegistry()
            skills_dir = Path(__file__).resolve().parents[1] / "skills" / "library"
            if skills_dir.is_dir():
                for skill in load_skills_lenient(skills_dir):
                    try:
                        reg.register(skill)
                    except ValueError:
                        pass
            for name in reg.names():
                skill = reg.get(name)
                if skill:
                    result.append({
                        "name": skill.name,
                        "domain": skill.domain,
                        "description": skill.description,
                    })
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "failed to load skills from library",
                level=logging.WARNING,
            )
        return result

    @app.get("/api/resources/models")
    async def list_models() -> list[dict[str, Any]]:
        from uap.models.catalog import ModelCatalog
        catalog = ModelCatalog.default()
        return [
            {
                "id": m.id,
                "provider": m.provider,
                "capabilities": [str(c) for c in m.capabilities],
            }
            for m in catalog.all()
        ]

    @app.get("/api/resources/policies")
    async def list_policies() -> list[dict[str, Any]]:
        from uap.policy.engine import PolicyEngine
        engine = PolicyEngine.default()
        return [
            {
                "id": r.id,
                "effect": str(r.effect),
                "priority": r.priority,
                "reason": r.reason,
                "subject": r.subject_pattern,
                "action": r.action_pattern,
                "resource_pattern": r.resource_pattern,
            }
            for r in engine._rules
        ]

    @app.get("/api/resources/mcp")
    async def list_mcp_servers() -> list[dict[str, Any]]:
        cfg = BUG_BOUNTY_MCP_CONFIG
        mcp_running = getattr(app.state, "mcp_client", None) is not None
        return [{
            "name": cfg.name,
            # Only the binary name: ``cfg.command`` is the operator's real
            # absolute host path, and echoing it publishes the host layout.
            "command": cfg.command_display,
            "tools": sorted(cfg.tier_map.keys()),
            "running": mcp_running,
        }]

    # -- /api/workspaces ------------------------------------------------- #

    def _workspace_json(ws: Any) -> dict[str, Any]:
        """The public workspace representation shared by every workspace route."""
        return {
            "id": str(ws.id),
            "name": ws.name,
            "description": ws.description or "",
            "status": str(ws.status),
            "created_at": ws.created_at.isoformat() if ws.created_at else None,
            "default_workflow_refs": ws.default_workflow_refs or [],
        }

    @app.get("/api/workspaces")
    async def list_workspaces() -> list[dict[str, Any]]:
        try:
            from uap.workspace.store import WorkspaceStore
            from uap.db.engine import session_scope
            with session_scope() as session:
                store = WorkspaceStore(session=session)
                items = store.list()
                return [_workspace_json(ws) for ws in items]
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "failed to list workspaces from database",
                level=logging.WARNING,
            )
            return []

    @app.post("/api/workspaces")
    async def create_workspace(body: WorkspaceCreateRequest) -> dict[str, Any]:
        try:
            from uap.workspace.store import WorkspaceStore
            from uap.workspace.model import Workspace
            from uap.db.engine import session_scope
            with session_scope() as session:
                store = WorkspaceStore(session=session)
                ws = store.create(Workspace(id=str(uuid.uuid4()), name=body.name, description=body.description or ""))
                session.commit()
                return _workspace_json(ws)
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "workspace creation failed",
                level=logging.ERROR,
                name=body.name,
            )
            raise HTTPException(status_code=503, detail=f"workspace creation failed: {redact_text(str(exc))}")

    # -- /api/templates -------------------------------------------------- #

    @app.get("/api/templates")
    async def list_templates() -> list[dict[str, Any]]:
        """The grouped template catalog (ComfyUI shape).

        Data only -- no database, no execution. Every template maps to a
        workflow that can run today; see :mod:`uap.templates.catalog`.
        """
        from uap.templates import catalog

        return catalog()

    @app.get("/api/templates/{name}")
    async def get_template(name: str) -> dict[str, Any]:
        """One template, with the workflow it maps to and the input shape."""
        from uap.templates import template_detail

        detail = template_detail(name)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"unknown template {name!r}")
        return detail

    @app.post("/api/workspaces/from-template")
    async def create_workspace_from_template(
        body: WorkspaceFromTemplateRequest,
    ) -> dict[str, Any]:
        """Create a workspace from a template AND start its task.

        Returns both the workspace and the started task (or the clarification
        question when the input could not be routed) so the UI can go straight
        to the canvas. The task runs through exactly the same gate as
        ``POST /tasks`` -- this endpoint does not add a second execution path.
        """
        from uap.templates import template_detail

        detail = template_detail(body.template)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"unknown template {body.template!r}")

        if not body.input or not body.input.strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    "input is required; the template documents the shape at "
                    f"GET /api/templates/{body.template}"
                ),
            )

        workflow_ref = detail["workflow"]["workflow_ref"]

        # 1. Create the workspace, pinned to the template's workflow ref.
        try:
            from uap.workspace.store import WorkspaceStore
            from uap.workspace.model import Workspace
            from uap.db.engine import session_scope

            with session_scope() as session:
                store = WorkspaceStore(session=session)
                ws = store.create(
                    Workspace(
                        id=str(uuid.uuid4()),
                        name=detail["title"],
                        description=detail["description"],
                        settings={
                            "template": body.template,
                            "tags": list(detail["tags"]),
                        },
                        default_workflow_refs=[workflow_ref],
                    )
                )
                workspace_json = _workspace_json(ws)
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "workspace creation from template failed",
                level=logging.ERROR,
                template=body.template,
            )
            raise HTTPException(
                status_code=503,
                detail=f"workspace creation failed: {redact_text(str(exc))}",
            )

        # 2. Start the task, bound to the new workspace.
        task = await _start_task(
            body.input, user_id=body.user_id, workspace_id=workspace_json["id"]
        )
        if task.get("status") == "clarification":
            return {
                "template": body.template,
                "workspace": workspace_json,
                "task": None,
                "status": "clarification",
                "question": task.get("question"),
            }
        return {
            "template": body.template,
            "workspace": workspace_json,
            "task": task,
            "status": task.get("status", "accepted"),
        }

    # -- /api/tasks/{task_id}/artifacts ---------------------------------- #

    @app.get("/api/tasks/{task_id}/artifacts")
    async def list_task_artifacts(task_id: str) -> list[dict[str, Any]]:
        record = runs.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown task")
        result = []
        for idx, art in enumerate(record.artifacts):
            uri = art.get("uri") or art.get("type", "")
            result.append({
                "index": idx,
                "type": art.get("type", "unknown"),
                "uri": uri,
                "source": art.get("source", "workflow"),
                "status": art.get("status", "final"),
                "content_available": bool(uri and Path(uri).is_file()),
            })
        return result

    @app.get("/api/tasks/{task_id}/artifacts/{index}/content")
    async def get_artifact_content(task_id: str, index: int) -> dict[str, Any]:
        record = runs.get(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown task")
        if index < 0 or index >= len(record.artifacts):
            raise HTTPException(status_code=404, detail="artifact not found")
        art = record.artifacts[index]
        uri = art.get("uri") or ""
        path = Path(uri) if uri else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="artifact content not available")
        # Only serve text; cap at ~200KB.
        try:
            content = path.read_text(encoding="utf-8")[:200_000]
        except (UnicodeDecodeError, OSError):
            raise HTTPException(status_code=415, detail="binary or unreadable artifact")
        return {"type": art.get("type", "unknown"), "content": content}

    # -- /api/knowledge -------------------------------------------------- #

    @app.get("/api/knowledge")
    async def list_knowledge() -> list[dict[str, Any]]:
        try:
            from uap.db.engine import session_scope
            from uap.knowledge.store import KnowledgeStore
            from uap.knowledge.model import KnowledgeStatus
            with session_scope() as session:
                store = KnowledgeStore(session)
                items = store.list_by_status(KnowledgeStatus.VERIFIED)
                return [
                    {
                        "knowledge_id": item.knowledge_id,
                        "statement": item.statement,
                        "domain": item.domain,
                        "status": str(item.status),
                        "confidence": item.confidence,
                        "provenance_count": len(item.provenance),
                    }
                    for item in items
                ]
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "failed to list verified knowledge items",
                level=logging.WARNING,
            )
            return []
    @app.get("/api/knowledge/{knowledge_id}/provenance")
    async def get_knowledge_provenance(knowledge_id: str) -> dict[str, Any]:
        try:
            from uap.db.engine import session_scope
            from uap.knowledge.store import KnowledgeStore
            with session_scope() as session:
                store = KnowledgeStore(session)
                item = store.get(knowledge_id)
                if item is None:
                    raise HTTPException(status_code=404, detail="unknown knowledge item")
                return {
                    "knowledge_id": item.knowledge_id,
                    "provenance": [
                        {
                            "source_kind": p.source_kind,
                            "source_ref": p.source_ref,
                            "extracted_by": p.extracted_by,
                            "evidence": p.evidence,
                        }
                        for p in item.provenance
                    ],
                }
        except HTTPException:
            raise
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "failed to get knowledge provenance",
                level=logging.WARNING,
                knowledge_id=knowledge_id,
            )
            raise HTTPException(status_code=404, detail="unknown knowledge item")

    # -- /api/library (unchanged) ---------------------------------------- #

    @app.get("/api/library")
    async def get_library() -> list[dict[str, Any]]:
        summary: list[dict[str, Any]] = []
        try:
            from uap.db.engine import session_scope
            from uap.library.service import LibraryService
            with session_scope() as session:
                lib_service = LibraryService(session)
                entries = lib_service.list_library()
                for entry in entries:
                    summary.append({
                        "name": entry.name,
                        "kind": entry.kind,
                        "version": entry.version,
                        "status": str(entry.status),
                        "ref": entry.ref,
                        "definition_id": entry.definition_id,
                    })
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "failed to list library entries from database",
                level=logging.WARNING,
            )
        return summary

    # Canvas IDE is the primary UI at the root: mounted LAST so every API route
    # above wins; only unmatched paths (/, /js/*, /css/*) fall through to it.
    if _UI_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_UI_DIR), html=True), name="root-ui")

        @app.middleware("http")
        async def _no_store_ui_assets(request: Request, call_next: Any) -> Any:
            """Serve UI assets with ``no-cache`` so an update is never hidden.

            The UI is mounted as plain static files with relative URLs
            (``css/app.css``, ``./js/*.js``). A browser that cached them kept
            rendering the PREVIOUS build after a fix landed — a stale CSS file
            made a verified layout fix look broken during a live review
            (2026-10-03). ``no-cache`` still allows a cached copy; it just forces
            revalidation, so the ETag keeps it cheap while never going stale.
            """
            response = await call_next(request)
            path = request.url.path
            if (
                path == "/"
                or path.endswith((".js", ".css", ".html"))
            ):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    return app

# --------------------------------------------------------------------------- #
# Node instrumentation
# --------------------------------------------------------------------------- #

def _instrument(workflow: Any, bus: EventBus) -> None:
    """Wrap every runner node so it emits ``node_started``/``node_finished``.

    The wrapper reads ``task_id`` from the :class:`WorkflowState` it is handed,
    so one workflow instance can serve concurrent runs without cross-talk. The
    ``Tracer`` also emits an ``error`` event (and re-raises) if a node fails.
    Works for any workflow exposing ``runner.nodes`` (research and bbp alike).
    """
    workflow_name = getattr(workflow.runner, "name", "")
    for name, node in list(workflow.runner.nodes.items()):
        workflow.runner.nodes[name] = _traced(bus, workflow_name, name, node)

def _traced(bus: EventBus, workflow_name: str, name: str, node: Any) -> Any:
    async def traced(state: Any) -> Any:
        # Cooperative pause: block between nodes (honest semantics).
        ctrl = run_controls().get(getattr(state, "task_id", None) or "")
        if ctrl is not None:
            await ctrl.wait_if_paused()
        with Tracer(
            bus,
            EventKind.NODE_STARTED,
            task_id=state.task_id,
            workflow=workflow_name,
            node=name,
        ):
            return await node(state)

    traced.__name__ = name
    return traced
