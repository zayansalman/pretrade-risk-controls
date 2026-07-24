"""SQLite-backed :class:`~pretrade_gate.store.StateStore`.

The ONLY module in the package that imports ``aiosqlite`` — install it with
the extra: ``pip install pretrade-gate[sqlite]``. The core package stays
dependency-free.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS state ("
    "  key TEXT PRIMARY KEY,"
    "  value TEXT NOT NULL,"
    "  updated_at TEXT NOT NULL"
    ")"
)


class SqliteStateStore:
    """StateStore over a single SQLite file.

    Opens a connection per call — simple and correct at the gate's low write
    rate — enables WAL so a dashboard process can read while the bot writes,
    and creates the ``state`` table lazily on first use.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    async def get(self, key: str) -> str | None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(_SCHEMA)
            async with db.execute("SELECT value FROM state WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def set(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(_SCHEMA)
            await db.execute(
                "INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET"
                "   value = excluded.value,"
                "   updated_at = excluded.updated_at",
                (key, value, datetime.now(UTC).isoformat()),
            )
            await db.commit()
