"""Tests for the WebSocket terminal backend (/ws/terminal).

Security properties under test:
1. Authenticated: Rejects handshakes without valid token when UAP_API_TOKEN is set (code 1008).
   Does not leak route existence.
2. Bounded: Enforces command timeout (honest stream error), output byte cap, and concurrent session limit.
3. Scoped: Working directory rooted in workspace directory; escapes via 'cd /' are refused.
4. Honest: Timeout kills the command and announces the failure in the stream.
5. No server-side shell injection: Input parsed via shlex and executed as program invocation.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from uap.server import create_app

TOKEN = "testtoken123"


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    return create_app(
        runs_dir=tmp_path / "runs",
        run_inline=True,
        heartbeat_interval=0.05,
    )


@pytest.fixture()
def no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UAP_API_TOKEN", raising=False)


@pytest.fixture()
def with_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAP_API_TOKEN", TOKEN)


# --------------------------------------------------------------------------- #
# 1. Auth rejection & acceptance
# --------------------------------------------------------------------------- #

def test_terminal_ws_without_token_is_rejected_1008(app: FastAPI, with_auth: None) -> None:
    """When auth is enabled, handshake without ?token= is rejected with 1008."""
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws/terminal") as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008


def test_terminal_ws_with_wrong_token_is_rejected_1008(app: FastAPI, with_auth: None) -> None:
    """Handshake with invalid ?token= is rejected with 1008."""
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws/terminal?token=badtoken") as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008


def test_terminal_ws_rejection_does_not_leak_route_existence(app: FastAPI, with_auth: None) -> None:
    """Rejection code is identical between /ws/terminal and a nonexistent route."""
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc1:
            with client.websocket_connect("/ws/terminal") as ws:
                ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as exc2:
            with client.websocket_connect("/ws/completely_nonexistent_xyz") as ws:
                ws.receive_json()
        assert exc1.value.code == exc2.value.code == 1008


def test_terminal_ws_with_token_connects(app: FastAPI, with_auth: None) -> None:
    """Handshake with valid ?token= connects and can run commands."""
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/terminal?token={TOKEN}") as ws:
            ws.send_json({"type": "stdin", "data": "echo auth_ok\n"})
            msg = ws.receive_json()
            assert "auth_ok" in msg.get("data", "")


# --------------------------------------------------------------------------- #
# 2. Command round-trip (unauthenticated default)
# --------------------------------------------------------------------------- #

def test_terminal_ws_normal_command_roundtrip(app: FastAPI, no_auth: None) -> None:
    """Command is executed and stdout is streamed back in the protocol expected by stage.js."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws/terminal") as ws:
            ws.send_json({"type": "stdin", "data": "echo hello world\n"})
            msg = ws.receive_json()
            assert msg.get("type") in ("stdout", "data")
            assert "hello world" in msg.get("data", "")


def test_terminal_ws_command_not_found(app: FastAPI, no_auth: None) -> None:
    """Non-existent commands return a clear error message rather than crashing."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws/terminal") as ws:
            ws.send_json({"type": "stdin", "data": "nonexistent_executable_12345\n"})
            msg = ws.receive_json()
            assert "command not found" in (msg.get("data", "") or msg.get("message", ""))


# --------------------------------------------------------------------------- #
# 3. Command timeout & honesty
# --------------------------------------------------------------------------- #

def test_terminal_ws_command_timeout_honesty(app: FastAPI, no_auth: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Commands exceeding timeout are killed and stream honestly announces the timeout."""
    # Patch terminal command timeout to 0.5s for fast test execution
    import uap.server.ws as ws_mod
    monkeypatch.setattr(ws_mod, "DEFAULT_TERMINAL_TIMEOUT", 0.5, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/terminal") as ws:
            ws.send_json({"type": "stdin", "data": "sleep 5\n"})
            msg = ws.receive_json()
            assert msg.get("type") == "error"
            # Honest message: must explicitly mention timeout
            content = msg.get("data", "") or msg.get("message", "")
            assert "timeout" in content.lower()


# --------------------------------------------------------------------------- #
# 4. Output truncation
# --------------------------------------------------------------------------- #

def test_terminal_ws_output_truncation(app: FastAPI, no_auth: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Large command output is truncated at the configured byte cap."""
    import uap.server.ws as ws_mod
    monkeypatch.setattr(ws_mod, "DEFAULT_MAX_OUTPUT_BYTES", 500, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/terminal") as ws:
            ws.send_json({"type": "stdin", "data": "python3 -c \"print('X' * 2000)\"\n"})
            msg = ws.receive_json()
            data = msg.get("data", "")
            # Output text must be truncated and mention truncation
            assert len(data.encode("utf-8")) <= 1000
            assert "truncated" in data.lower()


# --------------------------------------------------------------------------- #
# 5. Scoped cwd & escape prevention
# --------------------------------------------------------------------------- #

def test_terminal_ws_refuses_cd_escape(app: FastAPI, no_auth: None) -> None:
    """Working directory cannot escape workspace root via 'cd /' or 'cd ../..'."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws/terminal") as ws:
            # Check initial pwd is inside workspace
            ws.send_json({"type": "stdin", "data": "pwd\n"})
            msg = ws.receive_json()
            initial_cwd = msg.get("data", "").strip()

            # Attempt to cd /
            ws.send_json({"type": "stdin", "data": "cd /\n"})
            msg2 = ws.receive_json()
            assert "restricted" in (msg2.get("data", "") or msg2.get("message", "")).lower()

            # Verify pwd has NOT escaped to /
            ws.send_json({"type": "stdin", "data": "pwd\n"})
            msg3 = ws.receive_json()
            assert msg3.get("data", "").strip() == initial_cwd


# --------------------------------------------------------------------------- #
# 6. No server-side shell injection
# --------------------------------------------------------------------------- #

def test_terminal_ws_no_server_side_shell_injection(app: FastAPI, no_auth: None) -> None:
    """Commands are executed via program invocation (shell=False), not shell string interpolation."""
    with TestClient(app) as client:
        with client.websocket_connect("/ws/terminal") as ws:
            # In shell=False, echo args like ';' or '&&' are treated as literal arguments
            ws.send_json({"type": "stdin", "data": "echo safe ; echo injected\n"})
            msg = ws.receive_json()
            # If shell=False, echo prints the ';' and the rest as literal arguments
            assert "safe ; echo injected" in msg.get("data", "")


# --------------------------------------------------------------------------- #
# 7. Concurrent session limit
# --------------------------------------------------------------------------- #

def test_terminal_ws_concurrent_session_limit(app: FastAPI, no_auth: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opening more than MAX_CONCURRENT_TERMINAL_SESSIONS rejects additional handshakes."""
    import uap.server.ws as ws_mod
    monkeypatch.setattr(ws_mod, "MAX_CONCURRENT_TERMINAL_SESSIONS", 2, raising=False)

    with TestClient(app) as client:
        with client.websocket_connect("/ws/terminal") as ws1:
            with client.websocket_connect("/ws/terminal") as ws2:
                # Third connection exceeds limit of 2: receives error and close frame 1008
                with client.websocket_connect("/ws/terminal") as ws3:
                    msg = ws3.receive_json()
                    assert "too many" in (msg.get("data", "") or msg.get("message", "")).lower()
                    with pytest.raises(WebSocketDisconnect) as excinfo:
                        ws3.receive_json()
                    assert excinfo.value.code == 1008
