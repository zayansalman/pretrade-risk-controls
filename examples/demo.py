"""One trading session, narrated by the risk engine itself.

Every verdict printed below is the engine's own decision — the same reject
code and message a supervisor would find in the order log. Runs on the
standard library plus pretrade_risk, in well under a second:

    python examples/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from pretrade_risk import (
    InMemoryStateStore,
    ManualClock,
    OrderRequest,
    PreTradeRiskEngine,
    RiskLimits,
    SessionState,
    Side,
    bypass_loss_limit,
    clear_loss_limit_bypass,
)

SECOND = 1_000
MINUTE = 60 * SECOND

RESTRICTED = "OMEGA"  # in a research blackout


def build_order(
    quantity: float = 100.0,
    *,
    symbol: str = "ACME",
    side: Side = Side.BUY,
    limit_price: float | None = 10.00,
    decision_price: float | None = 10.00,
    best_bid: float | None = 9.99,
    best_ask: float | None = 10.01,
    quote_age_millis: int | None = 20,
    session_state: SessionState = SessionState.OPEN,
    working_orders: int = 0,
    position_quantity: float = 0.0,
) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=side,
        quantity=quantity,
        limit_price=limit_price,
        decision_price=decision_price,
        best_bid=best_bid,
        best_ask=best_ask,
        quote_age_millis=quote_age_millis,
        session_state=session_state,
        working_orders=working_orders,
        position_quantity=position_quantity,
        reference_price=10.00,
    )


def show(engine: PreTradeRiskEngine, label: str, order: OrderRequest) -> None:
    decision = engine.evaluate(order)
    if decision.accepted:
        print(f"  {label}\n      ACCEPTED")
        return
    print(f"  {label}\n      REJECTED [{decision.code.value}] {decision.message}")


def show_loss_limit(engine: PreTradeRiskEngine, note: str) -> None:
    print(
        f"      {note}: realized {engine.loss_limit_pnl:+.2f}  "
        f"peak {engine.loss_limit_peak:+.2f}  "
        f"floor {engine.loss_limit_floor:+.2f}  "
        f"headroom {engine.loss_limit_headroom:.2f}"
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        kill_switch = Path(tmp) / "KILL"
        limits = RiskLimits(
            kill_switch_path=kill_switch,
            daily_loss_limit_usd=500.0,
            daily_notional_limit_usd=50_000.0,
            max_order_quantity=1_000.0,
            max_order_notional_usd=10_000.0,
            price_band_fraction=0.03,
            max_execution_slippage=0.05,
            max_quote_age_millis=1_000,
            duplicate_window_millis=5 * SECOND,
            max_orders_per_window=3,
            order_rate_window_millis=SECOND,
            max_consecutive_rejects=3,
            max_executions_without_review=25,
            max_working_orders=5,
            max_position_quantity=2_000.0,
            prevent_self_match=True,
            require_short_sale_locate=True,
            restricted_instruments=frozenset({RESTRICTED}),
            tradeable_session_states=frozenset({SessionState.OPEN}),
        )
        store = InMemoryStateStore()
        clock = ManualClock(1_700_000_000_000)
        engine = PreTradeRiskEngine(limits, store, is_live=True, clock=clock)
        await engine.load()

        running = engine.running_controls()
        print(f"RUNNING CONTROLS ({len(running)} of {len(engine.control_status())})")
        for status in engine.control_status():
            if status.running:
                print(f"  - {status.name}")
        print("  not configured, so not running:")
        for status in engine.control_status():
            if not status.configured:
                print(f"      {status.name}")

        print("\n1. A NORMAL ORDER")
        show(engine, "buy 100 ACME at 10.00, book steady", build_order())

        print("\n2. FAT-FINGER SIZING")
        show(engine, "buy 50,000 ACME — quantity cap is 1,000", build_order(50_000.0))
        show(
            engine,
            "buy 900 ACME at 100.00 — within the quantity cap, over the notional cap",
            build_order(900.0, limit_price=100.00, decision_price=100.00),
        )

        print("\n3. A PRICE THAT DOES NOT BELONG TO THIS MARKET")
        show(
            engine,
            "buy 100 ACME at 14.00 against a 10.00 reference",
            build_order(limit_price=14.00),
        )

        print("\n4. THE MARKET MOVED WHILE THE DECISION WAS IN FLIGHT")
        show(
            engine,
            "signal computed at 10.00, ask now 10.30",
            build_order(best_ask=10.30),
        )

        print("\n5. THE QUOTES BEHIND THE PRICE CHECKS ARE STALE")
        show(engine, "quotes 4 seconds old, limit is 1 second", build_order(quote_age_millis=4_000))

        print("\n6. COMPLIANCE")
        show(engine, f"buy 100 {RESTRICTED} — restricted list", build_order(symbol=RESTRICTED))
        show(
            engine,
            "short 100 ACME with no secured locate",
            build_order(side=Side.SELL_SHORT),
        )
        show(engine, "instrument is halted", build_order(session_state=SessionState.HALTED))

        print("\n7. MESSAGE CONDUCT")
        engine.record_order_sent(build_order())
        show(engine, "the identical order again, 0 ms later", build_order())
        clock.advance_millis(5 * SECOND)
        show(engine, "the same order again after the window lapses", build_order())
        for _ in range(3):
            engine.record_order_sent(build_order(quantity=101.0))
        show(engine, "a fourth order in one second — the rate limit is 3", build_order(102.0))

        print("\n8. THE STRATEGY IS BEING REFUSED BY THE VENUE")
        clock.advance_millis(SECOND)
        for _ in range(3):
            await engine.record_order_rejected()
        show(engine, "after three consecutive venue rejections", build_order())
        await engine.rearm()
        show(engine, "after an operator re-arms the strategy", build_order())

        print("\n9. POSITION AND EXPOSURE")
        show(
            engine,
            "buy 100 ACME while long 1,950 — position limit is 2,000",
            build_order(position_quantity=1_950.0),
        )
        show(
            engine,
            "sell 100 ACME while long 2,500 — reducing, so permitted",
            build_order(side=Side.SELL, position_quantity=2_500.0),
        )

        print("\n10. THE TRAILING DAILY LOSS LIMIT (500)")
        await engine.record_realized_pnl(800.0, is_live=True)
        show_loss_limit(engine, "a +800.00 session banks a high water mark")
        for loss in (-200.0, -200.0, -150.0):
            await engine.record_realized_pnl(loss, is_live=True)
            show_loss_limit(engine, f"realized {loss:+.2f}")
        show(engine, "the next order after the drawdown", build_order())
        print("      the desk is up 250.00 on the day and still halted —")
        print("      the limit protects banked gains, it is not a floor at -500.00")

        print("\n11. A BOUNDED OPERATOR BYPASS")
        expires_at = await bypass_loss_limit(
            store,
            duration_millis=15 * MINUTE,
            actor="risk.manager",
            reason="unwinding an illiquid position after the halt",
            clock=clock,
        )
        await engine.refresh_overrides()
        show(engine, "with the loss limit suspended for 15 minutes", build_order())
        show(engine, "the fat-finger cap is still armed underneath it", build_order(50_000.0))
        print(f"      the suspension lapses at {expires_at} with no action from anyone")
        clock.set_millis(expires_at)
        show(engine, "fifteen minutes later, nobody having touched anything", build_order())
        await clear_loss_limit_bypass(store, actor="risk.manager", clock=clock)
        await engine.refresh_overrides()

        print("\n12. THE KILL SWITCH OUTRANKS EVERYTHING")
        expires_at = await bypass_loss_limit(
            store, duration_millis=MINUTE, actor="risk.manager", reason="demo", clock=clock
        )
        await engine.refresh_overrides()
        kill_switch.write_text("stop")
        show(engine, "with the kill file present AND the loss limit bypassed", build_order())
        kill_switch.unlink()
        await clear_loss_limit_bypass(store, actor="risk.manager", clock=clock)
        await engine.refresh_overrides()

        print("\n13. A RESTART DOES NOT GRANT A FRESH LIMIT")
        reborn = PreTradeRiskEngine(limits, store, is_live=True, clock=clock)
        await reborn.load()
        await reborn.refresh_overrides()
        show_loss_limit(reborn, "rebuilt from the store, new process")
        show(reborn, "the first order of the new process", build_order())
        print("      the drawdown, the high water mark and the halt all survived")


if __name__ == "__main__":
    asyncio.run(main())
