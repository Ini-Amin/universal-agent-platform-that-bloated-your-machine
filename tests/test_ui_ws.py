"""Tests for the visual canvas UI and WebSocket transport (§44, §52–56)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from uap.server import create_app
from uap.server.ws import ConnectionManager


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    return create_app(
        runs_dir=tmp_path / "runs",
        run_inline=True,
        heartbeat_interval=0.05,
    )


@pytest.fixture()
def client(app: FastAPI):
    with TestClient(app) as test_client:
        yield test_client


# 1. GET /ui/ serves the IDE shell (contains "canvas" and the four pane markers)
def test_ui_serves_ide_shell(client: TestClient) -> None:
    res = client.get("/ui/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    text = res.text.lower()
    assert "canvas" in text
    assert "sidebar" in text
    assert "inspector" in text
    assert "console" in text


# 2. GET /ui/js/graph-canvas.js is served and contains "WorkflowGraph" or "nodes"
def test_ui_graph_canvas_js_served(client: TestClient) -> None:
    res = client.get("/ui/js/graph-canvas.js")
    assert res.status_code == 200
    assert "WorkflowGraph" in res.text or "nodes" in res.text


# 3. GET /ui/js/undo.js served
def test_ui_undo_js_served(client: TestClient) -> None:
    res = client.get("/ui/js/undo.js")
    assert res.status_code == 200
    assert "createUndoStack" in res.text


# 4. Existing GET / still works (old UI untouched)
def test_legacy_ui_served_at_legacy_path(client: TestClient) -> None:
    """The old single-page UI moved to /legacy/ (canvas IDE is at /)."""
    res = client.get("/legacy/")
    assert res.status_code == 200
    assert "Universal Agent Platform" in res.text


# 5. GET /api/library returns 200 with a list
def test_get_library_endpoint(client: TestClient) -> None:
    res = client.get("/api/library")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)


# 6. GET /api/executions/{unknown}/graph -> 404
def test_get_unknown_execution_graph_404(client: TestClient) -> None:
    random_id = str(uuid.uuid4())
    res = client.get(f"/api/executions/{random_id}/graph")
    assert res.status_code == 404


# 7. GET /api/executions/{id}/traces -> 404 unknown; 200 empty list for a known execution
def test_get_execution_traces(client: TestClient) -> None:
    random_id = str(uuid.uuid4())
    res_unknown = client.get(f"/api/executions/{random_id}/traces")
    assert res_unknown.status_code == 404

    # Create known task
    task_res = client.post("/tasks", json={"input": "research best practices", "user_id": "tester"}).json()
    task_id = task_res["task_id"]

    res_known = client.get(f"/api/executions/{task_id}/traces")
    assert res_known.status_code == 200
    assert isinstance(res_known.json(), list)


# 8. WebSocket connect to /ws/executions/{id} -> receives a status message
def test_ws_connect_receives_status(client: TestClient) -> None:
    eid = str(uuid.uuid4())
    with client.websocket_connect(f"/ws/executions/{eid}") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "status"
        assert "status" in msg


# 9. WS resync after_seq -> receives only later events (or empty)
def test_ws_resync_after_seq(app: FastAPI) -> None:
    class FakeService:
        def status(self, eid: str):
            return {"status": "running"}

        def events_since(self, eid: str, after_seq: int = 0):
            all_events = [
                {"seq": 1, "kind": "node_started", "node": "start"},
                {"seq": 2, "kind": "node_finished", "node": "start"},
                {"seq": 3, "kind": "node_started", "node": "agent"},
            ]
            return [e for e in all_events if e["seq"] > after_seq]

    app.state.service = FakeService()
    with TestClient(app) as test_client:
        with test_client.websocket_connect("/ws/executions/test-resync-id") as ws:
            status_msg = ws.receive_json()
            assert status_msg["type"] == "status"
            # Receive initial events
            e1 = ws.receive_json()
            assert e1["seq"] == 1
            e2 = ws.receive_json()
            assert e2["seq"] == 2
            e3 = ws.receive_json()
            assert e3["seq"] == 3

            # Send resync with after_seq = 2
            ws.send_json({"type": "resync", "after_seq": 2})
            resynced = ws.receive_json()
            assert resynced["type"] == "event"
            assert resynced["seq"] == 3


# 10. WS ping -> pong
def test_ws_ping_pong(client: TestClient) -> None:
    with client.websocket_connect("/ws/executions/test-ping-id") as ws:
        _ = ws.receive_json()  # initial status
        ws.send_json({"type": "ping"})
        msg = ws.receive_json()
        assert msg["type"] == "pong"


# 11. WS pause action -> service.pause_request called (spy service) + status broadcast
def test_ws_pause_action_calls_service_and_broadcasts(app: FastAPI) -> None:
    called = []

    class SpyService:
        def status(self, eid: str):
            return {"status": "paused" if called else "running"}

        def events_since(self, eid: str, after_seq: int = 0):
            return []

        def pause_request(self, eid: str):
            called.append(eid)

    app.state.service = SpyService()
    with TestClient(app) as test_client:
        with test_client.websocket_connect("/ws/executions/test-pause-id") as ws:
            initial = ws.receive_json()
            assert initial["type"] == "status"

            ws.send_json({"type": "pause"})
            msg = ws.receive_json()
            assert msg["type"] == "status"
            assert msg["status"]["status"] == "paused"
            assert called == ["test-pause-id"]


# 12. WS unknown message type -> error message (no crash, connection stays open)
def test_ws_unknown_message_type(client: TestClient) -> None:
    with client.websocket_connect("/ws/executions/test-err-id") as ws:
        _ = ws.receive_json()  # initial status
        ws.send_json({"type": "unsupported_foobar"})
        err = ws.receive_json()
        assert err["type"] == "error"
        assert "unknown message type" in err["message"]

        # Connection is still open: verify with ping
        ws.send_json({"type": "ping"})
        pong = ws.receive_json()
        assert pong["type"] == "pong"


# 13. ConnectionManager broadcast to two sockets both receive (use the manager directly with fakes)
@pytest.mark.anyio
async def test_connection_manager_broadcast_to_two_sockets() -> None:
    mgr = ConnectionManager()

    class FakeSocket:
        def __init__(self):
            self.received = []

        async def send_json(self, msg):
            self.received.append(msg)

    ws1 = FakeSocket()
    ws2 = FakeSocket()
    eid = "exec-test-123"

    await mgr.connect(eid, ws1)
    await mgr.connect(eid, ws2)
    assert mgr.connection_count(eid) == 2

    payload = {"type": "status", "status": "running"}
    await mgr.broadcast(eid, payload)

    assert ws1.received == [payload]
    assert ws2.received == [payload]


# 14. ConnectionManager disconnect removes; broadcast to zero sockets is a no-op
@pytest.mark.anyio
async def test_connection_manager_disconnect_and_zero_sockets() -> None:
    mgr = ConnectionManager()

    class FakeSocket:
        def __init__(self):
            self.received = []

        async def send_json(self, msg):
            self.received.append(msg)

    ws1 = FakeSocket()
    eid = "exec-test-456"

    await mgr.connect(eid, ws1)
    assert mgr.connection_count(eid) == 1

    mgr.disconnect(eid, ws1)
    assert mgr.connection_count(eid) == 0

    # Broadcast to zero sockets: no-op, no exception
    await mgr.broadcast(eid, {"type": "event"})
    assert ws1.received == []


# 15. Static mount missing ui/ dir -> app still boots (monkeypatch path)
def test_static_mount_missing_ui_dir_boots_cleanly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    non_existent = tmp_path / "does_not_exist_ui"
    monkeypatch.setattr("uap.server.app._UI_DIR", non_existent)
    app = create_app(run_inline=True)
    with TestClient(app) as test_client:
        # Neither mount exists when the ui/ directory is missing; the legacy
        # route is still available.
        res_ui = test_client.get("/ui/")
        assert res_ui.status_code == 404
        res_legacy = test_client.get("/legacy")
        assert res_legacy.status_code == 200


# 16. Known task graph returns canonical graph model (§55)
def test_get_known_task_graph(client: TestClient) -> None:
    task_res = client.post("/tasks", json={"input": "research best practices", "user_id": "tester"}).json()
    task_id = task_res["task_id"]

    res = client.get(f"/api/executions/{task_id}/graph")
    assert res.status_code == 200
    data = res.json()
    assert "nodes" in data
    assert "edges" in data
    assert isinstance(data["nodes"], list)
    assert len(data["nodes"]) >= 1
