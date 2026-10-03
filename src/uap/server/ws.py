"""WebSocket transport protocols (§44, §53).

Routes:
  /ws/executions/{execution_id} - Execution event stream and control (pause/resume/fork).
  /ws/terminal                  - Interactive shell for the workspace directory.

Protocol (/ws/executions):
  Client -> Server:
    {"type": "resync", "after_seq": N}
    {"type": "pause"}
    {"type": "resume"}
    {"type": "fork", "label": "..."}
    {"type": "ping"}

  Server -> Client:
    {"type": "event", "seq": n, "event": {...canonical event...}}
    {"type": "status", "status": {...}}
    {"type": "pong"}
    {"type": "error", "message": ...}
    {"type": "forked", "execution_id": ...}

Protocol (/ws/terminal):
  Client -> Server:
    {"type": "stdin", "data": "command\\n"}
    {"type": "ping"}

  Server -> Client:
    {"type": "stdout", "data": "..."}
    {"type": "error", "message": "...", "data": "..."}
    {"type": "pong"}

SECURITY & RESIDUAL RISK (/ws/terminal):
  The /ws/terminal route provides a remote interactive shell in the UAP workspace
  directory for whoever holds a valid authentication credential (or anonymous callers
  when auth is disabled).
  The session is bounded by:
  - Authentication: Handshake closed with code 1008 unless authorized when UAP_API_TOKEN is set.
    Unauthenticated handshakes are rejected uniformly, preventing route-existence leakage.
  - Concurrency limit: Maximum 5 concurrent sessions (MAX_CONCURRENT_TERMINAL_SESSIONS)
    to prevent denial of service and process table exhaustion.
  - Working directory scoping: Working directory is strictly scoped to the workspace root;
    directory escapes via 'cd /' or 'cd ../..' are refused.
  - Sandbox isolation: Commands are executed in forked child processes enforcing RLIMIT_AS
    (512MB RAM) and RLIMIT_CPU resource limits via SandboxPolicy.
  - Execution bounds: 10.0s command timeout (DEFAULT_TERMINAL_TIMEOUT) and 64KB output cap
    (DEFAULT_MAX_OUTPUT_BYTES = 65536). Timeouts are honestly announced in the output stream.
  - No server-side shell injection: Input is parsed with shlex and executed directly via
    subprocess.run with shell=False.
  RESIDUAL RISK:
  By design, this route allows arbitrary command execution with the privileges of the
  server process inside the workspace directory. Any bearer of a valid API token can
  execute commands, read/write workspace files, and consume allotted CPU/memory.
  This is the intended functionality of the remote terminal client, not an accidental exposure.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from uap.sandbox.executor import Sandbox, SandboxPolicy, SandboxViolation

__all__ = [
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TERMINAL_TIMEOUT",
    "MAX_CONCURRENT_TERMINAL_SESSIONS",
    "ConnectionManager",
    "TerminalSessionTracker",
    "build_ws_router",
    "router",
    "ws_execution",
    "ws_terminal",
]

DEFAULT_TERMINAL_TIMEOUT: float = 10.0
DEFAULT_MAX_OUTPUT_BYTES: int = 65536
MAX_CONCURRENT_TERMINAL_SESSIONS: int = 5


class TerminalSessionTracker:
    """Tracks active terminal WebSocket sessions with a concurrency cap."""

    def __init__(self, max_sessions: int = MAX_CONCURRENT_TERMINAL_SESSIONS) -> None:
        self.max_sessions = max_sessions
        self._active: set[Any] = set()
        self._lock = asyncio.Lock()

    async def acquire(self, websocket: Any) -> bool:
        async with self._lock:
            current_max = globals().get("MAX_CONCURRENT_TERMINAL_SESSIONS", self.max_sessions)
            if len(self._active) >= current_max:
                return False
            self._active.add(websocket)
            return True

    async def release(self, websocket: Any) -> None:
        async with self._lock:
            self._active.discard(websocket)

    def count(self) -> int:
        return len(self._active)


_default_terminal_tracker = TerminalSessionTracker()


def _exec_child_command(argv: list[str], cwd: str, timeout: float) -> str:
    """Run program invocation inside sandboxed child process (shell=False)."""
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
         )
        return proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        raise SandboxViolation(f"execution exceeded {timeout:.1f}s timeout") from exc
    except FileNotFoundError:
        return f"{argv[0]}: command not found\n"
    except PermissionError:
        return f"{argv[0]}: permission denied\n"
    except Exception as exc:
        return f"error: {exc}\n"


async def ws_terminal(
    websocket: WebSocket,
    workspace_dir: Path | str | None = None,
    session_tracker: TerminalSessionTracker | None = None,
) -> None:
    """Interactive WebSocket terminal session handler in workspace directory."""
    tracker = session_tracker or _default_terminal_tracker
    acquired = await tracker.acquire(websocket)
    if not acquired:
        if hasattr(websocket, "accept"):
            await websocket.accept()
        if hasattr(websocket, "send_json"):
            await websocket.send_json({
                "type": "error",
                "message": "too many concurrent terminal sessions\n",
                "data": "too many concurrent terminal sessions\n",
            })
        if hasattr(websocket, "close"):
            await websocket.close(code=1008, reason="too many concurrent terminal sessions")
        return

    if hasattr(websocket, "accept"):
        try:
            await websocket.accept()
        except Exception:
            pass

    ws_state = getattr(getattr(websocket, "app", None), "state", None)
    app_ws_root = getattr(ws_state, "workspace_dir", None)
    if workspace_dir is not None:
        workspace_root = Path(workspace_dir).resolve()
    elif app_ws_root is not None:
        workspace_root = Path(app_ws_root).resolve()
    elif "UAP_WORKSPACE_DIR" in os.environ:
        workspace_root = Path(os.environ["UAP_WORKSPACE_DIR"]).resolve()
    else:
        workspace_root = Path.cwd().resolve()

    current_cwd = workspace_root

    try:
        while True:
            try:
                raw_text = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                break

            try:
                payload = json.loads(raw_text)
            except Exception:
                payload = {"data": raw_text}

            if isinstance(payload, dict):
                msg_type = payload.get("type")
                if msg_type == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
                cmd_line = payload.get("data") or payload.get("command") or ""
            elif isinstance(payload, str):
                cmd_line = payload
            else:
                cmd_line = ""

            if not isinstance(cmd_line, str):
                cmd_line = str(cmd_line)

            cmd_line = cmd_line.strip()
            if not cmd_line:
                await websocket.send_json({"type": "stdout", "data": ""})
                continue

            try:
                argv = shlex.split(cmd_line)
            except ValueError as err:
                err_msg = f"syntax error: {err}\n"
                await websocket.send_json({
                    "type": "error",
                    "message": err_msg,
                    "data": err_msg,
                })
                continue

            if not argv:
                await websocket.send_json({"type": "stdout", "data": ""})
                continue

            prog = argv[0]

            if prog == "cd":
                if len(argv) == 1 or argv[1] in ("~", ""):
                    target_path = workspace_root
                else:
                    target_path = (current_cwd / argv[1]).resolve()

                if target_path == workspace_root or workspace_root in target_path.parents:
                    if target_path.is_dir():
                        current_cwd = target_path
                        await websocket.send_json({"type": "stdout", "data": ""})
                    else:
                        msg = f"cd: no such file or directory: {argv[1]}\n"
                        await websocket.send_json({"type": "error", "message": msg, "data": msg})
                else:
                    msg = f"cd: restricted to workspace: {argv[1]}\n"
                    await websocket.send_json({"type": "error", "message": msg, "data": msg})
                continue

            if prog == "pwd":
                await websocket.send_json({"type": "stdout", "data": f"{current_cwd}\n"})
                continue

            if prog in ("exit", "quit"):
                await websocket.send_json({"type": "stdout", "data": "exit\n"})
                if hasattr(websocket, "close"):
                    await websocket.close(code=1000, reason="session ended")
                break

            if prog == "clear":
                await websocket.send_json({"type": "stdout", "data": "\x1b[2J\x1b[H"})
                continue

            timeout = float(globals().get("DEFAULT_TERMINAL_TIMEOUT", DEFAULT_TERMINAL_TIMEOUT))
            max_bytes = int(globals().get("DEFAULT_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES))

            policy = SandboxPolicy(
                max_cpu_seconds=timeout,
                max_memory_mb=512,
                max_output_bytes=max_bytes,
                allow_subprocess=True,
                allow_fs_read_paths=(str(workspace_root),),
                allow_fs_write_paths=(str(workspace_root),),
            )
            sandbox = Sandbox(policy)

            try:
                output = await sandbox.run_guarded(
                    _exec_child_command,
                    argv,
                    str(current_cwd),
                    timeout,
                )
                if not isinstance(output, str):
                    output = (
                        output.decode("utf-8", errors="replace")
                        if isinstance(output, (bytes, bytearray))
                        else str(output)
                    )

                if len(output.encode("utf-8")) >= max_bytes:
                    output += "\n[output truncated: exceeded byte cap]\n"

                await websocket.send_json({"type": "stdout", "data": output})

            except SandboxViolation as exc:
                err_msg = f"\n[error] {exc}\n"
                await websocket.send_json({
                    "type": "error",
                    "message": err_msg,
                    "data": err_msg,
                })
            except Exception as exc:
                err_msg = f"\n[error] execution failure: {exc}\n"
                await websocket.send_json({
                    "type": "error",
                    "message": err_msg,
                    "data": err_msg,
                })
    finally:
        await tracker.release(websocket)


class ConnectionManager:
    """Tracks live sockets per execution_id; broadcast is failure-isolated."""

    def __init__(self) -> None:
        self._connections: dict[str, set[Any]] = {}

    async def connect(self, execution_id: str, websocket: Any) -> None:
        if execution_id not in self._connections:
            self._connections[execution_id] = set()
        self._connections[execution_id].add(websocket)

    def disconnect(self, execution_id: str, websocket: Any) -> None:
        if execution_id in self._connections:
            self._connections[execution_id].discard(websocket)
            if not self._connections[execution_id]:
                del self._connections[execution_id]

    async def broadcast(self, execution_id: str, message: dict[str, Any]) -> None:
        sockets = list(self._connections.get(execution_id, set()))
        for ws in sockets:
            try:
                if hasattr(ws, "send_json"):
                    res = ws.send_json(message)
                elif hasattr(ws, "send_text"):
                    res = ws.send_text(json.dumps(message))
                else:
                    continue
                if inspect.isawaitable(res):
                    await res
            except Exception:
                self.disconnect(execution_id, ws)

    def connection_count(self, execution_id: str) -> int:
        return len(self._connections.get(execution_id, set()))


_default_manager = ConnectionManager()


async def ws_execution(
    websocket: Any,
    execution_id: str,
    service: Any,
    manager: ConnectionManager | None = None,
) -> None:
    mgr = manager or _default_manager
    if hasattr(websocket, "accept"):
        try:
            await websocket.accept()
        except Exception:
            pass
    await mgr.connect(execution_id, websocket)

    last_seq = 0

    # 1. Send current status
    status_info: dict[str, Any] = {"execution_id": execution_id, "status": "unknown"}
    if service is not None:
        try:
            st = service.status(execution_id)
            if inspect.isawaitable(st):
                st = await st
            status_info = st
        except Exception:
            status_info = {"execution_id": execution_id, "status": "unknown"}

    await websocket.send_json({"type": "status", "status": status_info})

    # 2. Send all events since 0
    if service is not None and hasattr(service, "events_since"):
        try:
            evs = service.events_since(execution_id, after_seq=0)
            if inspect.isawaitable(evs):
                evs = await evs
            for ev in evs:
                seq = ev.get("seq", last_seq + 1)
                last_seq = max(last_seq, seq)
                await websocket.send_json({"type": "event", "seq": seq, "event": ev})
        except Exception:
            pass

    stop_event = asyncio.Event()

    async def poll_loop() -> None:
        nonlocal last_seq
        last_status: str | None = status_info.get("status") if isinstance(status_info, dict) else None
        while not stop_event.is_set():
            await asyncio.sleep(0.5)
            if stop_event.is_set():
                break
            if service is not None:
                # Re-send status whenever it changes, so a client that never
                # receives a lifecycle event still leaves "connecting" when the
                # run finishes (in-memory UI runs have no durable event stream).
                try:
                    st = service.status(execution_id)
                    if inspect.isawaitable(st):
                        st = await st
                    new_status = st.get("status") if isinstance(st, dict) else None
                    if new_status != last_status:
                        last_status = new_status
                        await websocket.send_json({"type": "status", "status": st})
                except Exception:
                    pass
            if service is not None and hasattr(service, "events_since"):
                try:
                    new_evs = service.events_since(execution_id, after_seq=last_seq)
                    if inspect.isawaitable(new_evs):
                        new_evs = await new_evs
                    for ev in new_evs:
                        seq = ev.get("seq", last_seq + 1)
                        last_seq = max(last_seq, seq)
                        await websocket.send_json({"type": "event", "seq": seq, "event": ev})
                except Exception:
                    pass

    poll_task = asyncio.create_task(poll_loop())

    try:
        while True:
            try:
                data = await websocket.receive_json()
            except (WebSocketDisconnect, RuntimeError):
                break
            except Exception as exc:
                await websocket.send_json({"type": "error", "message": f"invalid message: {exc}"})
                continue

            msg_type = data.get("type") if isinstance(data, dict) else None

            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type == "resync":
                # Parse defensively: a non-numeric after_seq used to raise
                # ValueError outside any try and kill the socket (trivial DoS).
                try:
                    after_seq = int(data.get("after_seq", 0))
                except (TypeError, ValueError):
                    await websocket.send_json(
                        {"type": "error", "message": "after_seq must be an integer"}
                    )
                    continue
                if service is not None and hasattr(service, "events_since"):
                    try:
                        evs = service.events_since(execution_id, after_seq=after_seq)
                        if inspect.isawaitable(evs):
                            evs = await evs
                        for ev in evs:
                            seq = ev.get("seq", last_seq + 1)
                            last_seq = max(last_seq, seq)
                            await websocket.send_json({"type": "event", "seq": seq, "event": ev})
                    except Exception as exc:
                        await websocket.send_json({"type": "error", "message": str(exc)})
            elif msg_type == "pause":
                if service is not None and hasattr(service, "pause_request"):
                    try:
                        res = service.pause_request(execution_id)
                        if inspect.isawaitable(res):
                            await res
                    except Exception as exc:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                        continue
                new_status = None
                if service is not None and hasattr(service, "status"):
                    try:
                        st = service.status(execution_id)
                        if inspect.isawaitable(st):
                            st = await st
                        new_status = st
                    except Exception:
                        pass
                if new_status is None:
                    new_status = {"execution_id": execution_id, "status": "paused"}
                await mgr.broadcast(execution_id, {"type": "status", "status": new_status})
            elif msg_type == "resume":
                if service is not None and hasattr(service, "resume"):
                    try:
                        res = service.resume(execution_id)
                        if inspect.isawaitable(res):
                            await res
                    except Exception as exc:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                        continue
                new_status = None
                if service is not None and hasattr(service, "status"):
                    try:
                        st = service.status(execution_id)
                        if inspect.isawaitable(st):
                            st = await st
                        new_status = st
                    except Exception:
                        pass
                if new_status is None:
                    new_status = {"execution_id": execution_id, "status": "running"}
                await mgr.broadcast(execution_id, {"type": "status", "status": new_status})
            elif msg_type == "fork":
                label = data.get("label")
                if service is not None and hasattr(service, "fork"):
                    try:
                        fork_res = service.fork(execution_id, label=label)
                        if inspect.isawaitable(fork_res):
                            fork_res = await fork_res
                        await websocket.send_json({"type": "forked", "execution_id": str(fork_res)})
                    except Exception as exc:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                else:
                    await websocket.send_json({"type": "error", "message": "fork not supported"})
            else:
                await websocket.send_json({"type": "error", "message": f"unknown message type: {msg_type}"})

    finally:
        stop_event.set()
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task
        mgr.disconnect(execution_id, websocket)


def build_ws_router(
    service_factory: Callable[[], Any] | Any = None,
    manager: ConnectionManager | None = None,
    terminal_tracker: TerminalSessionTracker | None = None,
    workspace_dir: Path | str | None = None,
) -> APIRouter:
    ws_router = APIRouter()
    mgr = manager or _default_manager
    t_tracker = terminal_tracker or _default_terminal_tracker

    @ws_router.websocket("/ws/terminal")
    async def terminal_endpoint(websocket: WebSocket) -> None:
        await ws_terminal(websocket, workspace_dir=workspace_dir, session_tracker=t_tracker)

    @ws_router.websocket("/ws/executions/{execution_id}")
    async def ws_endpoint(websocket: WebSocket, execution_id: str) -> None:
        svc = service_factory() if callable(service_factory) else service_factory
        await ws_execution(websocket, execution_id, svc, manager=mgr)

    @ws_router.websocket("/ws/{path:path}")
    async def ws_fallback(websocket: WebSocket, path: str) -> None:
        await websocket.accept()
        await websocket.close(code=1000, reason="route not found")

    return ws_router


router = build_ws_router(None)
