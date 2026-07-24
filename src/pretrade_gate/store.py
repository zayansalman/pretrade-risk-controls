"""The persistence seam: an async string key-value contract, four methods wide.

The engine never sees a database. It sees ``get``, ``get_many``, ``set`` and
``set_many`` over strings, which is narrow enough that a dictionary satisfies
it for tests, a SQLite file satisfies it for a single host, and Redis or a
relational database satisfies it for a desk running several processes — all
without the engine changing.

Why the batch methods are part of the contract rather than an optional extra
-----------------------------------------------------------------------------

A snapshot of the daily counters is several values that are only meaningful
together: the trading date, both realized P&L legs, both high water marks and
the day's notional. Written one at a time, a crash halfway through leaves the
store holding half of one snapshot and half of another — and the half that
survives may be the half that relaxes a limit.

Making ``set_many`` part of the interface means a store that can write
atomically does, and the engine gets that guarantee without asking what kind
of store it holds. Capability sniffing — checking at runtime whether a store
happens to support batching — would be one more thing for a port to reproduce
and one more path to test. :class:`SequentialBatchMixin` gives backends with
no native transaction a correct, if non-atomic, implementation for free.

``get_many`` is the same argument for reads: the counters must be loaded as
one consistent set, and one round trip is both faster and less likely to tear.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol


class StateStore(Protocol):
    """Minimal async string key-value store.

    ``get`` returns ``None`` for a missing key. ``set`` takes a ``str``, never
    ``None`` — "cleared" is encoded as the empty string, and the contract is
    that ``""`` round-trips as ``""``, not ``None``. Readers that treat blank
    as unset do so explicitly, in :mod:`pretrade_gate.encoding`.
    """

    async def get(self, key: str) -> str | None: ...

    async def get_many(self, keys: Iterable[str]) -> dict[str, str | None]: ...

    async def set(self, key: str, value: str) -> None: ...

    async def set_many(self, entries: Mapping[str, str]) -> None: ...


class SequentialBatchMixin:
    """Batch methods for a backend that has no native transaction.

    Correct but not atomic: a crash part-way through ``set_many`` can leave
    some keys updated and others not. Every reader in this library loads
    fail-safe from a torn snapshot, so the outcome is a conservative limit
    rather than a wrong one — but a backend that can do better should override
    ``set_many`` rather than inherit this.
    """

    async def get_many(self, keys: Iterable[str]) -> dict[str, str | None]:
        return {key: await self.get(key) for key in keys}  # type: ignore[attr-defined]

    async def set_many(self, entries: Mapping[str, str]) -> None:
        for key, value in entries.items():
            await self.set(key, value)  # type: ignore[attr-defined]


class InMemoryStateStore(SequentialBatchMixin):
    """Dictionary-backed store for tests, demonstrations and ephemeral runs.

    Its batch write is atomic in the only sense available in-process: there is
    no await point inside it, so nothing can observe it half-applied.
    """

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._data.get(key)

    async def get_many(self, keys: Iterable[str]) -> dict[str, str | None]:
        return {key: self._data.get(key) for key in keys}

    async def set(self, key: str, value: str) -> None:
        self._data[key] = value

    async def set_many(self, entries: Mapping[str, str]) -> None:
        self._data.update(entries)
