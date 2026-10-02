"""Persistence for Workspaces, on top of the shared UAP database (Master §42).

A :class:`Workspace` is a pure value object; this store is the only place that
knows how to move one to and from the database. It works against the same
``sqlalchemy.orm.Session`` every other repository uses.

Transaction policy: the store owns its writes and commits each one, because it
manages its rows directly rather than through a repository. When a
``repository`` is injected instead, transaction policy follows that repository
(the ``uap.db`` convention is that repositories flush and the caller commits via
``session_scope``).

``uap.db`` (the ORM models) is built in parallel by another agent, so the import
is **lazy**: importing :mod:`uap.workspace.store` always succeeds, and the clear
:class:`ImportError` only fires when a store is actually constructed without a
database package present. That keeps ``from uap.workspace import WorkspaceStore``
usable while the DB package is still landing.

Expected ``uap.db`` surface
---------------------------
Either of these is accepted, tried in order:

* a repository, ``uap.db.WorkspaceRepository`` (or
  ``uap.db.repositories.WorkspaceRepository``) exposing
  ``create/get/list/update/delete``; or
* an ORM model, ``uap.db.models.Workspace`` (or ``uap.db.Workspace``), whose
  columns mirror :class:`~uap.workspace.model.Workspace`: ``id``, ``name``,
  ``description``, ``root_path``, ``status``, ``created_at``, ``updated_at``,
  ``settings`` (JSON), ``default_workflow_refs`` (JSON).

An explicit ``repository`` may be injected to bypass discovery entirely.
"""

from __future__ import annotations

from typing import Any

from ..contracts.models import utc_now
from .model import Workspace, WorkspaceStatus

__all__ = ["WorkspaceStore"]

# The concrete fields we copy between the ORM row and the value object.
_FIELDS = (
    "id",
    "name",
    "description",
    "root_path",
    "created_at",
    "updated_at",
    "settings",
    "default_workflow_refs",
)


def _load_workspace_model() -> object:
    """Resolve the ORM Workspace model, or raise a clear ImportError."""
    try:
        import uap.db  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on parallel agent
        raise ImportError(
            "WorkspaceStore requires the uap.db package (built in parallel), which "
            "is not importable in this environment yet. Construct the store once "
            "uap.db is available, or inject a repository explicitly with "
            "WorkspaceStore(session, repository=...)."
        ) from exc

    for module_path, attr in (
        ("uap.db.models", "Workspace"),
        ("uap.db", "Workspace"),
    ):
        try:
            module = __import__(module_path, fromlist=[attr])
        except ImportError:
            continue
        model = getattr(module, attr, None)
        if model is not None:
            return model

    raise ImportError(
        "uap.db is importable but exposes no 'Workspace' ORM model (looked in "
        "uap.db.models and uap.db). WorkspaceStore cannot persist without it."
    )


def _load_workspace_repository(session: object | None = None) -> object | None:
    """Resolve an optional WorkspaceRepository instance, or ``None``.

    ``WorkspaceRepository`` follows the ``uap.db`` convention: it is
    constructed with the shared :class:`Session`. The discovered class is
    therefore *instantiated* here (when a session is available) rather than
    returned as a bare class -- returning the class made every delegated call
    fail with ``missing 1 required positional argument: 'workspace'``.
    """
    for module_path, attr in (
        ("uap.db.repositories", "WorkspaceRepository"),
        ("uap.db", "WorkspaceRepository"),
    ):
        try:
            module = __import__(module_path, fromlist=[attr])
        except ImportError:
            continue
        repository = getattr(module, attr, None)
        if repository is None:
            continue
        if session is not None:
            try:
                return repository(session)
            except TypeError:
                # A repository whose constructor does not take a session.
                # Returning the bare CLASS here was a real defect: every
                # delegated call then failed with "missing 1 required
                # positional argument: 'workspace'" because the class was
                # never instantiated and `self` was never bound.
                # Try a no-arg construction instead, and only fall back to
                # the class when even that fails.
                try:
                    return repository()
                except TypeError:
                    return None
        return None
    return None


def _status_text(value: object) -> str:
    return str(getattr(value, "value", value))


class WorkspaceStore:
    """CRUD for :class:`Workspace` rows over a shared SQLAlchemy session."""

    def __init__(
        self,
        session: object,
        repository: object | None = None,
        model: object | None = None,
    ) -> None:
        self._session = session
        if repository is not None:
            self._repo = repository
            self._model: object | None = None
        elif model is not None:
            # Explicit ORM model: an injectable seam for tests and for callers
            # that own their table definition.
            self._repo = None
            self._model = model
        else:
            self._repo = _load_workspace_repository(session)
            # Resolve the ORM model eagerly so a missing uap.db fails here, with
            # the clear message above, rather than halfway through a write.
            self._model = None if self._repo is not None else _load_workspace_model()

    # ------------------------------------------------------------------ #
    # Create / read
    # ------------------------------------------------------------------ #

    def create(self, workspace: Workspace) -> Workspace:
        """Insert ``workspace`` and return the stored value object."""
        if self._repo is not None:
            stored = self._repo.create(workspace)  # type: ignore[attr-defined]
            return stored if isinstance(stored, Workspace) else self._from_repo(stored)

        row = self._model(  # type: ignore[operator]
            id=workspace.id,
            name=workspace.name,
            description=workspace.description,
            root_path=workspace.root_path,
            status=_status_text(workspace.status),
            created_at=workspace.created_at,
            updated_at=workspace.updated_at,
            settings=dict(workspace.settings),
            default_workflow_refs=list(workspace.default_workflow_refs),
        )
        self._session.add(row)  # type: ignore[attr-defined]
        self._session.commit()  # type: ignore[attr-defined]
        return self._to_model(row)

    def get(self, workspace_id: str) -> Workspace | None:
        """Return the workspace with ``workspace_id``, or ``None``."""
        if self._repo is not None:
            found = self._repo.get(workspace_id)  # type: ignore[attr-defined]
            if found is None:
                return None
            return found if isinstance(found, Workspace) else self._from_repo(found)

        row = (
            self._session.query(self._model)  # type: ignore[attr-defined]
            .filter(self._model.id == workspace_id)  # type: ignore[attr-defined]
            .one_or_none()
        )
        return None if row is None else self._to_model(row)

    def list(self, include_deleted: bool = False) -> list[Workspace]:
        """Return every workspace, newest first.

        Soft-deleted rows (status ``deleted``) are hidden unless
        ``include_deleted`` is set.
        """
        if self._repo is not None:
            rows = self._repo.list()  # type: ignore[attr-defined]
            models = [r if isinstance(r, Workspace) else self._from_repo(r) for r in rows]
        else:
            query = self._session.query(self._model)  # type: ignore[attr-defined]
            if not include_deleted:
                query = query.filter(
                    self._model.status != WorkspaceStatus.DELETED.value  # type: ignore
                )
            models = [self._to_model(row) for row in query.all()]

        if not include_deleted:
            models = [m for m in models if m.status != WorkspaceStatus.DELETED]
        models.sort(key=lambda w: (w.created_at, w.id), reverse=True)
        return models

    # ------------------------------------------------------------------ #
    # Update / delete
    # ------------------------------------------------------------------ #

    def update(self, workspace: Workspace) -> Workspace:
        """Persist ``workspace`` (matched by id), bumping ``updated_at``."""
        if self._repo is not None:
            stored = self._repo.update(workspace)  # type: ignore[attr-defined]
            return stored if isinstance(stored, Workspace) else self._from_repo(stored)

        row = (
            self._session.query(self._model)  # type: ignore[attr-defined]
            .filter(self._model.id == workspace.id)  # type: ignore[attr-defined]
            .one_or_none()
        )
        if row is None:
            raise KeyError(f"no workspace with id {workspace.id!r}")
        for field in _FIELDS:
            if field == "id":
                continue
            value = getattr(workspace, field)
            setattr(row, field, _status_text(value) if field == "status" else value)
        row.updated_at = utc_now()
        self._session.commit()  # type: ignore[attr-defined]
        return self._to_model(row)

    def delete(self, workspace_id: str) -> bool:
        """Soft-delete a workspace. Returns ``True`` when a row was affected."""
        if self._repo is not None:
            return bool(self._repo.delete(workspace_id))  # type: ignore[attr-defined]

        row = (
            self._session.query(self._model)  # type: ignore[attr-defined]
            .filter(self._model.id == workspace_id)  # type: ignore[attr-defined]
            .one_or_none()
        )
        if row is None:
            return False
        row.status = WorkspaceStatus.DELETED.value
        row.updated_at = utc_now()
        self._session.commit()  # type: ignore[attr-defined]
        return True

    # ------------------------------------------------------------------ #
    # Conversions
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_model(row: object) -> Workspace:
        return Workspace(
            id=str(getattr(row, "id")),
            name=str(getattr(row, "name")),
            description=str(getattr(row, "description", "") or ""),
            root_path=str(getattr(row, "root_path", "") or ""),
            status=WorkspaceStatus(_status_text(getattr(row, "status", "active"))),
            created_at=getattr(row, "created_at"),
            updated_at=getattr(row, "updated_at"),
            settings=dict(getattr(row, "settings", None) or {}),
            default_workflow_refs=list(getattr(row, "default_workflow_refs", None) or []),
        )

    @staticmethod
    def _from_repo(value: object) -> Workspace:
        if isinstance(value, Workspace):
            return value
        if isinstance(value, dict):
            return Workspace.model_validate(value)
        return Workspace.model_validate(
            {field: getattr(value, field) for field in _FIELDS if hasattr(value, field)}
            | {"status": _status_text(getattr(value, "status", "active"))}
        )
