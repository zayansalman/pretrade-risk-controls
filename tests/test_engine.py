"""Control-by-control behaviour, and the live/simulated parity that motivates
a single shared engine.

Two properties matter most here and are pinned deliberately.

Identical inputs give identical decisions in both modes. Divergence between a
simulated run and a live one is the defect the shared engine exists to
prevent, so the parity tests assert equality of the decision rather than
checking each mode separately.

An armed control that cannot be evaluated rejects. Several tests deliberately
withhold market data from a configured control and assert a rejection, because
the failure mode being guarded against is a control that silently stops firing
when its input stops arriving.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from pretrade_risk import (
    ALWAYS_ON,
    CONTROL_SEQUENCE,
    ControlId,
    InMemoryStateStore,
    ManualClock,
    OrderRequest,
    PreTradeRiskEngine,
    RejectCode,
    RiskLimits,
    SessionState,
    Side,
)

from conftest import FIXED_NOW_MILLIS


def engine(limits: RiskLimits, store=None, *, is_live: bool = True, clock=None):
    return PreTradeRiskEngine(
        limits,
        store if store is not None else InMemoryStateStore(),
        is_live=is_live,
        clock=clock if clock is not None else ManualClock(FIXED_NOW_MILLIS),
    )


def order(**overrides) -> OrderRequest:
    """A well-formed, unremarkable order, overridable field by field."""
    base = {
        "symbol": "ACME",
        "side": Side.BUY,
        "quantity": 100.0,
        "limit_price": 10.0,
        "decision_price": 10.0,
        "best_bid": 9.99,
        "best_ask": 10.01,
        "quote_age_millis": 5,
        "session_state": SessionState.OPEN,
    }
    base.update(overrides)
    return OrderRequest(**base)


class TestControlSequence:
    """The sequence is the specification, so its shape is asserted directly."""

    def test_names_are_unique(self) -> None:
        names = [control.name for control in CONTROL_SEQUENCE]
        assert len(names) == len(set(names))

    def test_every_control_is_documented(self) -> None:
        # The README tabulates the controls and states how many there are.
        # Pinning the count here means adding a control without documenting it
        # fails the suite rather than quietly leaving the table wrong.
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text().lower()
        assert "twenty-four controls" in readme
        assert len(CONTROL_SEQUENCE) == 24
        for control in CONTROL_SEQUENCE:
            assert control.name.lower() in readme, f"{control.name} is missing from the README"

    def test_every_reject_code_is_reachable(self) -> None:
        # A code nobody can produce is a code that misleads whoever routes on
        # it. Every member of the enum must be emitted by some control.
        import inspect

        from pretrade_risk import engine as engine_module

        source = inspect.getsource(engine_module)
        for code in RejectCode:
            assert f"RejectCode.{code.name}" in source, f"{code.name} is never emitted"

    def test_absolute_stops_come_first(self) -> None:
        names = [control.name for control in CONTROL_SEQUENCE]
        assert names[0] == "kill switch"
        assert names[1] == "daily loss limit"

    def test_every_control_id_appears_exactly_once(self) -> None:
        ids = [control.id for control in CONTROL_SEQUENCE]
        assert len(set(ids)) == len(ids)
        assert set(ids) == set(ControlId)

    def test_wide_open_limits_run_only_well_formedness(self) -> None:
        # With nothing configured, only the checks that make an order
        # well-formed remain — and an unremarkable order still passes.
        assert engine(RiskLimits()).running_controls() == (
            ControlId.ORDER_QUANTITY,
            ControlId.ORDER_PRICE,
            ControlId.ORDER_NOTIONAL,
        )
        assert engine(RiskLimits()).evaluate(order()).accepted

    def test_running_controls_follows_the_configuration(self) -> None:
        eng = engine(RiskLimits(max_order_quantity=10.0, prevent_self_match=True))
        assert ControlId.MAX_ORDER_QUANTITY in eng.running_controls()
        assert ControlId.SELF_MATCH_PREVENTION in eng.running_controls()
        assert ControlId.PRICE_BAND not in eng.running_controls()

    def test_asking_what_is_running_does_not_disturb_state(self) -> None:
        # The duplicate window is stateful; probing controls to ask whether
        # they are armed must not touch it.
        eng = engine(RiskLimits(duplicate_window_millis=1_000))
        eng.record_order_sent(order())
        eng.running_controls()
        eng.control_status()
        assert eng.evaluate(order()).code is RejectCode.DUPLICATE_ORDER


class TestEnablingAndDisablingControls:
    """A control runs when it is configured AND not disabled.

    Disabling is deliberately not the same as blanking a limit: the whole
    point is to stand a control down without throwing away the number
    somebody calibrated.
    """

    CONFIGURED = RiskLimits(
        daily_loss_limit_usd=500.0,
        max_order_quantity=1_000.0,
        price_band_fraction=0.03,
        max_orders_per_window=20,
    )

    def test_disable_stops_the_control_firing(self) -> None:
        wild = order(limit_price=99.0, reference_price=10.0)
        assert engine(self.CONFIGURED).evaluate(wild).code is RejectCode.PRICE_BAND_EXCEEDED
        off = self.CONFIGURED.disable(ControlId.PRICE_BAND)
        assert engine(off).evaluate(wild).accepted

    def test_disable_keeps_the_limit(self) -> None:
        # The calibration survives, so re-enabling does not need it re-derived.
        off = self.CONFIGURED.disable(ControlId.PRICE_BAND)
        assert off.price_band_fraction == 0.03

    def test_enable_restores_it(self) -> None:
        wild = order(limit_price=99.0, reference_price=10.0)
        round_trip = self.CONFIGURED.disable(ControlId.PRICE_BAND).enable(ControlId.PRICE_BAND)
        assert engine(round_trip).evaluate(wild).code is RejectCode.PRICE_BAND_EXCEEDED

    def test_disable_takes_several_controls(self) -> None:
        off = self.CONFIGURED.disable(ControlId.PRICE_BAND, ControlId.ORDER_RATE)
        running = engine(off).running_controls()
        assert ControlId.PRICE_BAND not in running
        assert ControlId.ORDER_RATE not in running
        assert ControlId.MAX_ORDER_QUANTITY in running

    def test_limits_are_immutable(self) -> None:
        # disable() returns a new limit set; the original is untouched.
        self.CONFIGURED.disable(ControlId.PRICE_BAND)
        assert self.CONFIGURED.disabled_controls == frozenset()

    def test_enabling_a_control_with_no_limit_does_nothing(self) -> None:
        enabled = RiskLimits().enable(ControlId.PRICE_BAND)
        assert ControlId.PRICE_BAND not in engine(enabled).running_controls()

    def test_disabling_is_idempotent(self) -> None:
        once = self.CONFIGURED.disable(ControlId.PRICE_BAND)
        assert once.disable(ControlId.PRICE_BAND).disabled_controls == once.disabled_controls

    def test_enabling_something_never_disabled_is_harmless(self) -> None:
        assert self.CONFIGURED.enable(ControlId.PRICE_BAND) == self.CONFIGURED

    @pytest.mark.parametrize("locked", sorted(ALWAYS_ON, key=lambda c: c.value))
    def test_well_formedness_controls_cannot_be_disabled(self, locked) -> None:
        # A negative quantity is malformed whatever a desk's risk appetite is.
        with pytest.raises(ValueError, match="well formed"):
            RiskLimits(disabled_controls=frozenset({locked}))

    def test_a_bare_string_is_refused(self) -> None:
        # Disabling by display name would fail silently on any typo, leaving a
        # control running that somebody believed they had switched off.
        with pytest.raises(ValueError, match="ControlId"):
            RiskLimits(disabled_controls=frozenset({"price band"}))

    def test_a_string_equal_to_a_control_id_is_still_refused(self) -> None:
        # ControlId subclasses str, so "PRICE_BAND" compares AND hashes equal
        # to the member. A membership test would accept it; the validation is
        # isinstance-based precisely so that it does not.
        assert "PRICE_BAND" == ControlId.PRICE_BAND
        assert "PRICE_BAND" in {ControlId.PRICE_BAND}
        with pytest.raises(ValueError, match="ControlId"):
            RiskLimits(disabled_controls=frozenset({"PRICE_BAND"}))

    def test_a_mutable_set_is_frozen_on_the_way_in(self) -> None:
        # Otherwise a "frozen" limit set holds a set somebody can still add to
        # after validation has passed — including an always-on control.
        handed_in = {ControlId.PRICE_BAND}
        limits = RiskLimits(price_band_fraction=0.03, disabled_controls=handed_in)
        assert isinstance(limits.disabled_controls, frozenset)
        with pytest.raises(AttributeError):
            limits.disabled_controls.add(ControlId.ORDER_QUANTITY)

    def test_limits_stay_hashable(self) -> None:
        # A limit set is a value: it gets logged, compared and put in sets.
        assert hash(RiskLimits(disabled_controls={ControlId.PRICE_BAND})) is not None

    def test_always_on_survives_a_forced_disable(self) -> None:
        # The second of two locks. Construction refuses to disable these; this
        # asserts the invariant also holds at the point of use, so reaching
        # past the constructor does not switch off well-formedness.
        limits = RiskLimits(max_order_quantity=5.0)
        object.__setattr__(limits, "disabled_controls", frozenset({ControlId.ORDER_QUANTITY}))
        assert limits.is_disabled(ControlId.ORDER_QUANTITY) is False
        eng = engine(limits)
        assert ControlId.ORDER_QUANTITY in eng.running_controls()
        assert eng.evaluate(order(quantity=-5.0)).code is RejectCode.ORDER_QUANTITY_NOT_POSITIVE

    def test_the_kill_switch_can_be_disabled(self, tmp_path: Path) -> None:
        # Not every control should be locked on. A desk that manages its kill
        # switch elsewhere must be able to turn this one off deliberately.
        kill = tmp_path / "KILL"
        kill.write_text("halt")
        limits = RiskLimits(kill_switch_path=kill)
        assert engine(limits).evaluate(order()).code is RejectCode.KILL_SWITCH_ENGAGED
        assert engine(limits.disable(ControlId.KILL_SWITCH)).evaluate(order()).accepted


class TestReportingMatchesReality:
    """What the engine SAYS it runs must be what it actually runs.

    This is the property that makes the reporting methods worth trusting: an
    operator console reading ``control_status()`` has to be looking at the same
    answer ``evaluate`` acts on, under every combination of configured limits
    and disabled controls — not just the ones somebody thought to test.
    """

    LIMIT_OPTIONS: ClassVar[dict[str, object]] = {
        "daily_loss_limit_usd": 500.0,
        "max_order_quantity": 10.0,
        "max_order_notional_usd": 1e6,
        "price_band_fraction": 0.03,
        "max_orders_per_window": 5,
        "max_position_quantity": 100.0,
        "prevent_self_match": True,
        "max_quote_age_millis": 1_000,
    }

    def test_the_three_views_never_disagree(self) -> None:
        import random

        random.seed(20260725)
        keys = list(self.LIMIT_OPTIONS)
        switchable = sorted(set(ControlId) - ALWAYS_ON, key=lambda c: c.value)

        for _ in range(200):
            chosen = {
                k: self.LIMIT_OPTIONS[k] for k in random.sample(keys, random.randint(0, len(keys)))
            }
            disabled = frozenset(random.sample(switchable, random.randint(0, 5)))
            eng = engine(RiskLimits(**chosen, disabled_controls=disabled))

            reported = set(eng.running_controls())
            from_status = {s.id for s in eng.control_status() if s.running}
            actually_evaluated = {c.id for c in CONTROL_SEQUENCE if eng._runs(c)}

            assert reported == from_status == actually_evaluated, (
                f"views disagree for limits={sorted(chosen)} disabled={sorted(disabled)}"
            )

    def test_a_disabled_control_is_never_evaluated(self) -> None:
        # The end-to-end version: every switchable control, disabled one at a
        # time, must be absent from what the engine runs.
        for control in sorted(set(ControlId) - ALWAYS_ON, key=lambda c: c.value):
            limits = RiskLimits(**self.LIMIT_OPTIONS).disable(control)
            assert control not in engine(limits).running_controls()


class TestControlStatus:
    def test_reports_every_control_in_evaluation_order(self) -> None:
        status = engine(RiskLimits()).control_status()
        assert len(status) == len(CONTROL_SEQUENCE)
        assert [s.id for s in status] == [c.id for c in CONTROL_SEQUENCE]

    def test_separates_unconfigured_from_disabled(self) -> None:
        limits = RiskLimits(price_band_fraction=0.03).disable(ControlId.PRICE_BAND)
        by_id = {s.id: s for s in engine(limits).control_status()}

        band = by_id[ControlId.PRICE_BAND]
        assert (band.configured, band.disabled, band.running) == (True, True, False)
        assert "DISABLED" in str(band)

        never_set = by_id[ControlId.GROSS_EXPOSURE]
        assert (never_set.configured, never_set.disabled, never_set.running) == (
            False,
            False,
            False,
        )
        assert "no limit configured" in str(never_set)

        live = by_id[ControlId.ORDER_QUANTITY]
        assert live.running is True
        assert "running" in str(live)

    def test_disabled_controls_lists_only_calibrated_ones(self) -> None:
        # Naming an unconfigured control in the disable list is not the thing a
        # supervisor asks about — only a control that was set up and then stood
        # down counts as a decision somebody made.
        limits = RiskLimits(price_band_fraction=0.03).disable(
            ControlId.PRICE_BAND, ControlId.GROSS_EXPOSURE
        )
        assert engine(limits).stood_down_controls() == (ControlId.PRICE_BAND,)


class TestKillSwitch:
    def test_absent_path_never_blocks(self) -> None:
        assert engine(RiskLimits(kill_switch_path=None)).evaluate(order()).accepted

    def test_present_file_blocks(self, tmp_path: Path) -> None:
        kill = tmp_path / "KILL"
        kill.write_text("halt")
        decision = engine(RiskLimits(kill_switch_path=kill)).evaluate(order())
        assert decision.code is RejectCode.KILL_SWITCH_ENGAGED

    def test_configured_but_missing_file_does_not_block(self, tmp_path: Path) -> None:
        eng = engine(RiskLimits(kill_switch_path=tmp_path / "KILL"))
        assert eng.evaluate(order()).accepted

    def test_outranks_an_active_bypass(self, tmp_path: Path, store) -> None:
        kill = tmp_path / "KILL"
        kill.write_text("halt")
        limits = RiskLimits(kill_switch_path=kill, daily_loss_limit_usd=10.0)
        eng = engine(limits, store)
        eng._bypass_loss_limit = True
        eng._bypass_expires_at = float(FIXED_NOW_MILLIS + 60_000)
        assert eng.evaluate(order()).code is RejectCode.KILL_SWITCH_ENGAGED

    def test_is_checked_without_a_refresh(self, tmp_path: Path) -> None:
        # A control that only takes effect after a refresh is not a kill
        # switch: the file appears mid-session and the next order is refused.
        kill = tmp_path / "KILL"
        eng = engine(RiskLimits(kill_switch_path=kill))
        assert eng.evaluate(order()).accepted
        kill.write_text("halt")
        assert eng.evaluate(order()).code is RejectCode.KILL_SWITCH_ENGAGED


class TestTrailingLossLimit:
    """The limit trails the session high water mark rather than sitting at a
    fixed floor, so banked gains are protected."""

    @pytest.mark.asyncio
    async def test_unarmed_when_unset(self, store) -> None:
        eng = engine(RiskLimits(), store)
        await eng.record_realized_pnl(-1_000_000.0, is_live=True)
        assert eng.evaluate(order()).accepted
        assert eng.loss_limit_floor is None
        assert eng.loss_limit_headroom is None

    @pytest.mark.asyncio
    async def test_never_profitable_behaves_as_a_fixed_floor(self, store) -> None:
        eng = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        await eng.record_realized_pnl(-9.99, is_live=True)
        assert eng.loss_limit_breached() is False
        await eng.record_realized_pnl(-0.01, is_live=True)
        assert eng.loss_limit_breached() is True

    @pytest.mark.asyncio
    async def test_banked_gains_are_protected(self, store) -> None:
        # After a +30 run with a 10 limit you may give back 10, not 40.
        eng = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        await eng.record_realized_pnl(30.0, is_live=True)
        assert eng.loss_limit_peak == pytest.approx(30.0)
        assert eng.loss_limit_floor == pytest.approx(20.0)
        await eng.record_realized_pnl(-9.0, is_live=True)
        assert eng.loss_limit_breached() is False
        await eng.record_realized_pnl(-1.5, is_live=True)
        assert eng.loss_limit_breached() is True

    @pytest.mark.asyncio
    async def test_headroom_is_restored_by_a_new_peak(self, store) -> None:
        eng = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        assert eng.loss_limit_headroom == pytest.approx(10.0)
        await eng.record_realized_pnl(2.0, is_live=True)
        assert eng.loss_limit_headroom == pytest.approx(10.0)
        await eng.record_realized_pnl(-2.0, is_live=True)
        assert eng.loss_limit_headroom == pytest.approx(8.0)
        await eng.record_realized_pnl(2.0, is_live=True)
        assert eng.loss_limit_headroom == pytest.approx(10.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("sequence", "breached"),
        [
            ([], False),  # pnl 0, peak 0, floor -10
            ([30.0, -10.0], True),  # pnl 20, peak 30, floor 20 — the boundary
            ([30.0, -9.0], False),  # pnl 21 > floor 20
            ([30.0, -11.0], True),  # pnl 19 <= floor 20
            ([-10.0], True),  # fixed-floor equivalence
            ([-9.99], False),
            ([2.0, -2.0], False),  # pnl 0, peak 2, floor -8
        ],
    )
    async def test_truth_table(self, store, sequence, breached) -> None:
        # Each expectation recomputed from: peak = max(0, running max);
        # floor = peak - limit; breached iff pnl <= floor.
        eng = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        for pnl in sequence:
            await eng.record_realized_pnl(pnl, is_live=True)
        assert eng.loss_limit_breached() is breached

    @pytest.mark.asyncio
    async def test_message_explains_the_trailing_floor(self, store) -> None:
        eng = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        await eng.record_realized_pnl(30.0, is_live=True)
        await eng.record_realized_pnl(-12.0, is_live=True)
        decision = eng.evaluate(order())
        assert decision.code is RejectCode.LOSS_LIMIT_BREACHED
        assert "peak" in decision.message
        assert decision.limit == pytest.approx(20.0)
        assert decision.observed == pytest.approx(18.0)


class TestLiveSimulatedSeparation:
    @pytest.mark.asyncio
    async def test_legs_are_tracked_apart(self, store) -> None:
        eng = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        await eng.record_realized_pnl(5.0, is_live=True)
        await eng.record_realized_pnl(-2.0, is_live=False)
        assert eng.live_pnl == pytest.approx(5.0)
        assert eng.simulated_pnl == pytest.approx(-2.0)
        assert eng.daily_realized_pnl == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_simulated_losses_never_halt_live(self, store) -> None:
        live = engine(RiskLimits(daily_loss_limit_usd=10.0), store, is_live=True)
        await live.record_realized_pnl(-3.0, is_live=True)
        await live.record_realized_pnl(-500.0, is_live=False)
        assert live.evaluate(order()).accepted

    @pytest.mark.asyncio
    async def test_live_losses_never_halt_simulated(self, store) -> None:
        simulated = engine(RiskLimits(daily_loss_limit_usd=10.0), store, is_live=False)
        await simulated.record_realized_pnl(-500.0, is_live=True)
        assert simulated.evaluate(order()).accepted

    @pytest.mark.asyncio
    async def test_peaks_ratchet_per_leg(self, store) -> None:
        live = engine(RiskLimits(daily_loss_limit_usd=10.0), store, is_live=True)
        await live.record_realized_pnl(25.0, is_live=False)
        assert live.loss_limit_peak == 0.0
        await live.record_realized_pnl(5.0, is_live=True)
        assert live.loss_limit_peak == pytest.approx(5.0)


class TestModeParity:
    """Identical configuration and inputs must give identical decisions."""

    @pytest.mark.parametrize(
        "request_kwargs",
        [
            {},
            {"quantity": 10_000.0},
            {"quantity": 0.0},
            {"limit_price": -1.0},
            {"limit_price": 50.0},
            {"symbol": "BLOCKED"},
            {"session_state": SessionState.HALTED},
            {"quote_age_millis": 10_000},
            {"working_orders": 5},
            {"open_positions": 5},
        ],
    )
    def test_same_verdict_in_both_modes(self, request_kwargs) -> None:
        limits = RiskLimits(
            max_order_quantity=1_000.0,
            max_order_notional_usd=5_000.0,
            price_band_fraction=0.05,
            max_quote_age_millis=1_000,
            restricted_instruments=frozenset({"BLOCKED"}),
            tradeable_session_states=frozenset({SessionState.OPEN}),
            max_working_orders=2,
            max_open_positions=2,
        )
        live = engine(limits, is_live=True)
        simulated = engine(limits, is_live=False)
        live_decision = live.evaluate(order(**request_kwargs))
        simulated_decision = simulated.evaluate(order(**request_kwargs))
        assert live_decision.code == simulated_decision.code
        assert live_decision.accepted == simulated_decision.accepted


class TestOrderWellFormedness:
    @pytest.mark.parametrize("quantity", [0.0, -1.0])
    def test_non_positive_quantity_rejected(self, quantity) -> None:
        decision = engine(RiskLimits()).evaluate(order(quantity=quantity))
        assert decision.code is RejectCode.ORDER_QUANTITY_NOT_POSITIVE

    @pytest.mark.parametrize("price", [0.0, -1.0])
    def test_non_positive_limit_price_rejected(self, price) -> None:
        decision = engine(RiskLimits()).evaluate(order(limit_price=price))
        assert decision.code is RejectCode.ORDER_PRICE_NOT_POSITIVE

    def test_market_order_has_no_price_to_reject(self) -> None:
        assert engine(RiskLimits()).evaluate(order(limit_price=None)).accepted

    def test_non_positive_notional_rejected(self) -> None:
        # A negative decision price is the only way to reach a non-positive
        # notional once quantity and limit price are known good.
        decision = engine(RiskLimits()).evaluate(order(limit_price=None, decision_price=-5.0))
        assert decision.code is RejectCode.ORDER_NOTIONAL_NOT_POSITIVE


class TestOrderSizeCaps:
    def test_quantity_cap(self) -> None:
        decision = engine(RiskLimits(max_order_quantity=50.0)).evaluate(order(quantity=51.0))
        assert decision.code is RejectCode.MAX_ORDER_QUANTITY_EXCEEDED
        assert decision.limit == 50.0
        assert decision.observed == 51.0

    def test_quantity_cap_boundary_is_inclusive(self) -> None:
        assert engine(RiskLimits(max_order_quantity=50.0)).evaluate(order(quantity=50.0)).accepted

    def test_notional_cap(self) -> None:
        # 100 at 10.00 is 1,000.
        decision = engine(RiskLimits(max_order_notional_usd=999.0)).evaluate(order())
        assert decision.code is RejectCode.MAX_ORDER_NOTIONAL_EXCEEDED
        assert decision.observed == pytest.approx(1_000.0)

    def test_notional_cap_boundary_is_inclusive(self) -> None:
        assert engine(RiskLimits(max_order_notional_usd=1_000.0)).evaluate(order()).accepted

    def test_quantity_and_notional_are_independent(self) -> None:
        # A small quantity of an expensive instrument passes the quantity cap
        # and still trips the notional cap — which is why both exist.
        limits = RiskLimits(max_order_quantity=10_000.0, max_order_notional_usd=500.0)
        decision = engine(limits).evaluate(order(quantity=1.0, limit_price=5_000.0))
        assert decision.code is RejectCode.MAX_ORDER_NOTIONAL_EXCEEDED

    def test_unpriceable_order_rejects_when_the_notional_cap_is_armed(self) -> None:
        decision = engine(RiskLimits(max_order_notional_usd=500.0)).evaluate(
            order(limit_price=None, decision_price=None, best_ask=None, best_bid=None)
        )
        assert decision.code is RejectCode.MARKET_DATA_UNAVAILABLE


class TestValuationPrice:
    def test_limit_price_wins(self) -> None:
        assert order(limit_price=7.0, decision_price=9.0).valuation_price() == 7.0

    def test_decision_price_next(self) -> None:
        assert order(limit_price=None, decision_price=9.0).valuation_price() == 9.0

    def test_buy_falls_back_to_the_ask(self) -> None:
        req = order(limit_price=None, decision_price=None, best_bid=9.0, best_ask=11.0)
        assert req.valuation_price() == 11.0

    def test_sell_falls_back_to_the_bid(self) -> None:
        req = order(
            side=Side.SELL, limit_price=None, decision_price=None, best_bid=9.0, best_ask=11.0
        )
        assert req.valuation_price() == 9.0


class TestPriceBand:
    def test_price_far_from_the_reference_is_rejected(self) -> None:
        limits = RiskLimits(price_band_fraction=0.02)
        decision = engine(limits).evaluate(order(limit_price=11.0, reference_price=10.0))
        assert decision.code is RejectCode.PRICE_BAND_EXCEEDED
        assert decision.observed == pytest.approx(0.10)

    def test_band_is_two_sided(self) -> None:
        # An absurdly LOW buy is as much a sign of a broken feed as a high one.
        limits = RiskLimits(price_band_fraction=0.02)
        decision = engine(limits).evaluate(order(limit_price=1.0, reference_price=10.0))
        assert decision.code is RejectCode.PRICE_BAND_EXCEEDED

    def test_within_the_band_passes(self) -> None:
        limits = RiskLimits(price_band_fraction=0.02)
        assert engine(limits).evaluate(order(limit_price=10.1, reference_price=10.0)).accepted

    def test_falls_back_to_the_mid(self) -> None:
        limits = RiskLimits(price_band_fraction=0.02)
        decision = engine(limits).evaluate(
            order(limit_price=12.0, reference_price=None, best_bid=9.0, best_ask=11.0)
        )
        assert decision.code is RejectCode.PRICE_BAND_EXCEEDED  # mid 10, 20% away

    def test_no_reference_rejects(self) -> None:
        limits = RiskLimits(price_band_fraction=0.02)
        decision = engine(limits).evaluate(
            order(reference_price=None, best_bid=None, best_ask=None)
        )
        assert decision.code is RejectCode.MARKET_DATA_UNAVAILABLE

    def test_market_order_carries_no_price_to_collar(self) -> None:
        limits = RiskLimits(price_band_fraction=0.02)
        assert engine(limits).evaluate(order(limit_price=None)).accepted


class TestExecutionSlippage:
    def test_adverse_move_on_a_buy_is_rejected(self) -> None:
        limits = RiskLimits(max_execution_slippage=0.02)
        decision = engine(limits).evaluate(order(decision_price=10.0, best_ask=10.10))
        assert decision.code is RejectCode.EXECUTION_SLIPPAGE_EXCEEDED
        assert decision.observed == pytest.approx(0.10)

    def test_favourable_move_is_not_a_risk_event(self) -> None:
        limits = RiskLimits(max_execution_slippage=0.02)
        assert engine(limits).evaluate(order(decision_price=10.0, best_ask=9.50)).accepted

    def test_sells_measure_against_the_bid(self) -> None:
        limits = RiskLimits(max_execution_slippage=0.02)
        decision = engine(limits).evaluate(
            order(side=Side.SELL, decision_price=10.0, best_bid=9.90)
        )
        assert decision.code is RejectCode.EXECUTION_SLIPPAGE_EXCEEDED

    def test_favourable_move_on_a_sell_passes(self) -> None:
        limits = RiskLimits(max_execution_slippage=0.02)
        assert (
            engine(limits)
            .evaluate(order(side=Side.SELL, decision_price=10.0, best_bid=10.50))
            .accepted
        )

    def test_missing_touch_rejects_rather_than_skipping(self) -> None:
        limits = RiskLimits(max_execution_slippage=0.02)
        decision = engine(limits).evaluate(order(best_ask=None))
        assert decision.code is RejectCode.MARKET_DATA_UNAVAILABLE


class TestQuoteAge:
    def test_stale_quotes_rejected(self) -> None:
        decision = engine(RiskLimits(max_quote_age_millis=100)).evaluate(
            order(quote_age_millis=101)
        )
        assert decision.code is RejectCode.MARKET_DATA_STALE

    def test_fresh_quotes_pass(self) -> None:
        assert (
            engine(RiskLimits(max_quote_age_millis=100))
            .evaluate(order(quote_age_millis=100))
            .accepted
        )

    def test_missing_age_rejects(self) -> None:
        decision = engine(RiskLimits(max_quote_age_millis=100)).evaluate(
            order(quote_age_millis=None)
        )
        assert decision.code is RejectCode.MARKET_DATA_UNAVAILABLE

    def test_staleness_is_checked_before_the_price_controls(self) -> None:
        # A stale quote makes a price control lie, so it must be caught first.
        limits = RiskLimits(max_quote_age_millis=100, price_band_fraction=0.001)
        decision = engine(limits).evaluate(
            order(quote_age_millis=5_000, limit_price=99.0, reference_price=10.0)
        )
        assert decision.code is RejectCode.MARKET_DATA_STALE


class TestEligibility:
    def test_instrument_outside_the_universe_rejected(self) -> None:
        limits = RiskLimits(permitted_instruments=frozenset({"OTHER"}))
        assert engine(limits).evaluate(order()).code is RejectCode.INSTRUMENT_NOT_PERMITTED

    def test_instrument_inside_the_universe_passes(self) -> None:
        limits = RiskLimits(permitted_instruments=frozenset({"ACME"}))
        assert engine(limits).evaluate(order()).accepted

    def test_empty_universe_stands_the_desk_down(self) -> None:
        limits = RiskLimits(permitted_instruments=frozenset())
        assert engine(limits).evaluate(order()).code is RejectCode.INSTRUMENT_NOT_PERMITTED

    def test_restricted_instrument_rejected(self) -> None:
        limits = RiskLimits(restricted_instruments=frozenset({"ACME"}))
        assert engine(limits).evaluate(order()).code is RejectCode.INSTRUMENT_RESTRICTED

    def test_restriction_beats_permission(self) -> None:
        limits = RiskLimits(
            permitted_instruments=frozenset({"ACME"}),
            restricted_instruments=frozenset({"ACME"}),
        )
        assert engine(limits).evaluate(order()).code is RejectCode.INSTRUMENT_RESTRICTED


class TestSessionState:
    def test_closed_session_rejected(self) -> None:
        limits = RiskLimits(tradeable_session_states=frozenset({SessionState.OPEN}))
        decision = engine(limits).evaluate(order(session_state=SessionState.CLOSED))
        assert decision.code is RejectCode.MARKET_SESSION_NOT_OPEN

    def test_unknown_session_rejected_when_only_open_permitted(self) -> None:
        limits = RiskLimits(tradeable_session_states=frozenset({SessionState.OPEN}))
        decision = engine(limits).evaluate(order(session_state=SessionState.UNKNOWN))
        assert decision.code is RejectCode.MARKET_SESSION_NOT_OPEN

    def test_auction_permitted_when_configured(self) -> None:
        limits = RiskLimits(
            tradeable_session_states=frozenset({SessionState.OPEN, SessionState.AUCTION})
        )
        assert engine(limits).evaluate(order(session_state=SessionState.AUCTION)).accepted


class TestShortSaleLocate:
    def test_short_without_locate_rejected(self) -> None:
        limits = RiskLimits(require_short_sale_locate=True)
        decision = engine(limits).evaluate(order(side=Side.SELL_SHORT, locate_secured=False))
        assert decision.code is RejectCode.SHORT_SALE_LOCATE_MISSING

    def test_short_with_locate_passes(self) -> None:
        limits = RiskLimits(require_short_sale_locate=True)
        assert engine(limits).evaluate(order(side=Side.SELL_SHORT, locate_secured=True)).accepted

    def test_long_sale_needs_no_locate(self) -> None:
        limits = RiskLimits(require_short_sale_locate=True)
        assert engine(limits).evaluate(order(side=Side.SELL, locate_secured=False)).accepted


class TestDuplicateSuppression:
    def test_identical_resubmission_rejected(self, clock) -> None:
        eng = engine(RiskLimits(duplicate_window_millis=1_000), clock=clock)
        eng.record_order_sent(order())
        assert eng.evaluate(order()).code is RejectCode.DUPLICATE_ORDER

    def test_forgotten_after_the_window(self, clock) -> None:
        eng = engine(RiskLimits(duplicate_window_millis=1_000), clock=clock)
        eng.record_order_sent(order())
        clock.advance_millis(1_000)
        assert eng.evaluate(order()).accepted

    def test_a_different_order_is_not_a_duplicate(self, clock) -> None:
        eng = engine(RiskLimits(duplicate_window_millis=1_000), clock=clock)
        eng.record_order_sent(order())
        assert eng.evaluate(order(quantity=101.0)).accepted

    def test_only_sent_orders_count(self, clock) -> None:
        # Evaluating does not record; an order the engine rejected was never
        # sent and must not make its retry look like a duplicate.
        eng = engine(RiskLimits(duplicate_window_millis=1_000), clock=clock)
        eng.evaluate(order())
        assert eng.evaluate(order()).accepted


class TestOrderRate:
    def test_burst_beyond_the_limit_rejected(self, clock) -> None:
        limits = RiskLimits(max_orders_per_window=3, order_rate_window_millis=1_000)
        eng = engine(limits, clock=clock)
        for _ in range(3):
            eng.record_order_sent(order())
        assert eng.evaluate(order()).code is RejectCode.ORDER_RATE_EXCEEDED

    def test_within_the_limit_passes(self, clock) -> None:
        limits = RiskLimits(max_orders_per_window=3, order_rate_window_millis=1_000)
        eng = engine(limits, clock=clock)
        for _ in range(2):
            eng.record_order_sent(order())
        assert eng.evaluate(order()).accepted

    def test_allowance_returns_as_the_window_slides(self, clock) -> None:
        limits = RiskLimits(max_orders_per_window=3, order_rate_window_millis=1_000)
        eng = engine(limits, clock=clock)
        for _ in range(3):
            eng.record_order_sent(order())
        assert eng.evaluate(order()).code is RejectCode.ORDER_RATE_EXCEEDED
        clock.advance_millis(1_000)
        assert eng.evaluate(order()).accepted


class TestThrottles:
    @pytest.mark.asyncio
    async def test_consecutive_rejections_throttle(self, store) -> None:
        eng = engine(RiskLimits(max_consecutive_rejects=3), store)
        for _ in range(2):
            await eng.record_order_rejected()
        assert eng.evaluate(order()).accepted
        await eng.record_order_rejected()
        assert eng.evaluate(order()).code is RejectCode.CONSECUTIVE_REJECT_LIMIT_EXCEEDED

    @pytest.mark.asyncio
    async def test_an_acknowledgement_clears_the_streak(self, store) -> None:
        eng = engine(RiskLimits(max_consecutive_rejects=2), store)
        await eng.record_order_rejected()
        await eng.record_order_accepted()
        await eng.record_order_rejected()
        assert eng.evaluate(order()).accepted

    @pytest.mark.asyncio
    async def test_reject_streak_survives_a_restart(self, store) -> None:
        # A crash-restart loop must not earn a fresh allowance of rejections.
        first = engine(RiskLimits(max_consecutive_rejects=2), store)
        for _ in range(2):
            await first.record_order_rejected()
        reborn = engine(RiskLimits(max_consecutive_rejects=2), store)
        await reborn.load()
        assert reborn.evaluate(order()).code is RejectCode.CONSECUTIVE_REJECT_LIMIT_EXCEEDED

    @pytest.mark.asyncio
    async def test_repeated_execution_throttle(self, store) -> None:
        eng = engine(RiskLimits(max_executions_without_review=2), store)
        await eng.record_execution()
        assert eng.evaluate(order()).accepted
        await eng.record_execution()
        assert eng.evaluate(order()).code is RejectCode.REPEATED_EXECUTION_THROTTLE

    @pytest.mark.asyncio
    async def test_only_a_rearm_clears_the_execution_throttle(self, store) -> None:
        eng = engine(RiskLimits(max_executions_without_review=1), store)
        await eng.record_execution()
        assert eng.evaluate(order()).code is RejectCode.REPEATED_EXECUTION_THROTTLE
        await eng.rearm()
        assert eng.evaluate(order()).accepted

    @pytest.mark.asyncio
    async def test_execution_throttle_survives_a_restart(self, store) -> None:
        first = engine(RiskLimits(max_executions_without_review=1), store)
        await first.record_execution()
        reborn = engine(RiskLimits(max_executions_without_review=1), store)
        await reborn.load()
        assert reborn.evaluate(order()).code is RejectCode.REPEATED_EXECUTION_THROTTLE

    @pytest.mark.asyncio
    async def test_execution_throttle_survives_the_day_roll(self, store, clock) -> None:
        # "Without human intervention" means exactly that — a new trading day
        # is not a human looking at the strategy.
        eng = engine(RiskLimits(max_executions_without_review=1), store, clock=clock)
        await eng.record_execution()
        clock.advance_millis(2 * 24 * 60 * 60 * 1_000)
        assert eng.evaluate(order()).code is RejectCode.REPEATED_EXECUTION_THROTTLE


class TestPositionAndExposure:
    def test_working_order_limit(self) -> None:
        decision = engine(RiskLimits(max_working_orders=1)).evaluate(order(working_orders=1))
        assert decision.code is RejectCode.MAX_WORKING_ORDERS_EXCEEDED

    def test_open_position_limit(self) -> None:
        decision = engine(RiskLimits(max_open_positions=1)).evaluate(order(open_positions=1))
        assert decision.code is RejectCode.MAX_OPEN_POSITIONS_EXCEEDED

    def test_one_at_a_time_is_just_a_limit_of_one(self) -> None:
        limits = RiskLimits(max_working_orders=1, max_open_positions=1)
        assert engine(limits).evaluate(order()).accepted
        assert not engine(limits).evaluate(order(open_positions=1)).accepted
        assert not engine(limits).evaluate(order(working_orders=1)).accepted

    def test_position_limit_uses_the_projected_position(self) -> None:
        limits = RiskLimits(max_position_quantity=150.0)
        decision = engine(limits).evaluate(order(quantity=100.0, position_quantity=60.0))
        assert decision.code is RejectCode.MAX_POSITION_EXCEEDED
        assert decision.observed == pytest.approx(160.0)

    def test_position_limit_is_two_sided(self) -> None:
        limits = RiskLimits(max_position_quantity=100.0)
        decision = engine(limits).evaluate(
            order(side=Side.SELL, quantity=200.0, position_quantity=0.0)
        )
        assert decision.code is RejectCode.MAX_POSITION_EXCEEDED

    def test_an_order_landing_inside_the_limit_passes(self) -> None:
        limits = RiskLimits(max_position_quantity=100.0)
        decision = engine(limits).evaluate(
            order(side=Side.SELL, quantity=50.0, position_quantity=60.0)
        )
        assert decision.accepted


class TestPositionLimitNeverTrapsAPosition:
    """A position limit must never be the reason a desk cannot trade out of
    the position that is breaching it.

    A limit gets lowered, an unexpected fill lands, a multiplier changes — and
    the book is suddenly outside a limit it has to be able to unwind.
    """

    LIMITS = RiskLimits(max_position_quantity=2_000.0)

    def test_reducing_an_over_limit_long_is_permitted(self) -> None:
        decision = engine(self.LIMITS).evaluate(
            order(side=Side.SELL, quantity=100.0, position_quantity=2_500.0)
        )
        assert decision.accepted

    def test_reducing_an_over_limit_short_is_permitted(self) -> None:
        decision = engine(self.LIMITS).evaluate(
            order(side=Side.BUY, quantity=100.0, position_quantity=-2_500.0)
        )
        assert decision.accepted

    def test_flattening_an_over_limit_position_is_permitted(self) -> None:
        decision = engine(self.LIMITS).evaluate(
            order(side=Side.SELL, quantity=2_500.0, position_quantity=2_500.0)
        )
        assert decision.accepted

    def test_increasing_an_over_limit_position_is_refused(self) -> None:
        decision = engine(self.LIMITS).evaluate(
            order(side=Side.BUY, quantity=100.0, position_quantity=2_500.0)
        )
        assert decision.code is RejectCode.MAX_POSITION_EXCEEDED

    def test_crossing_through_to_an_over_limit_short_is_refused(self) -> None:
        # Long 2,500 selling 4,600 lands at short 2,100 — smaller in absolute
        # terms, still outside the limit. That is re-taking risk, not reducing
        # it.
        decision = engine(self.LIMITS).evaluate(
            order(side=Side.SELL, quantity=4_600.0, position_quantity=2_500.0)
        )
        assert decision.code is RejectCode.MAX_POSITION_EXCEEDED

    def test_crossing_through_to_within_the_limit_is_permitted(self) -> None:
        # Long 2,500 selling 4,000 lands at short 1,500 — inside the limit.
        decision = engine(self.LIMITS).evaluate(
            order(side=Side.SELL, quantity=4_000.0, position_quantity=2_500.0)
        )
        assert decision.accepted

    def test_an_equal_and_opposite_flip_is_refused(self) -> None:
        # Long 2,500 selling 5,000 lands at short 2,500: the same breach,
        # mirrored. Absolute size is unchanged, so it does not reduce.
        decision = engine(self.LIMITS).evaluate(
            order(side=Side.SELL, quantity=5_000.0, position_quantity=2_500.0)
        )
        assert decision.code is RejectCode.MAX_POSITION_EXCEEDED

    def test_self_match_prevented(self) -> None:
        limits = RiskLimits(prevent_self_match=True)
        decision = engine(limits).evaluate(order(opposing_resting_quantity=10.0))
        assert decision.code is RejectCode.SELF_MATCH_PREVENTED

    def test_no_opposing_quantity_passes(self) -> None:
        limits = RiskLimits(prevent_self_match=True)
        assert engine(limits).evaluate(order(opposing_resting_quantity=0.0)).accepted

    def test_gross_exposure_limit(self) -> None:
        limits = RiskLimits(max_gross_exposure_usd=1_500.0)
        decision = engine(limits).evaluate(order(gross_exposure_usd=800.0))
        assert decision.code is RejectCode.GROSS_EXPOSURE_LIMIT_EXCEEDED
        assert decision.observed == pytest.approx(1_800.0)

    def test_gross_exposure_within_limit(self) -> None:
        limits = RiskLimits(max_gross_exposure_usd=2_000.0)
        assert engine(limits).evaluate(order(gross_exposure_usd=800.0)).accepted


class TestDailyNotional:
    @pytest.mark.asyncio
    async def test_cumulative_notional_limit(self, store) -> None:
        eng = engine(RiskLimits(daily_notional_limit_usd=1_500.0), store)
        await eng.record_notional(800.0)
        decision = eng.evaluate(order())  # 1,000 more
        assert decision.code is RejectCode.DAILY_NOTIONAL_LIMIT_EXCEEDED

    @pytest.mark.asyncio
    async def test_unfilled_notional_can_be_handed_back(self, store) -> None:
        eng = engine(RiskLimits(daily_notional_limit_usd=1_500.0), store)
        await eng.record_notional(800.0)
        assert not eng.evaluate(order()).accepted
        await eng.record_notional(-400.0)
        assert eng.evaluate(order()).accepted

    @pytest.mark.asyncio
    async def test_unarmed_when_unset(self, store) -> None:
        eng = engine(RiskLimits(), store)
        await eng.record_notional(1_000_000.0)
        assert eng.evaluate(order()).accepted


class TestPersistenceAcrossRestart:
    @pytest.mark.asyncio
    async def test_counters_and_peak_survive(self, store) -> None:
        first = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        await first.record_realized_pnl(20.0, is_live=True)
        await first.record_realized_pnl(-5.0, is_live=True)
        await first.record_notional(300.0)

        reborn = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        await reborn.load()
        assert reborn.loss_limit_peak == pytest.approx(20.0)
        assert reborn.live_pnl == pytest.approx(15.0)
        assert reborn.daily_notional == pytest.approx(300.0)

    @pytest.mark.asyncio
    async def test_a_restart_cannot_reset_a_breached_limit(self, store) -> None:
        first = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        await first.record_realized_pnl(-15.0, is_live=True)
        reborn = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        await reborn.load()
        assert reborn.evaluate(order()).code is RejectCode.LOSS_LIMIT_BREACHED

    @pytest.mark.asyncio
    async def test_yesterdays_state_is_not_adopted(self, store, clock) -> None:
        first = engine(RiskLimits(daily_loss_limit_usd=10.0), store, clock=clock)
        await first.record_realized_pnl(-15.0, is_live=True)
        clock.advance_millis(24 * 60 * 60 * 1_000)
        reborn = engine(RiskLimits(daily_loss_limit_usd=10.0), store, clock=clock)
        await reborn.load()
        assert reborn.live_pnl == 0.0
        assert reborn.evaluate(order()).accepted

    @pytest.mark.asyncio
    async def test_a_missing_peak_degrades_to_the_fixed_floor(self, store) -> None:
        from pretrade_risk import keys
        from pretrade_risk.encoding import encode_float

        eng = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        await store.set_many(
            {
                keys.TRADING_DAY: eng.trading_day,
                keys.LIVE_REALIZED_PNL: encode_float(8.0),
            }
        )
        await eng.load()
        assert eng.loss_limit_peak == pytest.approx(8.0)

    @pytest.mark.asyncio
    async def test_a_corrupt_counter_degrades_to_zero(self, store) -> None:
        from pretrade_risk import keys

        eng = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        await store.set_many(
            {keys.TRADING_DAY: eng.trading_day, keys.LIVE_REALIZED_PNL: "not-a-number"}
        )
        await eng.load()
        assert eng.live_pnl == 0.0

    @pytest.mark.asyncio
    async def test_a_stored_peak_below_pnl_is_not_trusted(self, store) -> None:
        # A peak that is lower than realized P&L is impossible; adopting it
        # would drop the floor and loosen the limit.
        from pretrade_risk import keys
        from pretrade_risk.encoding import encode_float

        eng = engine(RiskLimits(daily_loss_limit_usd=10.0), store)
        await store.set_many(
            {
                keys.TRADING_DAY: eng.trading_day,
                keys.LIVE_REALIZED_PNL: encode_float(30.0),
                keys.LIVE_PEAK_PNL: encode_float(5.0),
            }
        )
        await eng.load()
        assert eng.loss_limit_peak == pytest.approx(30.0)


class TestTradingDayRoll:
    @pytest.mark.asyncio
    async def test_counters_reset_when_the_day_rolls(self, store, clock) -> None:
        eng = engine(RiskLimits(daily_loss_limit_usd=10.0), store, clock=clock)
        await eng.record_realized_pnl(30.0, is_live=True)
        await eng.record_notional(500.0)
        clock.advance_millis(24 * 60 * 60 * 1_000)
        assert eng.live_pnl == 0.0
        assert eng.loss_limit_peak == 0.0
        assert eng.daily_notional == 0.0

    @pytest.mark.asyncio
    async def test_an_evening_session_roll_keeps_one_trading_date(self, store) -> None:
        # 22:00 UTC roll: the counters must NOT reset at UTC midnight, in the
        # middle of a live evening session.
        from datetime import datetime

        def at(iso: str) -> int:
            return int(datetime.fromisoformat(iso).timestamp() * 1_000)

        clock = ManualClock(at("2026-07-20T23:00:00+00:00"))
        limits = RiskLimits(daily_loss_limit_usd=10.0, session_roll_millis=22 * 60 * 60 * 1_000)
        eng = engine(limits, store, clock=clock)
        await eng.record_realized_pnl(-9.0, is_live=True)
        clock.set_millis(at("2026-07-21T01:00:00+00:00"))  # past UTC midnight
        assert eng.live_pnl == pytest.approx(-9.0)
        clock.set_millis(at("2026-07-21T22:00:00+00:00"))  # the real session roll
        assert eng.live_pnl == 0.0
