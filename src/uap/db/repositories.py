"""Typed repositories - the only place ORM queries live (Master sections 2.3,
2.4, 45, 46, 73).

Design rules:

* **Session is injected.** Every repository takes a :class:`~sqlalchemy.orm.Session`
  in its constructor and holds no other state. There is no global/ambient
  session anywhere (Master section 73: "Avoid global mutable state / hidden
  singleton behavior").
* **Repositories never commit.** Transaction boundaries belong to
  :func:`uap.db.engine.session_scope` (or the caller). This keeps multi-step
  operations - e.g. creating a definition and its first version - atomic.
* **Versions are immutable.** :meth:`DefinitionRepository.create_version`
  only ever ``INSERT``s. It never updates an existing version row, so a pinned
  v1 is byte-identical after v2 exists (Master section 2.4). The only mutable
  field on a version is ``status`` (draft -> active -> deprecated), changed
  through :meth:`DefinitionRepository.set_status`.
* **Event ``seq`` is allocated under a row lock** inside the writing
  transaction, so concurrent writers cannot produce duplicates or gaps.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from uap.db.models.definitions import (
    AgentDefinition,
    AgentVersion,
    SkillDefinition,
    SkillVersion,
    ToolDefinition,
    ToolVersion,
    VersionStatus,
    WorkflowDefinition,
    WorkflowVersion,
)
from uap.db.models.execution import Execution, ExecutionEvent, ExecutionStatus
from uap.db.models.workspace import WorkspaceRow

__all__ = [
    "DefinitionRepository",
    "EventRepository",
    "ExecutionRepository",
    "WorkspaceRepository",
    "canonical_json",
    "compute_content_hash",
]


# --------------------------------------------------------------------------- #
# Content hashing (Master section 50: reproducible packages)
# --------------------------------------------------------------------------- #

def canonical_json(spec: Mapping[str, Any]) -> str:
    """Render ``spec`` as canonical JSON: sorted keys, no insignificant space.

    Deterministic so that two structurally identical specs always hash the
    same regardless of key insertion order.
    """

    return json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_content_hash(spec: Mapping[str, Any]) -> str:
    """Return the sha256 hex digest of the canonical JSON form of ``spec``."""

    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Definitions + immutable versions
# --------------------------------------------------------------------------- #

class DefinitionRepository:
    """CRUD for a ``*_definitions`` / ``*_versions`` pair.

    Defaults to the workflow pair. Subclass (or override the two class
    attributes) to target agents/tools/skills - the shape is identical, which
    is exactly what Master section 67 prescribes.
    """

    definition_model: type[WorkflowDefinition] = WorkflowDefinition
    version_model: type[WorkflowVersion] = WorkflowVersion

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- definitions -------------------------------------------------------- #

    def create_definition(self, name: str) -> WorkflowDefinition:
        """Insert a new stable definition row and flush so ``id`` is populated."""

        definition = self.definition_model(name=name)
        self._session.add(definition)
        self._session.flush()
        return definition

    def get_definition(self, definition_id: uuid.UUID) -> WorkflowDefinition | None:
        """Return the definition row, or ``None`` if unknown."""

        return self._session.get(self.definition_model, definition_id)

    def get_definition_by_name(self, name: str) -> WorkflowDefinition | None:
        """Return the definition with this unique name, or ``None``."""

        stmt = select(self.definition_model).where(self.definition_model.name == name)
        return self._session.execute(stmt).scalar_one_or_none()

    # -- versions ----------------------------------------------------------- #

    def create_version(
        self,
        definition_id: uuid.UUID,
        spec: Mapping[str, Any],
        *,
        status: VersionStatus = VersionStatus.DRAFT,
        version: int | None = None,
    ) -> WorkflowVersion:
        """Append a new immutable version row for ``definition_id``.

        ``content_hash`` is always computed here from the canonical JSON of
        ``spec``. When ``version`` is omitted the next integer is allocated
        (``max(version) + 1``) inside the current transaction. Existing rows
        are never modified - publishing v2 leaves v1 untouched.
        """

        if version is None:
            version = self._next_version_number(definition_id)
        row = self.version_model(
            definition_id=definition_id,
            version=version,
            spec=dict(spec),
            status=status,
            content_hash=compute_content_hash(spec),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get_version(
        self, definition_id: uuid.UUID, version: int
    ) -> WorkflowVersion | None:
        """Return one exact version (the pinned reference), or ``None``."""

        stmt = select(self.version_model).where(
            self.version_model.definition_id == definition_id,
            self.version_model.version == version,
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def latest_version(
        self, definition_id: uuid.UUID, *, status: VersionStatus | None = None
    ) -> WorkflowVersion | None:
        """Return the highest-numbered version, optionally filtered by status."""

        stmt = select(self.version_model).where(
            self.version_model.definition_id == definition_id
        )
        if status is not None:
            stmt = stmt.where(self.version_model.status == status)
        stmt = stmt.order_by(self.version_model.version.desc()).limit(1)
        return self._session.execute(stmt).scalar_one_or_none()

    def list_versions(self, definition_id: uuid.UUID) -> list[WorkflowVersion]:
        """All versions for a definition, ascending by version number."""

        stmt = (
            select(self.version_model)
            .where(self.version_model.definition_id == definition_id)
            .order_by(self.version_model.version.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def set_status(
        self,
        definition_id: uuid.UUID,
        version: int,
        status: VersionStatus,
    ) -> WorkflowVersion:
        """Transition a version's lifecycle status (draft -> active -> ...).

        Raises :class:`LookupError` when the version does not exist.
        """

        row = self.get_version(definition_id, version)
        if row is None:
            raise LookupError(
                f"version {version} of definition {definition_id} not found"
            )
        row.status = status
        self._session.flush()
        return row

    # -- pin helpers (exact version references, Master section 67) ---------- #

    def pin(self, definition_id: uuid.UUID, version: int) -> WorkflowVersion:
        """Return the exact version, raising :class:`LookupError` if missing.

        Use this wherever a reference must not float to "latest": executions
        and cross-resource references pin a concrete version.
        """

        row = self.get_version(definition_id, version)
        if row is None:
            raise LookupError(
                f"cannot pin: version {version} of definition {definition_id} "
                "does not exist"
            )
        return row

    def pinned_reference(
        self, definition_id: uuid.UUID, version: int
    ) -> dict[str, Any]:
        """A storable, self-describing exact-version reference.

        Preserves the definition id, version number and content hash so a
        consumer can later verify the pinned content is unchanged.
        """

        row = self.pin(definition_id, version)
        return {
            "definition_id": str(row.definition_id),
            "version": row.version,
            "content_hash": row.content_hash,
        }

    # -- internal ----------------------------------------------------------- #

    def _next_version_number(self, definition_id: uuid.UUID) -> int:
        stmt = select(
            func.coalesce(func.max(self.version_model.version), 0)
        ).where(self.version_model.definition_id == definition_id)
        return int(self._session.execute(stmt).scalar_one()) + 1


class AgentDefinitionRepository(DefinitionRepository):
    """Same versioning semantics for agents (Master sections 5, 67)."""

    definition_model = AgentDefinition
    version_model = AgentVersion


class ToolDefinitionRepository(DefinitionRepository):
    """Same versioning semantics for tools (Master sections 6, 67)."""

    definition_model = ToolDefinition
    version_model = ToolVersion


class SkillDefinitionRepository(DefinitionRepository):
    """Same versioning semantics for skills (Master sections 7, 67)."""

    definition_model = SkillDefinition
    version_model = SkillVersion


#: Terminal execution statuses: rows in one of these states are finished and
#: safe to prune. ``pending``/``running``/``paused``/``awaiting_approval`` are
#: live and must never be deleted out from under a worker.
TERMINAL_EXECUTION_STATUSES: tuple[ExecutionStatus, ...] = (
    ExecutionStatus.COMPLETED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
)

# --------------------------------------------------------------------------- #
# Executions
# --------------------------------------------------------------------------- #

class ExecutionRepository:
    """Create and track runtime executions (Master sections 2.3, 21, 37, 67)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        workflow_version_id: uuid.UUID,
        *,
        input: Mapping[str, Any] | None = None,
        correlation_id: uuid.UUID | None = None,
        status: ExecutionStatus = ExecutionStatus.PENDING,
    ) -> Execution:
        """Insert a new execution pinned to ``workflow_version_id``.

        The foreign key is enforced by the database: a bad version id raises
        ``IntegrityError`` on flush.
        """

        row = Execution(
            workflow_version_id=workflow_version_id,
            status=status,
            input=dict(input) if input is not None else {},
            correlation_id=correlation_id,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get(self, execution_id: uuid.UUID) -> Execution | None:
        """Return the execution row, or ``None``."""

        return self._session.get(Execution, execution_id)

    def get_by_correlation_id(self, correlation_id: uuid.UUID) -> Execution | None:
        """Return the newest execution whose ``correlation_id`` matches.

        The HTTP API hands clients a task id that the slice pins as the
        execution's ``correlation_id`` (its own row ``id`` is generated by the
        service), so durable lookups must resolve either identifier.
        """

        from sqlalchemy import select

        stmt = (
            select(Execution)
            .where(Execution.correlation_id == correlation_id)
            .order_by(Execution.created_at.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def update_status(
        self,
        execution_id: uuid.UUID,
        status: ExecutionStatus,
        *,
        output: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> Execution:
        """Move an execution to ``status``, optionally recording output/error.

        Raises :class:`LookupError` when the execution does not exist.
        """

        row = self.get(execution_id)
        if row is None:
            raise LookupError(f"execution {execution_id} not found")
        row.status = status
        if output is not None:
            row.output = dict(output)
        if error is not None:
            row.error = error
        self._session.flush()
        return row

    def increment_resume_count(self, execution_id: uuid.UUID) -> Execution:
        """Bump ``resume_count`` (Master sections 37, 38)."""

        row = self.get(execution_id)
        if row is None:
            raise LookupError(f"execution {execution_id} not found")
        row.resume_count = row.resume_count + 1
        self._session.flush()
        return row

    def list(
        self,
        *,
        status: ExecutionStatus | None = None,
        workflow_version_id: uuid.UUID | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Execution]:
        """List executions, newest first, with optional filters."""

        stmt = select(Execution)
        if status is not None:
            stmt = stmt.where(Execution.status == status)
        if workflow_version_id is not None:
            stmt = stmt.where(Execution.workflow_version_id == workflow_version_id)
        stmt = stmt.order_by(Execution.created_at.desc(), Execution.id.desc())
        if offset:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.execute(stmt).scalars().all())

    def prune(self, cutoff: datetime, *, limit: int | None = None) -> int:
        """Delete terminal executions whose ``created_at`` is before ``cutoff``.

        Only executions in a terminal status (``completed`` / ``failed`` /
        ``cancelled``) are eligible; a live run (``pending`` / ``running`` /
        ``paused`` / ``awaiting_approval``) is never deleted out from under its
        worker, however old it is. Child rows (``execution_checkpoints``,
        ``execution_events``, ``decision_traces``) are removed by the
        ``ON DELETE CASCADE`` foreign keys.

        ``limit`` caps how many executions one call deletes so a large backlog
        does not lock the table; call repeatedly until the return value is 0.
        Returns the number of execution rows deleted.

        Retention is strictly opt-in: nothing calls this automatically — no
        background timer, no startup hook, no default policy. An installation
        that never invokes ``prune`` keeps every execution forever.
        """

        if limit is not None and limit <= 0:
            return 0
        stmt = delete(Execution).where(
            Execution.created_at < cutoff,
            Execution.status.in_(TERMINAL_EXECUTION_STATUSES),
        )
        if limit is not None:
            ids = (
                select(Execution.id)
                .where(
                    Execution.created_at < cutoff,
                    Execution.status.in_(TERMINAL_EXECUTION_STATUSES),
                )
                .order_by(Execution.created_at, Execution.id)
                .limit(limit)
            )
            stmt = delete(Execution).where(Execution.id.in_(ids))
        result = self._session.execute(stmt)
        self._session.flush()
        return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# Event store
# --------------------------------------------------------------------------- #

class EventRepository:
    """Append-only, per-execution ordered event log (Master sections 45, 46)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        execution_id: uuid.UUID,
        kind: str,
        *,
        node: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ExecutionEvent:
        """Append one event, allocating its per-execution ``seq``.

        Concurrency: PostgreSQL forbids ``FOR UPDATE`` together with aggregate
        functions, so instead of ``SELECT MAX(seq) ... FOR UPDATE`` we take a
        row-level lock on the **parent execution row**
        (``SELECT ... FROM executions WHERE id = :id FOR UPDATE``) and read
        ``COALESCE(MAX(seq), 0) + 1`` while holding it. The parent row is the
        single serialization point per execution, so two concurrent writers for
        the same execution cannot observe the same ``MAX`` - each transaction
        waits for the other to commit, then sees the committed ``seq``. The
        ``UNIQUE(execution_id, seq)`` constraint is the backstop that turns any
        residual race into a loud ``IntegrityError`` rather than silent
        corruption.
        """

        # Serialize concurrent appends for this execution on its parent row.
        lock_stmt = (
            select(Execution.id)
            .where(Execution.id == execution_id)
            .with_for_update()
        )
        locked = self._session.execute(lock_stmt).scalar_one_or_none()
        if locked is None:
            raise LookupError(f"execution {execution_id} not found")

        max_stmt = select(
            func.coalesce(func.max(ExecutionEvent.seq), 0)
        ).where(ExecutionEvent.execution_id == execution_id)
        next_seq = int(self._session.execute(max_stmt).scalar_one()) + 1

        row = ExecutionEvent(
            execution_id=execution_id,
            seq=next_seq,
            kind=kind,
            node=node,
            payload=dict(payload) if payload is not None else {},
        )
        self._session.add(row)
        self._session.flush()
        return row

    def read_since(
        self,
        execution_id: uuid.UUID,
        after_seq: int = 0,
        *,
        limit: int | None = None,
    ) -> Sequence[ExecutionEvent]:
        """Replay events with ``seq > after_seq``, ascending (Master section 46).

        ``after_seq=0`` returns the whole log; a UI that last saw ``seq=1``
        calls ``read_since(execution_id, 1)`` to resync with no gaps.
        """

        stmt = (
            select(ExecutionEvent)
            .where(
                ExecutionEvent.execution_id == execution_id,
                ExecutionEvent.seq > after_seq,
            )
            .order_by(ExecutionEvent.seq.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.execute(stmt).scalars().all())

    def count(self, execution_id: uuid.UUID) -> int:
        """Number of events recorded for an execution."""

        stmt = select(func.count(ExecutionEvent.id)).where(
            ExecutionEvent.execution_id == execution_id
        )
        return int(self._session.execute(stmt).scalar_one())


# --------------------------------------------------------------------------- #
# Workspaces (Master section 3)
# --------------------------------------------------------------------------- #

class WorkspaceRepository:
    """CRUD for ``workspaces`` rows (Master section 3).

    Implements the interface :class:`~uap.workspace.store.WorkspaceStore`
    discovers and documents (``create/get/list/update/delete``). Like every
    repository here it **flushes, never commits** -- the caller's
    :func:`~uap.db.engine.session_scope` owns the transaction.

    Rows are returned as :class:`~uap.db.models.workspace.WorkspaceRow`; the
    store converts them to its value object. This module stays free of any
    ``uap.workspace`` import, keeping the layering one-directional (``uap.db``
    must not depend on higher-level packages).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, workspace: Any) -> WorkspaceRow:
        """Insert ``workspace`` (a value object) and flush."""
        row = WorkspaceRow(
            id=str(getattr(workspace, "id")),
            name=str(getattr(workspace, "name")),
            description=str(getattr(workspace, "description", "") or ""),
            root_path=str(getattr(workspace, "root_path", "") or ""),
            status=_status_text(getattr(workspace, "status", "active")),
            created_at=getattr(workspace, "created_at", None),
            updated_at=getattr(workspace, "updated_at", None),
            settings=dict(getattr(workspace, "settings", None) or {}),
            default_workflow_refs=list(
                getattr(workspace, "default_workflow_refs", None) or []
            ),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def get(self, workspace_id: str) -> WorkspaceRow | None:
        return self._session.get(WorkspaceRow, str(workspace_id))

    def list(self) -> Sequence[WorkspaceRow]:
        stmt = select(WorkspaceRow).order_by(WorkspaceRow.created_at.desc())
        return list(self._session.execute(stmt).scalars().all())

    def update(self, workspace: Any) -> WorkspaceRow:
        row = self.get(getattr(workspace, "id"))
        if row is None:
            raise KeyError(f"no workspace with id {getattr(workspace, 'id')!r}")
        row.name = str(getattr(workspace, "name"))
        row.description = str(getattr(workspace, "description", "") or "")
        row.root_path = str(getattr(workspace, "root_path", "") or "")
        row.status = _status_text(getattr(workspace, "status", "active"))
        row.settings = dict(getattr(workspace, "settings", None) or {})
        row.default_workflow_refs = list(
            getattr(workspace, "default_workflow_refs", None) or []
        )
        row.updated_at = datetime.now(timezone.utc)
        self._session.flush()
        return row

    def delete(self, workspace_id: str) -> bool:
        """Soft-delete: mark ``status='deleted'``. ``True`` when a row changed."""
        row = self.get(workspace_id)
        if row is None:
            return False
        row.status = "deleted"
        row.updated_at = datetime.now(timezone.utc)
        self._session.flush()
        return True

def _status_text(value: Any) -> str:
    """Render a ``StrEnum``/string status as its plain string value."""
    return str(getattr(value, "value", value))
