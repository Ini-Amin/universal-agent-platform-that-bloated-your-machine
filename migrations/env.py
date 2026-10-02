"""Alembic environment (Master sections 42, 63).

Wired to :data:`uap.db.base.Base` metadata - importing ``uap.db.models`` is
what registers every table on that metadata. The URL comes from
:func:`uap.db.engine.get_database_url`, so ``DATABASE_URL`` overrides the
local-first default here exactly as it does for the application engine.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Register all tables on the shared metadata.
import uap.db.models  # noqa: F401  (import for side effect: table registration)
from uap.db.base import Base
from uap.db.engine import get_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the URL directly rather than through configparser: URLs may contain
# percent-encoded query options (e.g. ``?options=-csearch_path%3D...``) and
# configparser would treat ``%`` as interpolation syntax. A programmatic
# override passed via ``config.attributes`` wins, then ``DATABASE_URL`` /
# the local-first default from uap.db.engine.
_database_url: str = config.attributes.get("sqlalchemy.url") or get_database_url()

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (``--sql`` mode)."""

    context.configure(
        url=_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""

    connectable = create_engine(_database_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
