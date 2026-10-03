"""Tests for the Phase 0 persistence foundation (Master sections 2.1, 2.3, 2.4,
41, 42, 45, 46, 63, 64, 65, 66, 67, 73).

These tests run against the local PostgreSQL ``uap_test`` database. The URL is
taken from ``DATABASE_URL`` when set (override) and otherwise defaults to
``postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test``. If the
database is unreachable the whole module skips cleanly (no network required to
collect the suite).
"""

from __future__ import annotations

import inspect
import os
import typing
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect as sa_inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from uap.db import Base, create_db_engine, create_session_factory, session_scope
from uap.db.models import (
    ExecutionStatus,
    VersionStatus,
)
from uap.db.repositories import (
    DefinitionRepository,
    EventRepository,
    ExecutionRepository,
    compute_content_hash,
)

# --------------------------------------------------------------------------- #
# Connection / skip handling
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"


def _resolved_test_url() -> str:
    """DATABASE_URL wins (override); otherwise the local uap_test default."""

    return os.environ.get("DATABASE_URL") or DEFAULT_TEST_URL


TEST_DATABASE_URL = _resolved_test_url()


def _reachable(url: str) -> bool:
    try:
        engine = create_db_engine(url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


DATABASE_REACHABLE = _reachable(TEST_DATABASE_URL)

pytestmark = pytest.mark.skipif(
    not DATABASE_REACHABLE,
    reason=f"PostgreSQL not reachable at {TEST_DATABASE_URL}",
)

#: Tables owned by this foundation. Kept explicit so tests fail loudly if a
#: model is added/removed without updating expectations.
EXPECTED_TABLES = {
    "workflow_definitions",
    "workflow_versions",
    "agent_definitions",
    "agent_versions",
    "tool_definitions",
    "tool_versions",
    "skill_definitions",
    "skill_versions",
    "executions",
    "execution_events",
    "decision_traces",
    "execution_checkpoints",
    "knowledge_items",
    "knowledge_provenance",
    "knowledge_events",
    "node_views",
    "workspaces",
    "users",
    "workspace_members",
}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Session-wide engine with the full schema created via metadata.

    Uses a scratch schema so tests never disturb ``public``.
    """

    from sqlalchemy.engine import make_url

    schema = f"db_fnd_{uuid.uuid4().hex[:8]}"
    admin = create_db_engine(TEST_DATABASE_URL)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    scoped_url = make_url(TEST_DATABASE_URL).update_query_dict(
        {"options": f"-csearch_path={schema},public"}
    )
    eng = create_db_engine(scoped_url.render_as_string(hide_password=False))
    Base.metadata.create_all(eng, checkfirst=False)
    try:
        yield eng
    finally:
        eng.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture()
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """A per-test session factory (no global session state)."""

    yield create_session_factory(engine)


@pytest.fixture(autouse=True)
def _truncate_between_tests(engine: Engine) -> Iterator[None]:
    """TRUNCATE every foundation table after each test for isolation."""

    yield
    table_list = ", ".join(f'"{name}"' for name in EXPECTED_TABLES)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {table_list} RESTART IDENTITY CASCADE"))


def _make_workflow_version(session: Session, spec: dict | None = None):
    """Helper: one definition + one version, flushed but not committed."""

    repo = DefinitionRepository(session)
    definition = repo.create_definition(f"wf-{uuid.uuid4()}")
    version = repo.create_version(
        definition.id, spec if spec is not None else {"nodes": ["a", "b"]}
    )
    return definition, version


def _make_execution(session: Session):
    """Helper: a definition+version+execution, flushed but not committed."""

    _, version = _make_workflow_version(session)
    execution = ExecutionRepository(session).create(version.id, input={"x": 1})
    return version, execution


# --------------------------------------------------------------------------- #
# Alembic helpers (schema-scoped runs for tests 2 and 13)
# --------------------------------------------------------------------------- #

def _schema_url(schema: str) -> str:
    """Return TEST_DATABASE_URL with ``search_path`` pinned to ``schema``.

    Uses a scratch schema so migration tests never disturb the main ``public``
    schema. ``make_url`` percent-encodes the option for us.
    """

    from sqlalchemy.engine import make_url

    url = make_url(TEST_DATABASE_URL)
    url = url.update_query_dict({"options": f"-csearch_path={schema}"})
    return url.render_as_string(hide_password=False)


def _run_alembic_upgrade(url: str) -> None:
    """Programmatically run ``alembic upgrade head`` against ``url``."""

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    # Pass the URL out-of-band: it contains a percent-encoded query option that
    # configparser would reject as interpolation syntax.
    config.attributes["sqlalchemy.url"] = url
    command.upgrade(config, "head")


# --------------------------------------------------------------------------- #
# 1. Engine connects; vector extension present
# --------------------------------------------------------------------------- #

def test_engine_connects_and_vector_extension_present(engine: Engine) -> None:
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1
        ext = conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()
    assert ext == "vector"


# --------------------------------------------------------------------------- #
# 2. create_all and alembic head produce identical table sets
# --------------------------------------------------------------------------- #

def _table_names(engine: Engine, schema: str) -> set[str]:
    return set(sa_inspect(engine).get_table_names(schema=schema))


def test_create_all_matches_alembic_head_table_sets(engine: Engine) -> None:
    created_schema = f"meta_create_{uuid.uuid4().hex[:8]}"
    migrated_schema = f"meta_migr_{uuid.uuid4().hex[:8]}"

    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{created_schema}"'))
        conn.execute(text(f'CREATE SCHEMA "{migrated_schema}"'))

    try:
        # (a) metadata.create_all into one scratch schema
        create_all_engine = create_db_engine(_schema_url(created_schema))
        Base.metadata.create_all(create_all_engine)
        create_all_engine.dispose()

        # (b) alembic upgrade head into another scratch schema
        _run_alembic_upgrade(_schema_url(migrated_schema))

        created = _table_names(engine, created_schema)
        migrated = _table_names(engine, migrated_schema) - {"alembic_version"}

        assert created == EXPECTED_TABLES
        assert migrated == EXPECTED_TABLES
        assert created == migrated
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{created_schema}" CASCADE'))
            conn.execute(text(f'DROP SCHEMA "{migrated_schema}" CASCADE'))


# --------------------------------------------------------------------------- #
# 3. Immutability: get_version(1) still returns v1 after v2 exists
# --------------------------------------------------------------------------- #

def test_create_version_is_immutable(session_factory) -> None:
    with session_scope(session_factory) as session:
        repo = DefinitionRepository(session)
        definition = repo.create_definition(f"immut-{uuid.uuid4()}")
        v1 = repo.create_version(definition.id, {"revision": 1, "nodes": ["a"]})
        v2 = repo.create_version(definition.id, {"revision": 2, "nodes": ["a", "b"]})
        assert (v1.version, v2.version) == (1, 2)

    # Re-read in a brand new session: v1 must be byte-identical to its insert.
    with session_scope(session_factory) as session:
        repo = DefinitionRepository(session)
        pinned = repo.get_version(definition.id, 1)
        assert pinned is not None
        assert pinned.spec == {"revision": 1, "nodes": ["a"]}
        assert pinned.content_hash == v1.content_hash


# --------------------------------------------------------------------------- #
# 4. latest_version returns the highest version
# --------------------------------------------------------------------------- #

def test_latest_version_returns_highest(session_factory) -> None:
    with session_scope(session_factory) as session:
        repo = DefinitionRepository(session)
        definition = repo.create_definition(f"latest-{uuid.uuid4()}")
        repo.create_version(definition.id, {"n": 1})
        repo.create_version(definition.id, {"n": 2})
        v3 = repo.create_version(definition.id, {"n": 3})
        latest = repo.latest_version(definition.id)
        assert latest is not None
        assert latest.version == 3
        assert latest.id == v3.id


# --------------------------------------------------------------------------- #
# 5. content_hash stable across identical specs, differs for different specs
# --------------------------------------------------------------------------- #

def test_content_hash_is_stable_and_distinct(session_factory) -> None:
    spec_a = {"b": 2, "a": 1}
    spec_a_reordered = {"a": 1, "b": 2}  # canonical JSON sorts keys
    spec_b = {"a": 1, "b": 3}

    assert compute_content_hash(spec_a) == compute_content_hash(spec_a_reordered)
    assert compute_content_hash(spec_a) != compute_content_hash(spec_b)
    assert len(compute_content_hash(spec_a)) == 64

    with session_scope(session_factory) as session:
        repo = DefinitionRepository(session)
        d1 = repo.create_definition(f"hash1-{uuid.uuid4()}")
        d2 = repo.create_definition(f"hash2-{uuid.uuid4()}")
        v1 = repo.create_version(d1.id, spec_a)
        v1b = repo.create_version(d2.id, spec_a_reordered)
        v2 = repo.create_version(d1.id, spec_b)

        assert v1.content_hash == v1b.content_hash
        assert v1.content_hash != v2.content_hash


# --------------------------------------------------------------------------- #
# 6. status transitions draft -> active persist
# --------------------------------------------------------------------------- #

def test_status_transition_persists(session_factory) -> None:
    with session_scope(session_factory) as session:
        repo = DefinitionRepository(session)
        definition = repo.create_definition(f"status-{uuid.uuid4()}")
        version = repo.create_version(definition.id, {"n": 1})
        assert version.status == VersionStatus.DRAFT
        repo.set_status(definition.id, 1, VersionStatus.ACTIVE)

    with session_scope(session_factory) as session:
        repo = DefinitionRepository(session)
        reloaded = repo.get_version(definition.id, 1)
        assert reloaded is not None
        assert reloaded.status == VersionStatus.ACTIVE

        # active-only latest lookup sees it
        active = repo.latest_version(definition.id, status=VersionStatus.ACTIVE)
        assert active is not None and active.version == 1


# --------------------------------------------------------------------------- #
# 7. Execution create + update_status round-trip
# --------------------------------------------------------------------------- #

def test_execution_create_and_update_status_roundtrip(session_factory) -> None:
    with session_scope(session_factory) as session:
        _, version = _make_workflow_version(session)
        execution = ExecutionRepository(session).create(
            version.id, input={"question": "hi"}, status=ExecutionStatus.RUNNING
        )
        execution_id = execution.id
        assert execution.output is None
        assert execution.resume_count == 0

    with session_scope(session_factory) as session:
        repo = ExecutionRepository(session)
        repo.update_status(
            execution_id,
            ExecutionStatus.COMPLETED,
            output={"answer": "ok"},
        )
        repo.increment_resume_count(execution_id)

    with session_scope(session_factory) as session:
        row = ExecutionRepository(session).get(execution_id)
        assert row is not None
        assert row.status == ExecutionStatus.COMPLETED
        assert row.output == {"answer": "ok"}
        assert row.input == {"question": "hi"}
        assert row.resume_count == 1
        assert row.workflow_version_id == version.id


# --------------------------------------------------------------------------- #
# 8. FK enforced: a bad workflow_version_id raises
# --------------------------------------------------------------------------- #

def test_execution_requires_valid_workflow_version(session_factory) -> None:
    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            ExecutionRepository(session).create(uuid.uuid4(), input={"x": 1})


# --------------------------------------------------------------------------- #
# 9. Event append assigns seq 1,2,3; read_since returns the tail
# --------------------------------------------------------------------------- #

def test_event_append_assigns_dense_seq_and_read_since(session_factory) -> None:
    with session_scope(session_factory) as session:
        _, execution = _make_execution(session)
        repo = EventRepository(session)
        seqs = [repo.append(execution.id, kind=f"E{i}").seq for i in range(1, 4)]
        assert seqs == [1, 2, 3]

    with session_scope(session_factory) as session:
        repo = EventRepository(session)
        all_events = repo.read_since(execution.id)
        assert [e.seq for e in all_events] == [1, 2, 3]
        assert [e.kind for e in all_events] == ["E1", "E2", "E3"]

        tail = repo.read_since(execution.id, 1)
        assert [e.seq for e in tail] == [2, 3]
        assert [e.kind for e in tail] == ["E2", "E3"]

        assert repo.count(execution.id) == 3


# --------------------------------------------------------------------------- #
# 10. UNIQUE(execution_id, seq) enforced by the database
# --------------------------------------------------------------------------- #

def test_event_seq_unique_constraint_enforced(session_factory) -> None:
    with session_scope(session_factory) as session:
        _, execution = _make_execution(session)
        EventRepository(session).append(execution.id, kind="First")
        execution_id = execution.id

    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            session.execute(
                text(
                    "INSERT INTO execution_events (execution_id, seq, kind, payload) "
                    "VALUES (:eid, 1, 'Duplicate', '{}'::jsonb)"
                ),
                {"eid": execution_id},
            )


# --------------------------------------------------------------------------- #
# 11. Seq allocation across separate sessions still increments monotonically
# --------------------------------------------------------------------------- #

def test_seq_allocation_across_sessions_is_monotonic(session_factory) -> None:
    """Two sequential appends in *different* sessions get 2 then 3.

    True parallelism is optional here; the point is that the per-execution
    counter is read from committed state (and serialized on the parent row via
    ``FOR UPDATE``), so independent sessions never reuse a seq.
    """

    with session_scope(session_factory) as session:
        _, execution = _make_execution(session)
        first = EventRepository(session).append(execution.id, kind="SessionA")
        assert first.seq == 1
        execution_id = execution.id

    with session_scope(session_factory) as session:
        second = EventRepository(session).append(execution_id, kind="SessionB")
        assert second.seq == 2

    with session_scope(session_factory) as session:
        third = EventRepository(session).append(execution_id, kind="SessionC")
        assert third.seq == 3

    with session_scope(session_factory) as session:
        assert [e.seq for e in EventRepository(session).read_since(execution_id)] == [
            1,
            2,
            3,
        ]


# --------------------------------------------------------------------------- #
# 12. session_scope rolls back on exception
# --------------------------------------------------------------------------- #

def test_session_scope_rolls_back_on_exception(session_factory) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with session_scope(session_factory) as session:
            definition = DefinitionRepository(session).create_definition(
                f"rollback-{uuid.uuid4()}"
            )
            DefinitionRepository(session).create_version(definition.id, {"n": 1})
            assert session.execute(
                text("SELECT count(*) FROM workflow_definitions")
            ).scalar_one() == 1  # visible inside the transaction
            raise RuntimeError("boom")

    with session_scope(session_factory) as session:
        remaining = session.execute(
            text("SELECT count(*) FROM workflow_definitions")
        ).scalar_one()
        assert remaining == 0


# --------------------------------------------------------------------------- #
# 13. Alembic migration exists and upgrade head runs against a scratch schema
# --------------------------------------------------------------------------- #

def test_alembic_migration_exists_and_upgrades_cleanly(engine: Engine) -> None:
    versions_dir = REPO_ROOT / "migrations" / "versions"
    migration_files = sorted(versions_dir.glob("*.py"))
    assert migration_files, "no alembic migration files found"

    assert (REPO_ROOT / "alembic.ini").exists()
    assert (REPO_ROOT / "migrations" / "env.py").exists()

    scratch_schema = f"scratch_{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{scratch_schema}"'))
    try:
        _run_alembic_upgrade(_schema_url(scratch_schema))
        migrated = _table_names(engine, scratch_schema)
        assert EXPECTED_TABLES <= migrated
        assert "alembic_version" in migrated
    finally:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{scratch_schema}" CASCADE'))


# --------------------------------------------------------------------------- #
# 14. Repository public methods are fully annotated
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "repo_cls",
    [DefinitionRepository, ExecutionRepository, EventRepository],
)
def test_repository_methods_are_fully_annotated(repo_cls: type) -> None:
    methods = [
        (name, member)
        for name, member in inspect.getmembers(repo_cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert methods, f"{repo_cls.__name__} exposes no public methods"

    for name, method in methods:
        hints = typing.get_type_hints(method)
        assert "return" in hints, f"{repo_cls.__name__}.{name} lacks a return type"
        for param in inspect.signature(method).parameters.values():
            if param.name == "self":
                continue
            assert param.name in hints, (
                f"{repo_cls.__name__}.{name} parameter {param.name!r} is unannotated"
            )


# --------------------------------------------------------------------------- #
# Extra coverage: pin helper, event cascade, definition-name uniqueness
# --------------------------------------------------------------------------- #

def test_pin_preserves_exact_version_reference(session_factory) -> None:
    with session_scope(session_factory) as session:
        repo = DefinitionRepository(session)
        definition = repo.create_definition(f"pin-{uuid.uuid4()}")
        v1 = repo.create_version(definition.id, {"n": 1})
        repo.create_version(definition.id, {"n": 2})

        pinned = repo.pin(definition.id, 1)
        assert pinned.version == 1
        reference = repo.pinned_reference(definition.id, 1)
        assert reference["definition_id"] == str(definition.id)
        assert reference["version"] == 1
        assert reference["content_hash"] == v1.content_hash

        with pytest.raises(LookupError):
            repo.pin(definition.id, 99)


def test_deleting_execution_cascades_to_events(session_factory) -> None:
    with session_scope(session_factory) as session:
        _, execution = _make_execution(session)
        EventRepository(session).append(execution.id, kind="E1")
        execution_id = execution.id

    with session_scope(session_factory) as session:
        session.execute(
            text("DELETE FROM executions WHERE id = :eid"), {"eid": execution_id}
        )

    with session_scope(session_factory) as session:
        assert (
            session.execute(
                text("SELECT count(*) FROM execution_events WHERE execution_id = :eid"),
                {"eid": execution_id},
            ).scalar_one()
            == 0
        )


def test_definition_name_is_unique(session_factory) -> None:
    name = f"unique-{uuid.uuid4()}"
    with session_scope(session_factory) as session:
        DefinitionRepository(session).create_definition(name)

    with pytest.raises(IntegrityError):
        with session_scope(session_factory) as session:
            DefinitionRepository(session).create_definition(name)
