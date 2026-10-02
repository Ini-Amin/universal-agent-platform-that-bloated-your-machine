"""Engine / session factory and the ``session_scope`` transaction boundary.

Master sections 2.1 (local-first), 41 (storage), 42 (PostgreSQL), 73 (no hidden
singletons / explicit dependency injection).

Design rules encoded here:

* **One explicit factory, injectable everywhere.** :func:`create_db_engine` and
  :func:`create_session_factory` are pure factories. Repositories receive a
  ``Session``; they never reach for a global one (Master section 73).
* **A resettable process default.** :func:`get_engine` lazily builds the default
  engine from :func:`get_database_url` and :func:`dispose_engine` tears it down.
  It is an explicit, documented, resettable accessor - not hidden state. Tests
  inject their own engine and never touch it.
* **``pool_pre_ping=True``** so a stale connection (server restart, idle
  timeout) is transparently replaced instead of failing a request.
* **``session_scope``** commits on success and rolls back on any exception,
  always closing the session - the only place transaction policy lives.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

__all__ = [
    "DATABASE_URL_ENV",
    "DEFAULT_DATABASE_URL",
    "create_db_engine",
    "create_session_factory",
    "dispose_engine",
    "get_database_url",
    "get_engine",
    "get_session_factory",
    "session_scope",
]

#: Environment variable that overrides the database URL (Master section 62).
DATABASE_URL_ENV = "DATABASE_URL"

#: Local-first default (Master sections 2.1, 42). psycopg3 driver, SQLAlchemy 2.x.
DEFAULT_DATABASE_URL = "postgresql+psycopg://uap:uap_local_dev@127.0.0.1:5432/uap"


def get_database_url() -> str:
    """Return the configured database URL.

    ``DATABASE_URL`` wins when set (env override), otherwise the local-first
    default is used. This is the single resolution point used by the default
    engine and by Alembic's ``env.py``.
    """

    url = os.environ.get(DATABASE_URL_ENV)
    if url:
        return url
    return DEFAULT_DATABASE_URL


def create_db_engine(url: str | None = None, **kwargs: object) -> Engine:
    """Build a new :class:`~sqlalchemy.Engine` (no global state).

    ``pool_pre_ping=True`` is always on. Extra keyword arguments are forwarded
    to :func:`sqlalchemy.create_engine` (e.g. ``echo``, ``connect_args``).
    """

    options: dict[str, object] = {"pool_pre_ping": True, "future": True}
    options.update(kwargs)
    return create_engine(url or get_database_url(), **options)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a ``sessionmaker`` bound to ``engine``.

    ``expire_on_commit=False`` keeps loaded attribute values usable after a
    commit, which is convenient for repositories that return ORM rows.
    """

    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)


# --------------------------------------------------------------------------- #
# Explicit, resettable process default (inject your own in tests)
# --------------------------------------------------------------------------- #

_default_engine: Engine | None = None


def get_engine() -> Engine:
    """Return the lazily-created process-wide engine.

    Explicit and resettable (:func:`dispose_engine`); tests inject their own
    engine/factory and never depend on this one.
    """

    global _default_engine
    if _default_engine is None:
        _default_engine = create_db_engine()
    return _default_engine


def dispose_engine() -> None:
    """Dispose and forget the process-wide default engine."""

    global _default_engine
    if _default_engine is not None:
        _default_engine.dispose()
        _default_engine = None


def get_session_factory() -> sessionmaker[Session]:
    """Return a ``sessionmaker`` bound to the process-wide default engine."""

    return create_session_factory(get_engine())


@contextmanager
def session_scope(
    session_factory: sessionmaker[Session] | None = None,
) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on exception.

    Usage::

        with session_scope(factory) as session:
            repo = ExecutionRepository(session)
            repo.create(workflow_version_id)

    The session is always closed. When ``session_factory`` is omitted the
    process-wide default factory (:func:`get_session_factory`) is used; tests
    pass their own factory for isolation.
    """

    factory = session_factory if session_factory is not None else get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
