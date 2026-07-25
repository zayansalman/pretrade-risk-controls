"""The pre-trade risk engine: one ordered pass of controls over one order.

Every order — live or simulated — walks the same sequence in
:data:`CONTROL_SEQUENCE`, and the first control to object wins. Simulated flow
running the identical path is the point: a simulation permitted things live
would refuse is not a preview of anything, and divergence between the two
paths is the defect a single shared engine exists to make impossible.

Read side and write side
-------------------------

:meth:`PreTradeRiskEngine.evaluate` is synchronous and reads only cached
state. No store round trip sits on the order path, so the decision cost is
arithmetic and nothing else. The engine synchronises with the store at named
points the caller chooses — :meth:`load` at startup, :meth:`persist` after a
counter moves, :meth:`refresh_overrides` on a cadence the caller sets — which
leaves the caller deciding how stale an override may be.

The one deliberate exception is the kill switch, which stats its file on every
evaluation. A control that only takes effect after a refresh has run is not a
kill switch.

The operator control plane in :mod:`pretrade_gate.controls` writes the same
store from a separate process and never touches a live engine object. The
store is the only channel between them.

Counters the caller owns
-------------------------

The engine does not observe the market or the order book. It learns what
happened because the caller tells it: :meth:`record_order_sent` once an order
is away, :meth:`record_execution` on a fill, :meth:`record_order_rejected` on
a venue rejection, :meth:`record_realized_pnl` when a position closes. Nothing
is inferred, because a risk counter that quietly disagreed with the order
management system's book of record would be worse than no counter at all.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pretrade_gate import keys
from pretrade_gate.clock import Clock, SystemClock, trading_day
from pretrade_gate.decision import ACCEPTED, RejectCode, RiskDecision
from pretrade_gate.encoding import (
    decode_bool,
    decode_float,
    decode_positive_float,
    encode_float,
)
from pretrade_gate.limits import RiskLimits
from pretrade_gate.order import OrderRequest, Side
from pretrade_gate.store import StateStore
from pretrade_gate.windows import DuplicateWindow, RateWindow


@dataclass(frozen=True)
class Control:
    """One pre-trade control: a name, an arming test, and the check itself.

    Arming is kept separate from evaluation so that "which controls are live
    right now" can be answered without running any of them — some controls
    hold state, and asking a duplicate-suppression window a hypothetical
    question would disturb it.

    ``check`` returns ``None`` when the control is satisfied.
    """

    name: str
    is_armed: Callable[[PreTradeRiskEngine], bool]
    check: Callable[[PreTradeRiskEngine, OrderRequest, int], RiskDecision | None]


class PreTradeRiskEngine:
    """Pre-trade controls plus the persisted counters they read.

    One engine governs one book against one store namespace. ``is_live``
    selects which realized-P&L leg the loss limit measures: the live engine
    halts on the firm's own capital, the simulated engine on simulated
    results, and neither can halt the other.
    """

    def __init__(
        self,
        limits: RiskLimits,
        store: StateStore,
        *,
        is_live: bool = False,
        clock: Clock | None = None,
    ) -> None:
        self.limits = limits
        self._store = store
        self.is_live = is_live
        self._clock: Clock = clock if clock is not None else SystemClock()

        self._trading_day = self._today()

        # Realized P&L, tracked per leg so an operator console can show real
        # money apart from simulated results, and so neither halts the other.
        self._live_pnl = 0.0
        self._simulated_pnl = 0.0
        # Session high water marks. The loss limit is a drawdown from these.
        # They ratchet up only, and reset when the trading day rolls.
        self._live_peak = 0.0
        self._simulated_peak = 0.0
        self._daily_notional = 0.0

        # Throttle counters. Persisted, so a crash-restart loop cannot earn
        # itself a fresh allowance.
        self._consecutive_rejects = 0
        self._executions_since_rearm = 0

        # Session-scoped message windows — deliberately not persisted.
        self._rate_window = RateWindow(limits.order_rate_window_millis)
        self._duplicate_window = (
            DuplicateWindow(limits.duplicate_window_millis)
            if limits.duplicate_window_millis is not None
            else None
        )

        # Cached operator overrides, refreshed by ``refresh_overrides``.
        self._bypass_loss_limit = False
        self._bypass_expires_at: float | None = None
        self._runtime_max_order_notional: float | None = None
        self._runtime_max_order_quantity: float | None = None

    # ------------------------------------------------------------------
    # Trading day
    # ------------------------------------------------------------------

    def _today(self, now_millis: int | None = None) -> str:
        now = self._clock.now_millis() if now_millis is None else now_millis
        return trading_day(now, self.limits.session_roll_millis)

    @property
    def trading_day(self) -> str:
        """The trading date the current counters belong to."""
        self._roll_trading_day()
        return self._trading_day

    def _roll_trading_day(self, now_millis: int | None = None) -> None:
        """Reset the day's counters when the trading day has moved on.

        In-memory only — this runs on the synchronous decision path, which
        does no I/O. The store catches up at the next :meth:`persist`, and
        :meth:`load` adopts persisted counters only when they carry the
        current trading date, so a stale snapshot can never be resurrected.

        ``now_millis`` lets a caller that has already read the clock pass the
        same instant in, so one evaluation reads the clock exactly once and
        every control in it sees a single consistent "now".
        """
        today = self._today(now_millis)
        if today == self._trading_day:
            return
        self._trading_day = today
        self._live_pnl = 0.0
        self._simulated_pnl = 0.0
        self._live_peak = 0.0
        self._simulated_peak = 0.0
        self._daily_notional = 0.0
        self._consecutive_rejects = 0
        # Executions since re-arm deliberately survive the roll: only a human
        # clears that throttle.

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def load(self) -> None:
        """Rebuild counters from the store.

        Only same-trading-day state is adopted; anything else starts the day
        fresh. Every field degrades toward MORE protection when it is absent
        or damaged — a missing high water mark derives as ``max(0, pnl)``,
        which is fixed-floor behaviour, never a looser one.
        """
        raw = await self._store.get_many(keys.COUNTER_KEYS)
        today = self._today()

        # The re-arm counter is not day-scoped, so it is restored regardless
        # of which trading day the rest of the snapshot belongs to.
        self._executions_since_rearm = _as_count(raw.get(keys.EXECUTIONS_SINCE_REARM))

        if raw.get(keys.TRADING_DAY) == today:
            self._live_pnl = decode_float(raw.get(keys.LIVE_REALIZED_PNL)) or 0.0
            self._simulated_pnl = decode_float(raw.get(keys.SIMULATED_REALIZED_PNL)) or 0.0
            self._daily_notional = decode_float(raw.get(keys.DAILY_NOTIONAL)) or 0.0
            self._consecutive_rejects = _as_count(raw.get(keys.CONSECUTIVE_REJECTS))
            stored_live_peak = decode_float(raw.get(keys.LIVE_PEAK_PNL)) or 0.0
            stored_simulated_peak = decode_float(raw.get(keys.SIMULATED_PEAK_PNL)) or 0.0
            self._live_peak = max(0.0, self._live_pnl, stored_live_peak)
            self._simulated_peak = max(0.0, self._simulated_pnl, stored_simulated_peak)

        self._trading_day = today
        await self.persist()

    async def persist(self) -> None:
        """Write the counter snapshot as one batch.

        One :meth:`~pretrade_gate.store.StateStore.set_many` rather than a
        sequence of single writes, so a store that can commit atomically does
        and a crash cannot leave the snapshot half-updated.
        """
        await self._store.set_many(
            {
                keys.TRADING_DAY: self._trading_day,
                keys.LIVE_REALIZED_PNL: encode_float(self._live_pnl),
                keys.SIMULATED_REALIZED_PNL: encode_float(self._simulated_pnl),
                keys.LIVE_PEAK_PNL: encode_float(self._live_peak),
                keys.SIMULATED_PEAK_PNL: encode_float(self._simulated_peak),
                keys.DAILY_NOTIONAL: encode_float(self._daily_notional),
                keys.CONSECUTIVE_REJECTS: encode_float(float(self._consecutive_rejects)),
                keys.EXECUTIONS_SINCE_REARM: encode_float(float(self._executions_since_rearm)),
            }
        )

    async def refresh_overrides(self) -> None:
        """Re-read every operator override from the store, in one batch read.

        Until this runs, the engine acts on the values it last saw — which is
        why the kill switch is a file check on the decision path and not an
        override.
        """
        raw = await self._store.get_many(
            (
                keys.LOSS_LIMIT_BYPASS,
                keys.LOSS_LIMIT_BYPASS_EXPIRES_AT,
                keys.RUNTIME_MAX_ORDER_NOTIONAL,
                keys.RUNTIME_MAX_ORDER_QUANTITY,
            )
        )
        self._bypass_loss_limit = decode_bool(raw.get(keys.LOSS_LIMIT_BYPASS))
        self._bypass_expires_at = decode_float(raw.get(keys.LOSS_LIMIT_BYPASS_EXPIRES_AT))
        self._runtime_max_order_notional = decode_positive_float(
            raw.get(keys.RUNTIME_MAX_ORDER_NOTIONAL)
        )
        self._runtime_max_order_quantity = decode_positive_float(
            raw.get(keys.RUNTIME_MAX_ORDER_QUANTITY)
        )

    # ------------------------------------------------------------------
    # Counters the caller feeds
    # ------------------------------------------------------------------

    async def record_realized_pnl(self, pnl_usd: float, *, is_live: bool) -> None:
        """Add realized P&L to the live or simulated leg and ratchet its peak."""
        self._roll_trading_day()
        if is_live:
            self._live_pnl += pnl_usd
            self._live_peak = max(self._live_peak, self._live_pnl)
        else:
            self._simulated_pnl += pnl_usd
            self._simulated_peak = max(self._simulated_peak, self._simulated_pnl)
        await self.persist()

    async def record_notional(self, notional_usd: float) -> None:
        """Add to the day's cumulative notional.

        Accepts a negative amount so a cancelled or unfilled order can hand
        its allowance back.
        """
        self._roll_trading_day()
        self._daily_notional += notional_usd
        await self.persist()

    def record_order_sent(self, request: OrderRequest) -> None:
        """Note that an order actually went to the venue.

        Synchronous and in-memory: this feeds the message-rate and duplicate
        windows, which are session-scoped. Call it once the order is away, not
        when it is evaluated — an order the engine rejected was never sent and
        must not consume the message budget.
        """
        now = self._clock.now_millis()
        self._rate_window.record(now)
        if self._duplicate_window is not None:
            self._duplicate_window.record(request.fingerprint(), now)

    async def record_order_rejected(self) -> None:
        """Note a venue rejection, advancing the consecutive-rejection count."""
        self._roll_trading_day()
        self._consecutive_rejects += 1
        await self.persist()

    async def record_order_accepted(self) -> None:
        """Note a venue acknowledgement, clearing the consecutive-rejection count."""
        self._roll_trading_day()
        self._consecutive_rejects = 0
        await self.persist()

    async def record_execution(self) -> None:
        """Note a fill, advancing the repeated-execution throttle."""
        self._roll_trading_day()
        self._executions_since_rearm += 1
        await self.persist()

    async def rearm(self) -> None:
        """Clear both throttles — the deliberate human intervention.

        This is the act the repeated-execution throttle exists to require. It
        is a separate call from anything the strategy does, because a throttle
        the strategy can clear by itself is not a throttle.
        """
        self._roll_trading_day()
        self._consecutive_rejects = 0
        self._executions_since_rearm = 0
        await self.persist()

    # ------------------------------------------------------------------
    # Derived state
    # ------------------------------------------------------------------

    @property
    def live_pnl(self) -> float:
        self._roll_trading_day()
        return self._live_pnl

    @property
    def simulated_pnl(self) -> float:
        self._roll_trading_day()
        return self._simulated_pnl

    @property
    def daily_realized_pnl(self) -> float:
        """Live plus simulated — a reporting figure only. The loss limit reads
        this engine's own leg, never the sum."""
        self._roll_trading_day()
        return self._live_pnl + self._simulated_pnl

    @property
    def daily_notional(self) -> float:
        self._roll_trading_day()
        return self._daily_notional

    @property
    def consecutive_rejects(self) -> int:
        self._roll_trading_day()
        return self._consecutive_rejects

    @property
    def executions_since_rearm(self) -> int:
        return self._executions_since_rearm

    # The four accessors below come in pairs: a private one that reads the
    # already-rolled counters, and a public property that rolls first. The
    # split is what lets one evaluation read the clock exactly once — every
    # control inside it then sees a single consistent instant — while an
    # operator console reading a property in isolation still gets a correctly
    # rolled figure.

    def _leg_pnl(self) -> float:
        return self._live_pnl if self.is_live else self._simulated_pnl

    def _leg_peak(self) -> float:
        return self._live_peak if self.is_live else self._simulated_peak

    def _floor(self) -> float | None:
        if self.limits.daily_loss_limit_usd is None:
            return None
        return self._leg_peak() - self.limits.daily_loss_limit_usd

    def _breached(self, now_millis: int) -> bool:
        floor = self._floor()
        if floor is None or self._bypass_active(now_millis):
            return False
        return self._leg_pnl() <= floor

    def _bypass_active(self, now_millis: int) -> bool:
        if not self._bypass_loss_limit or self._bypass_expires_at is None:
            return False
        return now_millis < self._bypass_expires_at

    @property
    def loss_limit_pnl(self) -> float:
        """The realized-P&L leg this engine's loss limit measures."""
        self._roll_trading_day()
        return self._leg_pnl()

    @property
    def loss_limit_peak(self) -> float:
        """Session high water mark of this engine's leg."""
        self._roll_trading_day()
        return self._leg_peak()

    @property
    def loss_limit_floor(self) -> float | None:
        """P&L level at or below which trading halts, or ``None`` when unarmed.

        ``peak - limit``. With a peak of zero — a session that has never been
        profitable — this degrades to a plain fixed ``-limit`` floor.
        """
        self._roll_trading_day()
        return self._floor()

    @property
    def loss_limit_headroom(self) -> float | None:
        """USD this leg may still lose before the limit halts it."""
        self._roll_trading_day()
        floor = self._floor()
        return None if floor is None else self._leg_pnl() - floor

    def loss_limit_bypass_active(self, now_millis: int | None = None) -> bool:
        """Whether an operator bypass is currently suspending the loss limit.

        A bypass flag with no recorded expiry reads as INACTIVE. The control
        plane will not write one without an expiry, so a flag lacking it means
        damaged or hand-edited state, and the fail-safe reading of that is
        "the limit is armed".
        """
        now = self._clock.now_millis() if now_millis is None else now_millis
        return self._bypass_active(now)

    @property
    def loss_limit_bypass_expires_at(self) -> float | None:
        """Epoch millis at which the current bypass lapses, if there is one."""
        return self._bypass_expires_at

    def loss_limit_breached(self, now_millis: int | None = None) -> bool:
        """Whether this leg has drawn down to or through its trailing floor."""
        now = self._clock.now_millis() if now_millis is None else now_millis
        self._roll_trading_day(now)
        return self._breached(now)

    @property
    def effective_max_order_notional(self) -> float | None:
        """The per-order notional cap in force: runtime override, else configured."""
        if self._runtime_max_order_notional is not None:
            return self._runtime_max_order_notional
        return self.limits.max_order_notional_usd

    @property
    def effective_max_order_quantity(self) -> float | None:
        """The per-order quantity cap in force: runtime override, else configured."""
        if self._runtime_max_order_quantity is not None:
            return self._runtime_max_order_quantity
        return self.limits.max_order_quantity

    def kill_switch_engaged(self) -> bool:
        if self.limits.kill_switch_path is None:
            return False
        return self.limits.kill_switch_path.exists()

    def armed_controls(self) -> tuple[str, ...]:
        """The controls this engine currently arms, in evaluation order.

        A limit set is a long structure of mostly-``None`` fields, and "which
        controls are actually live right now" is the first question an
        operator asks. Answering it from the engine rather than by reading the
        configuration closes the gap between what a desk believes is armed and
        what is.
        """
        return tuple(control.name for control in CONTROL_SEQUENCE if control.is_armed(self))

    # ------------------------------------------------------------------
    # The decision
    # ------------------------------------------------------------------

    def evaluate(self, request: OrderRequest) -> RiskDecision:
        """Decide whether this order may be sent.

        Walks :data:`CONTROL_SEQUENCE` in order and returns the first
        rejection, or :data:`~pretrade_gate.decision.ACCEPTED`. Synchronous
        and free of store I/O; the kill switch's file check is the one
        deliberate exception.
        """
        now = self._clock.now_millis()
        self._roll_trading_day(now)
        for control in CONTROL_SEQUENCE:
            if not control.is_armed(self):
                continue
            decision = control.check(self, request, now)
            if decision is not None:
                return decision
        return ACCEPTED

    # ------------------------------------------------------------------
    # Individual controls
    # ------------------------------------------------------------------

    def _check_kill_switch(self, request: OrderRequest, now: int) -> RiskDecision | None:
        if not self.kill_switch_engaged():
            return None
        return RiskDecision.reject(
            RejectCode.KILL_SWITCH_ENGAGED,
            "kill switch",
            f"kill switch engaged at {self.limits.kill_switch_path}",
        )

    def _check_loss_limit(self, request: OrderRequest, now: int) -> RiskDecision | None:
        if not self._breached(now):
            return None
        floor = self._floor()
        pnl = self._leg_pnl()
        limit = self.limits.daily_loss_limit_usd
        leg = "live" if self.is_live else "simulated"
        return RiskDecision.reject(
            RejectCode.LOSS_LIMIT_BREACHED,
            "daily loss limit",
            f"daily loss limit: {leg} realized {pnl:+.2f} USD is at or below the "
            f"trailing floor {floor:+.2f} "
            f"(peak {self._leg_peak():+.2f} less {limit:.2f} limit)",
            limit=floor,
            observed=pnl,
        )

    def _check_instrument_permitted(self, request: OrderRequest, now: int) -> RiskDecision | None:
        if request.symbol in self.limits.permitted_instruments:
            return None
        return RiskDecision.reject(
            RejectCode.INSTRUMENT_NOT_PERMITTED,
            "instrument universe",
            f"{request.symbol} is not in the permitted trading universe",
        )

    def _check_instrument_restricted(self, request: OrderRequest, now: int) -> RiskDecision | None:
        if request.symbol not in self.limits.restricted_instruments:
            return None
        return RiskDecision.reject(
            RejectCode.INSTRUMENT_RESTRICTED,
            "restricted list",
            f"{request.symbol} is on the restricted list",
        )

    def _check_session_state(self, request: OrderRequest, now: int) -> RiskDecision | None:
        tradeable = self.limits.tradeable_session_states
        if request.session_state in tradeable:
            return None
        permitted = ", ".join(sorted(state.value for state in tradeable))
        return RiskDecision.reject(
            RejectCode.MARKET_SESSION_NOT_OPEN,
            "session state",
            f"{request.symbol} session state is {request.session_state.value}; order "
            f"entry is permitted only in {permitted}",
        )

    def _check_short_sale_locate(self, request: OrderRequest, now: int) -> RiskDecision | None:
        if request.side is not Side.SELL_SHORT or request.locate_secured:
            return None
        return RiskDecision.reject(
            RejectCode.SHORT_SALE_LOCATE_MISSING,
            "short sale locate",
            f"short sale of {request.symbol} carries no secured locate",
        )

    def _check_quantity_sign(self, request: OrderRequest, now: int) -> RiskDecision | None:
        if request.quantity > 0:
            return None
        return RiskDecision.reject(
            RejectCode.ORDER_QUANTITY_NOT_POSITIVE,
            "order quantity",
            f"order quantity must be positive, got {request.quantity:g}",
            observed=request.quantity,
        )

    def _check_price_sign(self, request: OrderRequest, now: int) -> RiskDecision | None:
        if request.limit_price is None or request.limit_price > 0:
            return None
        return RiskDecision.reject(
            RejectCode.ORDER_PRICE_NOT_POSITIVE,
            "order price",
            f"limit price must be positive, got {request.limit_price:g}",
            observed=request.limit_price,
        )

    def _check_notional_sign(self, request: OrderRequest, now: int) -> RiskDecision | None:
        notional = request.notional_usd()
        if notional is None or notional > 0:
            return None
        return RiskDecision.reject(
            RejectCode.ORDER_NOTIONAL_NOT_POSITIVE,
            "order notional",
            f"order notional must be positive, got {notional:.2f} USD",
            observed=notional,
        )

    def _check_max_quantity(self, request: OrderRequest, now: int) -> RiskDecision | None:
        cap = self.effective_max_order_quantity
        if request.quantity <= cap:
            return None
        return RiskDecision.reject(
            RejectCode.MAX_ORDER_QUANTITY_EXCEEDED,
            "maximum order quantity",
            f"order quantity {request.quantity:g} exceeds the {cap:g} cap",
            limit=cap,
            observed=request.quantity,
        )

    def _check_max_notional(self, request: OrderRequest, now: int) -> RiskDecision | None:
        cap = self.effective_max_order_notional
        notional = request.notional_usd()
        if notional is None:
            return _unpriced(request, "maximum order notional")
        if notional <= cap:
            return None
        return RiskDecision.reject(
            RejectCode.MAX_ORDER_NOTIONAL_EXCEEDED,
            "maximum order notional",
            f"order notional {notional:.2f} USD exceeds the {cap:.2f} USD cap",
            limit=cap,
            observed=notional,
        )

    def _check_quote_age(self, request: OrderRequest, now: int) -> RiskDecision | None:
        max_age = self.limits.max_quote_age_millis
        if request.quote_age_millis is None:
            return RiskDecision.reject(
                RejectCode.MARKET_DATA_UNAVAILABLE,
                "quote age",
                "the staleness control requires a quote age and none was supplied",
            )
        if request.quote_age_millis <= max_age:
            return None
        return RiskDecision.reject(
            RejectCode.MARKET_DATA_STALE,
            "quote age",
            f"quotes are {request.quote_age_millis} ms old, over the {max_age} ms limit",
            limit=float(max_age),
            observed=float(request.quote_age_millis),
        )

    def _check_price_band(self, request: OrderRequest, now: int) -> RiskDecision | None:
        # A market order carries no price to collar. The venue's own bands and
        # the notional cap are what constrain it; see the README limitations.
        if request.limit_price is None:
            return None
        band = self.limits.price_band_fraction
        reference = request.band_reference_price()
        if reference is None or reference <= 0:
            return RiskDecision.reject(
                RejectCode.MARKET_DATA_UNAVAILABLE,
                "price band",
                "no reference price was available to measure the price band against",
            )
        deviation = abs(request.limit_price - reference) / reference
        if deviation <= band:
            return None
        return RiskDecision.reject(
            RejectCode.PRICE_BAND_EXCEEDED,
            "price band",
            f"limit price {request.limit_price:g} deviates {deviation:.2%} from the "
            f"reference {reference:g}, over the {band:.2%} band",
            limit=band,
            observed=deviation,
        )

    def _check_execution_slippage(self, request: OrderRequest, now: int) -> RiskDecision | None:
        max_slippage = self.limits.max_execution_slippage
        touch = request.best_bid if request.side.is_sell else request.best_ask
        if request.decision_price is None or touch is None:
            return _unpriced(request, "execution slippage")
        # Adverse only: a buy suffers when the ask has risen, a sell when the
        # bid has fallen. A move in the desk's favour is not a risk event.
        drift = (
            request.decision_price - touch
            if request.side.is_sell
            else touch - request.decision_price
        )
        if drift <= max_slippage:
            return None
        return RiskDecision.reject(
            RejectCode.EXECUTION_SLIPPAGE_EXCEEDED,
            "execution slippage",
            f"the market has moved {drift:+.4g} against the {request.decision_price:g} "
            f"decision price (limit {max_slippage:g}); the opportunity that justified "
            "this order no longer exists",
            limit=max_slippage,
            observed=drift,
        )

    def _check_duplicate(self, request: OrderRequest, now: int) -> RiskDecision | None:
        window = self._duplicate_window
        if not window.seen_within_window(request.fingerprint(), now):
            return None
        span = self.limits.duplicate_window_millis
        return RiskDecision.reject(
            RejectCode.DUPLICATE_ORDER,
            "duplicate order",
            f"an identical order for {request.symbol} was sent within the last {span} ms",
            limit=float(span),
        )

    def _check_order_rate(self, request: OrderRequest, now: int) -> RiskDecision | None:
        cap = self.limits.max_orders_per_window
        sent = self._rate_window.count(now)
        if sent < cap:
            return None
        return RiskDecision.reject(
            RejectCode.ORDER_RATE_EXCEEDED,
            "order rate",
            f"{sent} orders already sent in the last "
            f"{self.limits.order_rate_window_millis} ms, at the {cap} limit",
            limit=float(cap),
            observed=float(sent),
        )

    def _check_consecutive_rejects(self, request: OrderRequest, now: int) -> RiskDecision | None:
        cap = self.limits.max_consecutive_rejects
        if self._consecutive_rejects < cap:
            return None
        return RiskDecision.reject(
            RejectCode.CONSECUTIVE_REJECT_LIMIT_EXCEEDED,
            "consecutive rejections",
            f"{self._consecutive_rejects} consecutive venue rejections, at the {cap} "
            "limit; the strategy must be re-armed",
            limit=float(cap),
            observed=float(self._consecutive_rejects),
        )

    def _check_repeated_execution(self, request: OrderRequest, now: int) -> RiskDecision | None:
        cap = self.limits.max_executions_without_review
        if self._executions_since_rearm < cap:
            return None
        return RiskDecision.reject(
            RejectCode.REPEATED_EXECUTION_THROTTLE,
            "repeated execution throttle",
            f"{self._executions_since_rearm} executions since the strategy was last "
            f"re-armed, at the {cap} limit; it must be reviewed before trading again",
            limit=float(cap),
            observed=float(self._executions_since_rearm),
        )

    def _check_working_orders(self, request: OrderRequest, now: int) -> RiskDecision | None:
        cap = self.limits.max_working_orders
        if request.working_orders < cap:
            return None
        return RiskDecision.reject(
            RejectCode.MAX_WORKING_ORDERS_EXCEEDED,
            "working orders",
            f"{request.working_orders} orders already working, at the {cap} limit",
            limit=float(cap),
            observed=float(request.working_orders),
        )

    def _check_open_positions(self, request: OrderRequest, now: int) -> RiskDecision | None:
        cap = self.limits.max_open_positions
        if request.open_positions < cap:
            return None
        return RiskDecision.reject(
            RejectCode.MAX_OPEN_POSITIONS_EXCEEDED,
            "open positions",
            f"{request.open_positions} positions already open, at the {cap} limit",
            limit=float(cap),
            observed=float(request.open_positions),
        )

    def _check_position_limit(self, request: OrderRequest, now: int) -> RiskDecision | None:
        cap = self.limits.max_position_quantity
        current = request.position_quantity
        projected = request.projected_position()
        if abs(projected) <= cap:
            return None
        # The position is already outside the limit — because the limit was
        # lowered, an unexpected fill landed, or the market moved a
        # multiplier. A position limit must never be the reason a desk cannot
        # trade OUT of the position it is breaching, so an order that strictly
        # shrinks the position without flipping its sign is permitted even
        # here. An order that would leave the desk still over the limit on the
        # other side is not: that is not reducing risk, it is re-taking it.
        reduces = abs(projected) < abs(current)
        flips_sign = current * projected < 0
        if reduces and not flips_sign:
            return None
        return RiskDecision.reject(
            RejectCode.MAX_POSITION_EXCEEDED,
            "position limit",
            f"this order would take {request.symbol} from {current:g} to {projected:g}, "
            f"outside the {cap:g} position limit",
            limit=cap,
            observed=abs(projected),
        )

    def _check_self_match(self, request: OrderRequest, now: int) -> RiskDecision | None:
        if request.opposing_resting_quantity <= 0:
            return None
        return RiskDecision.reject(
            RejectCode.SELF_MATCH_PREVENTED,
            "self-match prevention",
            f"{request.opposing_resting_quantity:g} of the firm's own quantity rests on "
            f"the opposite side of {request.symbol}; this order could trade against it",
            observed=request.opposing_resting_quantity,
        )

    def _check_gross_exposure(self, request: OrderRequest, now: int) -> RiskDecision | None:
        cap = self.limits.max_gross_exposure_usd
        notional = request.notional_usd()
        if notional is None:
            return _unpriced(request, "gross exposure")
        projected = request.gross_exposure_usd + abs(notional)
        if projected <= cap:
            return None
        return RiskDecision.reject(
            RejectCode.GROSS_EXPOSURE_LIMIT_EXCEEDED,
            "gross exposure",
            f"gross exposure would reach {projected:.2f} USD "
            f"({request.gross_exposure_usd:.2f} plus {abs(notional):.2f}), over the "
            f"{cap:.2f} USD limit",
            limit=cap,
            observed=projected,
        )

    def _check_daily_notional(self, request: OrderRequest, now: int) -> RiskDecision | None:
        cap = self.limits.daily_notional_limit_usd
        notional = request.notional_usd()
        if notional is None:
            return _unpriced(request, "daily notional limit")
        projected = self._daily_notional + abs(notional)
        if projected <= cap:
            return None
        return RiskDecision.reject(
            RejectCode.DAILY_NOTIONAL_LIMIT_EXCEEDED,
            "daily notional limit",
            f"today's notional would reach {projected:.2f} USD "
            f"({self._daily_notional:.2f} plus {abs(notional):.2f}), over the "
            f"{cap:.2f} USD limit",
            limit=cap,
            observed=projected,
        )


def _unpriced(request: OrderRequest, control: str) -> RiskDecision:
    """Reject because an armed control's market-data input never arrived.

    An armed control that cannot be evaluated must reject. Skipping quietly is
    how a desk comes to believe it is protected by a control that stopped
    firing when its input stopped arriving.
    """
    return RiskDecision.reject(
        RejectCode.MARKET_DATA_UNAVAILABLE,
        control,
        f"the {control} control needs a price for {request.symbol} and none was "
        "available (no limit price, decision price or touch)",
    )


def _as_count(raw: str | None) -> int:
    """Decode a persisted counter as a non-negative integer."""
    value = decode_float(raw)
    if value is None or value < 0:
        return 0
    return int(value)


def _always_armed(engine: PreTradeRiskEngine) -> bool:
    """Arming test for the well-formedness controls, which are never off."""
    return True


#: The controls, in the order every order walks them. This is the library's
#: statement about precedence, held as data rather than as a chain of ``if``
#: statements: the sequence IS the specification, and a port reproduces it by
#: copying the table rather than by re-deriving control flow.
#:
#: The ordering is not arbitrary. Absolute stops come first — a kill switch and
#: a breached loss limit end the conversation regardless of what the order
#: says. Eligibility follows, because an instrument the desk may not trade is
#: settled without pricing anything. Then the order's own well-formedness,
#: then the price controls that depend on it, then message conduct, and last
#: the position and exposure controls, which need the caller's book context.
#: Cheapest and most absolute first; most contextual last.
CONTROL_SEQUENCE: tuple[Control, ...] = (
    Control(
        "kill switch",
        lambda e: e.limits.kill_switch_path is not None,
        PreTradeRiskEngine._check_kill_switch,
    ),
    Control(
        "daily loss limit",
        lambda e: e.limits.daily_loss_limit_usd is not None,
        PreTradeRiskEngine._check_loss_limit,
    ),
    Control(
        "instrument universe",
        lambda e: e.limits.permitted_instruments is not None,
        PreTradeRiskEngine._check_instrument_permitted,
    ),
    Control(
        "restricted list",
        lambda e: bool(e.limits.restricted_instruments),
        PreTradeRiskEngine._check_instrument_restricted,
    ),
    Control(
        "session state",
        lambda e: e.limits.tradeable_session_states is not None,
        PreTradeRiskEngine._check_session_state,
    ),
    Control(
        "short sale locate",
        lambda e: e.limits.require_short_sale_locate,
        PreTradeRiskEngine._check_short_sale_locate,
    ),
    # Well-formedness is always armed: an order with a non-positive quantity,
    # price or notional is malformed regardless of which limits a desk sets.
    Control("order quantity", _always_armed, PreTradeRiskEngine._check_quantity_sign),
    Control("order price", _always_armed, PreTradeRiskEngine._check_price_sign),
    Control("order notional", _always_armed, PreTradeRiskEngine._check_notional_sign),
    Control(
        "maximum order quantity",
        lambda e: e.effective_max_order_quantity is not None,
        PreTradeRiskEngine._check_max_quantity,
    ),
    Control(
        "maximum order notional",
        lambda e: e.effective_max_order_notional is not None,
        PreTradeRiskEngine._check_max_notional,
    ),
    Control(
        "quote age",
        lambda e: e.limits.max_quote_age_millis is not None,
        PreTradeRiskEngine._check_quote_age,
    ),
    Control(
        "price band",
        lambda e: e.limits.price_band_fraction is not None,
        PreTradeRiskEngine._check_price_band,
    ),
    Control(
        "execution slippage",
        lambda e: e.limits.max_execution_slippage is not None,
        PreTradeRiskEngine._check_execution_slippage,
    ),
    Control(
        "duplicate order",
        lambda e: e._duplicate_window is not None,
        PreTradeRiskEngine._check_duplicate,
    ),
    Control(
        "order rate",
        lambda e: e.limits.max_orders_per_window is not None,
        PreTradeRiskEngine._check_order_rate,
    ),
    Control(
        "consecutive rejections",
        lambda e: e.limits.max_consecutive_rejects is not None,
        PreTradeRiskEngine._check_consecutive_rejects,
    ),
    Control(
        "repeated execution throttle",
        lambda e: e.limits.max_executions_without_review is not None,
        PreTradeRiskEngine._check_repeated_execution,
    ),
    Control(
        "working orders",
        lambda e: e.limits.max_working_orders is not None,
        PreTradeRiskEngine._check_working_orders,
    ),
    Control(
        "open positions",
        lambda e: e.limits.max_open_positions is not None,
        PreTradeRiskEngine._check_open_positions,
    ),
    Control(
        "position limit",
        lambda e: e.limits.max_position_quantity is not None,
        PreTradeRiskEngine._check_position_limit,
    ),
    Control(
        "self-match prevention",
        lambda e: e.limits.prevent_self_match,
        PreTradeRiskEngine._check_self_match,
    ),
    Control(
        "gross exposure",
        lambda e: e.limits.max_gross_exposure_usd is not None,
        PreTradeRiskEngine._check_gross_exposure,
    ),
    Control(
        "daily notional limit",
        lambda e: e.limits.daily_notional_limit_usd is not None,
        PreTradeRiskEngine._check_daily_notional,
    ),
)
