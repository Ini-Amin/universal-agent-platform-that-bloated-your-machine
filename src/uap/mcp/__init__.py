"""MCP integration: stdio client + registry adapter (Master sections 12, 13, 29).

MCP is an integration protocol, not the platform architecture. Remote tools are
wrapped as ordinary registry tools so agents never depend on the transport.
"""

from uap.mcp.adapter import convert_input_schema, register_mcp_server
from uap.mcp.client import (
    CLIENT_INFO,
    PROTOCOL_VERSION,
    HttpMCPClient,
    MCPClientError,
    StdioMCPClient,
)
from uap.mcp.config import BUG_BOUNTY_MCP_CONFIG, DEFAULT_TIER, MCPServerConfig

__all__ = [
    "BUG_BOUNTY_MCP_CONFIG",
    "CLIENT_INFO",
    "DEFAULT_TIER",
    "MCPClientError",
    "MCPServerConfig",
    "PROTOCOL_VERSION",
    "StdioMCPClient",
    "HttpMCPClient",
    "convert_input_schema",
    "register_mcp_server",
]
