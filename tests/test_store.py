"""StateStore contract.

Any store handed to the engine must satisfy exactly this contract, so the same
suite runs over every implementation. A third backend is validated by adding
one fixture parameter.
"""

from __future__ import annotations

import pytest

from pretrade_risk import (
    InMemoryStateStore,
    ManualClock,
    PreTradeRiskEngine,
    RiskLimits,
    keys,
    set_runtime_max_order_notional,
)
from pretrade_risk.sqlite_store import SqliteStateStore

from conftest import FIXED_NOW_MILLIS


@pytest.fixture(params=["memory", "sqlite"])
def any_store(request, tmp_path):
    if request.param == "memory":
        return InMemoryStateStore()
    return SqliteStateStore(tmp_path / "state.db")


def engine(store) -> PreTradeRiskEngine:
    return PreTradeRiskEngine(
        RiskLimits(daily_loss_limit_usd=10.0, max_order_notional_usd=5_000.0),
        store,
        is_live=True,
        clock=ManualClock(FIXED_NOW_MILLIS),
    )


class TestSingleKeyContract:
    @pytest.mark.asyncio
    async def test_missing_key_is_none(self, any_store) -> None:
        assert await any_store.get("never.written") is None

    @pytest.mark.asyncio
    async def test_round_trip(self, any_store) -> None:
        await any_store.set("k", "v")
        assert await any_store.get("k") == "v"

    @pytest.mark.asyncio
    async def test_overwrite(self, any_store) -> None:
        await any_store.set("k", "first")
        await any_store.set("k", "second")
        assert await any_store.get("k") == "second"

    @pytest.mark.asyncio
    async def test_empty_string_round_trips_as_empty_string(self, any_store) -> None:
        # "" is the cleared-override encoding; it must come back as "", not None.
        await any_store.set("k", "")
        assert await any_store.get("k") == ""

    @pytest.mark.asyncio
    async def test_keys_are_independent(self, any_store) -> None:
        await any_store.set("a", "1")
        await any_store.set("b", "2")
        assert await any_store.get("a") == "1"
        assert await any_store.get("b") == "2"


class TestBatchContract:
    @pytest.mark.asyncio
    async def test_get_many_returns_every_key_asked_for(self, any_store) -> None:
        await any_store.set("a", "1")
        result = await any_store.get_many(["a", "missing"])
        assert result == {"a": "1", "missing": None}

    @pytest.mark.asyncio
    async def test_get_many_of_nothing_is_empty(self, any_store) -> None:
        assert await any_store.get_many([]) == {}

    @pytest.mark.asyncio
    async def test_set_many_writes_every_entry(self, any_store) -> None:
        await any_store.set_many({"a": "1", "b": "2"})
        assert await any_store.get_many(["a", "b"]) == {"a": "1", "b": "2"}

    @pytest.mark.asyncio
    async def test_set_many_of_nothing_is_a_no_op(self, any_store) -> None:
        await any_store.set_many({})
        assert await any_store.get("a") is None

    @pytest.mark.asyncio
    async def test_set_many_overwrites(self, any_store) -> None:
        await any_store.set("a", "old")
        await any_store.set_many({"a": "new"})
        assert await any_store.get("a") == "new"

    @pytest.mark.asyncio
    async def test_batch_and_single_writes_share_one_namespace(self, any_store) -> None:
        await any_store.set("a", "1")
        await any_store.set_many({"b": "2"})
        assert await any_store.get_many(["a", "b"]) == {"a": "1", "b": "2"}


class TestDurability:
    @pytest.mark.asyncio
    async def test_sqlite_values_survive_a_new_instance(self, tmp_path) -> None:
        path = tmp_path / "state.db"
        await SqliteStateStore(path).set("k", "v")
        assert await SqliteStateStore(path).get("k") == "v"

    @pytest.mark.asyncio
    async def test_sqlite_batch_survives_a_new_instance(self, tmp_path) -> None:
        path = tmp_path / "state.db"
        await SqliteStateStore(path).set_many({"a": "1", "b": "2"})
        assert await SqliteStateStore(path).get_many(["a", "b"]) == {"a": "1", "b": "2"}


class TestEngineStateThroughEveryBackend:
    @pytest.mark.asyncio
    async def test_the_peak_reloads_identically(self, any_store) -> None:
        first = engine(any_store)
        await first.record_realized_pnl(20.0, is_live=True)
        await first.record_realized_pnl(-5.0, is_live=True)
        reborn = engine(any_store)
        await reborn.load()
        assert reborn.loss_limit_peak == pytest.approx(20.0)
        assert reborn.live_pnl == pytest.approx(15.0)

    @pytest.mark.asyncio
    async def test_accumulated_float_error_reloads_bit_exactly(self, any_store) -> None:
        # The counter must reload to the SAME double, or the limit boundary
        # moves across a restart.
        first = engine(any_store)
        for _ in range(100):
            await first.record_realized_pnl(0.1, is_live=True)
        before = first.live_pnl
        reborn = engine(any_store)
        await reborn.load()
        assert reborn.live_pnl == before

    @pytest.mark.asyncio
    async def test_runtime_override_round_trip(self, any_store) -> None:
        await set_runtime_max_order_notional(any_store, 250.0)
        eng = engine(any_store)
        await eng.refresh_overrides()
        assert eng.effective_max_order_notional == 250.0
        await set_runtime_max_order_notional(any_store, None)  # clears via ""
        await eng.refresh_overrides()
        assert eng.effective_max_order_notional == 5_000.0

    @pytest.mark.asyncio
    async def test_persist_writes_the_whole_snapshot(self, any_store) -> None:
        eng = engine(any_store)
        await eng.persist()
        stored = await any_store.get_many(keys.COUNTER_KEYS)
        assert all(value is not None for value in stored.values())
