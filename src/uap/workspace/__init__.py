"""Workspace model, deterministic detection and persistence (Master section 3).

A Workspace is a project/container that groups the workflows, agents, tools,
skills, contexts, runs, artifacts, memory, knowledge, evaluations and
configurations belonging to one project. It is *not* a Domain: the Domain is the
kind of work, the Workspace is where the work lives.

Public API::

    from uap.workspace import Workspace, WorkspaceDetector, WorkspaceStore

    workspace = Workspace(id="ws-1", name="Security Research / Target X")
    result = WorkspaceDetector().detect(task_spec, [workspace])
    if result.best is not None:
        ...  # propose it to the user; never switch silently

Detection is deterministic (no LLM) and never mutates state; see
:mod:`uap.workspace.detection` for the documented scoring rules.
"""

from .detection import (
    DEFAULT_AMBIGUITY_MARGIN,
    DEFAULT_THRESHOLD,
    Candidate,
    DetectionResult,
    WorkspaceDetector,
)
from .model import Workspace, WorkspaceStatus
from .store import WorkspaceStore

__all__ = [
    "Candidate",
    "DEFAULT_AMBIGUITY_MARGIN",
    "DEFAULT_THRESHOLD",
    "DetectionResult",
    "Workspace",
    "WorkspaceDetector",
    "WorkspaceStatus",
    "WorkspaceStore",
]
