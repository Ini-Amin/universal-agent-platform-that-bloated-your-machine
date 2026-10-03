"""Canvas bridge integration: live state, broadcast fan-out, and auth."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from uap.server import create_app


def test_canvas_hello_commands_and_disconnect(tmp_path, monkeypatch):
    monkeypatch.delenv("UAP_API_TOKEN", raising=False)
    app = create_app(runs_dir=tmp_path / "runs", workspace_dir=tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/canvas/state").json() == {"clients": []}
        with client.websocket_connect("/ws/canvas") as first:
            first.send_json({"type": "hello", "views": [
                {"id": "editor-1", "type": "editor", "path": "README.md", "root": "repo"},
            ], "focused": "editor-1"})
            # The state read runs on the same TestClient portal after hello.
            state = client.get("/api/canvas/state").json()
            assert state == {"clients": [{"views": [
                {"id": "editor-1", "type": "editor", "path": "README.md", "root": "repo"},
            ], "focused": "editor-1"}]}
            with client.websocket_connect("/ws/canvas") as second:
                commands = [
                    {"type": "open_file", "path": "README.md", "root": "repo"},
                    {"type": "focus_view", "id": "editor-1"},
                ]
                response = client.post("/api/canvas/commands", json={"commands": commands})
                assert response.status_code == 200
                assert response.json() == {"delivered": 2}
                for ws in (first, second):
                    assert [ws.receive_json() for _ in commands] == commands
                assert len(client.get("/api/canvas/state").json()["clients"]) == 1
            assert len(client.get("/api/canvas/state").json()["clients"]) == 1
        assert client.get("/api/canvas/state").json() == {"clients": []}
        assert client.post("/api/canvas/commands", json={"commands": [
            {"type": "add_view", "spec": {"type": "terminal"}},
        ]}).json() == {"delivered": 0}


def test_canvas_rejects_invalid_batch_without_delivering(tmp_path, monkeypatch):
    monkeypatch.delenv("UAP_API_TOKEN", raising=False)
    with TestClient(create_app(runs_dir=tmp_path / "runs")) as client:
        assert client.post("/api/canvas/commands", json={"commands": [
            {"type": "open_file", "path": "ok.py"}, {"type": "bogus"},
        ]}).status_code == 422


def test_canvas_authentication(tmp_path, monkeypatch):
    monkeypatch.setenv("UAP_API_TOKEN", "canvas-test-token")
    with TestClient(create_app(runs_dir=tmp_path / "runs")) as client:
        assert client.get("/api/canvas/state").status_code == 401
        assert client.post("/api/canvas/commands", json={"commands": []}).status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/canvas") as ws:
                ws.receive_json()
        with client.websocket_connect("/ws/canvas?token=canvas-test-token") as ws:
            ws.send_json({"type": "hello", "views": [], "focused": None})
            assert client.get("/api/canvas/state", headers={
                "Authorization": "Bearer canvas-test-token",
            }).json() == {"clients": [{"views": [], "focused": None}]}
