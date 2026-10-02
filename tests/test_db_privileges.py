"""Least-privilege database role tests (the ``uap`` SUPERUSER removal).

The application role must never be a database superuser. These tests prove the
posture holds while everything the app does against PostgreSQL keeps working:

1. Alembic migrations run to head against a database owned by a non-superuser
   role (in a scratch schema on ``uap_test`` so sibling modules never clash).
2. The pgvector guard in :mod:`uap.db.extensions` is a no-op when the
   extension exists, and raises an actionable operator error (SQLSTATE 42501)
   instead of demanding superuser rights when it is missing.
3. The live role really is NOSUPERUSER/NOCREATEDB (via ``pg_roles``).
4. Normal app writes/reads still work, including a pgvector operator query.

DB-backed tests run against ``uap_test`` and skip cleanly when it is
unreachable, mirroring ``tests/test_db_foundation.py``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

from uap.db import Base, create_db_engine, create_session_factory, session_scope
from uap.db.extensions import ensure_pgvector_installed
from uap.db.models import KnowledgeItemRow

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
SRC_DIR = REPO_ROOT / "src"

DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"

def _resolved_test_url() -> str:
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

requires_db = pytest.mark.skipif(
    not DATABASE_REACHABLE,
    reason=f"PostgreSQL not reachable at {TEST_DATABASE_URL}",
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _scoped_url(schema: str, *, include_public: bool = False) -> str:
    """Return TEST_DATABASE_URL with ``search_path`` pinned to ``schema``."""

    path = f"{schema},public" if include_public else schema
    url = make_url(TEST_DATABASE_URL)
    url = url.update_query_dict({"options": f"-csearch_path={path}"})
    return url.render_as_string(hide_password=False)

@contextmanager
def _scratch_schema() -> Iterator[tuple[object, str]]:
    """Create an isolated schema on the shared test database; drop it after.

    ``uap_test`` is shared with other test modules (and parallel agents), so
    writing to ``public`` would clobber concurrent runs. The connection user is
    the non-superuser ``uap`` role; owning the database is what allows
    ``CREATE SCHEMA`` here.
    """

    admin = create_db_engine(TEST_DATABASE_URL)
    schema = f"uap_priv_{uuid.uuid4().hex[:8]}"
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield admin, schema
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()

class _StubResult:
    """The two result methods :func:`ensure_pgvector_installed` calls."""

    def __init__(self, row: object) -> None:
        self._row = row

    def first(self) -> object:
        return self._row

    def scalar(self) -> object:
        return self._row

class _StubConnection:
    """Connection stand-in that records executed SQL and scripted failures."""

    def __init__(
        self,
        *,
        extension_present: bool = False,
        create_error: Exception | None = None,
        database_name: str = "uap_test",
    ) -> None:
        self.statements: list[str] = []
        self._extension_present = extension_present
        self._create_error = create_error
        self._database_name = database_name

    def exec_driver_sql(self, statement: str, *_args: object) -> _StubResult:
        self.statements.append(statement)
        if statement.startswith("SELECT 1 FROM pg_extension"):
            return _StubResult((1,) if self._extension_present else None)
        if statement.startswith("SELECT current_database"):
            return _StubResult(self._database_name)
        if statement.startswith("CREATE EXTENSION"):
            if self._create_error is not None:
                raise self._create_error
        return _StubResult(None)

def _permission_denied() -> ProgrammingError:
    """What psycopg/SQLAlchemy raise for ``CREATE EXTENSION`` without rights."""

    return ProgrammingError(
        "CREATE EXTENSION IF NOT EXISTS vector",
        {},
        psycopg.errors.InsufficientPrivilege(
            'permission denied to create extension "vector"'
        ),
    )

# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

@requires_db
def test_migrations_run_on_database_owned_by_non_superuser() -> None:
    """The full chain runs to head with the least-privilege app role."""

    with _scratch_schema() as (admin, schema):
        url = _scoped_url(schema)
        cfg = Config()
        cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
        cfg.set_main_option("prepend_sys_path", str(SRC_DIR))
        cfg.set_main_option("path_separator", "os")
        cfg.attributes["sqlalchemy.url"] = url
        command.upgrade(cfg, "head")

        head = ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()
        eng = create_db_engine(url)
        try:
            with eng.connect() as conn:
                version = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
                tables = {
                    row[0]
                    for row in conn.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = :s"),
                        {"s": schema},
                    )
                }
                # Self-check: the connection really is the least-privilege app
                # role and it owns the database it just migrated.
                is_superuser = conn.execute(
                    text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                ).scalar()
                owns_database = conn.execute(
                    text(
                        "SELECT pg_get_userbyid(datdba) = current_user"
                        " FROM pg_database WHERE datname = current_database()"
                    )
                ).scalar()
                extension_seen = conn.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                ).first()
        finally:
            eng.dispose()

    assert version == head
    assert {"executions", "knowledge_items", "alembic_version"} <= tables
    assert is_superuser is False
    assert owns_database is True
    assert extension_seen is not None

def test_missing_extension_without_permission_raises_actionable_error() -> None:
    conn = _StubConnection(create_error=_permission_denied())
    with pytest.raises(RuntimeError, match="CREATE EXTENSION IF NOT EXISTS vector") as excinfo:
        ensure_pgvector_installed(conn)  # type: ignore[arg-type]

    message = str(excinfo.value)
    assert "superuser" in message
    assert '"uap_test"' in message  # names the exact database to fix

def test_guard_is_noop_when_extension_already_installed() -> None:
    conn = _StubConnection(extension_present=True)
    ensure_pgvector_installed(conn)  # type: ignore[arg-type]

    assert conn.statements == ["SELECT 1 FROM pg_extension WHERE extname = 'vector'"]

def test_non_permission_error_propagates_unchanged() -> None:
    original = OperationalError(
        "CREATE EXTENSION IF NOT EXISTS vector",
        {},
        psycopg.errors.DiskFull("could not extend file"),
    )
    conn = _StubConnection(create_error=original)
    with pytest.raises(OperationalError) as excinfo:
        ensure_pgvector_installed(conn)  # type: ignore[arg-type]

    assert excinfo.value is original

@requires_db
def test_app_role_is_neither_superuser_nor_createdb() -> None:
    eng = create_db_engine(TEST_DATABASE_URL)
    try:
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT rolname, rolsuper, rolcreatedb FROM pg_roles"
                    " WHERE rolname IN (current_user, 'uap')"
                )
            ).all()
    finally:
        eng.dispose()

    names = {row.rolname for row in rows}
    assert "uap" in names  # the app role exists on this cluster
    for row in rows:
        assert row.rolsuper is False, f"{row.rolname} is SUPERUSER"
        assert row.rolcreatedb is False, f"{row.rolname} is CREATEDB"

@requires_db
def test_app_normal_db_operations_still_work() -> None:
    """Smoke: the app can still write a row, read it back, and use pgvector."""

    with _scratch_schema() as (_admin, schema):
        eng = create_db_engine(_scoped_url(schema, include_public=True))
        try:
            Base.metadata.create_all(eng, checkfirst=False)
            factory = create_session_factory(eng)
            statement = "the app role can write without superuser"
            with session_scope(factory) as session:
                item = KnowledgeItemRow(
                    statement=statement,
                    domain="bbp",
                    embedding=[0.25] * 384,
                )
                session.add(item)
                session.flush()
                item_id = item.id

            with session_scope(factory) as session:
                loaded = session.get(KnowledgeItemRow, item_id)
                assert loaded is not None
                assert loaded.statement == statement
                query_vector = "[" + ",".join(["0.25"] * 384) + "]"
                distance = session.execute(
                    text(
                        "SELECT embedding <=> CAST(:query AS public.vector)"
                        " FROM knowledge_items WHERE id = :id"
                    ),
                    {"query": query_vector, "id": item_id},
                ).scalar()
                assert float(distance) < 1e-6
        finally:
            eng.dispose()
