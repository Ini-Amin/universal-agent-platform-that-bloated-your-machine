"""PostgreSQL persistence layer (Master sections 41, 42, 63, 73).

Layering: ``uap.db`` depends only on SQLAlchemy/Alembic and, where relevant,
``uap.contracts``. It must never import ``uap.server`` or ``uap.workflows`` -
those depend on the database, not the other way around.

Public surface:

* :mod:`uap.db.engine` - engine/session factory + ``session_scope``.
* :mod:`uap.db.base` - ``Base`` + constraint naming convention.
* :mod:`uap.db.models` - ORM tables (definitions/versions, executions/events).
* :mod:`uap.db.repositories` - typed repositories taking an injected Session.
"""

from __future__ import annotations

from uap.db.base import Base
from uap.db.engine import (
    create_db_engine,
    create_session_factory,
    get_database_url,
    get_engine,
    get_session_factory,
    session_scope,
)

__all__ = [
    "Base",
    "create_db_engine",
    "create_session_factory",
    "get_database_url",
    "get_engine",
    "get_session_factory",
    "session_scope",
]
