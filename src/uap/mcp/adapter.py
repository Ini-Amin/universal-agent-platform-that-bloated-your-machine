"""MCP adapter: remote MCP tools become ordinary registry tools (Master 12, 13).

The adapter is the only place that knows a tool came from an MCP server. It
turns each remote tool into a `ToolSpec` plus an async callable and registers
both, so agents keep calling capabilities by name and never see the transport
(Master section 12: `browser.search()`, not "a JSON-RPC call to server X").

Import direction: this module imports `uap.tools` (registry) and
`uap.mcp.client`. `uap.tools` never imports `uap.mcp`, so there is no cycle.

Risk tiers come from the server config's `tier_map` (missing -> default tier 3),
which means the frozen policy guard in `uap.tools.policy` gates MCP tools with
exactly the same rules as local tools (Master sections 9, 20, 29).
"""

from __future__ import annotations

import asyncio
from typing import Any

from uap.mcp.client import MCPClientError, StdioMCPClient
from uap.mcp.config import MCPServerConfig
from uap.tools import ToolRegistry, ToolSpec

JSON_TYPES = {"string", "number", "integer", "boolean", "object", "array"}


def convert_input_schema(input_schema: Any) -> dict[str, dict[str, Any]]:
    """MCP JSON Schema -> the registry's ``{field: {type, required, description}}``.

    A missing/empty ``properties`` map becomes ``{}``: a tool that declares no
    arguments has no required arguments. Fields listed in ``required`` are
    flagged so the registry can reject calls that omit them.
    """
    if not isinstance(input_schema, dict):
        return {}

    properties = input_schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return {}

    required_raw = input_schema.get("required")
    required = set(required_raw) if isinstance(required_raw, list) else set()

    converted: dict[str, dict[str, Any]] = {}
    for field, raw in properties.items():
        raw = raw if isinstance(raw, dict) else {}
        json_type = raw.get("type")
        if not isinstance(json_type, str) or json_type not in JSON_TYPES:
            json_type = "string"
        meta: dict[str, Any] = {"type": json_type, "required": field in required}
        description = raw.get("description")
        if isinstance(description, str) and description:
            meta["description"] = description
        converted[field] = meta
    return converted


def register_mcp_server(
    registry: ToolRegistry, client: StdioMCPClient, config: MCPServerConfig
) -> list[str]:
    """Register every tool a server exposes; return the registered names.

    Names are prefixed with the server name (``bugbounty-mcp.validate_scope``)
    so tools from different servers can never collide. A collision with an
    already-registered name raises ``ValueError`` from the registry and is left
    to propagate: silent shadowing would be a security bug.
    """
    if not client.started:
        client.start()

    registered: list[str] = []
    for tool in client.list_tools():
        tool_name = tool.get("name")
        if not isinstance(tool_name, str) or not tool_name:
            continue

        qualified = f"{config.name}.{tool_name}"
        description = tool.get("description")
        if not isinstance(description, str) or not description:
            description = f"MCP tool {tool_name} from server {config.name}"

        spec = ToolSpec(
            name=qualified,
            description=description,
            risk_tier=config.tier_map.get(tool_name, config.default_tier),
            input_schema=convert_input_schema(tool.get("inputSchema")),
        )
        registry.register(spec, _make_tool_fn(client, tool_name))
        registered.append(qualified)
    return registered


def _make_tool_fn(client: StdioMCPClient, tool_name: str):
    """Build the async registry callable that forwards one tool to the server."""

    async def fn(**kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(client.call_tool, tool_name, dict(kwargs))
        except MCPClientError as exc:
            # The registry turns any exception into ToolResult.error; give it a
            # readable message instead of the raw client error type.
            raise RuntimeError(str(exc)) from exc

    fn.__name__ = f"mcp_{tool_name}"
    fn.__qualname__ = fn.__name__
    fn.__doc__ = f"Call MCP tool {tool_name!r} over stdio."
    return fn


__all__ = ["convert_input_schema", "register_mcp_server"]
