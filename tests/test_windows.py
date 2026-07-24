"""Trailing-window counter behaviour.

The sliding window is the point of these structures: a fixed bucket would let
twice the allowance through across a bucket seam, which is exactly the burst
a message-rate control exists to stop.
"""

from __future__ import annotations

import pytest

from pretrade_gate.windows import DuplicateWindow, RateWindow


class TestRateWindow:
    def test_counts_events_inside_the_window(self) -> None:
        window = RateWindow(1_000)
        for offset in (0, 100, 200):
            window.record(offset)
        assert window.count(300) == 3

    def test_expires_events_at_the_window_edge(self) -> None:
        window = RateWindow(1_000)
        window.record(0)
        # Half-open: exactly window_millis old has expired.
        assert window.count(999) == 1
        assert window.count(1_000) == 0

    def test_no_bucket_seam(self) -> None:
        # The failure a fixed bucket has: 5 at the end of one second and 5 at
        # the start of the next is 10 inside a 2 ms span. A trailing window
        # sees all 10.
        window = RateWindow(1_000)
        for _ in range(5):
            window.record(999)
        for _ in range(5):
            window.record(1_000)
        assert window.count(1_000) == 10

    def test_old_events_drop_out_as_time_passes(self) -> None:
        window = RateWindow(1_000)
        for _ in range(5):
            window.record(0)
        assert window.count(500) == 5
        assert window.count(1_500) == 0

    def test_reset_clears(self) -> None:
        window = RateWindow(1_000)
        window.record(0)
        window.reset()
        assert window.count(0) == 0

    def test_tolerates_a_backwards_clock_step(self) -> None:
        # An NTP correction must not leave future-dated entries pinning the
        # count high until real time catches up.
        window = RateWindow(1_000)
        window.record(10_000)
        assert window.count(5_000) == 0

    def test_rejects_a_non_positive_window(self) -> None:
        with pytest.raises(ValueError):
            RateWindow(0)


class TestDuplicateWindow:
    def test_recognises_a_repeat_inside_the_window(self) -> None:
        window = DuplicateWindow(1_000)
        window.record("abc", 0)
        assert window.seen_within_window("abc", 500) is True

    def test_forgets_after_the_window(self) -> None:
        window = DuplicateWindow(1_000)
        window.record("abc", 0)
        assert window.seen_within_window("abc", 1_000) is False

    def test_distinct_fingerprints_are_independent(self) -> None:
        window = DuplicateWindow(1_000)
        window.record("abc", 0)
        assert window.seen_within_window("xyz", 100) is False

    def test_re_recording_extends_the_window(self) -> None:
        window = DuplicateWindow(1_000)
        window.record("abc", 0)
        window.record("abc", 900)
        assert window.seen_within_window("abc", 1_500) is True

    def test_prunes_expired_entries(self) -> None:
        window = DuplicateWindow(1_000)
        window.record("abc", 0)
        window.seen_within_window("abc", 5_000)  # triggers the prune
        assert window._seen == {}

    def test_tolerates_a_backwards_clock_step(self) -> None:
        window = DuplicateWindow(1_000)
        window.record("abc", 10_000)
        assert window.seen_within_window("abc", 5_000) is False

    def test_reset_clears(self) -> None:
        window = DuplicateWindow(1_000)
        window.record("abc", 0)
        window.reset()
        assert window.seen_within_window("abc", 0) is False

    def test_rejects_a_non_positive_window(self) -> None:
        with pytest.raises(ValueError):
            DuplicateWindow(0)
