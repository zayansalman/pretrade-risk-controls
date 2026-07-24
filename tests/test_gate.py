"""Paper/live gate parity tests.

Both paper and live consult the SAME :class:`RiskGate`. These tests pin the
canonical decision for a fixed table of scenarios so a future change to the
gate trips a failure in both modes — never just one. Drift is the bug the
unified gate exists to prevent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from pretrade_gate import EntryRequest, GateConfig, RiskGate


def _cfg(
    *,
    max_trade_usd: float = 5.0,
    daily_loss_halt_usd: float = 10.0,
    bankroll_cap_usd: Optional[float] = None,
    max_entry_slippage: float = 0.02,
    kill_switch_path: Path | None = None,
) -> GateConfig:
    return GateConfig(
        max_trade_usd=max_trade_usd,
        daily_loss_halt_usd=daily_loss_halt_usd,
        bankroll_cap_usd=bankroll_cap_usd,
        max_entry_slippage=max_entry_slippage,
        kill_switch_path=kill_switch_path,
    )


def _req(
    *,
    notional_usd: float = 3.0,
    position_open: bool = False,
    entry_order_resting: bool = False,
    side_price: float | None = 0.55,
    best_ask: float | None = 0.55,
) -> EntryRequest:
    return EntryRequest(
        notional_usd=notional_usd,
        position_open=position_open,
        entry_order_resting=entry_order_resting,
        side_price=side_price,
        best_ask=best_ask,
    )


class TestKillSwitchOptional:
    """``kill_switch_path=None`` disables the kill switch entirely — the one
    deliberate semantic addition over the parent system (which always had a
    configured path)."""

    def test_none_never_blocks(self, store) -> None:
        gate = RiskGate(_cfg(kill_switch_path=None), store)
        assert gate.kill_switch_active() is False
        assert gate.block_reason(_req()) is None

    def test_configured_path_still_blocks(self, tmp_path: Path, store) -> None:
        kill = tmp_path / "KILL"
        kill.write_text("halt")
        gate = RiskGate(_cfg(kill_switch_path=kill), store)
        assert gate.kill_switch_active() is True
        assert "KILL switch active" in (gate.block_reason(_req()) or "")

    def test_configured_but_absent_path_does_not_block(self, tmp_path: Path, store) -> None:
        gate = RiskGate(_cfg(kill_switch_path=tmp_path / "KILL"), store)
        assert gate.kill_switch_active() is False
        assert gate.block_reason(_req()) is None
