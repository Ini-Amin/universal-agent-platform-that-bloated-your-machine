"""Local tools: the only tools in the platform that touch the filesystem.

Each factory returns (ToolSpec, async fn); `register_local_tools` wires them into
a registry. The same shape is how a future MCP adapter will register remote
tools, so agents never see the difference (Master sections 12 and 13).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from uap.tools.registry import ToolRegistry, ToolSpec
from uap.tools.canvas import canvas_commands, canvas_state

MAX_READ_BYTES = 64 * 1024

ToolFactory = Callable[[], tuple[ToolSpec, Callable[..., Awaitable[Any]]]]


def _confine(path: str, root: Path) -> Path:
    """Resolve `path` against `root`, refusing anything that escapes it."""
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise PermissionError(f"path escapes the artifacts root: {path}")
    return target


def write_artifact_file(
    artifacts_root: Path,
) -> tuple[ToolSpec, Callable[..., Awaitable[Any]]]:
    """Tier 3 (side-effectful): write UTF-8 text under the artifacts root."""
    root = Path(artifacts_root).resolve()

    spec = ToolSpec(
        name="write_artifact_file",
        description="Write a text artifact file under the configured artifacts root.",
        risk_tier=3,
        input_schema={
            "path": {
                "type": "string",
                "required": True,
                "description": "Path relative to the artifacts root.",
            },
            "content": {
                "type": "string",
                "required": True,
                "description": "UTF-8 text to write.",
            },
        },
    )

    async def fn(path: str, content: str) -> str:
        target = _confine(path, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return str(target)

    return spec, fn


def read_text_file(
    artifacts_root: Path,
) -> tuple[ToolSpec, Callable[..., Awaitable[Any]]]:
    """Tier 0 (pure local): read at most 64 KiB of UTF-8 text from the artifacts root."""
    root = Path(artifacts_root).resolve()

    spec = ToolSpec(
        name="read_text_file",
        description="Read a text file from the artifacts root (max 64 KiB).",
        risk_tier=0,
        input_schema={
            "path": {
                "type": "string",
                "required": True,
                "description": "Path relative to the artifacts root.",
            }
        },
    )

    async def fn(path: str) -> str:
        target = _confine(path, root)
        with target.open("rb") as handle:
            return handle.read(MAX_READ_BYTES).decode("utf-8", errors="replace")

    return spec, fn


def echo() -> tuple[ToolSpec, Callable[..., Awaitable[Any]]]:
    """Tier 0 (pure local): return its arguments unchanged (wiring smoke test)."""
    spec = ToolSpec(
        name="echo",
        description="Return the call arguments unchanged. Used to verify registry wiring.",
        risk_tier=0,
        input_schema={},
    )

    async def fn(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    return spec, fn


def register_local_tools(registry: ToolRegistry, artifacts_root: Path) -> None:
    """Register the platform's local tools into `registry`."""
    for spec, fn in (
        write_artifact_file(artifacts_root),
        read_text_file(artifacts_root),
        echo(),
        canvas_commands(),
        canvas_state(),
    ):
        registry.register(spec, fn)
