"""Time, as an injected dependency, in whole milliseconds since the UNIX epoch.

Two reasons the engine never calls a global clock directly.

**Testability.** Rate limits, duplicate-order windows, override expiries and
the trading-day roll are all time-dependent controls. Tests drive them with
:class:`ManualClock` instead of sleeping.

**Portability.** ``int`` milliseconds since the epoch is the one time
representation every target language expresses identically —
``System.currentTimeMillis()`` in Java, ``chrono::system_clock`` in C++,
``SystemTime`` in Rust, ``DateTimeOffset.ToUnixTimeMilliseconds()`` in C#.
Passing a language-specific datetime across the seam would not port.

Wall-clock time is deliberate here, not a lapse. The trading day and override
expiries are civil-time concepts that must survive a process restart, and a
monotonic clock resets on reboot. The cost is that a backwards step in wall
clock (an NTP correction) can briefly widen a rate-limit window; the
:mod:`~pretrade_risk.windows` counters are written to tolerate that rather
than to assume it cannot happen.

Trading day
-----------

A trading day is NOT a UTC calendar day on a real desk. Futures sessions roll
in the evening of the preceding local date — CME's trading day opens at 17:00
US/Central, so an order at 22:00 Chicago time on Monday belongs to Tuesday's
trading day, and a UTC-midnight counter would reset the day's loss limit in
the middle of a live session.

:func:`trading_day` therefore takes the UTC time-of-day at which the session
rolls. A roll of 0 reproduces plain UTC-midnight behaviour. The result is an
ISO ``YYYY-MM-DD`` string, which is human-readable in the store, sorts
correctly, and is reproducible in any language from integer arithmetic plus a
UTC epoch-to-date conversion — no timezone database required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

MILLIS_PER_SECOND = 1_000
MILLIS_PER_MINUTE = 60_000
MILLIS_PER_HOUR = 3_600_000
MILLIS_PER_DAY = 86_400_000


class Clock(Protocol):
    """Source of wall-clock time, in whole milliseconds since the UNIX epoch (UTC)."""

    def now_millis(self) -> int: ...


class SystemClock:
    """The real clock. The default for every engine that is not under test."""

    def now_millis(self) -> int:
        return int(datetime.now(UTC).timestamp() * MILLIS_PER_SECOND)


class ManualClock:
    """A clock the caller advances by hand.

    For tests and for replaying a recorded order flow through the engine at
    the timestamps it actually happened.
    """

    def __init__(self, now_millis: int = 0) -> None:
        self._now = int(now_millis)

    def now_millis(self) -> int:
        return self._now

    def advance_millis(self, delta_millis: int) -> None:
        if delta_millis < 0:
            raise ValueError("a clock may not move backwards")
        self._now += int(delta_millis)

    def set_millis(self, now_millis: int) -> None:
        self._now = int(now_millis)


def trading_day(now_millis: int, session_roll_millis: int = 0) -> str:
    """The ISO ``YYYY-MM-DD`` trading date a timestamp belongs to.

    ``session_roll_millis`` is the UTC time of day at which the trading day
    rolls, in milliseconds after UTC midnight, in ``[0, 86_400_000)``.
    Timestamps at or after that instant belong to the NEXT calendar date's
    trading day — the convention futures venues use, where the session opening
    on Sunday evening is Monday's trading day.

    Examples
    --------
    ``0`` — the trading day is the plain UTC calendar day.

    ``79_200_000`` (22:00 UTC, i.e. 17:00 US/Central in winter) — an order at
    22:30 UTC on Monday is part of Tuesday's trading day; one at 21:30 UTC is
    still Monday's.

    The caller owns daylight-saving: this function takes a fixed UTC offset,
    so a session anchored to a local wall-clock time needs its roll value
    updated when that local zone changes offset. That is deliberate — pulling
    in a timezone database would break both the zero-dependency guarantee and
    the 1:1 portability of this calculation.
    """
    if not 0 <= session_roll_millis < MILLIS_PER_DAY:
        raise ValueError("session_roll_millis must be within [0, 86_400_000)")
    # (MILLIS_PER_DAY - roll) % MILLIS_PER_DAY is the shift that moves the
    # roll instant onto UTC midnight; the modulo keeps a roll of 0 a no-op
    # rather than a whole-day shift.
    shift = (MILLIS_PER_DAY - session_roll_millis) % MILLIS_PER_DAY
    return datetime.fromtimestamp((now_millis + shift) / MILLIS_PER_SECOND, UTC).date().isoformat()
