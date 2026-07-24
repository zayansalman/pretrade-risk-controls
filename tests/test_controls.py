"""Operator control-plane tests: the write side of the gate.

Controls write keys through a bare ``StateStore`` handle; a gate reading the
same store picks the change up at its next ``load()`` / ``refresh_*``. These
tests always drive BOTH sides through one shared store instance.
"""

from __future__ import annotations

import pytest

from pretrade_gate import (
    GateConfig,
    RiskGate,
    get_loss_halt_bypass,
    reset_daily_loss_halt,
    set_loss_halt_bypass,
)


def _cfg(*, daily_loss_halt_usd: float = 10.0, bankroll_cap_usd: float | None = None) -> GateConfig:
    return GateConfig(
        max_trade_usd=5.0,
        daily_loss_halt_usd=daily_loss_halt_usd,
        bankroll_cap_usd=bankroll_cap_usd,
        max_entry_slippage=0.02,
        kill_switch_path=None,
    )


class TestLossHaltBypassControls:
    @pytest.mark.asyncio
    async def test_get_defaults_false(self, store) -> None:
        assert await get_loss_halt_bypass(store) is False

    @pytest.mark.asyncio
    async def test_set_get_round_trip(self, store) -> None:
        await set_loss_halt_bypass(store, True)
        assert await get_loss_halt_bypass(store) is True
        await set_loss_halt_bypass(store, False)
        assert await get_loss_halt_bypass(store) is False


class TestTrailingHaltTruthTable:
    """The trailing-halt decision, pinned against independently recomputed
    expectations (limit = 10):

        pnl   = sum(seq)
        peak  = max(0, running-max of cumulative pnl)
        floor = peak - 10
        halted iff pnl <= floor

    Each expectation below was recomputed from that arithmetic, not copied
    from the parent's dashboard-panel assertions.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("seq", "halted"),
        [
            ([], False),  # pnl 0, peak 0, floor -10 → full headroom
            ([30.0, -10.0], True),  # pnl 20, peak 30, floor 20 → 20 <= 20, boundary
            ([30.0, -9.0], False),  # pnl 21, peak 30, floor 20 → 21 > 20
            ([30.0, -11.0], True),  # pnl 19, peak 30, floor 20 → 19 <= 20
            ([-10.0], True),  # pnl -10, peak 0, floor -10 → fixed-floor equivalence
            ([-9.99], False),  # pnl -9.99, peak 0, floor -10 → just above
            ([2.0, -2.0], False),  # pnl 0, peak 2, floor -8 → 0 > -8
        ],
    )
    async def test_gate_matches_recomputed_expectation(self, store, seq, halted) -> None:
        gate = RiskGate(_cfg(), store, is_live=True)
        for pnl in seq:
            await gate.record_realized_pnl(pnl, is_live=True)
        assert gate.loss_halt_breached() is halted


class TestResetDailyLossHalt:
    @pytest.mark.asyncio
    async def test_zeroes_both_legs_pnl_and_peaks(self, store) -> None:
        gate = RiskGate(_cfg(), store, is_live=True)
        await gate.record_realized_pnl(12.0, is_live=True)
        await gate.record_realized_pnl(-3.0, is_live=False)
        await reset_daily_loss_halt(store)
        assert await store.get("gate.live_realized_pnl") == "0.0"
        assert await store.get("gate.paper_realized_pnl") == "0.0"
        assert await store.get("gate.live_peak_pnl") == "0.0"
        assert await store.get("gate.paper_peak_pnl") == "0.0"

    @pytest.mark.asyncio
    async def test_clears_trailing_halt_after_banked_peak(self, store) -> None:
        # Banked +$30 peak bled back to +$20 → floor +20, 20 <= 20 → HALTED.
        # Zeroing PnL alone would NOT clear this (floor would stay +20);
        # the reset must clear the peaks too. Proven through a freshly
        # load()ed gate — what the loop sees on the next Start.
        gate = RiskGate(_cfg(), store, is_live=True)
        await gate.record_realized_pnl(30.0, is_live=True)
        await gate.record_realized_pnl(-10.0, is_live=True)
        assert gate.loss_halt_breached() is True  # precondition: halted

        await reset_daily_loss_halt(store)

        fresh = RiskGate(_cfg(), store, is_live=True)
        await fresh.load()
        assert fresh.halt_peak == 0.0
        assert fresh.loss_halt_breached() is False

    @pytest.mark.asyncio
    async def test_date_and_notional_untouched(self, store) -> None:
        gate = RiskGate(_cfg(bankroll_cap_usd=100.0), store, is_live=True)
        await gate.record_realized_pnl(-5.0, is_live=True)
        await gate.record_buy_notional(40.0)
        date_before = await store.get("gate.date")
        notional_before = await store.get("gate.daily_buy_notional")
        assert date_before is not None and notional_before is not None

        await reset_daily_loss_halt(store)

        assert await store.get("gate.date") == date_before
        assert await store.get("gate.daily_buy_notional") == notional_before
