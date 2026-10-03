from __future__ import annotations

from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from uap.server import create_app

TOKEN = "test-token-sandbox-123"
AUTH_HEADER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    return create_app(runs_dir=tmp_path / "runs", run_inline=True, heartbeat_interval=0.05)


def test_sandbox_run_python_stdout(app: FastAPI) -> None:
    """1. A simple Python program returns its stdout, exit code 0, and not truncated."""
    with TestClient(app) as client:
        res = client.post(
            "/api/sandbox/run",
            json={"language": "python", "code": "print('hello from sandbox')"},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["stdout"] == "hello from sandbox\n"
        assert data["stderr"] == ""
        assert data["exit_code"] == 0
        assert data["truncated"] is False
        assert isinstance(data["duration_ms"], (int, float))
        assert data["duration_ms"] >= 0


def test_sandbox_run_python_syntax_and_runtime_error(app: FastAPI) -> None:
    """2. Code raising an exception returns exit_code != 0 and traceback in stderr."""
    with TestClient(app) as client:
        res = client.post(
            "/api/sandbox/run",
            json={"language": "python", "code": "1 / 0"},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["exit_code"] != 0
        assert "ZeroDivisionError" in data["stderr"]
        assert data["truncated"] is False


def test_sandbox_run_infinite_loop_killed(app: FastAPI) -> None:
    """3. An infinite loop is killed by the CPU / wall-clock timeout limit."""
    with TestClient(app) as client:
        res = client.post(
            "/api/sandbox/run",
            json={"language": "python", "code": "while True: pass", "timeout_s": 0.5},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["exit_code"] != 0
        err_msg = data["stderr"].lower()
        assert "timeout" in err_msg or "cpu limit" in err_msg or "limit exceeded" in err_msg


def test_sandbox_run_memory_bomb_killed(app: FastAPI) -> None:
    """4. A memory bomb is killed by RLIMIT_AS."""
    with TestClient(app) as client:
        res = client.post(
            "/api/sandbox/run",
            json={
                "language": "python",
                "code": "blob = bytearray(256 * 1024 * 1024)",
                "max_memory_mb": 64,
            },
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["exit_code"] != 0
        err_msg = data["stderr"].lower()
        assert "memory" in err_msg or "sigkill" in err_msg or "limit" in err_msg


def test_sandbox_run_output_truncation(app: FastAPI) -> None:
    """5. Output over the cap is truncated AND flagged with truncated=True and a visible notice."""
    with TestClient(app) as client:
        res = client.post(
            "/api/sandbox/run",
            json={
                "language": "python",
                "code": "print('A' * 200)",
                "max_output_bytes": 50,
            },
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["truncated"] is True
        assert len(data["stdout"].encode("utf-8")) == 50
        assert "truncated" in data["stderr"].lower()


def test_sandbox_run_unsupported_language_refused(app: FastAPI) -> None:
    """6. Non-python languages are clearly refused with HTTP 400 stating supported languages."""
    with TestClient(app) as client:
        res_bash = client.post(
            "/api/sandbox/run",
            json={"language": "bash", "code": "echo hi"},
        )
        assert res_bash.status_code == 400
        assert "unsupported language" in res_bash.json()["detail"].lower()
        assert "python" in res_bash.json()["detail"].lower()

        res_js = client.post(
            "/api/sandbox/run",
            json={"language": "javascript", "code": "console.log(1)"},
        )
        assert res_js.status_code == 400
        assert "unsupported language" in res_js.json()["detail"].lower()


def test_sandbox_run_auth_required_when_token_set(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """7. Auth: 401 without token when UAP_API_TOKEN is set, 200 with valid bearer token."""
    monkeypatch.setenv("UAP_API_TOKEN", TOKEN)
    with TestClient(app) as client:
        # Without token -> 401
        res_unauth = client.post(
            "/api/sandbox/run",
            json={"language": "python", "code": "print('secret')"},
        )
        assert res_unauth.status_code == 401

        # With wrong token -> 401
        res_bad = client.post(
            "/api/sandbox/run",
            headers={"Authorization": "Bearer wrong-token"},
            json={"language": "python", "code": "print('secret')"},
        )
        assert res_bad.status_code == 401

        # With valid token -> 200
        res_ok = client.post(
            "/api/sandbox/run",
            headers=AUTH_HEADER,
            json={"language": "python", "code": "print('secret')"},
        )
        assert res_ok.status_code == 200
        assert res_ok.json()["stdout"] == "secret\n"


def test_sandbox_run_docstring_explains_auth_necessity(app: FastAPI) -> None:
    """8. Docstring states why auth matters more here than elsewhere."""
    route = next((r for r in app.routes if getattr(r, "path", None) == "/api/sandbox/run"), None)
    assert route is not None, "route /api/sandbox/run not found"
    doc = getattr(route.endpoint, "__doc__", "") or ""
    assert "auth matters more here than elsewhere" in doc.lower()
    assert "arbitrary" in doc.lower()
    assert "sandbox" in doc.lower()


def test_sandbox_run_sys_exit_code(app: FastAPI) -> None:
    """9. Explicit sys.exit(42) preserves exit code."""
    with TestClient(app) as client:
        res = client.post(
            "/api/sandbox/run",
            json={"language": "python", "code": "import sys; sys.exit(42)"},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["exit_code"] == 42


def test_sandbox_run_invalid_timeout(app: FastAPI) -> None:
    """10. Negative or excessive timeout returns HTTP 400."""
    with TestClient(app) as client:
        res_neg = client.post(
            "/api/sandbox/run",
            json={"language": "python", "code": "print(1)", "timeout_s": -1.0},
        )
        assert res_neg.status_code == 400

        res_huge = client.post(
            "/api/sandbox/run",
            json={"language": "python", "code": "print(1)", "timeout_s": 5000.0},
        )
        assert res_huge.status_code == 400


def test_sandbox_run_default_memory_limit_1gb_bomb(app: FastAPI) -> None:
    """11. A 1GB memory bomb is killed under default 512MB memory limit."""
    with TestClient(app) as client:
        res = client.post(
            "/api/sandbox/run",
            json={
                "language": "python",
                "code": "blob = bytearray(1024 * 1024 * 1024)",
            },
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["exit_code"] != 0
        err_msg = data["stderr"].lower()
        assert "memory" in err_msg or "sigkill" in err_msg or "limit" in err_msg
