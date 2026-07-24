"""pretrade-gate: a venue-independent pre-trade risk gate with pluggable persistence.

Every order your bot wants to place answers one question first:
``gate.block_reason(request)`` — ``None`` means trade; a string tells the
operator exactly why not.

The SQLite-backed store lives in :mod:`pretrade_gate.sqlite_store` and is
imported explicitly (``from pretrade_gate.sqlite_store import SqliteStateStore``)
so the package root stays importable without the optional ``aiosqlite`` extra.
"""

from pretrade_gate.controls import (
    get_loss_halt_bypass,
    get_runtime_max_trade_usd,
    get_runtime_trade_shares,
    reset_daily_loss_halt,
    set_loss_halt_bypass,
    set_runtime_max_trade_usd,
    set_runtime_trade_shares,
)
from pretrade_gate.gate import EntryRequest, GateConfig, RiskGate
from pretrade_gate.store import InMemoryStateStore, StateStore

__all__ = [
    "EntryRequest",
    "GateConfig",
    "InMemoryStateStore",
    "RiskGate",
    "StateStore",
    "get_loss_halt_bypass",
    "get_runtime_max_trade_usd",
    "get_runtime_trade_shares",
    "reset_daily_loss_halt",
    "set_loss_halt_bypass",
    "set_runtime_max_trade_usd",
    "set_runtime_trade_shares",
]
