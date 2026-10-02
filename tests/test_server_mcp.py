"""Tests for wiring MCP into the server (create_app + lifespan).

Network-free: the default path starts no subprocess, and the only real MCP
server used is ``tests/mock_mcp_server.py`` (reused from test_mcp.py), launched
via ``sys.executable``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from uap.mcp.config import MCPServerConfig
from uap.observability import EventBus, EventKind, MemorySink
from uap.server import app as app_module
from uap.server import create_app
from uap.server.mcp_lifecycle import start_mcp_tools, stop_mcp_tools
from uap.tools import ToolRegistry, ToolSpec

MOCK_SERVER = Path(__file__).with_name("mock_mcp_server.py")


def _mock_config(tier_map: dict[str, int] | None = None) -> MCPServerConfig:
    return MCPServerConfig(
        name="mock",
        command=sys.executable,
        args=[str(MOCK_SERVER)],
        tier_map=tier_map or {},
    )


def _bus_with_memory() -> tuple[EventBus, MemorySink]:
    bus = EventBus()
    sink = MemorySink()
    bus.subscribe(sink)
    return bus, sink


async def _fake_tool(**kwargs):  # noqa: ANN003 - test stub
    return {"ok": True}


def _fake_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="mock.echo", description="fake", risk_tier=0),
        _fake_tool,
    )
    return registry


# --------------------------------------------------------------------------- #
# 1. Default: no env, no args -> no MCP, no subprocess.
# --------------------------------------------------------------------------- #


def test_default_boot_has_no_mcp_tools(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("UAP_MCP_ENABLED", raising=False)
    bus, sink = _bus_with_memory()
    app = create_app(bus=bus, runs_dir=tmp_path, run_inline=True)
    with TestClient(app) as client:
        assert app.state.mcp_tools is None
        assert app.state.mcp_client is None
        assert client.get("/legacy/").status_code == 200
    assert sink.query(kind=EventKind.MCP_CALL) == []


# --------------------------------------------------------------------------- #
# 2. Enabled via env + mocked start_mcp_tools succeeds -> BBP gets tools, info event.
# --------------------------------------------------------------------------- #


def test_enabled_registers_tools_and_emits_info(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UAP_MCP_ENABLED", "1")
    registry = _fake_registry()
    sentinel_client = object()

    def fake_start(config, timeout_s=20.0):
        return registry, sentinel_client

    monkeypatch.setattr(app_module, "start_mcp_tools", fake_start)
    monkeypatch.setattr(app_module, "stop_mcp_tools", lambda c: None)

    bus, sink = _bus_with_memory()
    app = create_app(bus=bus, runs_dir=tmp_path, run_inline=True)
    with TestClient(app) as client:  # triggers lifespan startup
        # workflow_for passes app.state.mcp_tools into each per-request BBPWorkflow.
        assert app.state.mcp_tools is registry
        resp = client.post("/tasks", json={"input": "Bug bounty on example.com"})
        assert resp.status_code == 200

    events = sink.query(kind=EventKind.MCP_CALL)
    assert len(events) == 1
    assert "mock.echo" in events[0].data["tools"]


# --------------------------------------------------------------------------- #
# 3. Enabled but start raises -> app still boots, warning recorded, tools=None.
# --------------------------------------------------------------------------- #


def test_enabled_start_failure_degrades_gracefully(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UAP_MCP_ENABLED", "true")

    def boom(config, timeout_s=20.0):
        raise RuntimeError("bbmcp exploded")

    monkeypatch.setattr(app_module, "start_mcp_tools", boom)

    bus, sink = _bus_with_memory()
    app = create_app(bus=bus, runs_dir=tmp_path, run_inline=True)
    with TestClient(app) as client:
        assert app.state.mcp_tools is None
        assert app.state.mcp_client is None
        assert client.get("/legacy/").status_code == 200

    errors = sink.query(kind=EventKind.ERROR)
    assert any("MCP" in (e.error or "") for e in errors)


# --------------------------------------------------------------------------- #
# 4. Explicit mcp_enabled=False wins over UAP_MCP_ENABLED=1.
# --------------------------------------------------------------------------- #


def test_explicit_disable_overrides_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UAP_MCP_ENABLED", "1")

    def should_not_run(config, timeout_s=20.0):
        raise AssertionError("start_mcp_tools must not be called when disabled")

    monkeypatch.setattr(app_module, "start_mcp_tools", should_not_run)

    bus, sink = _bus_with_memory()
    app = create_app(bus=bus, runs_dir=tmp_path, run_inline=True, mcp_enabled=False)
    with TestClient(app):
        assert app.state.mcp_tools is None
    assert sink.query(kind=EventKind.MCP_CALL) == []


# --------------------------------------------------------------------------- #
# 5. Real start_mcp_tools against the mock server -> prefixed tool round-trips.
# --------------------------------------------------------------------------- #


async def test_real_start_registers_and_calls_mock_tool():
    config = _mock_config(tier_map={"echo": 0})  # tier 0 so no approval needed
    registry, client = await asyncio.to_thread(start_mcp_tools, config)
    try:
        assert registry is not None and client is not None
        names = {spec.name for spec in registry.list()}
        assert "mock.echo" in names
        assert all(name.startswith("mock.") for name in names)
        result = await registry.call("mock.echo", {"text": "hello mcp"})
        assert result.ok
        assert result.result["content"][0]["text"] == "hello mcp"
    finally:
        await asyncio.to_thread(stop_mcp_tools, client)


# --------------------------------------------------------------------------- #
# 6. Missing binary -> (None, None), no exception, finishes quickly.
# --------------------------------------------------------------------------- #


async def test_missing_binary_returns_none_none():
    config = MCPServerConfig(name="dead", command="/nonexistent/binary")
    registry, client = await asyncio.to_thread(
        start_mcp_tools, config, 2.0
    )
    assert registry is None
    assert client is None


# --------------------------------------------------------------------------- #
# 7. Shutdown reaps the subprocess.
# --------------------------------------------------------------------------- #


async def test_stop_reaps_subprocess():
    config = _mock_config(tier_map={"echo": 0})
    registry, client = await asyncio.to_thread(start_mcp_tools, config)
    assert client is not None
    assert client.process is not None
    assert client.process.poll() is None  # alive
    await asyncio.to_thread(stop_mcp_tools, client)
    assert client.process.poll() is not None  # reaped


# --------------------------------------------------------------------------- #
# 8. Lifespan enter/exit invokes start on enter and stop on exit.
# --------------------------------------------------------------------------- #


def test_lifespan_triggers_start_and_stop(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UAP_MCP_ENABLED", "1")
    registry = _fake_registry()
    calls = {"start": 0, "stop": 0}
    sentinel_client = object()

    def fake_start(config, timeout_s=20.0):
        calls["start"] += 1
        return registry, sentinel_client

    def fake_stop(client):
        assert client is sentinel_client
        calls["stop"] += 1

    monkeypatch.setattr(app_module, "start_mcp_tools", fake_start)
    monkeypatch.setattr(app_module, "stop_mcp_tools", fake_stop)

    app = create_app(runs_dir=tmp_path, run_inline=True)
    with TestClient(app):
        assert calls["start"] == 1
        assert calls["stop"] == 0
    assert calls["stop"] == 1
