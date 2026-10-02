"""WebSocket execution transport protocol (§44).

Protocol:
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

On connect:
  Sends current status + all events since 0 (or since client's after_seq on resync) - reconnect-safe (§44).

Live updates:
  Polls service.events_since(execution_id, last_seq) every 0.5s and pushes new events.
  (Simple and deterministic for this server; a production worker deployment would push via pub/sub).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from typing import Any, Callable

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

__all__ = [
    "ConnectionManager",
    "build_ws_router",
    "router",
    "ws_execution",
]


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
) -> APIRouter:
    ws_router = APIRouter()
    mgr = manager or _default_manager

    @ws_router.websocket("/ws/executions/{execution_id}")
    async def ws_endpoint(websocket: WebSocket, execution_id: str) -> None:
        svc = service_factory() if callable(service_factory) else service_factory
        await ws_execution(websocket, execution_id, svc, manager=mgr)

    return ws_router


router = build_ws_router(None)
