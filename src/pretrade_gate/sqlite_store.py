"""SQLite-backed :class:`~pretrade_gate.store.StateStore`.

The only module in the package that imports ``aiosqlite`` — install it with
the extra, ``pip install pretrade-gate[sqlite]``. The core package stays
dependency-free.

``set_many`` is a single transaction, which is what makes a counter snapshot
atomic: after a crash the store holds either the whole previous snapshot or
the whole new one, never a mixture of the two.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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

_UPSERT = (
    "INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)"
    " ON CONFLICT(key) DO UPDATE SET"
    "   value = excluded.value,"
    "   updated_at = excluded.updated_at"
)


class SqliteStateStore:
    """State store over a single SQLite file.

    Opens a connection per call — simple and correct at the engine's low write
    rate — enables write-ahead logging so an operator console can read while
    the trading process writes, and creates the ``state`` table lazily.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @staticmethod
    async def _prepare(db: aiosqlite.Connection) -> None:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(_SCHEMA)

    async def get(self, key: str) -> str | None:
        async with aiosqlite.connect(self._path) as db:
            await self._prepare(db)
            async with db.execute("SELECT value FROM state WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def get_many(self, keys: Iterable[str]) -> dict[str, str | None]:
        wanted = list(keys)
        if not wanted:
            return {}
        found: dict[str, str] = {}
        placeholders = ",".join("?" * len(wanted))
        async with aiosqlite.connect(self._path) as db:
            await self._prepare(db)
            query = f"SELECT key, value FROM state WHERE key IN ({placeholders})"  # noqa: S608
            async with db.execute(query, wanted) as cur:
                async for key, value in cur:
                    found[key] = value
        return {key: found.get(key) for key in wanted}

    async def set(self, key: str, value: str) -> None:
        await self.set_many({key: value})

    async def set_many(self, entries: Mapping[str, str]) -> None:
        """Write every entry in one transaction, or none of them."""
        if not entries:
            return
        stamp = datetime.now(UTC).isoformat()
        rows = [(key, value, stamp) for key, value in entries.items()]
        async with aiosqlite.connect(self._path) as db:
            await self._prepare(db)
            await db.executemany(_UPSERT, rows)
            await db.commit()
