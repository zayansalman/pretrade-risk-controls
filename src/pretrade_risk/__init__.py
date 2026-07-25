"""A venue-independent pre-trade risk control layer with pluggable persistence.

Every order answers one question before it leaves the building:
``engine.evaluate(order)`` — an accepted decision means send it, a rejected one
carries a stable code, the limit that applied, and the value that breached it.

Distributed as ``pretrade-risk-controls``, imported as ``pretrade_risk``.

The SQLite-backed store lives in :mod:`pretrade_risk.sqlite_store` and is
imported explicitly, so the package root stays importable without the optional
``aiosqlite`` extra.
"""

from pretrade_risk import keys
from pretrade_risk.clock import Clock, ManualClock, SystemClock, trading_day
from pretrade_risk.controls import (
    MAX_BYPASS_DURATION_MILLIS,
    BypassRecord,
    bypass_loss_limit,
    clear_loss_limit_bypass,
    get_runtime_max_order_notional,
    get_runtime_max_order_quantity,
    read_loss_limit_bypass,
    reset_daily_loss_limit,
    set_runtime_max_order_notional,
    set_runtime_max_order_quantity,
)
from pretrade_risk.decision import ACCEPTED, RejectCode, RiskDecision
from pretrade_risk.engine import CONTROL_SEQUENCE, Control, PreTradeRiskEngine
from pretrade_risk.limits import RiskLimits
from pretrade_risk.order import OrderRequest, SessionState, Side
from pretrade_risk.store import InMemoryStateStore, SequentialBatchMixin, StateStore

__all__ = [
    "ACCEPTED",
    "CONTROL_SEQUENCE",
    "MAX_BYPASS_DURATION_MILLIS",
    "BypassRecord",
    "Clock",
    "Control",
    "InMemoryStateStore",
    "ManualClock",
    "OrderRequest",
    "PreTradeRiskEngine",
    "RejectCode",
    "RiskDecision",
    "RiskLimits",
    "SequentialBatchMixin",
    "SessionState",
    "Side",
    "StateStore",
    "SystemClock",
    "bypass_loss_limit",
    "clear_loss_limit_bypass",
    "get_runtime_max_order_notional",
    "get_runtime_max_order_quantity",
    "keys",
    "read_loss_limit_bypass",
    "reset_daily_loss_limit",
    "set_runtime_max_order_notional",
    "set_runtime_max_order_quantity",
    "trading_day",
]
