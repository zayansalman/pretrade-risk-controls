"""StateStore contract tests.

Any store the gate is handed must satisfy exactly this contract — the same
parametrized suite runs over the in-memory and SQLite implementations, so a
third backend can be validated by adding one fixture param.
"""

from __future__ import annotations

import pytest

from pretrade_gate import (
    GateConfig,
    InMemoryStateStore,
    RiskGate,
    get_runtime_max_trade_usd,
    set_runtime_max_trade_usd,
)
from pretrade_gate.sqlite_store import SqliteStateStore


@pytest.fixture(params=["memory", "sqlite"])
def any_store(request, tmp_path):
    if request.param == "memory":
        return InMemoryStateStore()
    return SqliteStateStore(tmp_path / "state.db")


def _cfg() -> GateConfig:
    return GateConfig(
        max_trade_usd=5.0,
        daily_loss_halt_usd=10.0,
        bankroll_cap_usd=None,
        max_entry_slippage=0.02,
        kill_switch_path=None,
    )


class TestStateStoreContract:
    @pytest.mark.asyncio
    async def test_missing_key_is_none(self, any_store) -> None:
        assert await any_store.get("never.written") is None

    @pytest.mark.asyncio
    async def test_set_get_round_trip(self, any_store) -> None:
        await any_store.set("k", "v")
        assert await any_store.get("k") == "v"

    @pytest.mark.asyncio
    async def test_overwrite(self, any_store) -> None:
        await any_store.set("k", "first")
        await any_store.set("k", "second")
        assert await any_store.get("k") == "second"

    @pytest.mark.asyncio
    async def test_empty_string_round_trips_as_empty_string(self, any_store) -> None:
        # "" is the runtime-clear encoding — it must come back as "", NOT None.
        await any_store.set("k", "")
        assert await any_store.get("k") == ""

    @pytest.mark.asyncio
    async def test_key_independence(self, any_store) -> None:
        await any_store.set("a", "1")
        await any_store.set("b", "2")
        assert await any_store.get("a") == "1"
        assert await any_store.get("b") == "2"


class TestSqliteDurability:
    @pytest.mark.asyncio
    async def test_values_survive_a_second_store_instance(self, tmp_path) -> None:
        path = tmp_path / "state.db"
        first = SqliteStateStore(path)
        await first.set("k", "v")
        second = SqliteStateStore(path)  # fresh instance, same file
        assert await second.get("k") == "v"


class TestPersistenceAcrossStores:
    """Gate state written through either backend reads back identically."""

    @pytest.mark.asyncio
    async def test_peak_persists_across_reload(self, any_store) -> None:
        gate = RiskGate(_cfg(), any_store, is_live=True)
        await gate.record_realized_pnl(20.0, is_live=True)  # peak 20
        await gate.record_realized_pnl(-5.0, is_live=True)  # +15
        fresh = RiskGate(_cfg(), any_store, is_live=True)
        await fresh.load()
        assert fresh.halt_peak == pytest.approx(20.0)
        assert fresh.live_pnl == pytest.approx(15.0)

    @pytest.mark.asyncio
    async def test_runtime_override_round_trip(self, any_store) -> None:
        await set_runtime_max_trade_usd(any_store, 2.0)
        gate = RiskGate(_cfg(), any_store)
        await gate.refresh_runtime_limits()
        assert gate.effective_max_trade_usd == 2.0
        await set_runtime_max_trade_usd(any_store, None)  # clears via ""
        assert await get_runtime_max_trade_usd(any_store) is None
        await gate.refresh_runtime_limits()
        assert gate.effective_max_trade_usd == 5.0
