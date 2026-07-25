"""Operator control plane: the write side, and the constraints on it.

The tests that matter most here are the ones asserting what the control plane
REFUSES to write. A bypass with no expiry, no named authoriser or no reason is
not a valid instruction, and the module rejecting it is the whole design.
"""

from __future__ import annotations

import pytest

from pretrade_risk import (
    MAX_BYPASS_DURATION_MILLIS,
    InMemoryStateStore,
    OrderRequest,
    PreTradeRiskEngine,
    RejectCode,
    RiskLimits,
    Side,
    bypass_loss_limit,
    clear_loss_limit_bypass,
    get_runtime_max_order_notional,
    get_runtime_max_order_quantity,
    keys,
    read_loss_limit_bypass,
    reset_daily_loss_limit,
    set_runtime_max_order_notional,
    set_runtime_max_order_quantity,
)

from conftest import FIXED_NOW_MILLIS

MINUTE = 60_000


def limits(**overrides) -> RiskLimits:
    base = {"daily_loss_limit_usd": 10.0}
    base.update(overrides)
    return RiskLimits(**base)


def engine(store, clock, *, is_live: bool = True, **limit_overrides) -> PreTradeRiskEngine:
    return PreTradeRiskEngine(limits(**limit_overrides), store, is_live=is_live, clock=clock)


def order(**overrides) -> OrderRequest:
    base = {"symbol": "ACME", "side": Side.BUY, "quantity": 100.0, "limit_price": 10.0}
    base.update(overrides)
    return OrderRequest(**base)


class TestBypassRequiresAnExpiry:
    @pytest.mark.asyncio
    async def test_a_bounded_bypass_suspends_the_limit(self, store, clock) -> None:
        eng = engine(store, clock)
        await eng.record_realized_pnl(-15.0, is_live=True)
        assert eng.evaluate(order()).code is RejectCode.LOSS_LIMIT_BREACHED

        await bypass_loss_limit(
            store,
            duration_millis=30 * MINUTE,
            actor="risk.manager",
            reason="manual unwind of an illiquid position",
            clock=clock,
        )
        await eng.refresh_overrides()
        assert eng.evaluate(order()).accepted

    @pytest.mark.asyncio
    async def test_the_limit_re_arms_itself_when_the_bypass_lapses(self, store, clock) -> None:
        eng = engine(store, clock)
        await eng.record_realized_pnl(-15.0, is_live=True)
        await bypass_loss_limit(
            store, duration_millis=30 * MINUTE, actor="risk.manager", reason="unwind", clock=clock
        )
        await eng.refresh_overrides()
        assert eng.evaluate(order()).accepted

        # Nobody does anything. No refresh, no console, no operator.
        clock.advance_millis(30 * MINUTE)
        assert eng.evaluate(order()).code is RejectCode.LOSS_LIMIT_BREACHED

    @pytest.mark.asyncio
    async def test_expiry_is_exclusive_at_the_boundary(self, store, clock) -> None:
        eng = engine(store, clock)
        await eng.record_realized_pnl(-15.0, is_live=True)
        await bypass_loss_limit(
            store, duration_millis=MINUTE, actor="risk.manager", reason="unwind", clock=clock
        )
        await eng.refresh_overrides()
        clock.advance_millis(MINUTE - 1)
        assert eng.evaluate(order()).accepted
        clock.advance_millis(1)
        assert not eng.evaluate(order()).accepted

    @pytest.mark.asyncio
    async def test_duration_must_be_positive(self, store, clock) -> None:
        with pytest.raises(ValueError):
            await bypass_loss_limit(store, duration_millis=0, actor="a", reason="b", clock=clock)

    @pytest.mark.asyncio
    async def test_duration_is_bounded(self, store, clock) -> None:
        with pytest.raises(ValueError):
            await bypass_loss_limit(
                store,
                duration_millis=MAX_BYPASS_DURATION_MILLIS + 1,
                actor="a",
                reason="b",
                clock=clock,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("actor", "reason"), [("", "why"), ("  ", "why"), ("who", " ")])
    async def test_attribution_is_mandatory(self, store, clock, actor, reason) -> None:
        with pytest.raises(ValueError):
            await bypass_loss_limit(
                store, duration_millis=MINUTE, actor=actor, reason=reason, clock=clock
            )

    @pytest.mark.asyncio
    async def test_a_flag_without_an_expiry_is_ignored(self, store, clock) -> None:
        # Hand-edited or damaged state. The fail-safe reading is "armed".
        eng = engine(store, clock)
        await eng.record_realized_pnl(-15.0, is_live=True)
        await store.set(keys.LOSS_LIMIT_BYPASS, "1")
        await eng.refresh_overrides()
        assert eng.evaluate(order()).code is RejectCode.LOSS_LIMIT_BREACHED


class TestBypassAuditTrail:
    @pytest.mark.asyncio
    async def test_records_who_why_and_until(self, store, clock) -> None:
        expires_at = await bypass_loss_limit(
            store,
            duration_millis=15 * MINUTE,
            actor="jane.doe",
            reason="venue outage recovery",
            clock=clock,
        )
        record = await read_loss_limit_bypass(store)
        assert record.enabled is True
        assert record.actor == "jane.doe"
        assert record.reason == "venue outage recovery"
        assert record.set_at_millis == FIXED_NOW_MILLIS
        assert record.expires_at_millis == expires_at == FIXED_NOW_MILLIS + 15 * MINUTE

    @pytest.mark.asyncio
    async def test_active_at_answers_the_control_question(self, store, clock) -> None:
        await bypass_loss_limit(
            store, duration_millis=MINUTE, actor="jane.doe", reason="unwind", clock=clock
        )
        record = await read_loss_limit_bypass(store)
        assert record.active_at(FIXED_NOW_MILLIS) is True
        assert record.active_at(FIXED_NOW_MILLIS + MINUTE) is False

    @pytest.mark.asyncio
    async def test_clearing_records_who_re_armed_it(self, store, clock) -> None:
        await bypass_loss_limit(
            store, duration_millis=MINUTE, actor="jane.doe", reason="unwind", clock=clock
        )
        await clear_loss_limit_bypass(store, actor="john.roe", clock=clock)
        record = await read_loss_limit_bypass(store)
        assert record.enabled is False
        assert record.actor == "john.roe"

    @pytest.mark.asyncio
    async def test_clearing_re_arms_the_limit_immediately(self, store, clock) -> None:
        eng = engine(store, clock)
        await eng.record_realized_pnl(-15.0, is_live=True)
        await bypass_loss_limit(
            store, duration_millis=MAX_BYPASS_DURATION_MILLIS, actor="a", reason="b", clock=clock
        )
        await eng.refresh_overrides()
        assert eng.evaluate(order()).accepted

        await clear_loss_limit_bypass(store, actor="risk.manager", clock=clock)
        await eng.refresh_overrides()
        assert eng.evaluate(order()).code is RejectCode.LOSS_LIMIT_BREACHED

    @pytest.mark.asyncio
    async def test_clearing_requires_attribution(self, store, clock) -> None:
        with pytest.raises(ValueError):
            await clear_loss_limit_bypass(store, actor="", clock=clock)


class TestBypassAppliesToBothModes:
    @pytest.mark.asyncio
    async def test_live_and_simulated_both_honour_it(self, clock) -> None:
        for is_live in (True, False):
            store = InMemoryStateStore()
            eng = engine(store, clock, is_live=is_live)
            await eng.record_realized_pnl(-15.0, is_live=is_live)
            assert not eng.evaluate(order()).accepted
            await bypass_loss_limit(
                store, duration_millis=MINUTE, actor="a", reason="b", clock=clock
            )
            await eng.refresh_overrides()
            assert eng.evaluate(order()).accepted

    @pytest.mark.asyncio
    async def test_other_controls_stay_armed_under_a_bypass(self, store, clock) -> None:
        eng = engine(store, clock, max_order_quantity=10.0)
        await eng.record_realized_pnl(-15.0, is_live=True)
        await bypass_loss_limit(store, duration_millis=MINUTE, actor="a", reason="b", clock=clock)
        await eng.refresh_overrides()
        assert eng.evaluate(order(quantity=5.0)).accepted
        assert eng.evaluate(order(quantity=50.0)).code is RejectCode.MAX_ORDER_QUANTITY_EXCEEDED


class TestResetDailyLossLimit:
    @pytest.mark.asyncio
    async def test_zeroes_both_legs_and_both_peaks(self, store, clock) -> None:
        eng = engine(store, clock)
        await eng.record_realized_pnl(12.0, is_live=True)
        await eng.record_realized_pnl(-3.0, is_live=False)
        await reset_daily_loss_limit(store)
        for key in (
            keys.LIVE_REALIZED_PNL,
            keys.SIMULATED_REALIZED_PNL,
            keys.LIVE_PEAK_PNL,
            keys.SIMULATED_PEAK_PNL,
        ):
            assert await store.get(key) == "0"

    @pytest.mark.asyncio
    async def test_clearing_the_peak_is_what_actually_re_arms_it(self, store, clock) -> None:
        # Banked +30 bled back to +20 leaves the floor at +20 and the limit
        # breached. Zeroing P&L alone would NOT clear it — the peak must go too.
        eng = engine(store, clock)
        await eng.record_realized_pnl(30.0, is_live=True)
        await eng.record_realized_pnl(-10.0, is_live=True)
        assert eng.loss_limit_breached() is True

        await reset_daily_loss_limit(store)

        reborn = engine(store, clock)
        await reborn.load()
        assert reborn.loss_limit_peak == 0.0
        assert reborn.loss_limit_breached() is False

    @pytest.mark.asyncio
    async def test_leaves_the_trading_date_and_notional_alone(self, store, clock) -> None:
        # This re-arms the loss limit; it does not grant a fresh day.
        eng = engine(store, clock, daily_notional_limit_usd=1_000.0)
        await eng.record_realized_pnl(-5.0, is_live=True)
        await eng.record_notional(400.0)
        day_before = await store.get(keys.TRADING_DAY)
        notional_before = await store.get(keys.DAILY_NOTIONAL)

        await reset_daily_loss_limit(store)

        assert await store.get(keys.TRADING_DAY) == day_before
        assert await store.get(keys.DAILY_NOTIONAL) == notional_before


class TestRuntimeCaps:
    @pytest.mark.asyncio
    async def test_notional_cap_round_trip(self, store) -> None:
        assert await get_runtime_max_order_notional(store) is None
        await set_runtime_max_order_notional(store, 250.0)
        assert await get_runtime_max_order_notional(store) == 250.0
        await set_runtime_max_order_notional(store, None)
        assert await get_runtime_max_order_notional(store) is None

    @pytest.mark.asyncio
    async def test_quantity_cap_round_trip(self, store) -> None:
        await set_runtime_max_order_quantity(store, 8.0)
        assert await get_runtime_max_order_quantity(store) == 8.0
        await set_runtime_max_order_quantity(store, 0)  # <= 0 clears
        assert await get_runtime_max_order_quantity(store) is None

    @pytest.mark.asyncio
    async def test_override_tightens_without_a_restart(self, store, clock) -> None:
        eng = engine(store, clock, max_order_notional_usd=5_000.0)
        assert eng.evaluate(order()).accepted  # 100 at 10.00 = 1,000
        await set_runtime_max_order_notional(store, 500.0)
        await eng.refresh_overrides()
        assert eng.evaluate(order()).code is RejectCode.MAX_ORDER_NOTIONAL_EXCEEDED

    @pytest.mark.asyncio
    async def test_override_loosens_within_the_session(self, store, clock) -> None:
        eng = engine(store, clock, max_order_notional_usd=500.0)
        assert not eng.evaluate(order()).accepted
        await set_runtime_max_order_notional(store, 5_000.0)
        await eng.refresh_overrides()
        assert eng.evaluate(order()).accepted

    @pytest.mark.asyncio
    async def test_clearing_falls_back_to_the_configured_limit(self, store, clock) -> None:
        eng = engine(store, clock, max_order_notional_usd=5_000.0)
        await set_runtime_max_order_notional(store, 500.0)
        await eng.refresh_overrides()
        assert eng.effective_max_order_notional == 500.0
        await set_runtime_max_order_notional(store, None)
        await eng.refresh_overrides()
        assert eng.effective_max_order_notional == 5_000.0

    @pytest.mark.asyncio
    async def test_an_override_can_arm_a_cap_that_was_not_configured(self, store, clock) -> None:
        eng = engine(store, clock)
        assert "maximum order quantity" not in eng.armed_controls()
        await set_runtime_max_order_quantity(store, 10.0)
        await eng.refresh_overrides()
        assert "maximum order quantity" in eng.armed_controls()

    @pytest.mark.asyncio
    async def test_a_damaged_override_reads_as_unset(self, store, clock) -> None:
        eng = engine(store, clock, max_order_notional_usd=5_000.0)
        await store.set(keys.RUNTIME_MAX_ORDER_NOTIONAL, "not-a-number")
        await eng.refresh_overrides()
        assert eng.effective_max_order_notional == 5_000.0

    @pytest.mark.asyncio
    async def test_overrides_apply_in_both_modes(self, clock) -> None:
        for is_live in (True, False):
            store = InMemoryStateStore()
            eng = engine(store, clock, is_live=is_live, max_order_notional_usd=5_000.0)
            await set_runtime_max_order_notional(store, 500.0)
            await eng.refresh_overrides()
            assert eng.effective_max_order_notional == 500.0


class TestControlPlaneIsDecoupled:
    @pytest.mark.asyncio
    async def test_a_write_is_invisible_until_the_engine_refreshes(self, store, clock) -> None:
        # The store is the only channel between the two sides, and the engine
        # decides when to look at it.
        eng = engine(store, clock, max_order_notional_usd=5_000.0)
        await set_runtime_max_order_notional(store, 1.0)
        assert eng.evaluate(order()).accepted  # not refreshed yet
        await eng.refresh_overrides()
        assert not eng.evaluate(order()).accepted

    @pytest.mark.asyncio
    async def test_the_control_plane_needs_no_engine(self, store, clock) -> None:
        # Every control-plane function takes a bare store handle — a console
        # process holds no engine at all.
        await bypass_loss_limit(store, duration_millis=MINUTE, actor="a", reason="b", clock=clock)
        await set_runtime_max_order_notional(store, 42.0)
        await reset_daily_loss_limit(store)
        assert (await read_loss_limit_bypass(store)).enabled is True
