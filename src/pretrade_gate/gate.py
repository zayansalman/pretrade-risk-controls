"""Venue-independent pre-trade risk gate.

Both paper and live entries go through the SAME ``RiskGate`` so paper is a
faithful preview of live: what paper blocks, live would have blocked; what
paper opens, live would have opened (modulo the actual order placement,
which is the only live-only concern).

The gate owns the persisted daily counters (date, realized PnL, cumulative
BUY notional) in an injected :class:`~pretrade_gate.store.StateStore`. They
are venue-independent — both paper closes and live closes feed them — so
every gate that consults a counter (the daily realized-loss halt, the
optional bankroll cap) advances identically in both modes.

State lives under the flat ``gate.*`` keys documented in
:mod:`pretrade_gate.keys`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from pretrade_gate import keys
from pretrade_gate.store import StateStore, read_positive_float

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateConfig:
    """Static gate configuration. Identical instance shared by paper and live."""

    max_trade_usd: float
    daily_loss_halt_usd: float
    bankroll_cap_usd: Optional[float]  # None / ≤0 → cap disabled
    max_entry_slippage: float
    kill_switch_path: Path


@dataclass(frozen=True)
class EntryRequest:
    """What the caller is asking permission to do.

    ``best_ask`` and ``side_price`` together drive the slippage guard. When
    either is ``None`` (e.g. the book is unavailable), the guard does not
    contribute a block reason — other gates still run.
    """

    notional_usd: float
    position_open: bool  # any open ledger / live position
    entry_order_resting: bool  # live: unfilled entry order resting in the book
    side_price: Optional[float]  # the price the signal was computed against
    best_ask: Optional[float]  # the live ask AT FILL TIME


# ---------------------------------------------------------------------------
# RiskGate
# ---------------------------------------------------------------------------


class RiskGate:
    """Pre-trade gate + persisted daily counters, shared by paper and live.

    All gate logic lives here so paper and live cannot diverge by accident.
    Counters are persisted to the injected store (``gate.*`` keys) and rebuilt
    at boot so Stop/Start or a process restart never resets the daily-loss
    halt or silently grants a fresh bankroll when the cap is enabled.
    """

    def __init__(self, cfg: GateConfig, store: StateStore, *, is_live: bool = False) -> None:
        self.cfg = cfg
        self._store = store
        # Which leg drives THIS gate's loss halt: the live gate halts on
        # real-money PnL, the paper gate on study PnL. Set True by the live
        # executor; the paper loop leaves it False.
        self.is_live = is_live
        self._date = self._today()
        # Split PnL counters. Live and paper realized PnL are tracked
        # separately; the dashboard shows each leg so the operator can tell
        # real-money PnL apart from paper study runs.
        self._live_pnl: float = 0.0
        self._paper_pnl: float = 0.0
        # Session high-water marks per leg — the trailing loss halt is
        # measured as drawdown from these. Ratchet up only; reset with the PnL
        # counters at the UTC day boundary.
        self._live_peak: float = 0.0
        self._paper_peak: float = 0.0
        self._daily_buy_notional: float = 0.0
        self._kill_handled = False
        self._loaded = False
        # Cached loss-halt bypass — refreshed by ``refresh_overrides`` on
        # each tick so the dashboard toggle takes effect immediately without a
        # restart. Applies in BOTH modes.
        self._bypass_loss_halt = False
        # Operator runtime per-trade cap override. None → use the configured
        # default (cfg.max_trade_usd). Refreshed every tick by
        # ``refresh_runtime_limits`` so the dashboard control applies without a
        # restart, in BOTH paper and live (this is a tuning knob, not a
        # safety-loosening override like the loss-halt bypass).
        self._runtime_max_trade_usd: float | None = None
        # Operator runtime trade size in SHARES. None → fall back to the
        # dollar cap above. Refreshed every tick by ``refresh_runtime_limits``.
        self._runtime_trade_shares: float | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def _roll_daily_window(self) -> None:
        today = self._today()
        if today != self._date:
            self._date = today
            self._live_pnl = 0.0
            self._paper_pnl = 0.0
            self._live_peak = 0.0
            self._paper_peak = 0.0
            self._daily_buy_notional = 0.0

    async def load(self) -> None:
        """Rebuild counters from the store.

        Only same-day state is adopted (the persisted ``gate.date`` must be
        today's UTC date); anything else starts the day fresh. Peaks are
        re-derived defensively — see the clamp below.
        """
        date = await self._store.get(keys.DATE)
        live_pnl_raw = await self._store.get(keys.LIVE_REALIZED_PNL)
        paper_pnl_raw = await self._store.get(keys.PAPER_REALIZED_PNL)
        live_peak_raw = await self._store.get(keys.LIVE_PEAK_PNL)
        paper_peak_raw = await self._store.get(keys.PAPER_PEAK_PNL)
        notional_raw = await self._store.get(keys.DAILY_BUY_NOTIONAL)
        if date == self._today():
            try:
                self._live_pnl = float(live_pnl_raw or 0)
                self._paper_pnl = float(paper_pnl_raw or 0)
                self._daily_buy_notional = float(notional_raw or 0)
            except ValueError:
                self._live_pnl = 0.0
                self._paper_pnl = 0.0
                self._daily_buy_notional = 0.0
            # Defensive invariant: a stored peak is never trusted below the
            # current leg PnL or 0. An absent or corrupt peak degrades to
            # max(0, leg_pnl) — i.e. fixed-floor behaviour — never to a
            # looser halt.
            try:
                self._live_peak = max(0.0, self._live_pnl, float(live_peak_raw or 0))
                self._paper_peak = max(0.0, self._paper_pnl, float(paper_peak_raw or 0))
            except ValueError:
                self._live_peak = max(0.0, self._live_pnl)
                self._paper_peak = max(0.0, self._paper_pnl)
        self._date = self._today()
        self._loaded = True
        await self.persist()

    async def persist(self) -> None:
        # Deliberately non-atomic: six sequential single-key writes, date
        # first, in a fixed order — preserved exactly from the production
        # system so crash behaviour is unchanged (see README, Limitations).
        await self._store.set(keys.DATE, self._date)
        await self._store.set(keys.LIVE_REALIZED_PNL, repr(self._live_pnl))
        await self._store.set(keys.PAPER_REALIZED_PNL, repr(self._paper_pnl))
        await self._store.set(keys.LIVE_PEAK_PNL, repr(self._live_peak))
        await self._store.set(keys.PAPER_PEAK_PNL, repr(self._paper_peak))
        await self._store.set(keys.DAILY_BUY_NOTIONAL, repr(self._daily_buy_notional))

    async def refresh_overrides(self) -> None:
        """Re-read the operator loss-halt bypass from the store (BOTH modes).

        The bypass is an operator runtime knob honoured in BOTH modes —
        re-read every tick so the dashboard toggle takes effect without a
        restart.
        """
        self._bypass_loss_halt = (await self._store.get(keys.BYPASS_LOSS_HALT)) == "1"

    @property
    def bypass_loss_halt(self) -> bool:
        """Operator loss-halt bypass — applies to paper AND live."""
        return self._bypass_loss_halt

    @property
    def halt_pnl(self) -> float:
        """The realized-loss leg that drives THIS gate's halt: live money
        in live mode, study PnL in paper mode."""
        self._roll_daily_window()
        return self._live_pnl if self.is_live else self._paper_pnl

    @property
    def halt_peak(self) -> float:
        """Session high-water mark of THIS gate's leg. The trailing loss
        halt is measured as drawdown from this peak."""
        self._roll_daily_window()
        return self._live_peak if self.is_live else self._paper_peak

    @property
    def loss_halt_floor(self) -> float:
        """The PnL level at/below which the halt fires: peak - limit.
        With peak 0 (never profitable) this is a plain fixed -limit floor."""
        return self.halt_peak - self.cfg.daily_loss_halt_usd

    @property
    def loss_halt_headroom(self) -> float:
        """USD this leg can still lose before the trailing halt fires:
        current PnL minus the floor. Equals the full limit at the peak; shrinks
        as PnL falls below the peak; restored when a new peak is set."""
        return self.halt_pnl - self.loss_halt_floor

    def loss_halt_breached(self) -> bool:
        """True when this mode's own realized PnL has drawn down to/through the
        trailing floor (peak - limit) and the bypass is off. The loop uses
        this to STOP the bot."""
        if self._bypass_loss_halt:
            return False
        return self.halt_pnl <= self.loss_halt_floor

    async def refresh_runtime_limits(self) -> None:
        """Re-read operator runtime risk knobs from the store (BOTH paper and live).

        Distinct from ``refresh_overrides``: that reads the loss-halt bypass
        (a safety-loosening operator toggle). The per-trade cap is a tuning
        knob the operator expects to apply everywhere, so this runs regardless
        of mode. A blank / unset / non-numeric / ≤0 value clears the override
        and the gate falls back to the configured default.
        """
        self._runtime_max_trade_usd = await read_positive_float(
            self._store, keys.RUNTIME_MAX_TRADE_USD
        )
        self._runtime_trade_shares = await read_positive_float(
            self._store, keys.RUNTIME_TRADE_SHARES
        )

    @property
    def runtime_max_trade_usd(self) -> float | None:
        """The operator-set per-trade cap override, or None when unset (raw)."""
        return self._runtime_max_trade_usd

    @property
    def runtime_trade_shares(self) -> float | None:
        """The operator-set trade size in shares, or None when unset."""
        return self._runtime_trade_shares

    @property
    def effective_max_trade_usd(self) -> float:
        """The per-trade dollar cap in force.

        Precedence: a share-denominated trade size wins — N shares cost at
        most ~$N (binary prices are < 1), so the dollar cap is N and never blocks
        the bot's own N-share clip. Else the dollar override, else the configured
        default.
        """
        if self._runtime_trade_shares is not None:
            return self._runtime_trade_shares
        if self._runtime_max_trade_usd is not None:
            return self._runtime_max_trade_usd
        return self.cfg.max_trade_usd

    # ------------------------------------------------------------------
    # Counters — fed by BOTH paper closes and live closes
    # ------------------------------------------------------------------

    async def record_realized_pnl(self, pnl_usd: float, *, is_live: bool) -> None:
        """Add realized PnL to the right bucket.

        ``is_live`` is the only call-site distinction — paper closes pass
        False so their PnL stays separated. The halt decision uses the gate's
        OWN leg (live or paper) per its ``is_live``, so paper losses never
        drive the live halt.
        """
        self._roll_daily_window()
        if is_live:
            self._live_pnl += pnl_usd
            self._live_peak = max(self._live_peak, self._live_pnl)
        else:
            self._paper_pnl += pnl_usd
            self._paper_peak = max(self._paper_peak, self._paper_pnl)
        await self.persist()

    async def record_buy_notional(self, notional_usd: float) -> None:
        self._roll_daily_window()
        self._daily_buy_notional += notional_usd
        await self.persist()

    @property
    def live_pnl(self) -> float:
        self._roll_daily_window()
        return self._live_pnl

    @property
    def paper_pnl(self) -> float:
        self._roll_daily_window()
        return self._paper_pnl

    @property
    def daily_realized_pnl(self) -> float:
        """Combined live+paper PnL. Reporting surface only — the halt
        decision uses the per-mode leg (``halt_pnl``)."""
        self._roll_daily_window()
        return self._live_pnl + self._paper_pnl

    @property
    def daily_buy_notional(self) -> float:
        self._roll_daily_window()
        return self._daily_buy_notional

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def kill_switch_active(self) -> bool:
        return self.cfg.kill_switch_path.exists()

    def mark_kill_handled(self) -> None:
        self._kill_handled = True

    def kill_already_handled(self) -> bool:
        return self._kill_handled

    def rearm_kill(self) -> None:
        self._kill_handled = False

    # ------------------------------------------------------------------
    # The gate
    # ------------------------------------------------------------------

    def block_reason(self, req: EntryRequest) -> str | None:
        """Return why this entry is blocked, or ``None`` if all gates pass.

        Order matters only for the error message — the FIRST tripped gate is
        reported. The set of gates is canonical: every entry, paper or live,
        gets the same answer for the same inputs.
        """
        if self.kill_switch_active():
            return f"KILL switch active at {self.cfg.kill_switch_path}"
        self._roll_daily_window()
        if self.loss_halt_breached():
            leg = "live" if self.is_live else "paper"
            return (
                f"daily loss halt: {leg} realized {self.halt_pnl:+.2f} USD at/below "
                f"trailing floor {self.loss_halt_floor:+.2f} "
                f"(peak {self.halt_peak:+.2f} − {self.cfg.daily_loss_halt_usd:.2f} limit)"
            )
        if req.position_open or req.entry_order_resting:
            return "an open position/order already exists (max 1)"
        if req.notional_usd <= 0:
            return "notional must be positive"
        cap = self.effective_max_trade_usd
        if req.notional_usd > cap:
            return f"per-trade cap: {req.notional_usd:.2f} USD exceeds {cap:.2f} USD"
        if (
            self.cfg.bankroll_cap_usd is not None
            and self.cfg.bankroll_cap_usd > 0
            and self._daily_buy_notional + req.notional_usd > self.cfg.bankroll_cap_usd
        ):
            return (
                f"daily bankroll cap: {self._daily_buy_notional:.2f} + "
                f"{req.notional_usd:.2f} USD exceeds "
                f"{self.cfg.bankroll_cap_usd:.2f} USD"
            )
        if (
            req.best_ask is not None
            and req.side_price is not None
            and req.side_price > 0
            and req.best_ask - req.side_price > self.cfg.max_entry_slippage
        ):
            return (
                f"entry slippage guard: book ask {req.best_ask:.3f} is "
                f"{req.best_ask - req.side_price:+.3f} above the signal price "
                f"{req.side_price:.3f} (max {self.cfg.max_entry_slippage:.3f}); "
                "the edge that justified this trade no longer exists"
            )
        return None
