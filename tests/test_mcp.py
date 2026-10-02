"""Tests for build Step 8 - MCP Integration (Master sections 9, 12, 13, 20, 29).

The MCP client is synchronous (it owns a subprocess), so the async tests hop to
a worker thread with ``asyncio.to_thread`` where a real blocking round-trip is
exercised. The mock server (tests/mock_mcp_server.py) is launched via
``sys.executable`` and speaks newline-delimited JSON-RPC.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError

from uap.contracts import ApprovalRequest, ApprovalState
from uap.mcp import (
    BUG_BOUNTY_MCP_CONFIG,
    MCPClientError,
    MCPServerConfig,
    StdioMCPClient,
    convert_input_schema,
    register_mcp_server,
)
from uap.tools import ToolRegistry

MOCK_SERVER = Path(__file__).with_name("mock_mcp_server.py")


def _config(*, tier_map: dict[str, int] | None = None, default_tier: int = 3) -> MCPServerConfig:
    return MCPServerConfig(
        name="mock",
        command=sys.executable,
        args=[str(MOCK_SERVER)],
        tier_map=tier_map or {},
        default_tier=default_tier,
    )


def _client(timeout_s: float = 20.0, **kwargs) -> StdioMCPClient:
    return StdioMCPClient(_config(**kwargs), timeout_s=timeout_s)


@asynccontextmanager
async def running_client(timeout_s: float = 20.0, **kwargs):
    client = _client(timeout_s=timeout_s, **kwargs)
    await asyncio.to_thread(client.start)
    try:
        yield client
    finally:
        await asyncio.to_thread(client.close)


async def _registered(tier_map: dict[str, int] | None = None, default_tier: int = 3):
    """Return (client, registry, names); caller must close the client."""
    client = _client(tier_map=tier_map, default_tier=default_tier)
    registry = ToolRegistry()
    names = await asyncio.to_thread(register_mcp_server, registry, client, client.config)
    return client, registry, names


# --------------------------------------------------------------------------- #
# Client lifecycle and wire protocol
# --------------------------------------------------------------------------- #


async def test_start_completes_handshake_and_returns_server_info():
    client = _client()
    try:
        info = await asyncio.to_thread(client.start)
    finally:
        await asyncio.to_thread(client.close)

    assert isinstance(info, dict)
    assert info["serverInfo"]["name"] == "mock-mcp"
    # Client accepts whatever protocolVersion the server returns.
    assert info["protocolVersion"] == "2024-11-05"


async def test_list_tools_returns_mock_catalog():
    async with running_client() as client:
        tools = await asyncio.to_thread(client.list_tools)

    by_name = {t["name"]: t for t in tools}
    assert set(by_name) == {"echo", "fail_tool", "slow_tool"}
    assert by_name["echo"]["description"]
    assert "text" in by_name["echo"]["inputSchema"]["properties"]


async def test_call_tool_echo_returns_echoed_text():
    async with running_client() as client:
        result = await asyncio.to_thread(client.call_tool, "echo", {"text": "hello mcp"})

    assert result["content"][0]["type"] == "text"
    assert result["content"][0]["text"] == "hello mcp"


async def test_call_tool_unknown_tool_raises_client_error():
    async with running_client() as client:
        with pytest.raises(MCPClientError) as excinfo:
            await asyncio.to_thread(client.call_tool, "nope", {})

    message = str(excinfo.value)
    assert "-32602" in message
    assert "unknown tool" in message


async def test_call_tool_fail_tool_surfaces_server_error_message():
    async with running_client() as client:
        with pytest.raises(MCPClientError) as excinfo:
            await asyncio.to_thread(client.call_tool, "fail_tool", {})

    message = str(excinfo.value)
    assert "-32000" in message
    assert "fail_tool exploded on purpose" in message


async def test_call_tool_timeout_mentions_timeout_and_tool():
    async with running_client(timeout_s=0.5) as client:
        with pytest.raises(MCPClientError) as excinfo:
            await asyncio.to_thread(client.call_tool, "slow_tool", {})

    message = str(excinfo.value)
    assert "timed out" in message.lower()
    assert "slow_tool" in message


async def test_close_terminates_process_and_is_idempotent():
    client = _client()
    await asyncio.to_thread(client.start)
    proc = client.process
    assert proc is not None
    assert proc.poll() is None

    await asyncio.to_thread(client.close)
    assert proc.poll() is not None

    # Closing twice must not raise.
    await asyncio.to_thread(client.close)


async def test_context_manager_works_end_to_end():
    client = _client()
    with client:
        tools = client.list_tools()
        result = client.call_tool("echo", {"text": "cm"})

    assert {t["name"] for t in tools} == {"echo", "fail_tool", "slow_tool"}
    assert result["content"][0]["text"] == "cm"
    assert client.process is not None
    assert client.process.poll() is not None


# --------------------------------------------------------------------------- #
# Adapter: remote tools become registry tools
# --------------------------------------------------------------------------- #


async def test_register_mcp_server_registers_prefixed_tools():
    # echo is tier 0 here so the call below needs no approval (registration is
    # what this test covers; policy gating is covered separately).
    client, registry, names = await _registered(tier_map={"echo": 0})
    try:
        assert set(names) == {"mock.echo", "mock.fail_tool", "mock.slow_tool"}
        for name in names:
            assert registry.get(name) is not None

        result = await registry.call("mock.echo", {"text": "via registry"})
        assert result.ok is True, result.error
        assert result.result["content"][0]["text"] == "via registry"
    finally:
        await asyncio.to_thread(client.close)


async def test_tier_mapping_uses_config_and_defaults_to_three():
    client, registry, _ = await _registered(tier_map={"echo": 1}, default_tier=3)
    try:
        assert registry.get("mock.echo")[0].risk_tier == 1
        assert registry.get("mock.fail_tool")[0].risk_tier == 3
        assert registry.get("mock.slow_tool")[0].risk_tier == 3
    finally:
        await asyncio.to_thread(client.close)


async def test_input_schema_conversion():
    client, registry, _ = await _registered()
    try:
        echo_schema = registry.get("mock.echo")[0].input_schema
        assert echo_schema == {
            "text": {"type": "string", "required": True, "description": "Text to echo."}
        }
        # A tool with no declared properties yields no required args.
        assert registry.get("mock.fail_tool")[0].input_schema == {}
    finally:
        await asyncio.to_thread(client.close)

    assert convert_input_schema({"type": "object", "properties": {}}) == {}
    assert convert_input_schema({"type": "object"}) == {}
    assert convert_input_schema(None) == {}


async def test_registry_policy_gates_mcp_tier3_tool():
    # echo carries tier 3 here, so the frozen policy guard must gate it.
    client, registry, _ = await _registered(tier_map={"echo": 3})
    try:
        blocked = await registry.call("mock.echo", {"text": "x"})
        assert blocked.ok is False
        assert "approved request" in (blocked.error or "")

        approved = ApprovalRequest(
            task_id="task_1",
            action="call mock.echo",
            state=ApprovalState.APPROVED,
            decided_by="tester",
        )
        allowed = await registry.call("mock.echo", {"text": "x"}, approval=approved)
        assert allowed.ok is True, allowed.error
        assert allowed.result["content"][0]["text"] == "x"
    finally:
        await asyncio.to_thread(client.close)


async def test_mcp_client_error_becomes_tool_result_error():
    # Tier 0 so policy lets the call through; the failure must surface as data.
    client, registry, _ = await _registered(tier_map={"fail_tool": 0})
    try:
        result = await registry.call("mock.fail_tool", {})
    finally:
        await asyncio.to_thread(client.close)

    assert result.ok is False
    assert "fail_tool exploded on purpose" in (result.error or "")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_bug_bounty_config_maps_all_eleven_documented_tools():
    expected = {
        "validate_scope": 0,
        "list_agents": 0,
        "list_skills": 0,
        "list_mcp_servers": 0,
        "get_run": 0,
        "lookup_dns": 1,
        "find_subdomains_passive": 1,
        "probe_http": 2,
        "run_agent": 3,
        "stop_run": 3,
        "test_mcp_server": 3,
    }
    assert BUG_BOUNTY_MCP_CONFIG.name == "bugbounty-mcp"
    assert BUG_BOUNTY_MCP_CONFIG.command == "/home/user/bugbounty-mcp/bbmcp"
    assert BUG_BOUNTY_MCP_CONFIG.args == []
    assert BUG_BOUNTY_MCP_CONFIG.tier_map == expected
    assert len(BUG_BOUNTY_MCP_CONFIG.tier_map) == 11
    assert BUG_BOUNTY_MCP_CONFIG.default_tier == 3


def test_config_forbids_extra_fields_and_validates_tiers():
    with pytest.raises(ValidationError):
        MCPServerConfig(name="x", command="y", unexpected=1)

    with pytest.raises(ValidationError):
        MCPServerConfig(name="x", command="y", tier_map={"tool": 5})
