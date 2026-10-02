"""Configuration for MCP stdio servers (Master sections 12, 13, 29).

A server is described by how to launch it (command + args + env) and by the risk
tier each of its tools carries. The tier map is *per server* because risk is a
property of what a tool does, not of the transport: the same registry policy
guard (uap.tools.policy) then applies to local and remote tools alike.

Tiers follow docs/research/bugbounty-mcp-inventory.md section 4:
0 pure local, 1 passive external, 2 active external, 3 side-effectful.

Fail-safe default: a tool the map does not mention is treated as tier 3
(side-effectful), so an unreviewed remote capability needs explicit approval
rather than silently running. Policy is enforced outside LLM reasoning
(Master section 9 / rule 17).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_TIER = 3


def _find_bbmcp() -> str:
    """Locate the bugbounty-mcp binary, or return the documented default.

    The old default was the absolute path ``/home/user/bugbounty-mcp/bbmcp`` —
    a developer machine path. On any other host MCP silently failed to start and
    the platform fell back to stub collectors, so a "bug bounty" run produced
    zero real reconnaissance. Verified 2026-10-03: the binary existed at
    ``/home/amen/bugbounty-mcp/bbmcp`` and worked, while the configured path did
    not exist, so MCP never ran.

    Resolution order:
      1. ``UAP_BBMCP_BIN`` — an explicit override always wins.
      2. ``bbmcp`` on ``PATH``.
      3. A sibling checkout next to this repository (``../bugbounty-mcp/bbmcp``).
      4. The user's home (``~/bugbounty-mcp/bbmcp``).
      5. The legacy literal, so the failure message stays recognisable.
    """

    override = os.environ.get("UAP_BBMCP_BIN")
    if override:
        return override

    on_path = shutil.which("bbmcp")
    if on_path:
        return on_path

    candidates = [
        Path(__file__).resolve().parents[3].parent / "bugbounty-mcp" / "bbmcp",
        Path.home() / "bugbounty-mcp" / "bbmcp",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return "/home/user/bugbounty-mcp/bbmcp"


class MCPServerConfig(BaseModel):
    """How to launch or connect to one MCP server and how to tier its tools."""

    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str | None = None
    url: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # tool name -> risk tier; a tool missing from the map falls back to
    # default_tier (3, fail-safe: unreviewed remote tools need approval).
    tier_map: dict[str, int] = Field(default_factory=dict)
    default_tier: int = Field(default=DEFAULT_TIER, ge=0, le=3)

    @model_validator(mode="after")
    def _validate_transport_requirements(self) -> "MCPServerConfig":
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio transport requires 'command'")
        elif self.transport == "http":
            if not self.url:
                raise ValueError("http transport requires 'url'")
        return self
    @field_validator("tier_map")
    @classmethod
    def _tiers_in_range(cls, value: dict[str, int]) -> dict[str, int]:
        for tool, tier in value.items():
            if not isinstance(tier, int) or tier < 0 or tier > 3:
                raise ValueError(f"risk tier for {tool!r} must be 0-3, got {tier!r}")
        return value

    @property
    def command_display(self) -> str:
        """Binary name only -- what an API caller may see.

        ``command`` is where the operator's server actually lives (an absolute
        host path by default), so returning it verbatim from
        ``/api/resources/mcp`` publishes the host's directory layout to anyone
        who can reach the port. Launch still uses :attr:`command`.
        """
        return os.path.basename(self.command) if self.command else ""


# --------------------------------------------------------------------------- #
# bugbounty-mcp: the first real MCP server wired into UAP.
#
# Source: docs/research/bugbounty-mcp-inventory.md (verified by direct source
# reading of a local bugbounty-mcp checkout, mcp-go v1.1.1, stdio mode `./bbmcp`).
# The 11 documented tools and their inventory tiers are mapped below; anything
# the server adds later lands on default_tier=3 until it is reviewed.
# --------------------------------------------------------------------------- #
BUG_BOUNTY_MCP_CONFIG = MCPServerConfig(
    name="bugbounty-mcp",
    # Discovered, not hardcoded: an absolute developer-machine path made MCP
    # silently fail on every other host. Override with UAP_BBMCP_BIN.
    command=_find_bbmcp(),
    args=[],
    env={"CLAUDE_BIN": "claude"},
    tier_map={
        # Tier 0 - pure local, deterministic.
        "validate_scope": 0,
        "list_agents": 0,
        "list_skills": 0,
        "list_mcp_servers": 0,
        "get_run": 0,
        # Tier 1 - passive external (third-party services, no target traffic).
        "lookup_dns": 1,
        "find_subdomains_passive": 1,
        # Tier 2 - active against the target.
        "probe_http": 2,
        # Tier 3 - subprocess / side-effectful (require explicit approval).
        "run_agent": 3,
        "stop_run": 3,
        "test_mcp_server": 3,
    },
    default_tier=DEFAULT_TIER,
)

__all__ = ["DEFAULT_TIER", "BUG_BOUNTY_MCP_CONFIG", "MCPServerConfig"]
