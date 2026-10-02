"""SQLite-backed user model store (Master section 16).

The user model is the evidence-backed half of memory: preferences, per-domain
skill levels, and label-free observations about a user. It is deliberately
separate from runtime state (Master section 16) and is only updated through
explicit calls -- nothing is inferred or stored automatically.

Each user is one row: the full ``UserModel`` JSON plus an indexed ``updated_at``
column. Datetimes are stored as ISO-8601 UTC strings. Infra is stdlib
``sqlite3`` only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from uap.contracts import Observation, UserModel, utc_now

__all__ = ["UserModelStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_models (
    user_id    TEXT PRIMARY KEY,
    model_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_models_updated_at ON user_models(updated_at);
"""


def _connect(db_path: Path | str) -> sqlite3.Connection:
    path = str(db_path)
    if path != ":memory:":
        parent = Path(path).parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


class UserModelStore:
    """Durable store for ``UserModel`` rows, keyed by ``user_id``."""

    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self._conn = _connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- internals --------------------------------------------------------- #

    def _save(self, model: UserModel) -> UserModel:
        self._conn.execute(
            "INSERT OR REPLACE INTO user_models (user_id, model_json, updated_at) "
            "VALUES (?, ?, ?)",
            (model.user_id, model.model_dump_json(), model.updated_at.isoformat()),
        )
        self._conn.commit()
        return model

    @staticmethod
    def _load(user_id: str, row: tuple | None) -> UserModel:
        if row is None:
            return UserModel(user_id=user_id)
        return UserModel.model_validate_json(row[0])

    def _row(self, user_id: str) -> tuple | None:
        return self._conn.execute(
            "SELECT model_json FROM user_models WHERE user_id = ?", (user_id,)
        ).fetchone()

    @staticmethod
    def _touched(model: UserModel) -> UserModel:
        return model.model_copy(update={"updated_at": utc_now()})

    # -- public API -------------------------------------------------------- #

    def get(self, user_id: str) -> UserModel:
        """Return the stored model, or a default empty model when absent."""
        return self._load(user_id, self._row(user_id))

    def add_observation(self, user_id: str, observation: Observation) -> UserModel:
        """Append one evidence-backed observation and persist."""
        model = self.get(user_id)
        updated = model.model_copy(
            update={
                "observations": [*model.observations, observation],
                "updated_at": utc_now(),
            }
        )
        return self._save(updated)

    def set_skill_level(self, user_id: str, domain: str, level: str) -> UserModel:
        """Record a per-domain skill level and persist."""
        model = self.get(user_id)
        levels = {**model.domain_skill_levels, str(domain): str(level)}
        updated = model.model_copy(
            update={"domain_skill_levels": levels, "updated_at": utc_now()}
        )
        return self._save(updated)

    def set_preference(self, user_id: str, key: str, value: object) -> UserModel:
        """Record a preference value and persist."""
        model = self.get(user_id)
        preferences = {**model.preferences, str(key): value}
        updated = model.model_copy(
            update={"preferences": preferences, "updated_at": utc_now()}
        )
        return self._save(updated)

    def delete(self, user_id: str) -> bool:
        """Delete a user's model; True if a row was removed."""
        cursor = self._conn.execute(
            "DELETE FROM user_models WHERE user_id = ?", (user_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()
