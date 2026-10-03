"""Agent-facing bridge to the local canvas API (never accepts an agent-supplied URL)."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable
from urllib.request import Request, urlopen

from uap.tools.registry import ToolSpec


def _request(method: str, endpoint: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call only the operator-configured UAP server, not arbitrary model-provided hosts."""
    base = os.environ.get("UAP_CANVAS_URL", "http://127.0.0.1:8090").rstrip("/")
    if not (base.startswith("http://127.0.0.1:") or base.startswith("http://localhost:")):
        raise ValueError("UAP_CANVAS_URL must point to the local UAP server")
    token = os.environ.get("UAP_API_TOKEN", "")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(base + endpoint, data=data, headers=headers, method=method)
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def canvas_commands() -> tuple[ToolSpec, Callable[..., Awaitable[Any]]]:
    """Deliver UI commands to every connected browser canvas."""
    spec = ToolSpec(
        name="canvas_commands",
        description="Open a workspace file, add a view, or focus a card in connected user canvases.",
        risk_tier=3,
        input_schema={"commands": {
            "type": "array", "required": True,
            "description": "Canvas commands: open_file(path, optional root), add_view(spec), or focus_view(id).",
        }},
    )

    async def fn(commands: list[dict[str, Any]]) -> dict[str, Any]:
        return await asyncio.to_thread(_request, "POST", "/api/canvas/commands", {"commands": commands})

    return spec, fn


def canvas_state() -> tuple[ToolSpec, Callable[..., Awaitable[Any]]]:
    """Inspect the latest view/file/focus state of connected browser tabs."""
    spec = ToolSpec(
        name="canvas_state", description="Read the views, focused card, and open editor files in connected canvases.",
        risk_tier=0, input_schema={},
    )

    async def fn() -> dict[str, Any]:
        return await asyncio.to_thread(_request, "GET", "/api/canvas/state")

    return spec, fn
