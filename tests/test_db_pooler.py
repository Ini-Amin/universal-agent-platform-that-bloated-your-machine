"""Pooler detection: a transaction-mode pooler must disable prepared statements.

Supabase (and pgbouncer, and RDS Proxy) hand each transaction a different
backend connection. psycopg3 prepares statements automatically after a few
executions, so a statement prepared in one transaction is missing in the next —
producing ``prepared statement "_pg3_0" already exists``. That error looks like
a driver bug but is a topology mismatch.

These tests pin the detection logic only; they never open a real connection, so
they run with no database.
"""

from __future__ import annotations

import pytest

from uap.db.engine import _is_pooled_connection, create_db_engine


# -- detection -------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        # Supabase pooler host, transaction mode (port 6543)
        "postgresql+psycopg://postgres.abc:pw@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres",
        # Supabase pooler host on the session port is still a pooler
        "postgresql+psycopg://postgres.abc:pw@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres",
        # explicit pgbouncer host
        "postgresql+psycopg://uap:pw@pgbouncer.internal:5432/uap",
        # bare transaction port on a plain host
        "postgresql+psycopg://uap:pw@db.example.com:6543/uap",
    ],
)
def test_pooler_detected(url: str) -> None:
    assert _is_pooled_connection(url) is True, url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap",
        "postgresql+psycopg://uap:pw@db.example.com:5432/uap",
        # a password that happens to contain the pooler port must not trigger it
        "postgresql+psycopg://uap:secret6543@db.example.com:5432/uap",
        # a database name containing the number must not trigger it
        "postgresql+psycopg://uap:pw@db.example.com:5432/db6543",
    ],
)
def test_direct_connection_not_flagged(url: str) -> None:
    assert _is_pooled_connection(url) is False, url


# -- effect on the engine --------------------------------------------------- #


def test_pooled_engine_disables_prepared_statements() -> None:
    """A pooled URL must reach psycopg with ``prepare_threshold=None``."""
    engine = create_db_engine(
        "postgresql+psycopg://postgres.abc:pw@aws-0-x.pooler.supabase.com:6543/postgres"
    )
    try:
        connect_args = engine.dialect.create_connect_args(engine.url)[1]
        assert connect_args.get("prepare_threshold") is None, connect_args
    finally:
        engine.dispose()


def test_direct_engine_keeps_automatic_preparation() -> None:
    """A direct URL must NOT get the pooler workaround."""
    engine = create_db_engine("postgresql+psycopg://uap:pw@127.0.0.1:5432/uap")
    try:
        connect_args = engine.dialect.create_connect_args(engine.url)[1]
        assert "prepare_threshold" not in connect_args, connect_args
    finally:
        engine.dispose()


def test_pooled_engine_bounds_the_local_pool() -> None:
    """A pooler multiplexes many clients; a large local pool starves the project."""
    engine = create_db_engine(
        "postgresql+psycopg://postgres.abc:pw@aws-0-x.pooler.supabase.com:6543/postgres"
    )
    try:
        assert engine.pool.size() == 5
    finally:
        engine.dispose()
