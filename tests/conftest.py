"""Test configuration and shared fixtures for universal-agent-platform.

Provides per-module database schema isolation so test modules never race or
collide on shared tables in PostgreSQL.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterator
import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from uap.db import create_db_engine, create_session_factory
from uap.db.engine import dispose_engine

DEFAULT_TEST_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap_test"

_TABLES_TO_TRUNCATE = (
    "knowledge_events",
    "knowledge_provenance",
    "knowledge_items",
    "decision_traces",
    "execution_checkpoints",
    "execution_events",
    "executions",
    "node_views",
    "workflow_versions",
    "workflow_definitions",
    "agent_versions",
    "agent_definitions",
    "skill_versions",
    "skill_definitions",
    "tool_versions",
    "tool_definitions",
    "workspace_members",
    "workspaces",
    "users",
)

_TRUNCATE_STMT = text(
    "TRUNCATE "
    + ", ".join(f'"{name}"' for name in _TABLES_TO_TRUNCATE)
    + " RESTART IDENTITY CASCADE"
)


def resolved_test_url() -> str:
    """Return configured test database URL (DATABASE_URL env override wins)."""
    return os.environ.get("DATABASE_URL") or DEFAULT_TEST_URL


def compute_db_skip_reason(url: str | None = None) -> str | None:
    """Return a human-readable reason to skip if PostgreSQL is unreachable."""
    target_url = url or resolved_test_url()
    try:
        engine = create_db_engine(target_url, connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        return f"PostgreSQL not reachable at {target_url}: {exc}"
    return None


def apply_migrations(migration_url: str) -> None:
    """Apply all Alembic migrations to head against the specified URL."""
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parent.parent
    config = Config(str(repo_root / "alembic.ini"))
    config.set_main_option("script_location", str(repo_root / "migrations"))
    config.attributes["sqlalchemy.url"] = migration_url

    alembic_logger = logging.getLogger("alembic")
    old_level = alembic_logger.level
    alembic_logger.setLevel(logging.WARNING)
    try:
        command.upgrade(config, "head")
    finally:
        alembic_logger.setLevel(old_level)


@pytest.fixture(name="apply_migrations")
def _apply_migrations_fixture():
    return apply_migrations


@pytest.fixture(scope="module")
def isolated_db(request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    """Per-module schema isolation.

    Creates a dedicated schema for the test module, applies Alembic migrations,
    pins search_path to the schema, and sets DATABASE_URL so all application
    calls (e.g. create_app, get_engine) resolve to the isolated schema.
    Tears down the schema CASCADE at the end of the module.
    """
    skip_reason = compute_db_skip_reason()
    if skip_reason is not None:
        pytest.skip(skip_reason)

    base_url = resolved_test_url()
    mod_name = request.module.__name__.split(".")[-1]
    clean_mod = "".join(c if c.isalnum() else "_" for c in mod_name)[:18]
    schema = f"t_{clean_mod}_{uuid.uuid4().hex[:8]}"

    admin = create_db_engine(base_url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    migration_url = (
        make_url(base_url)
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    apply_migrations(migration_url)

    scoped_url = (
        make_url(base_url)
        .update_query_dict({"options": f"-csearch_path={schema},public"})
        .render_as_string(hide_password=False)
    )

    engine = create_db_engine(scoped_url)
    factory = create_session_factory(engine)

    old_env_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = scoped_url
    dispose_engine()

    data = {
        "schema": schema,
        "url": scoped_url,
        "engine": engine,
        "session_factory": factory,
    }

    try:
        yield data
    finally:
        engine.dispose()
        dispose_engine()
        if old_env_url is not None:
            os.environ["DATABASE_URL"] = old_env_url
        else:
            os.environ.pop("DATABASE_URL", None)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture(scope="module")
def isolated_engine(isolated_db: dict[str, Any]) -> Engine:
    """Return the SQLAlchemy Engine connected to the module's isolated schema."""
    return isolated_db["engine"]


@pytest.fixture(scope="module")
def isolated_session_factory(isolated_db: dict[str, Any]) -> sessionmaker[Session]:
    """Return the sessionmaker bound to the module's isolated schema."""
    return isolated_db["session_factory"]


@pytest.fixture(scope="module")
def isolated_db_url(isolated_db: dict[str, Any]) -> str:
    """Return the connection URL pointing to the module's isolated schema."""
    return isolated_db["url"]


@pytest.fixture()
def db_session_factory(isolated_db: dict[str, Any]) -> Iterator[sessionmaker[Session]]:
    """Session factory over the isolated schema with per-test table truncation."""
    engine: Engine = isolated_db["engine"]
    factory: sessionmaker[Session] = isolated_db["session_factory"]

    with engine.begin() as conn:
        conn.execute(_TRUNCATE_STMT)
    try:
        yield factory
    finally:
        with engine.begin() as conn:
            conn.execute(_TRUNCATE_STMT)
