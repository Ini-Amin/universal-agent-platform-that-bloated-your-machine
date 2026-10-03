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
import re
import shutil
import subprocess
import threading
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
from uap.workflows.learning import WORKFLOW_NAME as LEARNING_WORKFLOW_NAME
from uap.workflows.learning import LearningWorkflow
from uap.workflows.scope import ScopeGate, ScopeRuleError
from uap.sandbox import run_code_in_sandbox

from uap.mcp.config import BUG_BOUNTY_MCP_CONFIG, MCPServerConfig
from uap.server.mcp_lifecycle import start_mcp_tools, stop_mcp_tools

from starlette.exceptions import WebSocketException
from starlette.staticfiles import StaticFiles

from uap.server.auth import (
    auth_enabled,
    close_unauthorized_websocket,
    get_current_identity,
    require_token,
    scrub_token_from_logs,
)
from uap.server.ws import CanvasHub, build_ws_router
from uap.server.run_control import drop_control, get_control, run_controls

__all__ = ["create_app", "EventStream"]

from uap.observability.errors import log_swallowed_exception, redact_text

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"
_UI_DIR = Path(__file__).resolve().parents[3] / "ui"
#: Domains the server can actually execute. Everything else is a clarification.
_EXECUTABLE = frozenset({Domain.RESEARCH, Domain.BBP, Domain.LEARNING})

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

#: Editor launcher (§Editor). ``UAP_EDITOR_BIN`` overrides the safe default;
#: the value is always invoked as an argv element, never through a shell.
DEFAULT_EDITOR_BIN = "zed"
#: Largest file the workspace file API will read into an editor buffer.
MAX_WORKSPACE_FILE_BYTES = 1024 * 1024
#: Largest number of entries one directory listing returns (honest truncation).
MAX_WORKSPACE_LISTING = 500

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

class CanvasCommandsRequest(BaseModel):
    """A batch of UI commands to deliver to each connected browser canvas."""

    commands: list[dict[str, Any]]


class WorkspaceCreateRequest(BaseModel):
    """Body of ``POST /api/workspaces``."""

    name: str
    description: str | None = None

class WorkspaceMemberAddRequest(BaseModel):
    """Body of ``POST /api/workspaces/{id}/members``."""

    user_id: str
    role: str = "member"

class UserCreateRequest(BaseModel):
    """Body of ``POST /api/users``."""

    name: str
    email: str | None = None
    role: str = "member"
    id: str | None = None

class WorkspaceFromTemplateRequest(BaseModel):
    """Body of ``POST /api/workspaces/from-template``.

    ``template`` is a catalog template name; ``input`` is the user's request in
    the shape the template documents (see ``GET /api/templates/{name}``).
    """

    template: str
    input: str = ""
    user_id: str | None = None

class StartProgramRequest(BaseModel):
    """Body of ``POST /api/providers/{name}/programs/{program_id}/start``."""

    scope: str | None = None
    user_id: str | None = None
    workspace_id: str | None = None

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

class ForkRequest(BaseModel):
    """Body of ``POST /api/executions/{id}/fork`` (both fields optional)."""

    from_seq: int | None = None
    label: str = "fork"

class RerunRequest(BaseModel):
    """Body of ``POST /api/executions/{id}/rerun``.

    ``input`` is an optional override: omitted (or ``None``) reuses the source
    execution's original input verbatim; present runs the new text through the
    same workflow.
    """

    input: str | None = None


class SandboxRunRequest(BaseModel):
    """Body of ``POST /api/sandbox/run``."""

    language: str
    code: str
    timeout_s: float | None = None
    max_memory_mb: int | None = None
    max_output_bytes: int | None = None


class SandboxRunResponse(BaseModel):
    """Response of ``POST /api/sandbox/run``."""

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: float
    truncated: bool

class EditorOpenRequest(BaseModel):
    """Body of ``POST /api/editor/open``."""

    path: str
    root: str | None = None

class WorkspaceWriteRequest(BaseModel):
    """Body of ``POST /api/workspace/files`` (save a file back to disk)."""

    path: str
    content: str
    root: str | None = None

class EditorStatusResponse(BaseModel):
    """Response of ``GET /api/editor/status``."""

    available: bool
    binary: str
    reason: str

class WorkspaceRootAddRequest(BaseModel):
    """Body of ``POST /api/workspace/roots``."""

    path: str
    label: str | None = None
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
    views: list[dict[str, Any]] = field(default_factory=list)

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


def _workspace_root() -> Path:
    """The default workspace root: ``UAP_WORKSPACE_DIR`` or the process cwd.

    Mirrors :mod:`uap.server.ws` so the terminal and the file browser agree on
    exactly which directory the workspace is.
    """
    env = os.environ.get("UAP_WORKSPACE_DIR")
    if env:
        return Path(env).resolve()
    return Path.cwd().resolve()

def _workspace_roots(workspace_dir: Path) -> dict[str, Path]:
    """Configure ordered, resolved roots; reject unusable roots at startup."""
    setting = os.environ.get("UAP_WORKSPACE_ROOTS")
    if setting is None:
        configured = {"workspace": workspace_dir, "home": Path.home()}
    else:
        configured = {}
        for item in setting.split(","):
            identifier, separator, location = item.strip().partition(":")
            if not separator or not identifier or not location.strip():
                raise ValueError(f"invalid UAP_WORKSPACE_ROOTS entry {item!r}; expected id:path")
            if identifier in configured:
                raise ValueError(f"duplicate workspace root id {identifier!r}")
            configured[identifier] = Path(location.strip()).expanduser()
    for identifier, path in configured.items():
        resolved = path.resolve()
        if not resolved.is_dir():
            raise ValueError(f"workspace root {identifier!r} is missing or not a directory: {path}")
        configured[identifier] = resolved
    return configured

def _load_persisted_roots(
    roots: dict[str, Path],
    labels: dict[str, str],
    store_path: Path,
) -> None:
    if not store_path.is_file():
        return
    try:
        data = json.loads(store_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return
        for item in data:
            if not isinstance(item, dict):
                continue
            path_str = item.get("path")
            if not path_str or not isinstance(path_str, str):
                continue
            p = Path(path_str).resolve()
            if not p.is_dir():
                continue
            if p in roots.values():
                continue
            label = str(item.get("label") or p.name or str(p))
            base_id = str(
                item.get("id")
                or re.sub(r"[^a-zA-Z0-9_-]", "_", label.lower()).strip("_")
                or "root"
            )
            root_id = base_id
            counter = 1
            while root_id in roots:
                counter += 1
                root_id = f"{base_id}_{counter}"
            roots[root_id] = p
            labels[root_id] = label
    except Exception as exc:
        logger.warning(
            "Failed to load persisted workspace roots from %s: %s", store_path, exc
        )


def _save_persisted_roots(
    roots: dict[str, Path],
    labels: dict[str, str],
    builtins: set[str],
    store_path: Path,
) -> None:
    to_save = []
    for rid, rpath in roots.items():
        if rid in builtins:
            continue
        to_save.append({
            "id": rid,
            "label": labels.get(rid, rid),
            "path": str(rpath),
        })
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store_path.write_text(json.dumps(to_save, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning(
            "Failed to save persisted workspace roots to %s: %s", store_path, exc
        )

def confine_workspace_path(path: str, root: Path) -> Path:
    """Resolve ``path`` under ``root`` and refuse anything that escapes it.

    Same containment discipline as the artifact store: the candidate is
    *resolved* first (which follows symlinks) and only then checked, so a
    symlink pointing outside the root, a ``..`` traversal, and an absolute
    path outside the root are all refused. A NUL byte is refused outright.
    The empty string and ``.`` mean the root itself.
    """
    if not isinstance(path, str):
        raise ValueError("path must be a string")
    if "\x00" in path:
        raise ValueError("path must not contain NUL bytes")
    raw = path.strip()
    if raw in ("", ".", "./"):
        return root
    candidate = Path(raw)
    target = (
        candidate.resolve()
        if candidate.is_absolute()
        else (root / candidate).resolve()
    )
    if not target.is_relative_to(root):
        raise PermissionError(f"path {path!r} escapes the workspace root")
    return target

def _rel_workspace_path(target: Path, root: Path) -> str:
    """The POSIX-style, root-relative form of ``target`` (never absolute)."""
    if target == root:
        return ""
    return target.relative_to(root).as_posix()

def _editor_binary_path(binary: str) -> str | None:
    """Resolve the editor binary to an executable path, or ``None``.

    A bare name is looked up on ``PATH``; a path with a separator is accepted
    only when it points at an executable file. No shell is involved.
    """
    if os.sep in binary or (os.altsep and os.altsep in binary):
        candidate = Path(binary)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    return shutil.which(binary)

def editor_status() -> dict[str, Any]:
    """Whether the configured editor can be launched, and why not when it cannot."""
    binary = (os.environ.get("UAP_EDITOR_BIN") or DEFAULT_EDITOR_BIN).strip() or DEFAULT_EDITOR_BIN
    resolved = _editor_binary_path(binary)
    if resolved is not None:
        return {
            "available": True,
            "binary": binary,
            "reason": f"'{binary}' is installed and executable",
        }
    return {
        "available": False,
        "binary": binary,
        "reason": (
            f"'{binary}' was not found on PATH; install it or set UAP_EDITOR_BIN "
            "to the editor binary"
        ),
    }

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
    workspace_dir: Path | str | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        bus: observability bus to reuse; a fresh :class:`EventBus` is created
            when omitted. A :class:`JsonlSink` and :class:`MemorySink` are always
            attached under ``runs_dir``.
        runs_dir: filesystem root for event logs and artifacts (default
            ``./data/runs``).
        workspace_dir: filesystem root the editor file browser and the "Open in
            Zed" launcher are confined to. When omitted, ``UAP_WORKSPACE_DIR``
            (if set) is used, else the process cwd -- the same default the
            terminal uses.
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
    learning = LearningWorkflow()
    _instrument(learning, event_bus)

    registry = WorkflowRegistry()
    registry.register(RESEARCH_WORKFLOW_NAME, research)
    registry.register(BBP_WORKFLOW_NAME, bbp)
    registry.register(LEARNING_WORKFLOW_NAME, learning)
    registry.register(Domain.LEARNING.value, learning)
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
    #: Root the editor's file browser and "Open in Zed" launcher are confined
    #: to. Explicit param wins; otherwise UAP_WORKSPACE_DIR / cwd (same default
    #: as the terminal). Stored resolved so every containment check is absolute.
    app.state.workspace_dir = (
        Path(workspace_dir).resolve() if workspace_dir is not None else _workspace_root()
    )
    app.state.workspace_roots = _workspace_roots(app.state.workspace_dir)
    app.state.builtin_workspace_roots = set(app.state.workspace_roots.keys())
    app.state.workspace_root_labels = {k: k for k in app.state.workspace_roots}
    app.state.workspace_roots_file = runs_root.parent / "workspace_roots.json"
    _load_persisted_roots(
        app.state.workspace_roots,
        app.state.workspace_root_labels,
        app.state.workspace_roots_file,
    )
    app.state.gate = gate
    app.state.entry = entry
    app.state.router = router
    app.state.model_router = model_router
    app.state.research = research
    app.state.bbp = bbp
    app.state.learning = learning
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
            elif name in (LEARNING_WORKFLOW_NAME, "LearningWorkflow"):
                wf = learning
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

    app.state.canvas_hub = CanvasHub()
    app.include_router(build_ws_router(
        _ws_service_factory, workspace_dir=app.state.workspace_dir,
        canvas_hub=app.state.canvas_hub,
    ))

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
                node_views = state.data.get("node_views") or []
                if node_views:
                    record.views = node_views
                    factory = (
                        getattr(getattr(app.state, "slice", None), "_session_factory", None)
                        or getattr(getattr(app.state, "service", None), "_session_factory", None)
                    )
                    if factory is not None:
                        try:
                            import uuid as _uuid
                            from uap.db.engine import session_scope
                            from uap.db.models.definitions import VersionStatus
                            from uap.db.models.execution import ExecutionStatus
                            from uap.db.repositories import (
                                DefinitionRepository,
                                EventRepository,
                                ExecutionRepository,
                            )
                            from uap.views.store import NODE_VIEW_EVENT_KIND, NodeViewStore

                            with session_scope(factory) as session:
                                repo = DefinitionRepository(session)
                                def_row = repo.get_definition_by_name("learning-workflow")
                                if def_row is None:
                                    def_row = repo.create_definition(name="learning-workflow")
                                versions = repo.list_versions(def_row.id)
                                v_row = versions[0] if versions else repo.create_version(
                                    def_row.id,
                                    version=1,
                                    status=VersionStatus.ACTIVE,
                                    spec={"graph_ref": "workflow:learning@v1"},
                                )
                                corr_id = _uuid.UUID(str(record.task_id))
                                exec_repo = ExecutionRepository(session)
                                exec_row = exec_repo.get_by_correlation_id(corr_id)
                                if exec_row is None:
                                    exec_row = exec_repo.create(
                                        v_row.id,
                                        correlation_id=corr_id,
                                        status=ExecutionStatus.COMPLETED,
                                        requested_by=record.requested_by,
                                    )
                                store = NodeViewStore(session)
                                event_repo = EventRepository(session)
                                for item in node_views:
                                    store.publish(exec_row.id, item["node_id"], item["view"])
                                    event_repo.append(
                                        exec_row.id,
                                        NODE_VIEW_EVENT_KIND,
                                        node=item["node_id"],
                                        payload={"view": item["view"]},
                                    )
                        except Exception as exc:
                            log_swallowed_exception(
                                logger,
                                exc,
                                "failed to persist workflow views to database",
                                level=logging.WARNING,
                                task_id=record.task_id,
                            )
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
            _fold_slice_result(record, result)
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
            if record.status != "paused":
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

    def _fold_slice_result(record: RunRecord, result: Any) -> None:
        if result.task_id and result.task_id != record.task_id:
            runs[result.task_id] = record
            record.task_id = result.task_id
        if result.execution_id:
            runs[result.execution_id] = record
        record.workspace_id = result.workspace_id or record.workspace_id
        record.context = result.context
        record.evidence_source = _evidence_source_for(record.task_id, app)
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
        if getattr(result, "execution_status", "") == "paused":
            record.status = "paused"
        else:
            record.status = "failed" if result.error else "completed"
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

    async def _execute_slice_resume(
        record: RunRecord | None,
        execution_id: str,
        slice_runner: Any,
    ) -> None:
        rec = record or runs.get(execution_id)
        if rec is not None:
            rec.status = "running"
            event_bus.emit_kind(
                EventKind.TASK_STARTED,
                task_id=rec.task_id,
                workflow=rec.workflow,
                data={"domain": rec.domain, "goal": getattr(rec, "input", "")},
            )
        try:
            result = await asyncio.to_thread(
                slice_runner.resume,
                execution_id,
            )
            if rec is not None:
                _fold_slice_result(rec, result)
        except Exception as exc:
            if rec is not None:
                rec.status = "failed"
                rec.error = f"{type(exc).__name__}: {redact_text(str(exc))}"
            log_swallowed_exception(
                logger,
                exc,
                "slice resume execution failed",
                level=logging.ERROR,
                task_id=getattr(rec, "task_id", execution_id),
            )
        finally:
            if rec is not None:
                if rec.status != "paused":
                    drop_control(rec.task_id)
                event_bus.emit_kind(
                    EventKind.TASK_FINISHED,
                    task_id=rec.task_id,
                    workflow=rec.workflow,
                    data={"status": rec.status},
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
        force_domain: str | None = None,
    ) -> dict[str, Any]:
        """Entry -> Router -> workflow -> dispatch; return the task payload.

        Shared by ``POST /tasks`` and ``POST /api/workspaces/from-template`` so
        a template-started run passes through exactly the same gate as a manual
        one: no duplicated routing, no second execution path. Returns either
        ``{"status": "clarification", "question": ...}`` or the accepted-task
        payload.

        ``force_domain`` pins the workflow to a known domain instead of letting
        the Entry Workflow re-classify the input. ``POST /api/executions/{id}/
        rerun`` uses it so a changed input still runs through the SAME workflow
        the user is reusing, rather than silently hopping to another domain.
        """
        outcome = entry.run(UserRequest(raw_input=raw_input, user_id=user_id))
        if outcome.needs_clarification or outcome.spec is None:
            return {"status": "clarification", "question": outcome.question}

        spec = outcome.spec
        if force_domain is not None:
            try:
                spec = spec.model_copy(update={"domain": Domain(force_domain)})
            except ValueError:
                pass  # unknown domain: fall through to normal routing

        decision = router.route(spec)
        if decision.domain not in _EXECUTABLE or decision.target is None:
            return {
                "status": "clarification",
                "question": (
                    f"The '{decision.domain}' domain is not available yet. "
                    f"{_AVAILABLE_DOMAINS_EXAMPLE}"
                ),
            }

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
    async def create_task(request: Request, body: TaskRequest) -> dict[str, Any]:
        identity = get_current_identity(request)
        if identity is not None:
            if identity.role == "viewer":
                raise HTTPException(
                    status_code=403, detail="forbidden: viewers cannot create tasks"
                )
            effective_user_id = identity.user_id
        else:
            if auth_enabled():
                raise HTTPException(status_code=401, detail="unauthorized")
            effective_user_id = body.user_id

        return await _start_task(
            body.input, user_id=effective_user_id, workspace_id=body.workspace_id
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
        svc = getattr(app.state, "service", None)
        if svc is not None and hasattr(svc, "status"):
            try:
                st = svc.status(task_id)
                if st and st.get("status") == "paused":
                    record.status = "paused"
                elif st and st.get("status") == "completed" and record.status != "completed" and record.output:
                    record.status = "completed"
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "failed to query execution status from service",
                    level=logging.DEBUG,
                )
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
        LEARNING_WORKFLOW_NAME: learning,
        "ResearchWorkflow": research,
        "BBPWorkflow": bbp,
        "LearningWorkflow": learning,
    }

    def _graph_for_workflow(wf_name: str) -> dict[str, Any] | None:
        wf = _workflow_map.get(wf_name)
        if wf is not None and hasattr(wf, "graph_spec"):
            return wf.graph_spec()
        return None

    def _domain_from_ref(workflow_ref: str | None) -> str | None:
        """Map a stored ``workflow_ref`` (e.g. ``"research@v1"``) to a domain.

        Used by rerun to pin the reused workflow's domain. Returns ``None`` when
        the ref is absent or unrecognized, letting normal routing decide.
        """

        if not workflow_ref:
            return None
        name = str(workflow_ref).split("@", 1)[0].strip().lower()
        for domain in (Domain.RESEARCH, Domain.BBP, Domain.LEARNING):
            if domain.value == name:
                return domain.value
        # Tolerate class-style refs ("ResearchWorkflow") the Library may store.
        for domain in (Domain.RESEARCH, Domain.BBP, Domain.LEARNING):
            if domain.value in name:
                return domain.value
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
                        for wf, expected_key in (
                            (learning, "learning"),
                            (bbp, "bbp"),
                            (research, "research"),
                        ):
                            if ref and expected_key not in ref:
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
        record = runs.get(execution_id)
        if record is not None and record.status in ("completed", "failed", "cancelled"):
            return {"status": "not_running"}
        svc = getattr(app.state, "service", None)
        if svc is not None and hasattr(svc, "status"):
            try:
                st = svc.status(execution_id)
                if st and st.get("status") in ("completed", "failed", "cancelled"):
                    return {"status": "not_running"}
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "failed to query execution status from service",
                    level=logging.DEBUG,
                )
        # Durable path (delegate to service).
        if svc is not None and hasattr(svc, "pause_request"):
            try:
                svc.pause_request(execution_id)
                if record is not None:
                    record.status = "paused"
                ctrl = run_controls().get(execution_id)
                if ctrl is not None:
                    ctrl.pause()
                return {"status": "paused"}
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "service.pause_request failed; falling back to in-memory control",
                    level=logging.WARNING,
                    execution_id=str(execution_id),
                )
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
        record = runs.get(execution_id)
        if record is not None and record.status in ("completed", "failed", "cancelled"):
            return {"status": "not_running"}
        svc = getattr(app.state, "service", None)
        if svc is not None and hasattr(svc, "status"):
            try:
                st = svc.status(execution_id)
                if st and st.get("status") in ("completed", "failed", "cancelled"):
                    return {"status": "not_running"}
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "failed to query execution status from service",
                    level=logging.DEBUG,
                )
        slice_runner = getattr(app.state, "slice", None)
        if svc is not None and hasattr(svc, "resume"):
            try:
                svc.resume(execution_id)
                if record is not None:
                    record.status = "running"
                ctrl = run_controls().get(execution_id)
                if ctrl is not None:
                    ctrl.resume()
                if slice_runner is not None and hasattr(slice_runner, "resume"):
                    task = asyncio.create_task(
                        _execute_slice_resume(record, execution_id, slice_runner)
                    )
                    app.state.background.add(task)
                    task.add_done_callback(app.state.background.discard)
                return {"status": "running"}
            except Exception as exc:
                log_swallowed_exception(
                    logger,
                    exc,
                    "service.resume failed; falling back to in-memory control",
                    level=logging.WARNING,
                    execution_id=str(execution_id),
                )

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

        record = runs.get(execution_id)
        if not found and record is not None:
            found = True
            views = getattr(record, "views", [])
        elif not views and record is not None and getattr(record, "views", None):
            views = getattr(record, "views", [])

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

    # -- /api/executions/{id}: reuse (fork / rerun) + delete --------------- #
    #
    # Reuse-before-delete: a user who is done with a run can BRANCH it (fork),
    # REPLAY it with new input (rerun), and only then DELETE the original. All
    # three accept EITHER id form the UI might hold (the durable row id OR the
    # client-facing correlation id) and sit behind the app-level token gate.

    def _execution_actor(request: Request) -> Any:
        """Return the resolved identity, failing closed when auth is on."""

        identity = get_current_identity(request)
        if identity is None and auth_enabled():
            raise HTTPException(status_code=401, detail="unauthorized")
        return identity

    def _require_execution_access(request: Request, requested_by: str | None) -> Any:
        """Authorize the caller against one execution's requester.

        Rule (mirrored in the DELETE docstring): an admin may act on ANY
        execution; a member may act only on a run they requested; a viewer may
        not mutate runs at all. A run with no recorded requester is treated as
        operator-owned and is admin-only once auth is on.
        """

        identity = _execution_actor(request)
        if identity is None:
            return None  # auth disabled: single-user local mode
        if identity.role == "viewer":
            raise HTTPException(
                status_code=403, detail="forbidden: viewers cannot manage runs"
            )
        if identity.role == "admin":
            return identity
        if requested_by is not None and requested_by == identity.user_id:
            return identity
        raise HTTPException(
            status_code=403,
            detail="forbidden: a member may only manage their own runs",
        )

    def _remove_artifact_dir(*candidate_ids: str | None) -> dict[str, Any]:
        """Remove artifact directories for the given ids; report honestly.

        Artifacts live at ``<runs_root>/artifacts/<task_id>/`` and are NOT
        covered by the DB cascade, so a delete that skipped them would leak
        files. Returns ``{"removed": [...], "left_behind": [...]}`` — a
        directory that exists but could not be removed is reported, never
        silently ignored.
        """

        removed: list[str] = []
        left_behind: list[str] = []
        for candidate in candidate_ids:
            if not candidate:
                continue
            directory = runs_root / "artifacts" / str(candidate)
            if not directory.is_dir():
                continue
            try:
                shutil.rmtree(directory)
                removed.append(str(candidate))
            except OSError as exc:  # permissions, busy files, ...
                log_swallowed_exception(
                    logger,
                    exc,
                    "artifact directory could not be removed",
                    level=logging.WARNING,
                    execution_id=str(candidate),
                )
                left_behind.append(str(candidate))
        return {"removed": removed, "left_behind": left_behind}

    async def _drive_durable_run(
        record: RunRecord | None,
        execution_id: str,
        *,
        workflow_ref: str | None = None,
    ) -> bool:
        """Drive a durable (slice) execution to completion; ``True`` if started.

        Used to make a fork actually RUN. The slice's resume path loads the
        row's pinned graph + copied checkpoint state, so a forked execution
        continues from the branch point instead of restarting. Returns
        ``False`` when no slice is wired (the caller must then run it), so the
        route can say so rather than hand back a dead id.
        """

        slice_runner = getattr(app.state, "slice", None)
        if slice_runner is None or not hasattr(slice_runner, "resume"):
            return False
        if app.state.run_inline:
            await _execute_slice_resume(record, execution_id, slice_runner)
        else:
            task = asyncio.create_task(
                _execute_slice_resume(record, execution_id, slice_runner)
            )
            app.state.background.add(task)
            task.add_done_callback(app.state.background.discard)
        return True

    @app.post("/api/executions/{execution_id}/fork")
    async def fork_execution_route(
        request: Request, execution_id: str, body: ForkRequest | None = None
    ) -> dict[str, Any]:
        """Fork a finished execution into a NEW, RUNNABLE branch.

        Body (both optional): ``from_seq`` (checkpoint sequence to branch from;
        default latest) and ``label``. Returns the new execution id AND its
        correlation id.

        Authorization: the same rule as DELETE — an admin may fork ANY
        execution; a member only their own (``requested_by``); a viewer may not
        fork at all. The fork inherits the source's ``requested_by``.

        The fork is STARTED here (through the same slice/worker path that drives
        ``POST /tasks`` and ``/resume``), so the returned id is live. When no
        durable slice is wired, ``started`` is ``false`` and ``next_step``
        states plainly that the caller must run it — the id is never silently
        dead.
        """

        body = body or ForkRequest()
        svc = getattr(app.state, "service", None)
        if svc is None or not hasattr(svc, "fork_execution"):
            raise HTTPException(
                status_code=503, detail="durable execution service unavailable"
            )
        try:
            source = svc.source_for_reuse(execution_id)
        except LookupError:
            raise HTTPException(status_code=404, detail=f"unknown execution {execution_id!r}")
        _require_execution_access(request, source["requested_by"])

        try:
            forked = svc.fork_execution(
                execution_id, from_seq=body.from_seq, label=body.label
            )
        except LookupError:
            raise HTTPException(status_code=404, detail=f"unknown execution {execution_id!r}")

        record = RunRecord(
            task_id=forked["correlation_id"] or forked["execution_id"],
            domain=source.get("workflow_ref") or "",
            workflow=source.get("workflow_ref") or "",
            input=source.get("input") or "",
            workspace_id=source.get("workspace_id"),
            requested_by=source.get("requested_by"),
        )
        runs[record.task_id] = record
        started = await _drive_durable_run(record, forked["execution_id"])

        payload: dict[str, Any] = {
            "execution_id": forked["execution_id"],
            "correlation_id": forked["correlation_id"],
            "status": forked["status"],
            "forked_from": source["execution_id"],
            "from_seq": body.from_seq,
            "label": body.label,
            "started": started,
        }
        if not started:
            payload["next_step"] = (
                "not started: no durable slice is wired, so this fork will not "
                f"run on its own. Start it with POST /api/executions/"
                f"{forked['execution_id']}/resume once a worker is available."
            )
        return payload

    @app.post("/api/executions/{execution_id}/rerun")
    async def rerun_execution_route(
        request: Request, execution_id: str, body: RerunRequest | None = None
    ) -> dict[str, Any]:
        """Reuse a finished execution's workflow to run it again — the "adapt" case.

        Body: optional ``input`` override. Omitted -> the original input is
        reused verbatim; present -> the new text runs through the SAME workflow
        (the source's domain is pinned so a changed input cannot silently hop to
        another workflow). Returns the new task/execution payload.

        Authorization: same rule as DELETE/fork — admin any, member own only,
        viewer forbidden.
        """

        body = body or RerunRequest()
        svc = getattr(app.state, "service", None)
        if svc is None or not hasattr(svc, "source_for_reuse"):
            raise HTTPException(
                status_code=503, detail="durable execution service unavailable"
            )
        try:
            source = svc.source_for_reuse(execution_id)
        except LookupError:
            raise HTTPException(status_code=404, detail=f"unknown execution {execution_id!r}")
        _require_execution_access(request, source["requested_by"])

        raw_input = body.input if body.input is not None else source.get("input") or ""
        if not raw_input.strip():
            raise HTTPException(
                status_code=422,
                detail="no input to rerun: the source execution has no reusable input",
            )

        result = await _start_task(
            raw_input,
            user_id=source.get("requested_by"),
            workspace_id=source.get("workspace_id"),
            force_domain=_domain_from_ref(source.get("workflow_ref")),
        )
        return {
            "rerun_of": source["execution_id"],
            "input": raw_input,
            "reused_original_input": body.input is None,
            **result,
        }

    @app.delete("/api/executions/{execution_id}")
    async def delete_execution_route(request: Request, execution_id: str) -> dict[str, Any]:
        """Delete ONE finished execution and everything that belongs to it.

        Accepts EITHER the durable row id OR the client-facing correlation id.

        REFUSES a live run (``pending`` / ``running`` / ``paused`` /
        ``awaiting_approval``) with **409** and a clear reason: deleting a
        running execution out from under its worker is a data-integrity bug.

        Authorization: behind the app-level token gate. With a user key, an
        ADMIN may delete ANY execution; a MEMBER may delete only their OWN runs
        (matching ``requested_by``); a viewer may not delete at all.

        Returns the execution id and the child row counts removed by the
        ``ON DELETE CASCADE`` (events / checkpoints / decision traces / node
        views). Artifacts live outside the database and are removed explicitly:
        the response reports the directories removed and, if any could not be
        removed, lists them under ``artifacts.left_behind`` rather than leaking
        them silently.
        """

        svc = getattr(app.state, "service", None)
        if svc is None or not hasattr(svc, "delete_execution"):
            raise HTTPException(
                status_code=503, detail="durable execution service unavailable"
            )
        # Resolve + authorize BEFORE mutating: a member must not be able to
        # delete someone else's run and only then be told "forbidden".
        try:
            source = svc.source_for_reuse(execution_id)
        except LookupError:
            raise HTTPException(status_code=404, detail=f"unknown execution {execution_id!r}")
        _require_execution_access(request, source.get("requested_by"))

        try:
            result = svc.delete_execution(execution_id)
        except LookupError:
            raise HTTPException(status_code=404, detail=f"unknown execution {execution_id!r}")
        except ValueError as exc:
            # A live run: 409 Conflict, with the reason.
            raise HTTPException(status_code=409, detail=redact_text(str(exc)))

        artifacts = _remove_artifact_dir(
            result.get("correlation_id"), result.get("execution_id")
        )
        # The in-memory run record (if any) must not outlive the durable row.
        for key in (result.get("execution_id"), result.get("correlation_id")):
            if key:
                runs.pop(key, None)
                drop_control(str(key))

        return {
            "execution_id": result["execution_id"],
            "correlation_id": result.get("correlation_id"),
            "deleted": result["deleted"],
            "artifacts": artifacts,
        }

    @app.post("/api/canvas/commands")
    async def send_canvas_commands(
        request: Request, body: CanvasCommandsRequest,
    ) -> dict[str, int]:
        """Broadcast validated UI commands to all currently connected tabs."""
        identity = get_current_identity(request)
        if identity is not None and identity.role == "viewer":
            raise HTTPException(status_code=403, detail="forbidden: viewers cannot control canvases")
        for command in body.commands:
            kind = command.get("type")
            if kind == "open_file":
                if not isinstance(command.get("path"), str) or not command["path"]:
                    raise HTTPException(status_code=422, detail="open_file needs a path")
                if "root" in command and not isinstance(command["root"], str):
                    raise HTTPException(status_code=422, detail="root must be a string")
            elif kind == "add_view":
                if not isinstance(command.get("spec"), dict):
                    raise HTTPException(status_code=422, detail="add_view needs a spec")
            elif kind == "focus_view":
                if not isinstance(command.get("id"), str):
                    raise HTTPException(status_code=422, detail="focus_view needs an id")
            else:
                raise HTTPException(status_code=422, detail=f"unsupported canvas command: {kind}")
        return {"delivered": await app.state.canvas_hub.broadcast(body.commands)}

    @app.get("/api/canvas/state")
    async def get_canvas_state() -> dict[str, list[dict[str, Any]]]:
        """Return the most recent hello from each connected browser tab."""
        return {"clients": app.state.canvas_hub.states()}

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
            "owner_id": getattr(ws, "owner_id", None),
        }

    @app.get("/api/workspaces")
    async def list_workspaces(request: Request) -> list[dict[str, Any]]:
        try:
            from uap.db.engine import session_scope
            from uap.db.repositories import WorkspaceRepository

            identity = get_current_identity(request)
            with session_scope() as session:
                repo = WorkspaceRepository(session)
                if identity is not None and identity.role != "admin":
                    items = repo.list(user_id=identity.user_id, is_admin=False)
                else:
                    items = repo.list()
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
    async def create_workspace(
        request: Request, body: WorkspaceCreateRequest
    ) -> dict[str, Any]:
        identity = get_current_identity(request)
        if identity is not None and identity.role == "viewer":
            raise HTTPException(
                status_code=403, detail="forbidden: viewers cannot create workspaces"
            )
        try:
            from uap.workspace.store import WorkspaceStore
            from uap.workspace.model import Workspace
            from uap.db.engine import session_scope
            from uap.db.repositories import WorkspaceMemberRepository
            from uap.db.models.workspace import WorkspaceRow

            owner = identity.user_id if identity and identity.user_id != "operator" else None
            with session_scope() as session:
                store = WorkspaceStore(session=session)
                ws = store.create(
                    Workspace(
                        id=str(uuid.uuid4()),
                        name=body.name,
                        description=body.description or "",
                    )
                )
                if owner:
                    row = session.get(WorkspaceRow, ws.id)
                    if row is not None:
                        row.owner_id = owner
                    mem_repo = WorkspaceMemberRepository(session)
                    mem_repo.add_member(ws.id, owner, role="owner")
                session.commit()
                res = _workspace_json(ws)
                if owner:
                    res["owner_id"] = owner
                return res
        except HTTPException:
            raise
        except Exception as exc:
            log_swallowed_exception(
                logger,
                exc,
                "workspace creation failed",
                level=logging.ERROR,
                name=body.name,
            )
            raise HTTPException(
                status_code=503,
                detail=f"workspace creation failed: {redact_text(str(exc))}",
            )

    @app.post("/api/workspaces/{workspace_id}/members")
    async def add_workspace_member_route(
        request: Request,
        workspace_id: str,
        body: WorkspaceMemberAddRequest,
    ) -> dict[str, Any]:
        """Add a member to a workspace."""
        identity = get_current_identity(request)
        if identity is not None and identity.role == "viewer":
            raise HTTPException(
                status_code=403, detail="forbidden: viewers cannot manage members"
            )

        from uap.db.engine import session_scope
        from uap.db.repositories import (
            UserRepository,
            WorkspaceMemberRepository,
            WorkspaceRepository,
        )

        with session_scope() as session:
            ws_repo = WorkspaceRepository(session)
            ws = ws_repo.get(workspace_id)
            if ws is None:
                raise HTTPException(
                    status_code=404, detail=f"workspace {workspace_id!r} not found"
                )

            # Check permissions: admin or workspace owner / admin member
            if identity is not None and identity.role != "admin":
                is_owner = ws.owner_id == identity.user_id
                mem_repo = WorkspaceMemberRepository(session)
                membership = (
                    mem_repo.get_member(workspace_id, identity.user_id)
                    if identity.user_id
                    else None
                )
                is_ws_admin = membership is not None and membership.role in (
                    "owner",
                    "admin",
                )
                if not (is_owner or is_ws_admin):
                    raise HTTPException(
                        status_code=403,
                        detail="forbidden: workspace admin or owner required",
                    )

            user_repo = UserRepository(session)
            target_user = user_repo.get(body.user_id)
            if target_user is None:
                raise HTTPException(
                    status_code=404, detail=f"user {body.user_id!r} not found"
                )

            mem_repo = WorkspaceMemberRepository(session)
            member = mem_repo.add_member(workspace_id, body.user_id, role=body.role)
            session.commit()
            return {
                "workspace_id": member.workspace_id,
                "user_id": member.user_id,
                "role": member.role,
                "created_at": member.created_at.isoformat()
                if member.created_at
                else None,
            }

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
        request: Request,
        body: WorkspaceFromTemplateRequest,
    ) -> dict[str, Any]:
        """Create a workspace from a template AND start its task.

        Returns both the workspace and the started task (or the clarification
        question when the input could not be routed) so the UI can go straight
        to the canvas. The task runs through exactly the same gate as
        ``POST /tasks`` -- this endpoint does not add a second execution path.
        """
        identity = get_current_identity(request)
        if identity is not None and identity.role == "viewer":
            raise HTTPException(
                status_code=403, detail="forbidden: viewers cannot create workspaces"
            )

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
        effective_user = (
            identity.user_id
            if identity and identity.user_id != "operator"
            else body.user_id
        )

        # 1. Create the workspace, pinned to the template's workflow ref.
        try:
            from uap.db.engine import session_scope
            from uap.db.repositories import WorkspaceMemberRepository, WorkspaceRepository

            ws_id = str(uuid.uuid4())
            with session_scope() as session:
                ws_repo = WorkspaceRepository(session)
                ws = ws_repo.create(
                    type(
                        "WS",
                        (),
                        {
                            "id": ws_id,
                            "name": detail["title"],
                            "description": detail["description"],
                            "root_path": "",
                            "status": "active",
                            "settings": {
                                "template": body.template,
                                "tags": list(detail["tags"]),
                            },
                            "default_workflow_refs": [workflow_ref],
                            "owner_id": effective_user,
                        },
                    )()
                )
                if effective_user:
                    mem_repo = WorkspaceMemberRepository(session)
                    mem_repo.add_member(ws_id, effective_user, role="owner")
                session.commit()
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
            body.input, user_id=effective_user, workspace_id=workspace_json["id"]
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


    # -- /api/users ------------------------------------------------------ #

    def _user_json(user: Any, api_key: str | None = None) -> dict[str, Any]:
        data = {
            "id": str(user.id),
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "is_active": user.is_active,
            "created_at": user.created_at.isoformat()
            if getattr(user, "created_at", None)
            else None,
            "last_seen_at": user.last_seen_at.isoformat()
            if getattr(user, "last_seen_at", None)
            else None,
        }
        if api_key is not None:
            data["api_key"] = api_key
        return data

    def _check_admin(request: Request) -> None:
        identity = get_current_identity(request)
        if identity is None:
            if auth_enabled():
                raise HTTPException(status_code=401, detail="unauthorized")
            return
        if identity.role != "admin":
            raise HTTPException(status_code=403, detail="forbidden: admin required")

    @app.post("/api/users")
    async def create_user_route(
        request: Request, body: UserCreateRequest
    ) -> dict[str, Any]:
        """Create a user and return their raw API key (shown ONCE). Requires admin."""
        _check_admin(request)
        if body.role not in ("admin", "member", "viewer"):
            raise HTTPException(
                status_code=422,
                detail=f"invalid role {body.role!r}; must be one of admin, member, viewer",
            )

        from uap.users import generate_api_key, hash_api_key
        from uap.db.engine import session_scope
        from uap.db.repositories import UserRepository

        raw_key = generate_api_key()
        key_hash = hash_api_key(raw_key)

        with session_scope() as session:
            repo = UserRepository(session)
            user = repo.create(
                name=body.name,
                role=body.role,
                email=body.email,
                api_key_hash=key_hash,
                user_id=body.id,
            )
            session.commit()
            return _user_json(user, api_key=raw_key)

    @app.get("/api/users")
    async def list_users_route(request: Request) -> list[dict[str, Any]]:
        """List users without sensitive keys. Requires admin."""
        _check_admin(request)
        from uap.db.engine import session_scope
        from uap.db.repositories import UserRepository

        with session_scope() as session:
            repo = UserRepository(session)
            users = repo.list()
            return [_user_json(u) for u in users]

    @app.post("/api/users/{user_id}/rotate")
    async def rotate_user_key_route(request: Request, user_id: str) -> dict[str, Any]:
        """Rotate an API key. Old key stops working immediately. Requires admin."""
        _check_admin(request)
        from uap.users import generate_api_key, hash_api_key
        from uap.db.engine import session_scope
        from uap.db.repositories import UserRepository

        raw_key = generate_api_key()
        key_hash = hash_api_key(raw_key)

        with session_scope() as session:
            repo = UserRepository(session)
            user = repo.get(user_id)
            if user is None:
                raise HTTPException(
                    status_code=404, detail=f"user {user_id!r} not found"
                )
            user = repo.rotate_key(user_id, key_hash)
            session.commit()
            return _user_json(user, api_key=raw_key)

    @app.post("/api/users/{user_id}/deactivate")
    async def deactivate_user_route(request: Request, user_id: str) -> dict[str, Any]:
        """Deactivate a user. Key stops working immediately. Requires admin."""
        _check_admin(request)
        from uap.db.engine import session_scope
        from uap.db.repositories import UserRepository

        with session_scope() as session:
            repo = UserRepository(session)
            user = repo.get(user_id)
            if user is None:
                raise HTTPException(
                    status_code=404, detail=f"user {user_id!r} not found"
                )
            user = repo.deactivate(user_id)
            session.commit()
            return _user_json(user)
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

    # -- /api/providers -------------------------------------------------- #

    @app.get("/api/providers")
    async def list_providers() -> list[dict[str, Any]]:
        """Return configured platforms and honest reasons for unavailable ones."""
        from uap.providers import get_default_registry
        registry = get_default_registry()
        return [status.model_dump(mode="json") for status in registry.list_statuses()]

    @app.get("/api/providers/{name}/programs")
    async def list_provider_programs(name: str) -> list[dict[str, Any]]:
        """Return normalized program list for a provider, or degrade honestly."""
        from uap.providers import ProviderUnavailableError, get_default_registry
        registry = get_default_registry()
        source = registry.get(name)
        if source is None:
            raise HTTPException(status_code=404, detail=f"unknown provider: {name}")
        try:
            programs = await asyncio.to_thread(source.list_programs)
            return [p.model_dump(mode="json") for p in programs]
        except ProviderUnavailableError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.warning("Failed to fetch programs for provider '%s': %s", name, exc)
            raise HTTPException(status_code=502, detail=f"provider error: {exc}")

    @app.post("/api/providers/{name}/programs/{program_id}/start")
    async def start_provider_program(
        name: str,
        program_id: str,
        body: StartProgramRequest | None = None,
    ) -> dict[str, Any]:
        """Start a bug bounty task for the program's scope."""
        from uap.providers import (
            ProviderUnavailableError,
            StartProgramRequest,
            get_default_registry,
        )
        registry = get_default_registry()
        source = registry.get(name)
        if source is None:
            raise HTTPException(status_code=404, detail=f"unknown provider: {name}")
        if not source.available():
            raise HTTPException(status_code=400, detail=source.unavailable_reason())

        scope: str | None = None
        user_id: str | None = None
        workspace_id: str | None = None
        if body is not None:
            scope = body.scope
            user_id = body.user_id
            workspace_id = body.workspace_id

        if not scope:
            try:
                scope = await asyncio.to_thread(source.get_primary_scope, program_id)
            except ProviderUnavailableError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                logger.warning("Failed to resolve scope for '%s': %s", program_id, exc)
                raise HTTPException(
                    status_code=404, detail=f"could not resolve scope for {program_id}"
                )

        if not scope:
            raise HTTPException(status_code=404, detail=f"no scope found for {program_id}")

        task_input = f"bug bounty on {scope}"
        return await _start_task(task_input, user_id=user_id, workspace_id=workspace_id)

    # -- /api/sandbox/run ------------------------------------------------ #

    @app.post("/api/sandbox/run", response_model=SandboxRunResponse)
    async def run_sandbox_code(
        request: Request, body: SandboxRunRequest
    ) -> dict[str, Any]:
        """Execute arbitrary user code safely inside the isolated process sandbox.

        Why auth matters more here than elsewhere:
        This endpoint executes arbitrary user-supplied code via the sandbox. Unlike
        read-only or constrained workflow endpoints, code execution has direct access
        to CPU, memory, and system calls within the child process boundary. Even with
        OS-level resource limits (RLIMIT_AS, RLIMIT_CPU, wall-clock timeout, output
        caps), unauthenticated access would expose the host to denial-of-service,
        resource exhaustion, or potential sandbox escape exploits. Therefore, strict
        authentication is mandatory before any execution request reaches the sandbox.
        """
        identity = get_current_identity(request)
        if identity is not None and identity.role == "viewer":
            raise HTTPException(
                status_code=403, detail="forbidden: viewers cannot execute code"
            )

        lang = body.language.strip().lower()
        if lang != "python":
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported language '{body.language}'. Supported languages: python",
            )

        timeout_s = body.timeout_s if body.timeout_s is not None else 5.0
        if timeout_s <= 0 or timeout_s > 300.0:
            raise HTTPException(
                status_code=400,
                detail="timeout_s must be between 0.05 and 300.0 seconds",
            )

        max_memory_mb = body.max_memory_mb if body.max_memory_mb is not None else 512
        max_output_bytes = (
            body.max_output_bytes if body.max_output_bytes is not None else 65536
        )

        return await run_code_in_sandbox(
            code=body.code,
            language=lang,
            timeout_s=timeout_s,
            max_memory_mb=max_memory_mb,
            max_output_bytes=max_output_bytes,
        )

    # -- /api/editor ----------------------------------------------------- #
    #
    # "Open in Zed" and the workspace file browser. Zed is a native GUI app:
    # it cannot be embedded in a browser, so these routes let the server --
    # which runs on the same machine as the user -- open the real editor on
    # the user's screen. Both routes are confined to ``workspace_dir`` with the
    # same resolve-then-check discipline the artifact store uses.

    def _ws_root(request: Request, root_id: str | None = None) -> Path:
        roots = request.app.state.workspace_roots
        selected = root_id or "workspace"
        if selected not in roots:
            raise HTTPException(
                status_code=400,
                detail=f"unknown workspace root {selected!r}; valid ids: {', '.join(roots)}",
            )
        return roots[selected]

    @app.get("/api/workspace/roots")
    async def list_workspace_roots(request: Request) -> dict[str, Any]:
        labels = getattr(request.app.state, "workspace_root_labels", {})
        return {
            "roots": [
                {
                    "id": identifier,
                    "label": labels.get(identifier, identifier),
                    "path": str(path),
                }
                for identifier, path in request.app.state.workspace_roots.items()
            ]
        }

    def _require_editor_writer(
        request: Request,
        detail: str = "forbidden: viewers cannot open files in the editor",
    ) -> None:
        """Auth gate for editor side effects: viewers are refused."""
        identity = get_current_identity(request)
        if identity is not None and identity.role == "viewer":
            raise HTTPException(
                status_code=403,
                detail=detail,
            )

    @app.post("/api/workspace/roots")
    async def add_workspace_root(
        request: Request, body: WorkspaceRootAddRequest
    ) -> dict[str, Any]:
        """Register a new workspace root directory at runtime."""
        _require_editor_writer(
            request, detail="forbidden: viewers cannot modify workspace roots"
        )
        if not body.path or not isinstance(body.path, str):
            raise HTTPException(
                status_code=400, detail="path must be a non-empty string"
            )
        if "\x00" in body.path:
            raise HTTPException(status_code=400, detail="path must not contain NUL bytes")
        candidate = Path(body.path.strip())
        if not candidate.is_absolute():
            raise HTTPException(
                status_code=400, detail=f"path must be absolute: {body.path!r}"
            )
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail=f"could not resolve path: {exc}"
            ) from exc

        if not resolved.exists():
            raise HTTPException(
                status_code=400, detail=f"directory does not exist: {body.path!r}"
            )
        if not resolved.is_dir():
            raise HTTPException(
                status_code=400, detail=f"path is not a directory: {body.path!r}"
            )

        roots = request.app.state.workspace_roots
        labels = getattr(request.app.state, "workspace_root_labels", {})
        builtins = getattr(request.app.state, "builtin_workspace_roots", set())

        # Dedupe by resolved path
        for rid, rpath in roots.items():
            if rpath == resolved:
                return {
                    "id": rid,
                    "label": labels.get(rid, rid),
                    "path": str(resolved),
                }

        label = (
            body.label.strip()
            if body.label and body.label.strip()
            else resolved.name or str(resolved)
        )
        base_id = re.sub(r"[^a-zA-Z0-9_-]", "_", label.lower()).strip("_") or "root"
        root_id = base_id
        counter = 1
        while root_id in roots:
            counter += 1
            root_id = f"{base_id}_{counter}"

        roots[root_id] = resolved
        labels[root_id] = label

        roots_file = getattr(request.app.state, "workspace_roots_file", None)
        if roots_file:
            _save_persisted_roots(roots, labels, builtins, roots_file)

        return {
            "id": root_id,
            "label": label,
            "path": str(resolved),
        }

    @app.delete("/api/workspace/roots/{root_id}")
    async def remove_workspace_root(
        request: Request, root_id: str
    ) -> dict[str, Any]:
        """Remove an added workspace root. Built-in roots cannot be deleted."""
        _require_editor_writer(
            request, detail="forbidden: viewers cannot modify workspace roots"
        )
        roots = request.app.state.workspace_roots
        builtins = getattr(request.app.state, "builtin_workspace_roots", set())
        labels = getattr(request.app.state, "workspace_root_labels", {})

        if root_id not in roots:
            raise HTTPException(
                status_code=404, detail=f"unknown workspace root {root_id!r}"
            )
        if root_id in builtins:
            raise HTTPException(
                status_code=400,
                detail=f"cannot delete built-in workspace root {root_id!r}",
            )

        del roots[root_id]
        labels.pop(root_id, None)

        roots_file = getattr(request.app.state, "workspace_roots_file", None)
        if roots_file:
            _save_persisted_roots(roots, labels, builtins, roots_file)

        return {"deleted": True, "id": root_id}

    @app.get("/api/editor/status", response_model=EditorStatusResponse)
    async def get_editor_status() -> dict[str, Any]:
        """Whether "Open in Zed" can work here, and why not when it cannot."""
        return editor_status()

    @app.post("/api/editor/open")
    async def open_in_editor(
        request: Request, body: EditorOpenRequest
    ) -> dict[str, Any]:
        """Launch the configured editor (default ``zed``) on a workspace file.

        Security properties:

        * the path is resolved under ``workspace_dir`` and a ``..`` traversal,
          an absolute path outside the root, or a symlink pointing out is
          refused (``400``);
        * the binary is a single argv element -- the path is NEVER interpolated
          into a shell string, so a filename with ``;``/``$(...)`` is inert;
        * viewers are refused (``403``);
        * the process is spawned detached and the request returns immediately;
          the server never waits for the editor to exit.
        """
        _require_editor_writer(request)
        root = _ws_root(request, body.root)
        try:
            target = confine_workspace_path(body.path, root)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not target.exists():
            raise HTTPException(
                status_code=404, detail=f"file not found in workspace: {body.path!r}"
            )

        status = editor_status()
        if not status["available"]:
            raise HTTPException(status_code=409, detail=status["reason"])
        binary = _editor_binary_path(status["binary"])
        assert binary is not None  # status said available

        rel = _rel_workspace_path(target, root)
        try:
            # argv list, shell=False (default): the path can never be parsed as
            # a command. start_new_session detaches the child from the server's
            # process group so it survives (and is not killed with) the server.
            proc = subprocess.Popen(  # noqa: S603 - argv form, no shell
                [binary, str(target)],
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"could not launch editor: {redact_text(str(exc))}"
            ) from exc

        # Reap the child when it exits WITHOUT blocking the request or the event
        # loop. ``start_new_session`` detaches it, but if nobody ever waits on
        # it the kernel keeps a zombie in our process table (seen live: the
        # launched ``zed`` shim sat as ``Zs``). A tiny background thread does
        # the ``wait()``; it is daemonised and dies with the server.
        def _reap(p: subprocess.Popen) -> None:
            try:
                p.wait()
            except Exception as exc:  # pragma: no cover - defensive
                log_swallowed_exception(
                    logger, exc, "failed to reap a launched editor process"
                )

        threading.Thread(target=_reap, args=(proc,), daemon=True).start()

        return {
            "launched": True,
            "binary": status["binary"],
            "pid": proc.pid,
            "path": rel,
        }

    # -- /api/workspace/files -------------------------------------------- #

    @app.get("/api/workspace/files")
    async def list_workspace_files(
        request: Request, path: str = "", root: str = "workspace"
    ) -> dict[str, Any]:
        """List one directory inside the workspace root (contained).

        ``path`` is relative to the workspace root; the empty string lists the
        root. Directories are marked with ``is_dir`` so the UI can navigate.
        A ``..`` traversal, an absolute path outside the root, or a symlink
        pointing out is refused with ``400``.
        """
        root = _ws_root(request, root)
        try:
            target = confine_workspace_path(path, root)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not target.exists():
            raise HTTPException(status_code=404, detail=f"no such path: {path!r}")
        if not target.is_dir():
            raise HTTPException(status_code=400, detail=f"not a directory: {path!r}")

        entries: list[dict[str, Any]] = []
        truncated = False
        try:
            children = sorted(
                target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            )
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"could not read directory: {redact_text(str(exc))}"
            ) from exc
        for child in children:
            if len(entries) >= MAX_WORKSPACE_LISTING:
                truncated = True
                break
            # ``child`` comes from the already-contained directory. Resolve it
            # so a symlink pointing outside the root is listed as such (and is
            # not navigable) instead of silently leading the browser out.
            try:
                resolved = child.resolve()
            except OSError:
                continue
            contained = resolved.is_relative_to(root)
            is_dir = contained and child.is_dir()
            size: int | None = None
            if contained and child.is_file():
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
            entries.append(
                {
                    "name": child.name,
                    "path": _rel_workspace_path(resolved if contained else child, root)
                    if contained
                    else child.name,
                    "is_dir": is_dir,
                    "size": size,
                    "contained": contained,
                }
            )

        return {
            "path": _rel_workspace_path(target, root),
            "root": str(root),
            "entries": entries,
            "truncated": truncated,
        }

    @app.get("/api/workspace/file")
    async def read_workspace_file(
        request: Request, path: str, root: str = "workspace"
    ) -> dict[str, Any]:
        """Read one UTF-8 text file inside the selected workspace root."""
        root = _ws_root(request, root)
        try:
            target = confine_workspace_path(path, root)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail=f"no such file: {path!r}")

        try:
            size = target.stat().st_size
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"could not stat file: {redact_text(str(exc))}"
            ) from exc
        if size > MAX_WORKSPACE_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"file is {size} bytes; the editor reads at most "
                    f"{MAX_WORKSPACE_FILE_BYTES} bytes"
                ),
            )
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=415, detail="file is not UTF-8 text and cannot be opened"
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"could not read file: {redact_text(str(exc))}"
            ) from exc

        return {
            "path": _rel_workspace_path(target, root),
            "size": size,
            "content": content,
        }

    @app.post("/api/workspace/file")
    async def write_workspace_file(
        request: Request, body: WorkspaceWriteRequest
    ) -> dict[str, Any]:
        """Write a UTF-8 text file inside the workspace root (contained).

        This is what makes "edit the project" real: the buffer is persisted to
        the file the browser opened, not downloaded. Viewers are refused, and
        the same containment rules apply as on read.
        """
        _require_editor_writer(request)
        root = _ws_root(request, body.root)
        try:
            target = confine_workspace_path(body.path, root)
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if target == root or target.is_dir():
            raise HTTPException(status_code=400, detail=f"not a file path: {body.path!r}")

        data = body.content.encode("utf-8")
        if len(data) > MAX_WORKSPACE_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"content is {len(data)} bytes; the editor writes at most "
                    f"{MAX_WORKSPACE_FILE_BYTES} bytes"
                ),
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            raise HTTPException(
                status_code=503, detail=f"could not write file: {redact_text(str(exc))}"
            ) from exc

        return {
            "path": _rel_workspace_path(target, root),
            "bytes": len(data),
        }

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
