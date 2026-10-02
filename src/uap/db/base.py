"""Declarative base and constraint naming conventions (Master sections 42, 63, 73).

Every ORM model in ``uap.db.models`` inherits from :class:`Base`. The single
shared ``MetaData`` carries a deterministic *naming convention* so that every
constraint (primary key, unique, foreign key, check, index) gets a stable,
readable name. This matters for three reasons:

* Alembic autogenerate can diff constraints reliably (unnamed constraints get
  Postgres-generated names that churn and produce spurious migrations).
* Tests can assert on constraint names without guessing.
* Downgrades / future compatibility work can address constraints explicitly.

The convention uses the ``%(column_0_N_name)s`` token (all columns of the
constraint joined by ``_``) rather than only ``%(column_0_name)s`` so that
composite constraints stay unique.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

__all__ = ["NAMING_CONVENTION", "Base"]

#: Deterministic constraint-name templates (Master section 42: "proper
#: indexes ... constraints"). Applied by SQLAlchemy at DDL compile time.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Shared declarative base for all UAP persistence models.

    ``Base.metadata`` is the single source of truth that Alembic's ``env.py``
    points at (``target_metadata``). Importing ``uap.db.models`` registers
    every table on this metadata object.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
