"""The Global Library service (Master sections 4 and 2.4).

The Library holds *definitions* -- agents, tools, skills and workflows -- as
immutable, explicitly versioned documents. This service is the only sanctioned
way to create or move them, and it enforces the two rules that matter:

1. **Never mutate a version.** Editing a definition means creating a new
   version; ``v3`` keeps meaning exactly what it meant the day it was written
   (Master section 2.4: "Updating v3 to v4 must NOT silently change workflows
   using v3").
2. **References are exact.** A workspace (or any other consumer) pins
   ``name@v3`` and stays there. ``resolve_ref("name")`` is the one convenience
   that floats to the *latest active* version, and it never floats to a draft.

Lifecycle (Master section 4: Draft / Active / Archived / Deprecated)::

    register(name, spec)        -> v1, status "draft"
    publish(def_id, version)    -> status "active"
    new_version(def_id, spec)   -> v(n+1), status "draft"
    deprecate(def_id, version)  -> status "deprecated"

Persistence goes through ``uap.db``. Two shapes are supported, in this order:

* **An injected repository** implementing the pinned, kind-parameterised
  interface (``create_definition(kind, name)``, ``create_version``,
  ``get_version``, ``latest_version(definition_id, active_only=False)``,
  ``set_status``) -- this is the interface :class:`LibraryService` documents and
  the one unit tests use.
* **A real session**, wrapped by :class:`_RealDefinitionRepository`, an adapter
  over ``uap.db``'s per-kind repositories (``AgentDefinitionRepository`` ...),
  which are keyed by kind rather than taking a ``kind`` argument.

The ``uap.db`` import is lazy, so this module imports (and is unit-testable with
an injected repository) even before the database package exists.
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from pydantic import BaseModel

from .models import SPEC_MODELS

__all__ = [
    "LibraryService",
    "LibraryEntry",
    "LibraryError",
    "DuplicateDefinitionError",
    "UnknownReferenceError",
    "KINDS",
    "STATUS_DRAFT",
    "STATUS_ACTIVE",
    "STATUS_DEPRECATED",
]

# The four reusable kinds the Library stores today (Master section 4). Context
# templates and evaluation definitions slot in here later without changing the
# service shape.
KINDS: tuple[str, ...] = ("agent", "tool", "skill", "workflow")

STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_DEPRECATED = "deprecated"

# Library kind -> the per-kind repository class name in uap.db.repositories.
_REPOSITORY_NAMES: dict[str, str] = {
    "agent": "AgentDefinitionRepository",
    "tool": "ToolDefinitionRepository",
    "skill": "SkillDefinitionRepository",
    "workflow": "DefinitionRepository",  # the base class targets workflows
}


class LibraryError(Exception):
    """Base class for Library failures."""


class DuplicateDefinitionError(ValueError, LibraryError):
    """A definition with this kind+name already exists.

    Registering is not an upsert: callers must use ``new_version`` instead, so a
    name never silently changes meaning (Master sections 2.4 and 68).
    """


class UnknownReferenceError(KeyError, LibraryError):
    """A ``name@version`` reference did not resolve.

    Subclasses :class:`KeyError` so ``resolve_ref`` callers can catch the
    standard exception, but the message is always explicit.
    """


@dataclass(frozen=True)
class LibraryEntry:
    """A resolved (definition, version) pair -- the unit the Library returns."""

    definition_id: str
    kind: str
    name: str
    version: int
    spec: dict[str, Any] = field(default_factory=dict)
    status: str = STATUS_DRAFT
    tags: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        """The exact reference for this entry, e.g. ``"recon-agent@v3"``."""
        return f"{self.name}@v{self.version}"


# --------------------------------------------------------------------------- #
# Attribute access helpers -- ORM objects are the other agent's contract, so we
# read them defensively but predictably.
# --------------------------------------------------------------------------- #

def _definition_id(definition: object) -> str:
    for attr in ("id", "definition_id"):
        value = getattr(definition, attr, None)
        if value is not None:
            return str(value)
    raise LibraryError(f"definition object {definition!r} has no id attribute")


def _definition_kind(definition: object) -> str:
    return str(getattr(definition, "kind", "") or "")


def _definition_name(definition: object) -> str:
    return str(getattr(definition, "name", "") or "")


def _version_number(version: object) -> int:
    value = getattr(version, "version", None)
    if value is None:
        raise LibraryError(f"version object {version!r} has no version attribute")
    return int(value)


def _status_value(version: object) -> str:
    raw = getattr(version, "status", STATUS_DRAFT)
    return str(getattr(raw, "value", raw))


def _spec_value(version: object) -> dict[str, Any]:
    raw = getattr(version, "spec", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _tags_of(obj: object) -> tuple[str, ...]:
    raw = getattr(obj, "tags", None)
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    try:
        return tuple(str(item) for item in raw)
    except TypeError:
        return ()


def _to_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class _DefinitionRow:
    """A normalised definition identity, independent of the ORM model used."""

    id: str
    kind: str
    name: str
    tags: tuple[str, ...] = ()


class _RealDefinitionRepository:
    """Adapter over ``uap.db``'s per-kind repositories.

    Presents the kind-parameterised interface :class:`LibraryService` expects,
    while delegating to the real ``AgentDefinitionRepository`` /
    ``ToolDefinitionRepository`` / ``SkillDefinitionRepository`` and the
    workflow-backed base ``DefinitionRepository``.
    """

    def __init__(self, session: object) -> None:
        try:
            from uap.db.models.definitions import VersionStatus
            from uap.db.repositories import (
                AgentDefinitionRepository,
                SkillDefinitionRepository,
                ToolDefinitionRepository,
            )
        except ImportError as exc:  # pragma: no cover - depends on parallel agent
            raise ImportError(
                "LibraryService requires the uap.db package (uap.db.repositories "
                "and uap.db.models.definitions), which is not importable yet. "
                "Wait for the database package or inject a repository explicitly: "
                "LibraryService(repository=...)."
            ) from exc

        from uap.db.repositories import DefinitionRepository

        self._session = session
        self._VersionStatus = VersionStatus
        classes = {
            "agent": AgentDefinitionRepository,
            "tool": ToolDefinitionRepository,
            "skill": SkillDefinitionRepository,
            "workflow": DefinitionRepository,
        }
        self._repos: dict[str, Any] = {
            kind: cls(session) for kind, cls in classes.items()
        }

    # -- internal ------------------------------------------------------- #

    def _kind_of(self, definition_id: str) -> str | None:
        identifier = _to_uuid(definition_id)
        if identifier is None:
            return None
        for kind, repo in self._repos.items():
            if repo.get_definition(identifier) is not None:
                return kind
        return None

    def _wrap(self, kind: str, row: object) -> _DefinitionRow:
        return _DefinitionRow(
            id=str(getattr(row, "id")),
            kind=kind,
            name=str(getattr(row, "name", "")),
            tags=(),
        )

    # -- definitions ---------------------------------------------------- #

    def create_definition(
        self, kind: str, name: str, tags: Iterable[str] | None = None
    ) -> _DefinitionRow:
        row = self._repos[kind].create_definition(name)
        return self._wrap(kind, row)

    def get_definition(self, definition_id: str) -> _DefinitionRow | None:
        identifier = _to_uuid(definition_id)
        if identifier is None:
            return None
        for kind, repo in self._repos.items():
            row = repo.get_definition(identifier)
            if row is not None:
                return self._wrap(kind, row)
        return None

    def get_definition_by_name(self, kind: str, name: str) -> _DefinitionRow | None:
        row = self._repos[kind].get_definition_by_name(name)
        return None if row is None else self._wrap(kind, row)

    def list_definitions(self, kind: str | None = None) -> list[_DefinitionRow]:
        from sqlalchemy import select

        kinds = [kind] if kind is not None else list(self._repos)
        rows: list[_DefinitionRow] = []
        for current in kinds:
            repo = self._repos[current]
            model = repo.definition_model
            for row in self._session.execute(select(model)).scalars().all():
                rows.append(self._wrap(current, row))
        return rows

    # -- versions ------------------------------------------------------- #

    def create_version(self, definition_id: str, spec: dict[str, Any]) -> object:
        kind = self._kind_of(definition_id)
        if kind is None:
            raise LookupError(f"no definition with id {definition_id!r}")
        identifier = _to_uuid(definition_id)
        return self._repos[kind].create_version(identifier, spec)

    def get_version(self, definition_id: str, version: int) -> object | None:
        identifier = _to_uuid(definition_id)
        if identifier is None:
            return None
        for repo in self._repos.values():
            row = repo.get_version(identifier, version)
            if row is not None:
                return row
        return None

    def latest_version(self, definition_id: str, active_only: bool = False) -> object | None:
        identifier = _to_uuid(definition_id)
        if identifier is None:
            return None
        status = self._VersionStatus.ACTIVE if active_only else None
        for repo in self._repos.values():
            row = repo.latest_version(identifier, status=status)
            if row is not None:
                return row
        return None

    def set_status(self, definition_id: str, version: int, status: str) -> object | None:
        kind = self._kind_of(definition_id)
        if kind is None:
            return None
        identifier = _to_uuid(definition_id)
        return self._repos[kind].set_status(
            identifier, version, self._VersionStatus(status)
        )


class LibraryService:
    """Create, version, publish and resolve global-Library definitions.

    Parameters
    ----------
    session:
        A ``sqlalchemy.orm.Session`` bound to the UAP database. Used to build
        the default repository adapter over ``uap.db``.
    repository:
        An explicit repository implementing the pinned interface. Passing this
        bypasses the ``uap.db`` import entirely, which is how the service is
        unit-tested before (or independently of) the database package.

    Pinned repository interface
    ---------------------------
    ::

        create_definition(kind, name[, tags])   -> Definition
        create_version(definition_id, spec)     -> Version
        get_version(definition_id, version)     -> Version | None
        latest_version(definition_id, active_only=False) -> Version | None
        set_status(definition_id, version, status)

    Optional, used when present: ``get_definition``, ``get_definition_by_name``,
    ``list_definitions``. The default adapter over ``uap.db`` provides all of
    them.
    """

    def __init__(
        self,
        session: object | None = None,
        repository: object | None = None,
    ) -> None:
        if repository is not None:
            self._session = session
            self._repo = repository
        else:
            if session is None:
                raise ValueError(
                    "LibraryService needs a SQLAlchemy session or an explicit repository"
                )
            self._session = session
            self._repo = _RealDefinitionRepository(session)
        # Tags are not a first-class column in uap.db yet; keep them for the
        # life of the service and prefer any tags the repository does expose.
        self._tags: dict[str, tuple[str, ...]] = {}

    # ------------------------------------------------------------------ #
    # Registration (per kind + generic)
    # ------------------------------------------------------------------ #

    def register_agent(
        self, name: str, spec: dict[str, Any], tags: Iterable[str] | None = None
    ) -> LibraryEntry:
        return self.register("agent", name, spec, tags)

    def register_tool(
        self, name: str, spec: dict[str, Any], tags: Iterable[str] | None = None
    ) -> LibraryEntry:
        return self.register("tool", name, spec, tags)

    def register_skill(
        self, name: str, spec: dict[str, Any], tags: Iterable[str] | None = None
    ) -> LibraryEntry:
        return self.register("skill", name, spec, tags)

    def register_workflow(
        self, name: str, spec: dict[str, Any], tags: Iterable[str] | None = None
    ) -> LibraryEntry:
        return self.register("workflow", name, spec, tags)

    def register(
        self,
        kind: str,
        name: str,
        spec: dict[str, Any] | BaseModel,
        tags: Iterable[str] | None = None,
    ) -> LibraryEntry:
        """Register a new definition and create its ``v1`` draft.

        Raises :class:`DuplicateDefinitionError` (a ``ValueError``) if the
        kind+name already exists -- registering is never an upsert.
        """
        kind = self._check_kind(kind)
        if not name or not str(name).strip():
            raise ValueError("definition name must be a non-empty string")
        if self._find_definition(kind, name) is not None:
            raise DuplicateDefinitionError(
                f"{kind} {name!r} already exists in the library; "
                f"use new_version(definition_id, spec) to add a version"
            )

        validated = self._validate_spec(kind, spec)
        tag_tuple = tuple(str(t) for t in (tags or ()))
        definition = self._create_definition(kind, name, tag_tuple)
        version = self._repo.create_version(  # type: ignore[attr-defined]
            _definition_id(definition), validated.model_dump()
        )
        self._tags[_definition_id(definition)] = tag_tuple
        return self._entry(definition, version)

    def new_version(
        self, definition_id: str, spec: dict[str, Any] | BaseModel
    ) -> LibraryEntry:
        """Create the next version (``n+1``) of an existing definition.

        The new version starts as a draft. Existing versions are never touched.
        """
        definition = self._get_definition(definition_id)
        if definition is None:
            raise UnknownReferenceError(f"no definition with id {definition_id!r}")
        kind = self._check_kind(_definition_kind(definition))
        validated = self._validate_spec(kind, spec)
        version = self._repo.create_version(  # type: ignore[attr-defined]
            _definition_id(definition), validated.model_dump()
        )
        return self._entry(definition, version)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def publish(self, definition_id: str, version: int) -> LibraryEntry:
        """Move ``definition_id`` at ``version`` to ``active``."""
        return self._set_status(definition_id, version, STATUS_ACTIVE)

    def deprecate(self, definition_id: str, version: int) -> LibraryEntry:
        """Move ``definition_id`` at ``version`` to ``deprecated``."""
        return self._set_status(definition_id, version, STATUS_DEPRECATED)

    def _set_status(self, definition_id: str, version: int, status: str) -> LibraryEntry:
        definition = self._get_definition(definition_id)
        if definition is None:
            raise UnknownReferenceError(f"no definition with id {definition_id!r}")
        stored = self._repo.get_version(definition_id, version)  # type: ignore
        if stored is None:
            raise UnknownReferenceError(
                f"{_definition_name(definition)!r} has no version {version}"
            )
        self._repo.set_status(definition_id, version, status)  # type: ignore
        refreshed = self._repo.get_version(definition_id, version)  # type: ignore
        return self._entry(definition, refreshed if refreshed is not None else stored)

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def get(self, name: str, version: int | None = None, kind: str | None = None):
        """Return a :class:`LibraryEntry`, or ``None`` when it does not exist.

        * ``version`` given  -> that exact version, whatever its status.
        * ``version`` omitted -> the latest **active** version (never a draft).
        """
        definitions = self._definitions_by_name(name, kind)
        if not definitions:
            return None
        definition = definitions[0]
        definition_id = _definition_id(definition)
        if version is None:
            stored = self._repo.latest_version(  # type: ignore[attr-defined]
                definition_id, active_only=True
            )
        else:
            stored = self._repo.get_version(definition_id, version)  # type: ignore
        if stored is None:
            return None
        return self._entry(definition, stored)

    def resolve_ref(self, ref: str) -> LibraryEntry:
        """Resolve an exact or floating reference string.

        ``"name@v3"`` -> exactly version 3 (any status).
        ``"name"``    -> latest **active** version.
        ``"agent:name@v3"`` -> optionally kind-qualified, useful when the same
        name exists under more than one kind.

        Raises :class:`UnknownReferenceError` (a ``KeyError``) with a clear
        message for an unknown name, an unknown version, no active version, or
        an ambiguous bare name.
        """
        kind, name, version = self._parse_ref(ref)
        definitions = self._definitions_by_name(name, kind)
        if not definitions:
            raise UnknownReferenceError(
                f"no {kind or 'library'} definition named {name!r} (ref {ref!r})"
            )
        if len(definitions) > 1:
            kinds = sorted(_definition_kind(d) for d in definitions)
            raise UnknownReferenceError(
                f"reference {ref!r} is ambiguous across kinds {kinds}; "
                f"qualify it as 'kind:name@version'"
            )
        definition = definitions[0]
        definition_id = _definition_id(definition)

        if version is None:
            stored = self._repo.latest_version(  # type: ignore[attr-defined]
                definition_id, active_only=True
            )
            if stored is None:
                raise UnknownReferenceError(
                    f"{name!r} has no active version (ref {ref!r})"
                )
        else:
            stored = self._repo.get_version(definition_id, version)  # type: ignore
            if stored is None:
                raise UnknownReferenceError(
                    f"{name!r} has no version {version} (ref {ref!r})"
                )
        return self._entry(definition, stored)

    def list_library(
        self, kind: str | None = None, status: str | None = None
    ) -> list[LibraryEntry]:
        """List definitions, newest version each, optionally filtered.

        ``status`` filters on the status of the definition's latest version.
        Results are sorted by ``(kind, name, version)`` for determinism.
        """
        if kind is not None:
            kind = self._check_kind(kind)
        entries: list[LibraryEntry] = []
        for definition in self._all_definitions(kind):
            stored = self._repo.latest_version(  # type: ignore[attr-defined]
                _definition_id(definition), active_only=False
            )
            if stored is None:
                continue
            if status is not None and _status_value(stored) != status:
                continue
            entries.append(self._entry(definition, stored))
        return sorted(entries, key=lambda e: (e.kind, e.name, e.version))

    # ------------------------------------------------------------------ #
    # Internal plumbing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _check_kind(kind: str) -> str:
        normalized = str(kind).strip().lower()
        if normalized not in SPEC_MODELS:
            raise ValueError(
                f"unknown library kind {kind!r}; expected one of {list(KINDS)}"
            )
        return normalized

    @staticmethod
    def _validate_spec(kind: str, spec: dict[str, Any] | BaseModel) -> BaseModel:
        """Validate a spec dict (or coerce an existing model) for ``kind``."""
        model = SPEC_MODELS[kind]
        if isinstance(spec, BaseModel):
            return model.model_validate(spec.model_dump())
        return model.model_validate(spec)

    def _create_definition(
        self, kind: str, name: str, tags: tuple[str, ...]
    ) -> object:
        create = self._repo.create_definition  # type: ignore[attr-defined]
        if self._accepts_tags(create):
            return create(kind, name, tags=list(tags))
        return create(kind, name)

    @staticmethod
    def _accepts_tags(create: object) -> bool:
        try:
            parameters = inspect.signature(create).parameters  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return "tags" in parameters

    def _entry(self, definition: object, version: object) -> LibraryEntry:
        definition_id = _definition_id(definition)
        tags = (
            _tags_of(definition)
            or _tags_of(version)
            or self._tags.get(definition_id, ())
        )
        return LibraryEntry(
            definition_id=definition_id,
            kind=_definition_kind(definition),
            name=_definition_name(definition),
            version=_version_number(version),
            spec=_spec_value(version),
            status=_status_value(version),
            tags=tags,
        )

    def _get_definition(self, definition_id: str) -> object | None:
        getter = getattr(self._repo, "get_definition", None)
        if callable(getter):
            try:
                found = getter(definition_id)
            except Exception:  # noqa: BLE001 - fall through to a scan
                found = None
            if found is not None:
                return found
        for definition in self._all_definitions():
            if _definition_id(definition) == definition_id:
                return definition
        return None

    def _find_definition(self, kind: str, name: str) -> object | None:
        by_name = getattr(self._repo, "get_definition_by_name", None)
        if callable(by_name):
            try:
                found = by_name(kind, name)
            except Exception:  # noqa: BLE001 - fall through to a scan
                found = None
            if found is not None:
                return found
        for definition in self._all_definitions(kind):
            if _definition_name(definition) == name:
                return definition
        return None

    def _definitions_by_name(self, name: str, kind: str | None) -> list[object]:
        if kind is not None:
            kind = self._check_kind(kind)
        matches = [
            d
            for d in self._all_definitions(kind)
            if _definition_name(d) == name
        ]
        # Stable, kind-ordered output so callers are deterministic.
        order = {k: i for i, k in enumerate(KINDS)}
        return sorted(matches, key=lambda d: order.get(_definition_kind(d), len(KINDS)))

    def _all_definitions(self, kind: str | None = None) -> list[object]:
        lister = getattr(self._repo, "list_definitions", None)
        if callable(lister):
            try:
                found = lister(kind=kind)
            except TypeError:
                found = lister()
            return [
                d for d in found if kind is None or _definition_kind(d) == kind
            ]

        # Fallback: query the ORM model directly through the session.
        model = self._definition_model()
        if model is None or self._session is None:
            raise LibraryError(
                "the repository exposes neither list_definitions() nor a queryable "
                "Definition model; LibraryService cannot enumerate definitions"
            )
        rows = self._session.query(model).all()  # type: ignore[attr-defined]
        return [d for d in rows if kind is None or _definition_kind(d) == kind]

    @staticmethod
    def _definition_model() -> object | None:
        for module_path, attr in (
            ("uap.db.models", "Definition"),
            ("uap.db", "Definition"),
        ):
            try:
                module = __import__(module_path, fromlist=[attr])
            except ImportError:
                continue
            model = getattr(module, attr, None)
            if model is not None:
                return model
        return None

    @staticmethod
    def _parse_ref(ref: str) -> tuple[str | None, str, int | None]:
        """Split ``[kind:]name[@vN]`` into ``(kind, name, version)``."""
        if not ref or not str(ref).strip():
            raise UnknownReferenceError("empty library reference")
        text = str(ref).strip()

        kind: str | None = None
        prefix, sep, remainder = text.partition(":")
        if sep and prefix.strip().lower() in SPEC_MODELS:
            kind = prefix.strip().lower()
            text = remainder.strip()

        name, at, raw_version = text.partition("@")
        name = name.strip()
        if not name:
            raise UnknownReferenceError(f"malformed library reference {ref!r}")

        version: int | None = None
        if at:
            token = raw_version.strip().lower()
            if token.startswith("v"):
                token = token[1:]
            if not token.isdigit():
                raise UnknownReferenceError(
                    f"malformed version in reference {ref!r}: expected 'name@vN'"
                )
            version = int(token)
        return kind, name, version
