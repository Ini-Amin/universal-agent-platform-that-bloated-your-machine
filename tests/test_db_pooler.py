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


# --------------------------------------------------------------------------- #
# Regression: the pool-sizing branch must not break non-QueuePool classes.
#
# Found by running the real fix against a real pgbouncer (transaction mode,
# port 6543, max_prepared_statements=0) on 2026-10-03. Passing pool_size to
# create_engine alongside NullPool raises TypeError — so the branch meant to
# SUPPORT pooled connections crashed for anyone who chose NullPool.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "poolclass_name",
    ["NullPool", "StaticPool"],
)
def test_pooler_workaround_tolerates_non_queue_pools(poolclass_name: str) -> None:
    import sqlalchemy.pool as sa_pool

    poolclass = getattr(sa_pool, poolclass_name)
    engine = create_db_engine(
        "postgresql+psycopg://uap:pw@db.example.com:6543/uap",
        poolclass=poolclass,
    )
    try:
        # The workaround that matters must still be applied.
        connect_args = engine.dialect.create_connect_args(engine.url)[1]
        assert connect_args.get("prepare_threshold") is None, connect_args
    finally:
        engine.dispose()


def test_pooler_workaround_still_sizes_a_queue_pool() -> None:
    """The sizing must not be lost while fixing the crash above."""
    engine = create_db_engine("postgresql+psycopg://uap:pw@db.example.com:6543/uap")
    try:
        assert engine.pool.size() == 5
    finally:
        engine.dispose()
