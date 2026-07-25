"""Clock injection and trading-day roll semantics.

The trading day is the unit every daily counter is keyed to, so its boundary
is worth pinning precisely — a roll that fires mid-session would reset the
day's loss limit while the desk is still trading it.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from pretrade_risk.clock import (
    MILLIS_PER_DAY,
    MILLIS_PER_HOUR,
    ManualClock,
    SystemClock,
    trading_day,
)

CME_ROLL = 22 * MILLIS_PER_HOUR  # 17:00 US/Central in winter


def at(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


class TestTradingDayUtcRoll:
    """A roll of 0 is the plain UTC calendar day."""

    def test_midnight_starts_the_day(self) -> None:
        assert trading_day(at("2026-07-20T00:00:00+00:00"), 0) == "2026-07-20"

    def test_last_millisecond_still_the_same_day(self) -> None:
        assert trading_day(at("2026-07-20T23:59:59.999+00:00"), 0) == "2026-07-20"

    def test_next_midnight_rolls(self) -> None:
        assert trading_day(at("2026-07-21T00:00:00+00:00"), 0) == "2026-07-21"


class TestTradingDayEveningRoll:
    """A futures-style evening roll: the session opening Monday evening is
    Tuesday's trading day."""

    def test_before_the_roll_is_the_current_date(self) -> None:
        assert trading_day(at("2026-07-20T21:59:59+00:00"), CME_ROLL) == "2026-07-20"

    def test_at_the_roll_moves_to_the_next_date(self) -> None:
        assert trading_day(at("2026-07-20T22:00:00+00:00"), CME_ROLL) == "2026-07-21"

    def test_after_the_roll_stays_on_the_next_date(self) -> None:
        assert trading_day(at("2026-07-20T22:30:00+00:00"), CME_ROLL) == "2026-07-21"

    def test_utc_midnight_does_not_roll(self) -> None:
        # The bug this whole parameter exists to prevent: a UTC-midnight reset
        # in the middle of a live evening session.
        assert trading_day(at("2026-07-21T00:30:00+00:00"), CME_ROLL) == "2026-07-21"

    def test_next_day_before_its_roll(self) -> None:
        assert trading_day(at("2026-07-21T21:00:00+00:00"), CME_ROLL) == "2026-07-21"

    def test_a_full_session_shares_one_trading_date(self) -> None:
        opened = trading_day(at("2026-07-20T22:00:00+00:00"), CME_ROLL)
        overnight = trading_day(at("2026-07-21T03:00:00+00:00"), CME_ROLL)
        closed = trading_day(at("2026-07-21T20:00:00+00:00"), CME_ROLL)
        assert opened == overnight == closed == "2026-07-21"


class TestTradingDayValidation:
    @pytest.mark.parametrize("roll", [-1, MILLIS_PER_DAY, MILLIS_PER_DAY + 1])
    def test_out_of_range_roll_rejected(self, roll: int) -> None:
        with pytest.raises(ValueError):
            trading_day(0, roll)


class TestManualClock:
    def test_starts_where_told_and_advances(self) -> None:
        clock = ManualClock(1_000)
        assert clock.now_millis() == 1_000
        clock.advance_millis(500)
        assert clock.now_millis() == 1_500

    def test_will_not_move_backwards(self) -> None:
        clock = ManualClock(1_000)
        with pytest.raises(ValueError):
            clock.advance_millis(-1)

    def test_set_is_absolute(self) -> None:
        clock = ManualClock(1_000)
        clock.set_millis(42)
        assert clock.now_millis() == 42


class TestSystemClock:
    def test_returns_whole_millis_near_now(self) -> None:
        now = SystemClock().now_millis()
        assert isinstance(now, int)
        # Sanity band rather than a fixed value: after 2020, before 2100.
        assert 1_577_836_800_000 < now < 4_102_444_800_000
