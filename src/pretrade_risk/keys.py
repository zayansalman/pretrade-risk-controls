"""The persisted-state schema — every key the engine reads or writes.

One flat ``pretrade.*`` namespace, one engine per namespace. There is no
configurable prefix: to run several engines against one backend, wrap the
store in a thin prefixing adapter, which keeps the schema here a fixed,
auditable list rather than something assembled at runtime.

Values are always strings, encoded by :mod:`pretrade_risk.encoding`. Floats
carry 17 significant digits so they reload bit-exactly; an empty string is the
"cleared" encoding for the override keys.

What is persisted, and what deliberately is not
------------------------------------------------

Persisted state is state whose whole purpose is to survive a restart. The
day's realized loss, the high water mark it is measured from, the day's
notional, the count of executions since a human last re-armed the strategy —
losing any of these to a process restart would hand the desk a fresh limit it
has already spent, which is precisely the failure a persisted counter exists
to prevent.

The message-rate and duplicate windows are NOT persisted, for the mirror
reason: they describe what a live session has just sent. A process that has
restarted has sent nothing, and reloading a stale burst would reject the first
legitimate orders of the new session.
"""

# -- Trading-day counters ------------------------------------------------
# Written by ``PreTradeRiskEngine.persist``, rebuilt by ``load``. All reset
# when the trading day rolls.

#: The trading date (ISO ``YYYY-MM-DD``) the counters below belong to.
TRADING_DAY = "pretrade.trading_day"
#: Today's realized P&L on the live leg, USD.
LIVE_REALIZED_PNL = "pretrade.live_realized_pnl"
#: Today's realized P&L on the simulated leg, USD.
SIMULATED_REALIZED_PNL = "pretrade.simulated_realized_pnl"
#: Session high water mark of the live leg — the loss limit trails this.
LIVE_PEAK_PNL = "pretrade.live_peak_pnl"
#: Session high water mark of the simulated leg.
SIMULATED_PEAK_PNL = "pretrade.simulated_peak_pnl"
#: Cumulative notional sent today, measured against the daily notional limit.
DAILY_NOTIONAL = "pretrade.daily_notional"

# -- Throttle counters ----------------------------------------------------
# Persisted precisely so a restart loop cannot clear them: a strategy that
# crashes and restarts must not thereby earn a fresh allowance of rejections.

#: Consecutive rejections since the last acceptance or re-arm.
CONSECUTIVE_REJECTS = "pretrade.consecutive_rejects"
#: Executions since a human last re-armed the strategy.
EXECUTIONS_SINCE_REARM = "pretrade.executions_since_rearm"

# -- Operator overrides ---------------------------------------------------
# Written by ``controls``, re-read by the engine on refresh.

#: "1" suspends the loss limit. Only honoured while unexpired.
LOSS_LIMIT_BYPASS = "pretrade.loss_limit_bypass"
#: Epoch milliseconds at which the bypass above stops being honoured.
LOSS_LIMIT_BYPASS_EXPIRES_AT = "pretrade.loss_limit_bypass_expires_at"
#: Who authorised the bypass, when, and why. Recorded for the audit trail;
#: never read by a control.
LOSS_LIMIT_BYPASS_ACTOR = "pretrade.loss_limit_bypass_actor"
LOSS_LIMIT_BYPASS_REASON = "pretrade.loss_limit_bypass_reason"
LOSS_LIMIT_BYPASS_SET_AT = "pretrade.loss_limit_bypass_set_at"

#: Runtime per-order caps. Empty string means unset — fall back to the
#: configured limit.
RUNTIME_MAX_ORDER_NOTIONAL = "pretrade.runtime_max_order_notional"
RUNTIME_MAX_ORDER_QUANTITY = "pretrade.runtime_max_order_quantity"

#: Every key holding a trading-day or throttle counter, in the order
#: ``persist`` writes them. Defined once here so the snapshot is a single
#: auditable list rather than something reassembled at each call site.
COUNTER_KEYS = (
    TRADING_DAY,
    LIVE_REALIZED_PNL,
    SIMULATED_REALIZED_PNL,
    LIVE_PEAK_PNL,
    SIMULATED_PEAK_PNL,
    DAILY_NOTIONAL,
    CONSECUTIVE_REJECTS,
    EXECUTIONS_SINCE_REARM,
)

#: Every key the operator control plane writes.
OVERRIDE_KEYS = (
    LOSS_LIMIT_BYPASS,
    LOSS_LIMIT_BYPASS_EXPIRES_AT,
    LOSS_LIMIT_BYPASS_ACTOR,
    LOSS_LIMIT_BYPASS_REASON,
    LOSS_LIMIT_BYPASS_SET_AT,
    RUNTIME_MAX_ORDER_NOTIONAL,
    RUNTIME_MAX_ORDER_QUANTITY,
)
