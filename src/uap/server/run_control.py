"""Cooperative pause/resume for in-memory workflow runs.

Semantics: pause takes effect BETWEEN nodes. When paused, the next node
invocation blocks on an ``asyncio.Event`` until ``resume()`` is called.
A running node is never interrupted mid-execution.
"""

from __future__ import annotations

import asyncio

__all__ = ["RunControl", "run_controls", "get_control", "drop_control"]


class RunControl:
    """Per-task pause gate.  ``_gate`` is set (open) when running."""

    __slots__ = ("task_id", "status", "_gate")

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.status: str = "running"
        self._gate = asyncio.Event()
        self._gate.set()  # starts open

    def pause(self) -> str:
        """Request pause; takes effect before the next node."""
        if self.status in ("completed", "failed"):
            return self.status
        self._gate.clear()
        self.status = "paused"
        return "paused"

    def resume(self) -> str:
        """Resume a paused run."""
        if self.status in ("completed", "failed"):
            return self.status
        self._gate.set()
        self.status = "running"
        return "running"

    async def wait_if_paused(self) -> None:
        """Block until the gate is open (no-op when running)."""
        await self._gate.wait()


# Global registry; one entry per in-memory task_id.
_controls: dict[str, RunControl] = {}


def get_control(task_id: str) -> RunControl:
    """Get or create the control for ``task_id``."""
    ctrl = _controls.get(task_id)
    if ctrl is None:
        ctrl = RunControl(task_id)
        _controls[task_id] = ctrl
    return ctrl


def run_controls() -> dict[str, RunControl]:
    """Return the live registry (for cleanup / inspection)."""
    return _controls


def drop_control(task_id: str) -> RunControl | None:
    """Remove and return the control for ``task_id`` (e.g. after terminal state)."""
    return _controls.pop(task_id, None)
