"""Tests for opt-in bearer-token auth on the HTTP/WS API (``server/auth.py``).

The hole this closes: before auth existed, ``POST /approvals/{id}/decide`` --
the route the whole governance layer hangs off -- was callable by anyone who
could reach the port.

Design under test: auth is **off** unless ``UAP_API_TOKEN`` is set (local-first
default), the UI shell stays reachable so the page can load and present its
token, HTTP uses ``Authorization: Bearer``, the WS handshake uses ``?token=``,
and everything fails closed (401 / WS close 1008).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from uap.server import create_app
from uap.server import auth as auth_mod

TOKEN = "testtoken123"
RESEARCH_INPUT = "research the best langgraph checkpointer approach"
UI_JS = Path(__file__).resolve().parents[1] / "ui" / "js"


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    return create_app(runs_dir=tmp_path / "runs", run_inline=True, heartbeat_interval=0.05)


@pytest.fixture()
def no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default: no token configured anywhere."""
    monkeypatch.delenv("UAP_API_TOKEN", raising=False)


@pytest.fixture()
def with_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UAP_API_TOKEN", TOKEN)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


# --------------------------------------------------------------------------- #
# 1. Auth disabled (the default) -- nothing changes
# --------------------------------------------------------------------------- #

def test_auth_disabled_by_default_every_route_open(app: FastAPI, no_auth: None) -> None:
    """Env unset -> auth off; the pre-auth behaviour is preserved exactly."""
    assert auth_mod.auth_enabled() is False
    with TestClient(app) as client:
        created = client.post("/tasks", json={"input": RESEARCH_INPUT, "user_id": "t"})
        assert created.status_code == 200
        task_id = created.json()["task_id"]
        for path in (
            "/",
            "/tasks",
            f"/tasks/{task_id}",
            "/api/library",
            "/api/workflows",
            "/api/resources/agents",
            f"/api/tasks/{task_id}/artifacts",
        ):
            assert client.get(path).status_code == 200, path
        # The approval route still answers on its own merits (404 for an
        # unknown id), not with a 401.
        decided = client.post(
            "/approvals/does-not-exist/decide",
            json={"approved": True, "decided_by": "t"},
        )
        assert decided.status_code == 404
        with client.websocket_connect(f"/ws/executions/{task_id}") as ws:
            assert ws.receive_json()["type"] == "status"


def test_blank_token_counts_as_disabled(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
    """``UAP_API_TOKEN="  "`` must not masquerade as auth."""
    monkeypatch.setenv("UAP_API_TOKEN", "   ")
    assert auth_mod.auth_enabled() is False
    with TestClient(app) as client:
        assert client.post("/tasks", json={"input": RESEARCH_INPUT}).status_code == 200


# --------------------------------------------------------------------------- #
# 2-4. Auth enabled: missing / correct / wrong credentials
# --------------------------------------------------------------------------- #

def test_post_tasks_without_token_is_401(app: FastAPI, with_auth: None) -> None:
    with TestClient(app) as client:
        res = client.post("/tasks", json={"input": RESEARCH_INPUT})
    assert res.status_code == 401
    assert res.json() == {"detail": "unauthorized"}


def test_post_tasks_with_correct_token_works(app: FastAPI, with_auth: None) -> None:
    with TestClient(app) as client:
        res = client.post("/tasks", json={"input": RESEARCH_INPUT}, headers=AUTH)
        assert res.status_code == 200
        assert res.json()["task_id"]
        assert client.get("/tasks", headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Bearer {TOKEN}x"},          # prefix of the real token
        {"Authorization": TOKEN},                        # no scheme
        {"Authorization": f"Basic {TOKEN}"},             # wrong scheme
        {"Authorization": "Bearer "},                    # empty value
    ],
)
def test_wrong_credentials_are_401(app: FastAPI, with_auth: None, header: dict) -> None:
    with TestClient(app) as client:
        res = client.post("/tasks", json={"input": RESEARCH_INPUT}, headers=header)
    assert res.status_code == 401
    assert res.json() == {"detail": "unauthorized"}


def test_http_ignores_the_ws_query_param(app: FastAPI, with_auth: None) -> None:
    """``?token=`` is the WS channel only; HTTP must use the header."""
    with TestClient(app) as client:
        assert client.get(f"/tasks?token={TOKEN}").status_code == 401


# --------------------------------------------------------------------------- #
# 5. Constant-time comparison
# --------------------------------------------------------------------------- #

def test_comparison_is_constant_time(app: FastAPI, with_auth: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The token check goes through ``secrets.compare_digest``, not ``==``."""
    calls: list[tuple] = []
    real = auth_mod.secrets.compare_digest

    def spy(a, b):  # noqa: ANN001 - mirrors compare_digest's signature
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth_mod.secrets, "compare_digest", spy)
    with TestClient(app) as client:
        assert client.post("/tasks", json={"input": RESEARCH_INPUT}, headers=AUTH).status_code == 200
    assert calls, "compare_digest was never called"
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


# --------------------------------------------------------------------------- #
# 6. Exempt UI-shell paths
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", ["/", "/index.html", "/js/api.js", "/css/app.css", "/legacy/"])
def test_ui_shell_reachable_without_token(app: FastAPI, with_auth: None, path: str) -> None:
    """The page must load before it can present a token."""
    with TestClient(app) as client:
        assert client.get(path).status_code == 200, path


def test_only_ui_shell_is_exempt() -> None:
    """Every state-bearing path is non-exempt (guards the prefix list)."""
    for path in (
        "/tasks",
        "/events",
        "/approvals/abc/decide",
        "/api/proposals",
        "/api/workspaces",
        "/api/library",
        "/api/knowledge",
        "/api/executions/abc/graph",
        "/ws/executions/abc",
        "/jsonl",          # not a /js/ prefix match
        "/cssx",           # not a /css/ prefix match
    ):
        assert auth_mod.is_exempt(path) is False, path
    for path in ("/", "/index.html", "/js/api.js", "/css/app.css", "/legacy", "/legacy/"):
        assert auth_mod.is_exempt(path) is True, path


# --------------------------------------------------------------------------- #
# 7. The route the security review named
# --------------------------------------------------------------------------- #

def test_approval_decide_requires_token(app: FastAPI, with_auth: None) -> None:
    """``POST /approvals/{id}/decide`` is closed without a token, open with it."""
    body = {"approved": True, "decided_by": "reviewer"}
    with TestClient(app) as client:
        anon = client.post("/approvals/some-approval/decide", json=body)
        assert anon.status_code == 401
        # With the token the request reaches the gate (unknown id -> 404),
        # proving auth is the only thing that was blocking it.
        authed = client.post("/approvals/some-approval/decide", json=body, headers=AUTH)
        assert authed.status_code == 404


def test_every_mutating_route_is_closed(app: FastAPI, with_auth: None) -> None:
    with TestClient(app) as client:
        assert client.post("/api/proposals", json={"input": RESEARCH_INPUT}).status_code == 401
        assert client.post("/api/workspaces", json={"name": "x"}).status_code == 401
        assert client.post("/api/executions/abc/pause").status_code == 401
        assert client.post("/api/executions/abc/resume").status_code == 401
        for path in (
            "/tasks",
            "/api/library",
            "/api/workflows",
            "/api/knowledge",
            "/api/workspaces",
            "/api/resources/tools",
            "/api/executions/abc/graph",
            "/api/executions/abc/traces",
            "/api/executions/abc/context",
            "/api/tasks/abc/artifacts",
        ):
            assert client.get(path).status_code == 401, path


# --------------------------------------------------------------------------- #
# 8. WebSocket handshake
# --------------------------------------------------------------------------- #

def test_ws_without_token_is_rejected_1008(app: FastAPI, with_auth: None) -> None:
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws/executions/abc") as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008


def test_ws_with_wrong_token_is_rejected_1008(app: FastAPI, with_auth: None) -> None:
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws/executions/abc?token=nope") as ws:
            ws.receive_json()
    assert excinfo.value.code == 1008


def test_ws_with_token_connects(app: FastAPI, with_auth: None) -> None:
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/executions/abc?token={TOKEN}") as ws:
            assert ws.receive_json()["type"] == "status"
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_ws_rejection_sends_a_real_close_frame(app: FastAPI, with_auth: None) -> None:
    """The handshake is accepted, then closed with 1008.

    Starlette's default behaviour (close before accept) makes uvicorn answer
    HTTP 403, which browsers surface as an opaque 1006 -- indistinguishable
    from "server down". The app installs a handler that accepts first so the
    1008 actually reaches the client.
    """
    sent: list[dict] = []

    async def asgi_receive() -> dict:
        return {"type": "websocket.connect"}

    async def asgi_send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "path": "/ws/executions/abc",
        "raw_path": b"/ws/executions/abc",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "scheme": "ws",
        "root_path": "",
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
        "state": {},
    }
    import anyio

    anyio.run(app, scope, asgi_receive, asgi_send)
    kinds = [m["type"] for m in sent]
    assert kinds == ["websocket.accept", "websocket.close"], kinds
    assert sent[-1]["code"] == 1008
    assert sent[-1].get("reason") == "unauthorized"


def test_request_logs_scrub_the_ws_query_token(app: FastAPI, with_auth: None) -> None:
    """Request-line logs must not print the token.

    Covers both loggers uvicorn uses: ``uvicorn.access`` for HTTP and
    ``uvicorn.error`` for the WebSocket handshake line -- which is the one that
    actually carries ``?token=`` (confirmed against a live uvicorn run).
    """
    import logging

    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        handler = Capture()
        logger.addHandler(handler)
        try:
            # Exactly how uvicorn emits these: msg template + args tuple.
            logger.warning(
                '%s - "WebSocket %s" [accepted]',
                "127.0.0.1:1234",
                f"/ws/executions/abc?token={TOKEN}",
            )
            logger.warning(f"plain message with ?token={TOKEN} inline")
        finally:
            logger.removeHandler(handler)

    assert len(records) == 4, records
    for line in records:
        assert TOKEN not in line, line
        assert "token=***" in line


# --------------------------------------------------------------------------- #
# 9. The token never leaks
# --------------------------------------------------------------------------- #

def test_token_never_appears_in_responses_or_logs(
    app: FastAPI, with_auth: None, caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither the 401 body/headers nor captured logs may echo the secret."""
    with caplog.at_level("DEBUG"):
        with TestClient(app) as client:
            anon = client.post("/tasks", json={"input": RESEARCH_INPUT})
            wrong = client.post("/tasks", json={"input": RESEARCH_INPUT}, headers={"Authorization": f"Bearer {TOKEN}"[:-1]})
            ok = client.post("/tasks", json={"input": RESEARCH_INPUT}, headers=AUTH)
    assert anon.status_code == 401 and wrong.status_code == 401 and ok.status_code == 200
    for res in (anon, wrong):
        assert TOKEN not in res.text
        assert TOKEN not in str(dict(res.headers))
    assert TOKEN not in caplog.text
    # The 401 body carries no hint about the expected credential at all.
    assert anon.json() == {"detail": "unauthorized"}


# --------------------------------------------------------------------------- #
# 10. The UI attaches the token
# --------------------------------------------------------------------------- #

_RUNTIME = shutil.which("bun") or shutil.which("node")

_UI_SCRIPT = r"""
// Minimal DOM/stdlib stubs: api.js only needs localStorage, location, history, fetch.
const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
const calls = [];
globalThis.fetch = async (path, options = {}) => {
  calls.push({ path, headers: options.headers || {} });
  return { ok: true, status: 200, json: async () => ({}), text: async () => "" };
};
let replaced = null;
globalThis.window = {
  location: { protocol: "http:", host: "127.0.0.1:8000", pathname: "/", search: "?token=testtoken123", hash: "" },
  history: { replaceState: (_s, _t, url) => { replaced = url; } },
};
globalThis.WebSocket = class {
  constructor(url) { globalThis.__wsUrl = url; }
  close() {}
};

const api = await import("./api.js");
const failures = [];
const check = (name, cond) => { if (!cond) failures.push(name); };

// 1. The ?token= query param is adopted at import time and scrubbed from the URL.
check("token adopted from url", api.getApiToken() === "testtoken123");
check("token stripped from url", replaced === "/");

// 2. Every REST call carries the bearer header.
await api.listTasks();
await api.createTask("hello");
await api.decideApproval("a1", true);
check("fetch calls recorded", calls.length === 3);
for (const c of calls) {
  check("Authorization on " + c.path, c.headers.Authorization === "Bearer testtoken123");
}

// 3. The WS url carries ?token= (browsers cannot set handshake headers).
const { createEventStream } = await import("./event-stream.js");
createEventStream().connect("exec-1");
check("ws url has token", String(globalThis.__wsUrl).includes("token=testtoken123"));
check("ws url path", String(globalThis.__wsUrl).startsWith("ws://127.0.0.1:8000/ws/executions/exec-1?"));

// 4. No token -> no Authorization header and a clean WS url (auth-off default).
api.setApiToken("");
calls.length = 0;
await api.listTasks();
check("no header without token", calls[0].headers.Authorization === undefined);
createEventStream().connect("exec-2");
check("clean ws url without token", !String(globalThis.__wsUrl).includes("token="));

if (failures.length) {
  console.error("FAILURES: " + failures.join(", "));
  process.exit(1);
}
console.log("ALL_UI_AUTH_CONTRACTS_OK");
"""


@pytest.mark.skipif(_RUNTIME is None, reason="needs a JS runtime (bun or node) to execute ui/js/api.js")
def test_ui_attaches_token_to_fetch_and_ws(tmp_path: Path) -> None:
    """Run the real ui/js modules and assert the token reaches fetch + WS."""
    for name in ("api.js", "event-stream.js", "state.js"):
        shutil.copy(UI_JS / name, tmp_path / name)
    script = tmp_path / "check_auth.mjs"
    script.write_text(_UI_SCRIPT, encoding="utf-8")

    result = subprocess.run(
        [_RUNTIME, str(script)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, (
        f"UI auth contract violated:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "ALL_UI_AUTH_CONTRACTS_OK" in result.stdout
