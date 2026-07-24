"""The persisted-state key schema — every key the gate reads or writes.

One flat ``gate.*`` namespace, one gate per store namespace. There is no
configurable prefix: if you run several gates against one backend, wrap the
store with a thin prefixing adapter instead.

Values are always strings (see ``StateStore``). Floats are written with
``repr()`` so they round-trip exactly; an empty string is the runtime-clear
encoding for the two override keys.
"""

# Daily counters — written by ``RiskGate.persist``, rebuilt by ``RiskGate.load``.
DATE = "gate.date"  # UTC day (ISO date) the counters below belong to
LIVE_REALIZED_PNL = "gate.live_realized_pnl"  # today's realized real-money PnL, USD
PAPER_REALIZED_PNL = "gate.paper_realized_pnl"  # today's realized paper/study PnL, USD
LIVE_PEAK_PNL = "gate.live_peak_pnl"  # session high-water mark of the live leg
PAPER_PEAK_PNL = "gate.paper_peak_pnl"  # session high-water mark of the paper leg
DAILY_BUY_NOTIONAL = "gate.daily_buy_notional"  # cumulative BUY notional vs the bankroll cap

# Operator controls — written by ``controls``, read by the gate on refresh.
BYPASS_LOSS_HALT = "gate.bypass_loss_halt"  # "1" disables the loss halt (both modes)
RUNTIME_MAX_TRADE_USD = "gate.runtime_max_trade_usd"  # per-trade cap override; "" = unset
RUNTIME_TRADE_SHARES = "gate.runtime_trade_shares"  # share-denominated clip size; "" = unset
