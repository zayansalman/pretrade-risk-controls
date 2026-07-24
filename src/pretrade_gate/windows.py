"""Bounded time-window counters behind the rate and duplicate controls.

Both structures here answer a question about the recent past — "how many
orders in the last second?", "have I already sent this exact order?" — and
both are deliberately in-memory only.

That is a design decision, not an omission. A message-rate limit protects the
venue's gateway and the firm's own message budget from a runaway loop, and
those are properties of a *live session*: a process that has just restarted
has by definition sent nothing. Persisting the window would carry a stale
burst across a restart and reject the first legitimate orders of a new
session. The persisted counters — realized loss, the day's notional, the
throttle counts — are the ones whose whole purpose is to survive a restart,
and those live in the store.

Portability
-----------

``deque`` is a ring buffer and ``dict`` is a hash map; both have a direct
equivalent in every target language. Neither structure uses an idiom that
does not translate. Memory is bounded in both: the rate counter never holds
more than ``limit + 1`` timestamps because it prunes before it appends, and
the duplicate guard prunes every expired fingerprint on each use.
"""

from __future__ import annotations

from collections import deque


class RateWindow:
    """Counts events within a trailing time window.

    A sliding window, not a fixed bucket: fixed buckets let a caller send the
    full allowance in the last millisecond of one bucket and again in the
    first millisecond of the next, passing a "10 per second" limit with 20
    orders inside two milliseconds. The trailing window has no such seam.

    The window is half-open — an event exactly ``window_millis`` old has
    expired — so a limit of N per second admits exactly N orders in any
    one-second span.
    """

    def __init__(self, window_millis: int) -> None:
        if window_millis <= 0:
            raise ValueError("window_millis must be positive")
        self._window_millis = window_millis
        self._events: deque[int] = deque()

    def count(self, now_millis: int) -> int:
        """Events still inside the window as of ``now_millis``."""
        self._prune(now_millis)
        return len(self._events)

    def record(self, now_millis: int) -> None:
        self._prune(now_millis)
        self._events.append(now_millis)

    def reset(self) -> None:
        self._events.clear()

    def _prune(self, now_millis: int) -> None:
        cutoff = now_millis - self._window_millis
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()
        # A wall clock that steps backwards (an NTP correction) would leave
        # timestamps ahead of `now`. Dropping them keeps the window honest
        # rather than letting a future-dated entry pin the count high until
        # real time catches up.
        while self._events and self._events[-1] > now_millis:
            self._events.pop()


class DuplicateWindow:
    """Remembers recently seen order fingerprints for a trailing window.

    Duplicate suppression catches the classic retry storm: a strategy that
    does not see its own acknowledgement and resubmits the identical order,
    and the operator who double-clicks. The caller decides what makes two
    orders "the same" by choosing the fingerprint — see
    :meth:`~pretrade_gate.order.OrderRequest.fingerprint`.
    """

    def __init__(self, window_millis: int) -> None:
        if window_millis <= 0:
            raise ValueError("window_millis must be positive")
        self._window_millis = window_millis
        self._seen: dict[str, int] = {}

    def seen_within_window(self, fingerprint: str, now_millis: int) -> bool:
        self._prune(now_millis)
        return fingerprint in self._seen

    def record(self, fingerprint: str, now_millis: int) -> None:
        self._prune(now_millis)
        self._seen[fingerprint] = now_millis

    def reset(self) -> None:
        self._seen.clear()

    def _prune(self, now_millis: int) -> None:
        cutoff = now_millis - self._window_millis
        expired = [
            key for key, seen_at in self._seen.items() if seen_at <= cutoff or seen_at > now_millis
        ]
        for key in expired:
            del self._seen[key]
