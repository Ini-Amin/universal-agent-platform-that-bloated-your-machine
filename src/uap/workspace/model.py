"""The Workspace contract (Master section 3).

A Workspace is a project/container -- *not* a Domain. A Domain is the kind of
work (research, coding, ...); a Workspace is the thing the work belongs to
("Security Research / Target X") and it groups the workflows, agents, tools,
skills, contexts, runs, artifacts, memory, knowledge, evaluations and
configurations produced for that project.

The model is a plain Pydantic value object: it holds no database handle and no
runtime state, so it can be created, serialized and compared without a session
(Master sections 2.3 and 73). The :class:`~uap.workspace.store.WorkspaceStore`
is what persists it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ..contracts.models import UTCDateTime, utc_now

__all__ = ["Workspace", "WorkspaceStatus"]


class WorkspaceStatus(StrEnum):
    """Lifecycle of a Workspace.

    ``ARCHIVED`` keeps a workspace resolvable by an explicit id/name hint while
    keeping it out of day-to-day listings; ``DELETED`` is the soft-delete marker
    written by :meth:`WorkspaceStore.delete`.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class Workspace(BaseModel):
    """A project/container that owns resources and scopes execution."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str = ""
    # Absolute path on disk that anchors the workspace's runs/artifacts.
    root_path: str = ""
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    created_at: UTCDateTime = Field(default_factory=utc_now)
    updated_at: UTCDateTime = Field(default_factory=utc_now)
    # Free-form workspace configuration (Master section 62: the Workspace layer
    # sits between User and Workflow). ``tags`` here feed workspace detection.
    settings: dict[str, object] = Field(default_factory=dict)
    # Exact global-Library references this workspace pins, e.g.
    # ``["recon-agent@v3", "httpx@v2"]`` (Master sections 4 and 67). Storing
    # exact refs -- never floating names -- is what keeps a workspace on v3 when
    # the Library publishes v4.
    default_workflow_refs: list[str] = Field(default_factory=list)
