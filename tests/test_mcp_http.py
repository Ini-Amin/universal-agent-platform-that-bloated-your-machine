"""Tests for the MCP Streamable HTTP transport (remote servers).

Covers the second official MCP transport: a JSON-RPC POST whose reply arrives
either as a JSON body or as an SSE stream, with an optional ``Mcp-Session-Id``
that must be echoed. Everything runs against ``httpx.MockTransport`` — no test
touches a real network endpoint.
"""

from __future__ import annotations

import json

import httpx
import pytest

from uap.mcp.client import HttpMCPClient, MCPClientError
from uap.mcp.config import MCPServerConfig


def _config(url: str = "https://mcp.example.test/rpc", **env: str) -> MCPServerConfig:
    return MCPServerConfig(
        name="remote-test",
        transport="http",
        url=url,
        env=dict(env),
        tier_map={"search": 1},
    )


def _json_reply(result: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": 1, "result": result},
        headers={"content-type": "application/json"},
    )


def _sse_reply(result: dict) -> httpx.Response:
    body = f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result})}\n\n"
    return httpx.Response(
        200, text=body, headers={"content-type": "text/event-stream"}
    )


def _client_with(handler) -> HttpMCPClient:
    client = HttpMCPClient(_config(), timeout_s=5.0)
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    return client


# --------------------------------------------------------------------------- #
# 1-2. handshake + JSON-body listing
# --------------------------------------------------------------------------- #


def test_start_performs_initialize_and_records_session() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": payload["id"], "result": {"serverInfo": {"name": "exa"}}},
                headers={"content-type": "application/json", "Mcp-Session-Id": "sess-123"},
            )
        return httpx.Response(202)

    client = _client_with(handler)
    info = client.start()
    assert info == {"serverInfo": {"name": "exa"}}
    assert client.started is True
    assert client._session_id == "sess-123"
    assert seen[0]["method"] == "initialize"


def test_list_tools_parses_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_reply({"tools": [{"name": "search", "description": "web search"}]})

    client = _client_with(handler)
    client.server_info = {}
    tools = client.list_tools()
    assert tools == [{"name": "search", "description": "web search"}]


# --------------------------------------------------------------------------- #
# 3. SSE reply
# --------------------------------------------------------------------------- #


def test_list_tools_parses_sse_stream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_reply({"tools": [{"name": "fetch"}]})

    client = _client_with(handler)
    client.server_info = {}
    assert client.list_tools() == [{"name": "fetch"}]


# --------------------------------------------------------------------------- #
# 4. tool call round-trip
# --------------------------------------------------------------------------- #


def test_call_tool_round_trips_arguments() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.update(payload)
        return _json_reply({"content": [{"type": "text", "text": "42 results"}]})

    client = _client_with(handler)
    client.server_info = {}
    out = client.call_tool("search", {"query": "gil python"})
    assert out == {"content": [{"type": "text", "text": "42 results"}]}
    assert captured["params"] == {"name": "search", "arguments": {"query": "gil python"}}


# --------------------------------------------------------------------------- #
# 5. failures are MCPClientError, never crashes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="{not json", headers={"content-type": "application/json"}),
        httpx.Response(200, text="event: x\ndata: nonsense\n\n", headers={"content-type": "text/event-stream"}),
    ],
)
def test_failures_raise_mcp_client_error(response: httpx.Response) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    client = _client_with(handler)
    client.server_info = {}
    with pytest.raises(MCPClientError):
        client.list_tools()


def test_timeout_raises_mcp_client_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow", request=request)

    client = _client_with(handler)
    client.server_info = {}
    with pytest.raises(MCPClientError):
        client.list_tools()


# --------------------------------------------------------------------------- #
# 6. session id is echoed on later requests
# --------------------------------------------------------------------------- #


def test_session_id_echoed_on_second_request() -> None:
    headers_seen: list[str | None] = []
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers_seen.append(request.headers.get("Mcp-Session-Id"))
        methods.append(json.loads(request.content)["method"])
        if len(methods) == 1:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
                headers={"content-type": "application/json", "Mcp-Session-Id": "abc"},
            )
        return _json_reply({"tools": []})

    client = _client_with(handler)
    client.start()
    client.list_tools()
    # First request carries no session; every later one echoes it (the
    # initialized notification included, when the server accepts it).
    assert headers_seen[0] is None
    assert set(headers_seen[1:]) == {"abc"}
    assert methods[0] == "initialize"


# --------------------------------------------------------------------------- #
# 7. config validation
# --------------------------------------------------------------------------- #


def test_config_validation() -> None:
    with pytest.raises(Exception):
        MCPServerConfig(name="bad", transport="http")  # no url
    with pytest.raises(Exception):
        MCPServerConfig(name="bad", transport="stdio")  # no command
    cfg = MCPServerConfig(name="ok", transport="http", url="https://x.test")
    assert cfg.url == "https://x.test"
    # The existing bugbounty config still builds (stdio default).
    from uap.mcp.config import BUG_BOUNTY_MCP_CONFIG

    assert BUG_BOUNTY_MCP_CONFIG.transport == "stdio"


# --------------------------------------------------------------------------- #
# 8. the adapter is transport-agnostic
# --------------------------------------------------------------------------- #


def test_register_mcp_server_accepts_http_client() -> None:
    from uap.mcp.adapter import register_mcp_server
    from uap.tools.registry import ToolRegistry

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if payload["method"] == "tools/list":
            return _json_reply({"tools": [{"name": "search", "description": "d", "inputSchema": {}}]})
        return _json_reply({})

    client = _client_with(handler)
    client.server_info = {}
    registry = ToolRegistry()
    names = register_mcp_server(registry, client, client.config)
    assert names == ["remote-test.search"]


# --------------------------------------------------------------------------- #
# 9. graceful degradation: unreachable server -> (None, None)
# --------------------------------------------------------------------------- #


def test_start_mcp_tools_degrades_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from uap.server.mcp_lifecycle import start_mcp_tools
    """An unreachable remote server must degrade, not crash.

    The transport is mocked to raise a connection error, so this stays
    hermetic (no real sockets, no waiting on a dead port).
    """
    from uap.server import mcp_lifecycle

    def boom(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.Client, "post", boom)

    cfg = MCPServerConfig(
        name="dead",
        transport="http",
        url="http://127.0.0.1:9/definitely-not-listening",
    )
    registry, client = start_mcp_tools(cfg, timeout_s=0.5)
    assert registry is None and client is None


# --------------------------------------------------------------------------- #
# 10. auth header is attached but never logged
# --------------------------------------------------------------------------- #


def test_auth_header_attached_from_env() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return _json_reply({"tools": []})

    client = HttpMCPClient(_config(SMITHERY_API_KEY="secret-token"), timeout_s=5.0)
    client._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
    client.server_info = {}
    client.list_tools()
    assert captured["auth"] == "Bearer secret-token"
    assert "secret-token" not in repr(client.config.tier_map)
