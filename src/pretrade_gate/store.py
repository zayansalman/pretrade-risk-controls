"""Pluggable persistence: the async string key-value contract the gate writes through.

The gate never sees a database — it sees two methods. A dict satisfies them for
tests and demos; ``SqliteStateStore`` satisfies them for a single-host bot;
Redis or Postgres can satisfy them behind the same two signatures.
"""

from __future__ import annotations

from typing import Protocol


class StateStore(Protocol):
    """Minimal async string KV store.

    ``get`` returns ``None`` for a missing key. ``set`` takes a ``str``, never
    ``None`` — "cleared" is encoded as the empty string, and the contract is
    that ``""`` round-trips as ``""``, not ``None`` (readers that treat blank
    as unset do so explicitly, e.g. ``read_positive_float``).
    """

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str) -> None: ...


class InMemoryStateStore:
    """Dict-backed :class:`StateStore` for tests, demos, and ephemeral runs."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def set(self, key: str, value: str) -> None:
        self._data[key] = value


async def read_positive_float(store: StateStore, key: str) -> float | None:
    """Read a key as a positive float, or ``None`` when unset / invalid / <= 0.

    Blank (``""``) and absent both read as unset — the empty string is the
    runtime-clear encoding used by the operator controls, since ``set`` never
    takes ``None``.
    """
    raw = await store.get(key)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None
