from __future__ import annotations

import pytest

from pretrade_risk import InMemoryStateStore, ManualClock

#: A fixed instant well inside a trading day, so a test advancing the clock by
#: seconds cannot accidentally roll the day underneath itself.
FIXED_NOW_MILLIS = 1_700_000_000_000


@pytest.fixture
def store() -> InMemoryStateStore:
    """A fresh in-memory store per test.

    A test pairing a control-plane function with an engine MUST pass this same
    instance to both — that shared store IS the seam between the write side
    and the read side.
    """
    return InMemoryStateStore()


@pytest.fixture
def clock() -> ManualClock:
    """A clock the test advances by hand.

    Every time-dependent control is driven through this rather than by
    sleeping, so the suite pins exact boundaries instead of approximate ones.
    """
    return ManualClock(FIXED_NOW_MILLIS)
