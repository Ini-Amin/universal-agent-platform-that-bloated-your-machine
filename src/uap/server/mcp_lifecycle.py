"""Sync helpers for MCP client lifecycle (start/stop + tool registration).

Callers in async contexts wrap these in ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from uap.mcp.adapter import register_mcp_server
from uap.mcp.client import HttpMCPClient, StdioMCPClient
from uap.tools import ToolRegistry

if TYPE_CHECKING:
    from uap.mcp.config import MCPServerConfig

log = logging.getLogger(__name__)


def start_mcp_tools(
    config: MCPServerConfig,
    timeout_s: float = 20.0,
) -> tuple[ToolRegistry | None, Any | None]:
    """Start an MCP server, register its tools, return ``(registry, client)``.

    Returns ``(None, None)`` on any failure — the caller must not crash.
    """
    try:
        # Pick the transport the config declares (stdio = local subprocess,
        # http = remote Streamable HTTP server, e.g. a Smithery URL).
        client_cls = HttpMCPClient if getattr(config, "transport", "stdio") == "http" else StdioMCPClient
        client = client_cls(config, timeout_s=timeout_s)
        client.start()
        registry = ToolRegistry()
        register_mcp_server(registry, client, config)
        return registry, client
    except Exception:
        log.exception("MCP server %r failed to start", config.name)
        return None, None


def stop_mcp_tools(client: Any | None) -> None:
    """Close the MCP client (and reap its subprocess).  Safe to call with None."""
    if client is not None:
        try:
            client.close()
        except Exception:
            log.exception("MCP client close failed")
