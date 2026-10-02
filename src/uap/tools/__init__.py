"""Tool Registry: implementation-agnostic tool surface (Master sections 9, 12, 13).

Agents call capabilities by name through the registry and never need to know
whether a tool is local Python, an API, or an MCP tool.
"""

from uap.tools.local import (
    echo,
    read_text_file,
    register_local_tools,
    write_artifact_file,
)
from uap.tools.policy import DEFAULT_ALLOWED_TIERS, PolicyDecision, ToolPolicy
from uap.tools.registry import RISK_TIERS, ToolRegistry, ToolSpec

__all__ = [
    "DEFAULT_ALLOWED_TIERS",
    "RISK_TIERS",
    "PolicyDecision",
    "ToolPolicy",
    "ToolRegistry",
    "ToolSpec",
    "echo",
    "read_text_file",
    "register_local_tools",
    "write_artifact_file",
]
