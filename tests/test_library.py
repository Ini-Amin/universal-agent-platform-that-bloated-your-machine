"""Tests for the Global Library service (Master sections 2.4 and 4).

The service talks to ``uap.db.DefinitionRepository`` (built in parallel). To keep
these tests meaningful before that package lands, most run against an injected
in-memory repository implementing the pinned interface. The remainder run
against a real session on ``uap_test`` and skip cleanly until ``uap.db`` exists.
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from uap.library import (
    AgentDefinitionSpec,
    LibraryEntry,
    LibraryService,
    SkillDefinitionSpec,
    ToolDefinitionSpec,
    WorkflowDefinitionSpec,
)
from uap.library.service import (
    DuplicateDefinitionError,
    UnknownReferenceError,
)

# --------------------------------------------------------------------------- #
# In-memory DefinitionRepository (mirrors the pinned uap.db interface)
# --------------------------------------------------------------------------- #


class _Definition:
    def __init__(self, definition_id: str, kind: str, name: str, tags=None) -> None:
        self.id = definition_id
        self.kind = kind
        self.name = name
        self.tags = list(tags or [])


class _Version:
    def __init__(self, definition_id: str, version: int, spec: dict) -> None:
        self.definition_id = definition_id
        self.version = version
        self.spec = spec
        self.status = "draft"


class FakeDefinitionRepository:
    """In-memory implementation of the exact interface LibraryService pins."""

    def __init__(self) -> None:
        self.definitions: dict[str, _Definition] = {}
        self.versions: dict[tuple[str, int], _Version] = {}
        self._next_id = 0

    # -- guaranteed interface ------------------------------------------- #
    def create_definition(self, kind, name, tags=None):
        self._next_id += 1
        definition = _Definition(f"def-{self._next_id}", kind, name, tags)
        self.definitions[definition.id] = definition
        return definition

    def create_version(self, definition_id, spec):
        version = 1 + max(
            (v for (d, v) in self.versions if d == definition_id), default=0
        )
        row = _Version(definition_id, version, dict(spec))
        self.versions[(definition_id, version)] = row
        return row

    def get_version(self, definition_id, version):
        return self.versions.get((definition_id, version))

    def latest_version(self, definition_id, active_only=False):
        rows = [
            v for (d, _), v in self.versions.items() if d == definition_id
        ]
        if active_only:
            rows = [v for v in rows if v.status == "active"]
        if not rows:
            return None
        return max(rows, key=lambda v: v.version)

    def set_status(self, definition_id, version, status):
        row = self.versions.get((definition_id, version))
        if row is None:
            raise KeyError((definition_id, version))
        row.status = status
        return row

    # -- optional lookup helpers ---------------------------------------- #
    def get_definition(self, definition_id):
        return self.definitions.get(definition_id)

    def get_definition_by_name(self, kind, name):
        for definition in self.definitions.values():
            if definition.kind == kind and definition.name == name:
                return definition
        return None

    def list_definitions(self, kind=None):
        rows = list(self.definitions.values())
        if kind is not None:
            rows = [d for d in rows if d.kind == kind]
        return rows


@pytest.fixture()
def library() -> LibraryService:
    return LibraryService(repository=FakeDefinitionRepository())


def _agent_spec(**overrides) -> dict:
    spec = {
        "model": "jdw/claude-opus-4-8",
        "instructions": "Do the thing",
        "skills": ["api-recon@v1"],
        "tools": ["http.request@v1"],
    }
    spec.update(overrides)
    return spec


# --------------------------------------------------------------------------- #
# 8. register agent -> v1 draft
# --------------------------------------------------------------------------- #

def test_register_agent_creates_v1_draft(library: LibraryService) -> None:
    entry = library.register_agent("recon-agent", _agent_spec(), tags=["bbp", "recon"])

    assert isinstance(entry, LibraryEntry)
    assert entry.kind == "agent"
    assert entry.name == "recon-agent"
    assert entry.version == 1
    assert entry.status == "draft"
    assert entry.ref == "recon-agent@v1"
    assert entry.spec["model"] == "jdw/claude-opus-4-8"
    assert entry.tags == ("bbp", "recon")


# --------------------------------------------------------------------------- #
# 9. publish -> active; get() returns it
# --------------------------------------------------------------------------- #

def test_publish_makes_active_and_get_returns_it(library: LibraryService) -> None:
    entry = library.register_agent("recon-agent", _agent_spec())
    assert library.get("recon-agent") is None  # no active version yet

    published = library.publish(entry.definition_id, 1)
    assert published.status == "active"

    fetched = library.get("recon-agent")
    assert fetched is not None
    assert fetched.version == 1 and fetched.status == "active"
    assert library.get("recon-agent", version=1).status == "active"


# --------------------------------------------------------------------------- #
# 10. duplicate name -> ValueError
# --------------------------------------------------------------------------- #

def test_register_duplicate_name_raises(library: LibraryService) -> None:
    library.register_agent("recon-agent", _agent_spec())
    with pytest.raises(ValueError):
        library.register_agent("recon-agent", _agent_spec())
    # And it is the specific, catchable type.
    with pytest.raises(DuplicateDefinitionError):
        library.register_agent("recon-agent", _agent_spec())


# --------------------------------------------------------------------------- #
# 11. new_version -> v2; v1 unchanged (immutability)
# --------------------------------------------------------------------------- #

def test_new_version_creates_v2_and_leaves_v1_intact(library: LibraryService) -> None:
    entry = library.register_agent("recon-agent", _agent_spec(instructions="v1 text"))
    library.publish(entry.definition_id, 1)

    v2 = library.new_version(
        entry.definition_id, _agent_spec(instructions="v2 text")
    )
    assert v2.version == 2
    assert v2.status == "draft"

    v1 = library.get("recon-agent", version=1)
    assert v1.version == 1
    assert v1.spec["instructions"] == "v1 text"  # never mutated
    assert v1.status == "active"

    assert library.get("recon-agent", version=2).spec["instructions"] == "v2 text"


# --------------------------------------------------------------------------- #
# 12. resolve_ref exact "name@v1" works after v2 exists
# --------------------------------------------------------------------------- #

def test_resolve_ref_exact_after_new_version(library: LibraryService) -> None:
    entry = library.register_agent("recon-agent", _agent_spec(instructions="v1"))
    library.publish(entry.definition_id, 1)
    library.new_version(entry.definition_id, _agent_spec(instructions="v2"))

    resolved = library.resolve_ref("recon-agent@v1")
    assert resolved.version == 1
    assert resolved.spec["instructions"] == "v1"
    assert resolved.ref == "recon-agent@v1"

    # Exact refs also resolve drafts (that is what "exact" means).
    draft = library.resolve_ref("recon-agent@v2")
    assert draft.version == 2 and draft.status == "draft"


# --------------------------------------------------------------------------- #
# 13. resolve_ref bare name returns latest ACTIVE, not latest draft
# --------------------------------------------------------------------------- #

def test_resolve_ref_bare_name_returns_latest_active(library: LibraryService) -> None:
    entry = library.register_agent("recon-agent", _agent_spec(instructions="v1"))
    library.publish(entry.definition_id, 1)
    library.new_version(entry.definition_id, _agent_spec(instructions="v2"))  # draft

    resolved = library.resolve_ref("recon-agent")
    assert resolved.version == 1  # v2 exists but is only a draft
    assert resolved.status == "active"

    # Once v2 is published, the bare name floats forward.
    library.publish(entry.definition_id, 2)
    assert library.resolve_ref("recon-agent").version == 2


def test_resolve_ref_bare_name_with_only_drafts_raises(library: LibraryService) -> None:
    library.register_agent("recon-agent", _agent_spec())  # v1 stays a draft
    with pytest.raises(UnknownReferenceError):
        library.resolve_ref("recon-agent")


# --------------------------------------------------------------------------- #
# 14. unknown ref -> KeyError with clear message
# --------------------------------------------------------------------------- #

def test_unknown_ref_raises_keyerror(library: LibraryService) -> None:
    entry = library.register_agent("recon-agent", _agent_spec())
    library.publish(entry.definition_id, 1)

    with pytest.raises(KeyError):
        library.resolve_ref("ghost-agent")
    with pytest.raises(UnknownReferenceError) as excinfo:
        library.resolve_ref("recon-agent@v9")
    assert "recon-agent" in str(excinfo.value)
    with pytest.raises(UnknownReferenceError):
        library.resolve_ref("recon-agent@not-a-version")
    with pytest.raises(UnknownReferenceError):
        library.resolve_ref("")


# --------------------------------------------------------------------------- #
# 15. deprecate -> status deprecated; bare-name resolve no longer returns it
# --------------------------------------------------------------------------- #

def test_deprecate_removes_from_bare_name_resolution(library: LibraryService) -> None:
    entry = library.register_agent("recon-agent", _agent_spec())
    library.publish(entry.definition_id, 1)
    assert library.resolve_ref("recon-agent").version == 1

    deprecated = library.deprecate(entry.definition_id, 1)
    assert deprecated.status == "deprecated"
    assert library.get("recon-agent", version=1).status == "deprecated"

    with pytest.raises(UnknownReferenceError):
        library.resolve_ref("recon-agent")
    # Exact refs still work after deprecation (pinned consumers keep working).
    assert library.resolve_ref("recon-agent@v1").status == "deprecated"


# --------------------------------------------------------------------------- #
# 16. four kinds register independently
# --------------------------------------------------------------------------- #

def test_all_four_kinds_register_independently(library: LibraryService) -> None:
    agent = library.register_agent("shared-name", _agent_spec())
    tool = library.register_tool(
        "shared-name",
        {
            "id": "http.request",
            "description": "HTTP client",
            "input_schema": {"type": "object"},
            "trust_level": "untrusted",
            "execution_boundary": "sandbox",
        },
    )
    skill = library.register_skill(
        "shared-name",
        {
            "description": "Passive recon",
            "methodology": "Run subfinder then httpx",
            "required_capabilities": ["dns.lookup"],
            "dependencies": ["subfinder", "httpx"],
        },
    )
    workflow = library.register_workflow(
        "shared-name",
        {"graph_ref": "graphs/recon.v1.json", "inputs": {}, "outputs": {}},
    )

    kinds = {agent.kind, tool.kind, skill.kind, workflow.kind}
    assert kinds == {"agent", "tool", "skill", "workflow"}
    # Same name under different kinds is allowed; kinds do not collide.
    assert len({e.definition_id for e in (agent, tool, skill, workflow)}) == 4

    listed = library.list_library()
    assert [e.kind for e in listed] == ["agent", "skill", "tool", "workflow"]


# --------------------------------------------------------------------------- #
# 17. spec validation: invalid dict -> ValidationError
# --------------------------------------------------------------------------- #

def test_invalid_agent_spec_missing_model_raises(library: LibraryService) -> None:
    with pytest.raises(ValidationError):
        library.register_agent("bad-agent", {"instructions": "no model here"})


def test_invalid_specs_for_every_kind_raise(library: LibraryService) -> None:
    with pytest.raises(ValidationError):
        library.register_tool("bad-tool", {"description": "missing id"})
    with pytest.raises(ValidationError):
        library.register_workflow("bad-workflow", {"inputs": {}})  # no graph_ref
    # Extra fields are rejected, matching the core contracts' strictness.
    with pytest.raises(ValidationError):
        library.register_skill("bad-skill", {"surprise": True})


def test_typed_specs_validate_directly() -> None:
    assert AgentDefinitionSpec(model="m").model == "m"
    assert ToolDefinitionSpec(id="t").execution_boundary == "sandbox"
    assert SkillDefinitionSpec(description="d").methodology == ""
    assert WorkflowDefinitionSpec(graph_ref="g").inputs == {}


def test_register_accepts_a_typed_spec_model(library: LibraryService) -> None:
    entry = library.register_agent(
        "typed-agent", AgentDefinitionSpec(model="cbai/deepseek-v4.1-flash")
    )
    assert entry.version == 1
    assert entry.spec["model"] == "cbai/deepseek-v4.1-flash"


def test_list_library_filters_by_kind_and_status(library: LibraryService) -> None:
    agent = library.register_agent("a", _agent_spec())
    library.register_tool("t", {"id": "t"})
    library.publish(agent.definition_id, 1)

    assert {e.kind for e in library.list_library(kind="agent")} == {"agent"}
    active = library.list_library(status="active")
    assert [e.name for e in active] == ["a"]
    drafts = library.list_library(status="draft")
    assert [e.name for e in drafts] == ["t"]


def test_unknown_kind_is_rejected(library: LibraryService) -> None:
    with pytest.raises(ValueError):
        library.register("sorcery", "x", {})


# --------------------------------------------------------------------------- #
# Real database path (skipped until uap.db lands)
# --------------------------------------------------------------------------- #

def _uap_db_available() -> bool:
    """True when the real per-kind repositories are importable."""
    try:
        import uap.db.repositories as repositories
        from uap.db.models.definitions import VersionStatus  # noqa: F401
    except ImportError:
        return False
    return all(
        hasattr(repositories, name)
        for name in ("DefinitionRepository", "AgentDefinitionRepository")
    )


def _test_database_url() -> str:
    return (
        os.environ.get("DATABASE_URL")
        or os.environ.get("UAP_DATABASE_URL")
        or "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"
    )


requires_db = pytest.mark.skipif(
    not _uap_db_available(),
    reason="uap.db is not built yet (parallel agent); library DB tests skip",
)


@requires_db
def test_library_against_real_database(isolated_db) -> None:
    """End-to-end on uap_test using the real uap.db repositories."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from uap.db.base import Base

    engine = create_engine(_test_database_url(), future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        service = LibraryService(session)
        entry = service.register_agent("db-recon-agent", _agent_spec())
        assert entry.version == 1 and entry.status == "draft"

        service.publish(entry.definition_id, 1)
        assert service.resolve_ref("db-recon-agent").status == "active"

        v2 = service.new_version(entry.definition_id, _agent_spec(instructions="v2"))
        assert v2.version == 2
        assert service.get("db-recon-agent", version=1).spec["instructions"] != "v2"
        assert service.resolve_ref("db-recon-agent").version == 1  # v2 still draft
        assert service.resolve_ref("db-recon-agent@v2").version == 2

        with pytest.raises(UnknownReferenceError):
            service.resolve_ref("db-ghost-agent")
        with pytest.raises(ValueError):
            service.register_agent("db-recon-agent", _agent_spec())

        # A tool under the same name is an independent definition.
        tool = service.register_tool("db-recon-agent", {"id": "http.request"})
        assert tool.kind == "tool"
        assert service.resolve_ref("tool:db-recon-agent@v1").version == 1

        service.deprecate(entry.definition_id, 1)
        assert service.get("db-recon-agent", version=1).status == "deprecated"
        session.rollback()
    finally:
        session.close()
