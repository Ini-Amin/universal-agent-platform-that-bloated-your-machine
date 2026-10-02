"""Regression tests: ``/openapi.json``, ``/docs`` and ``/redoc`` need the token.

The hole these close: FastAPI registers its three built-in doc routes in
``FastAPI.setup`` as plain Starlette routes, so the app-level
``Depends(require_token)`` never ran for them. With ``UAP_API_TOKEN`` set,
``/tasks`` answered 401 while ``GET /openapi.json`` still handed an anonymous
caller every route and request schema in the platform.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from uap.server import create_app
from uap.server import auth as auth_mod

TOKEN = "testtoken123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DOC_PATHS = ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect")


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    return create_app(runs_dir=tmp_path / "runs", run_inline=True, heartbeat_interval=0.05)


@pytest.mark.parametrize("path", DOC_PATHS)
def test_builtin_doc_routes_need_the_token(app: FastAPI, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.setenv("UAP_API_TOKEN", TOKEN)
    with TestClient(app) as client:
        res = client.get(path)
    assert res.status_code == 401, path
    assert res.json() == {"detail": "unauthorized"}


@pytest.mark.parametrize("path", DOC_PATHS)
def test_builtin_doc_routes_reject_a_wrong_token(app: FastAPI, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    monkeypatch.setenv("UAP_API_TOKEN", TOKEN)
    with TestClient(app) as client:
        res = client.get(path, headers={"Authorization": "Bearer not-the-token"})
    assert res.status_code == 401, path


def test_openapi_schema_served_to_an_authenticated_caller(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate must not break the real consumer: the schema still comes back."""
    monkeypatch.setenv("UAP_API_TOKEN", TOKEN)
    with TestClient(app) as client:
        schema = client.get("/openapi.json", headers=AUTH)
        docs = client.get("/docs", headers=AUTH)
    assert schema.status_code == 200
    assert schema.json()["openapi"].startswith("3.")
    assert "/tasks" in schema.json()["paths"]
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()


@pytest.mark.parametrize("path", DOC_PATHS)
def test_builtin_doc_routes_stay_open_when_auth_is_off(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    """Local-first default: no token configured -> nothing changes."""
    monkeypatch.delenv("UAP_API_TOKEN", raising=False)
    assert auth_mod.auth_enabled() is False
    with TestClient(app) as client:
        assert client.get(path).status_code == 200, path


def test_ui_shell_is_still_exempt(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """The page must still load without a token, or it can never present one."""
    monkeypatch.setenv("UAP_API_TOKEN", TOKEN)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/css/app.css").status_code == 200


def test_guard_only_wraps_apps_that_opted_into_require_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unrelated FastAPI apps in this process are not gated by UAP's env var."""
    monkeypatch.setenv("UAP_API_TOKEN", TOKEN)
    other = FastAPI()

    @other.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(other) as client:
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/ping").status_code == 200


def test_gate_install_is_idempotent() -> None:
    """Re-running the installer must not stack a second wrapper on new routes."""
    auth_mod.guard_fastapi_builtin_routes()
    auth_mod.guard_fastapi_builtin_routes()
    assert auth_mod._BUILTIN_ROUTES_GUARDED is True