"""Least-privilege install guard for the pgvector ``vector`` extension.

The application role is deliberately **not** a database superuser (see the
README setup section). pgvector is a non-trusted extension, so
``CREATE EXTENSION vector`` needs superuser rights the app must not have. The
documented flow is therefore: a DBA runs ``CREATE EXTENSION IF NOT EXISTS
vector`` once (as superuser) before the first ``alembic upgrade head``, and
migrations must tolerate the extension already being present.

:func:`ensure_pgvector_installed` keeps both migrations self-sufficient:
it is a no-op when the extension exists, and converts a permission failure
into an actionable operator error naming the one-time superuser command.
"""

from __future__ import annotations

from sqlalchemy.engine import Connection

__all__ = ["ensure_pgvector_installed"]

#: SQLSTATE 42501 - insufficient_privilege (psycopg exposes ``.sqlstate``;
#: psycopg2 names the same field ``.pgcode``).
_INSUFFICIENT_PRIVILEGE = "42501"


def ensure_pgvector_installed(connection: Connection) -> None:
    """Install the ``vector`` extension if possible; never fail when present.

    * Extension already installed -> no-op, no statement executed.
    * Extension missing and installable (e.g. a trusted/owner setup) ->
      ``CREATE EXTENSION IF NOT EXISTS vector``.
    * Extension missing and not installable -> ``RuntimeError`` with the
      exact one-time superuser command the operator must run.
    """

    present = connection.exec_driver_sql(
        "SELECT 1 FROM pg_extension WHERE extname = 'vector'"
    ).first()
    if present is not None:
        return

    # Read the database name BEFORE attempting the CREATE: on failure the
    # transaction is aborted and a follow-up query would itself fail.
    database = connection.exec_driver_sql("SELECT current_database()").scalar()
    try:
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:
        if _is_insufficient_privilege(exc):
            raise RuntimeError(_operator_message(database)) from exc
        raise


def _is_insufficient_privilege(exc: Exception) -> bool:
    """True when the DBAPI error behind a SQLAlchemy wrapper is SQLSTATE 42501."""

    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate == _INSUFFICIENT_PRIVILEGE


def _operator_message(database: str | None) -> str:
    return (
        f"pgvector extension 'vector' is not installed in database '{database}' "
        "and this role lacks permission to install it (pgvector is not a "
        "trusted extension). Run once as a superuser, then re-run the "
        'migrations: psql -d "%s" -U postgres -c "CREATE EXTENSION IF NOT '
        'EXISTS vector;"' % database
    )
