"""Regression test: the MCP binary path must not reach an API caller.

``/api/resources/mcp`` used to return ``command`` verbatim, which is the
operator's absolute host path (``/home/user/bugbounty-mcp/bbmcp`` by default),
publishing the host's directory layout to anyone who could reach the port.
``MCPServerConfig.command_display`` is the redacted view the route serves:
launch still uses ``command``, which stays the real path.
"""

from __future__ import annotations

import os

from uap.mcp.config import BUG_BOUNTY_MCP_CONFIG, MCPServerConfig


def test_command_display_hides_the_host_path() -> None:
    assert BUG_BOUNTY_MCP_CONFIG.command == "/home/user/bugbounty-mcp/bbmcp"
    assert BUG_BOUNTY_MCP_CONFIG.command_display == "bbmcp"
    assert os.sep not in BUG_BOUNTY_MCP_CONFIG.command_display


def test_command_display_follows_the_env_override(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("UAP_BBMCP_BIN", "/opt/vendor/bbmcp-v2")
    cfg = MCPServerConfig(name="bb", command=os.environ["UAP_BBMCP_BIN"])
    assert cfg.command_display == "bbmcp-v2"


def test_command_display_is_empty_for_a_http_server() -> None:
    cfg = MCPServerConfig(name="remote", transport="http", url="https://mcp.example/rpc")
    assert cfg.command is None
    assert cfg.command_display == ""