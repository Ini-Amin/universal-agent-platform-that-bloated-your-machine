"""SQLite-backed memory store (Master section 16).

Memory is durable knowledge separated from runtime/checkpoint state: this store
never holds workflow state, it only holds scored, expirable ``Memory`` rows.

Each row keeps the full ``Memory`` model as JSON (``model_json``) next to
indexed scalar columns (``category``, ``relevance``, ``expires_at``,
``created_at``, ``last_accessed_at``) so querying and expiry never need to
deserialize every row. The JSON column stays the source of truth on read.

Infra is stdlib ``sqlite3`` only (no ORM); the upgrade path to PostgreSQL +
pgvector (Master section 25) replaces this module behind the same interface.
All datetimes are stored as ISO-8601 UTC strings.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from uap.contracts import Memory, MemoryCategory, utc_now

__all__ = ["MemoryStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    memory_id        TEXT PRIMARY KEY,
    model_json       TEXT NOT NULL,
    category         TEXT NOT NULL,
    relevance        REAL NOT NULL,
    expires_at       TEXT,
    created_at       TEXT NOT NULL,
    last_accessed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_category   ON memories(category);
CREATE INDEX IF NOT EXISTS idx_memories_relevance  ON memories(relevance);
CREATE INDEX IF NOT EXISTS idx_memories_expires_at ON memories(expires_at);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
"""


def _connect(db_path: Path | str) -> sqlite3.Connection:
    """Open ``db_path``, creating the parent directory for file-backed stores."""
    path = str(db_path)
    if path != ":memory:":
        parent = Path(path).parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def _iso(value: datetime) -> str:
    """Render a datetime as an ISO-8601 UTC string; naive input is read as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _category_value(category: MemoryCategory | str) -> str:
    return category.value if isinstance(category, MemoryCategory) else str(category)


class MemoryStore:
    """Durable store for ``Memory`` rows, keyed by ``memory_id``."""

    def __init__(self, db_path: Path | str = ":memory:") -> None:
        self._conn = _connect(db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- internals --------------------------------------------------------- #

    def _upsert(self, memory: Memory) -> None:
        """Insert or fully replace one memory row (JSON + indexed columns)."""
        self._conn.execute(
            """
            INSERT OR REPLACE INTO memories
                (memory_id, model_json, category, relevance,
                 expires_at, created_at, last_accessed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory.memory_id,
                memory.model_dump_json(),
                _category_value(memory.category),
                float(memory.relevance),
                _iso(memory.expires_at) if memory.expires_at is not None else None,
                _iso(memory.created_at),
                _iso(memory.last_accessed_at)
                if memory.last_accessed_at is not None
                else None,
            ),
        )
        self._conn.commit()

    @staticmethod
    def _decode(row: sqlite3.Row | tuple) -> Memory:
        return Memory.model_validate_json(row[0])

    # -- public API -------------------------------------------------------- #

    def add(self, memory: Memory) -> Memory:
        """Persist ``memory`` (replacing any row with the same id) and return it."""
        self._upsert(memory)
        return memory

    def get(self, memory_id: str) -> Memory | None:
        """Return the memory, touching ``last_accessed_at``; None if absent."""
        row = self._conn.execute(
            "SELECT model_json FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return None

        memory = self._decode(row)
        now = utc_now()
        previous = memory.last_accessed_at
        if previous is not None and now <= previous:
            # Guarantee a strictly newer timestamp even within one clock tick.
            now = previous + timedelta(microseconds=1)
        touched = memory.model_copy(update={"last_accessed_at": now})
        self._upsert(touched)
        return touched

    def query(
        self,
        *,
        category: str | None = None,
        min_relevance: float = 0.0,
        limit: int = 50,
    ) -> list[Memory]:
        """Memories with ``relevance >= min_relevance``, most relevant first."""
        sql = "SELECT model_json FROM memories WHERE relevance >= ?"
        params: list[object] = [float(min_relevance)]
        if category is not None:
            sql += " AND category = ?"
            params.append(_category_value(category))
        sql += " ORDER BY relevance DESC, created_at DESC, memory_id ASC LIMIT ?"
        params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [self._decode(row) for row in rows]

    def update_relevance(self, memory_id: str, relevance: float) -> bool:
        """Set the relevance score; False if no such memory. Raises if out of [0, 1]."""
        if not 0.0 <= float(relevance) <= 1.0:
            raise ValueError("relevance must be within [0.0, 1.0]")
        row = self._conn.execute(
            "SELECT model_json FROM memories WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row is None:
            return False
        updated = self._decode(row).model_copy(update={"relevance": float(relevance)})
        self._upsert(updated)
        return True

    def delete(self, memory_id: str) -> bool:
        """Delete one memory; True if a row was removed."""
        cursor = self._conn.execute(
            "DELETE FROM memories WHERE memory_id = ?", (memory_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def purge_expired(self, now: datetime | None = None) -> int:
        """Delete memories whose ``expires_at`` is in the past; return the count.

        Rows with ``expires_at IS NULL`` never expire and always survive.
        """
        cutoff = _iso(now if now is not None else utc_now())
        cursor = self._conn.execute(
            "DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at < ?",
            (cutoff,),
        )
        self._conn.commit()
        return cursor.rowcount

    def count(self) -> int:
        """Total number of stored memories."""
        return int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()
