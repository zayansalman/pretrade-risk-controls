"""pretrade-gate demo: one trading day in eight acts, narrated by the gate itself.

Every verdict printed below is the gate's own ``block_reason`` string — the
exact text a live operator would see in the logs. Runs on the standard library
plus pretrade_gate only, in well under a second:

    python examples/demo.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from pretrade_gate import (
    EntryRequest,
    GateConfig,
    InMemoryStateStore,
    RiskGate,
    set_loss_halt_bypass,
)


def _req(
    notional_usd: float,
    *,
    position_open: bool = False,
    side_price: float | None = 0.55,
    best_ask: float | None = 0.55,
) -> EntryRequest:
    return EntryRequest(
        notional_usd=notional_usd,
        position_open=position_open,
        entry_order_resting=False,
        side_price=side_price,
        best_ask=best_ask,
    )


def show(label: str, verdict: str | None) -> None:
    print(f"  {label}")
    print(f"    -> {'ALLOWED' if verdict is None else f'BLOCKED: {verdict}'}")


def show_headroom(gate: RiskGate, note: str) -> None:
    print(
        f"    {note}: pnl {gate.halt_pnl:+.2f}  peak {gate.halt_peak:+.2f}  "
        f"floor {gate.loss_halt_floor:+.2f}  headroom {gate.loss_halt_headroom:.2f}"
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        kill_path = Path(tmp) / "KILL"
        cfg = GateConfig(
            max_trade_usd=25.0,
            daily_loss_halt_usd=50.0,
            bankroll_cap_usd=200.0,
            max_entry_slippage=0.02,
            kill_switch_path=kill_path,
        )
        store = InMemoryStateStore()
        gate = RiskGate(cfg, store, is_live=True)
        await gate.load()

        print("ACT 1 — a normal entry passes")
        show("$20 entry, book still at the signal price", gate.block_reason(_req(20.0)))

        print("\nACT 2 — fat finger")
        show("$2,500 entry (cap is $25)", gate.block_reason(_req(2500.0)))

        print("\nACT 3 — the book moved away")
        show(
            "signal computed at 0.55, but the ask is 0.61 at fill time",
            gate.block_reason(_req(20.0, side_price=0.55, best_ask=0.61)),
        )

        print("\nACT 4 — one position at a time")
        show(
            "$20 entry while a position is already open",
            gate.block_reason(_req(20.0, position_open=True)),
        )

        print("\nACT 5 — trailing drawdown halt (limit $50)")
        await gate.record_realized_pnl(80.0, is_live=True)
        show_headroom(gate, "win +$80.00 banks a peak")
        for loss in (-20.0, -20.0, -15.0):
            await gate.record_realized_pnl(loss, is_live=True)
            show_headroom(gate, f"loss {loss:+.2f}")
        show("$20 entry after the losing streak", gate.block_reason(_req(20.0)))

        print("\nACT 6 — operator bypass (the dashboard writes, the gate reads)")
        await set_loss_halt_bypass(store, True)
        await gate.refresh_overrides()
        show("$20 entry with the halt bypassed", gate.block_reason(_req(20.0)))
        show("$2,500 entry with the halt bypassed", gate.block_reason(_req(2500.0)))

        print("\nACT 7 — kill switch beats everything, bypass included")
        kill_path.write_text("stop")
        show("$20 entry with the KILL file present", gate.block_reason(_req(20.0)))
        kill_path.unlink()
        await set_loss_halt_bypass(store, False)  # operator re-arms the halt

        print("\nACT 8 — restart resilience (new process, same store)")
        reborn = RiskGate(cfg, store, is_live=True)
        await reborn.load()
        await reborn.refresh_overrides()
        show_headroom(reborn, "rebuilt from the store")
        show("$20 entry after the restart", reborn.block_reason(_req(20.0)))
        print("    the drawdown — and the halt — survived the restart")


if __name__ == "__main__":
    asyncio.run(main())
