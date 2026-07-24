"""Operator control plane — the WRITE side of the gate.

Free functions over a :class:`~pretrade_gate.store.StateStore`, deliberately
NOT methods on ``RiskGate``: a dashboard or CLI holds only a store handle and
writes these keys; the trading loop's gate picks the change up at its next
``refresh_overrides`` / ``refresh_runtime_limits`` call. The two sides never
share an object, so the control plane cannot reach into a running gate's
memory.
"""

from __future__ import annotations

from pretrade_gate import keys
from pretrade_gate.store import StateStore, read_positive_float


async def set_loss_halt_bypass(store: StateStore, enabled: bool) -> None:
    """Persist the operator loss-halt bypass (applies to paper AND live)."""
    await store.set(keys.BYPASS_LOSS_HALT, "1" if enabled else "0")


async def get_loss_halt_bypass(store: StateStore) -> bool:
    return (await store.get(keys.BYPASS_LOSS_HALT)) == "1"


async def reset_daily_loss_halt(store: StateStore) -> None:
    """Operator action: zero today's realized-loss tally AND the session peaks so
    the trailing halt clears. Zeroing PnL alone is not enough — the floor
    is ``peak - limit``, so a banked peak would hold the halt breached even at
    PnL 0 (a +$30 day reset to PnL 0 still floors at +$20). Resetting the peaks
    drops the floor back to ``-limit``. The date and the bankroll-cap notional
    are intentionally left untouched. Caller enforces stopped-only — a running
    loop holds these counters in memory and would re-persist over the reset."""
    await store.set(keys.LIVE_REALIZED_PNL, "0.0")
    await store.set(keys.PAPER_REALIZED_PNL, "0.0")
    await store.set(keys.LIVE_PEAK_PNL, "0.0")
    await store.set(keys.PAPER_PEAK_PNL, "0.0")


async def set_runtime_max_trade_usd(store: StateStore, value: float | None) -> None:
    """Persist the operator runtime per-trade cap.

    ``None`` (or ≤0) clears the override so the gate falls back to the
    configured default. The trading loop re-reads this every tick, so the
    change takes effect without a restart, in both paper and live.
    """
    if value is None or value <= 0:
        await store.set(keys.RUNTIME_MAX_TRADE_USD, "")
    else:
        await store.set(keys.RUNTIME_MAX_TRADE_USD, repr(float(value)))


async def get_runtime_max_trade_usd(store: StateStore) -> float | None:
    """The persisted runtime per-trade cap, or None when unset/invalid."""
    return await read_positive_float(store, keys.RUNTIME_MAX_TRADE_USD)


async def set_runtime_trade_shares(store: StateStore, value: float | None) -> None:
    """Persist the operator runtime trade size in shares.

    When set it takes precedence over the dollar cap: the bot sizes each clip
    to this many shares and the per-trade dollar cap derives from it (N shares
    cost ≤ ~$N since binary prices are < 1). ``None`` (or ≤0) clears it so the
    gate falls back to the dollar cap. Re-read every tick, so it takes effect
    without a restart, in both paper and live.
    """
    if value is None or value <= 0:
        await store.set(keys.RUNTIME_TRADE_SHARES, "")
    else:
        await store.set(keys.RUNTIME_TRADE_SHARES, repr(float(value)))


async def get_runtime_trade_shares(store: StateStore) -> float | None:
    """The persisted runtime trade size in shares, or None when unset/invalid."""
    return await read_positive_float(store, keys.RUNTIME_TRADE_SHARES)
