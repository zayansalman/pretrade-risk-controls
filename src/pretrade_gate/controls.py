"""Operator control plane — the write side of the risk engine.

Free functions over a :class:`~pretrade_gate.store.StateStore`, deliberately
NOT methods on the engine. A risk console or command-line tool holds only a
store handle and writes these keys; the trading process picks the change up at
its next :meth:`~pretrade_gate.engine.PreTradeRiskEngine.refresh_overrides`.
The two sides never share an object, so the control plane cannot reach into a
running engine's memory, and the store is the only channel between them.

Bypasses expire
----------------

:func:`bypass_loss_limit` will not write a bypass without an expiry. That is
the deliberate constraint of this module, and it is the one supervisors keep
finding firms on the wrong side of: a control switched off "just for now"
during a busy session, and still off months later because nothing forced
anybody to revisit it.

An expiring bypass fails in the safe direction. If the operator who set it is
unreachable, if the console is down, if everyone has gone home — the limit
re-arms itself. Turning it back on requires no action; leaving it off does.

Every bypass also records who authorised it and why. Those fields are never
read by a control. They exist because "the limit was suspended" is an answer
nobody can act on months later, and "suspended by this person, for this
reason, at this time, until that time" is.
"""

from __future__ import annotations

from dataclasses import dataclass

from pretrade_gate import keys
from pretrade_gate.clock import Clock, SystemClock
from pretrade_gate.encoding import (
    decode_bool,
    decode_float,
    decode_positive_float,
    encode_bool,
    encode_float,
)
from pretrade_gate.store import StateStore

#: Longest bypass this module will write. Not a regulatory figure — a
#: deliberately awkward one. A suspension that needs to outlast a trading day
#: is a limit that needs re-setting, and that is a decision with an owner, not
#: a toggle.
MAX_BYPASS_DURATION_MILLIS = 24 * 60 * 60 * 1_000


@dataclass(frozen=True)
class BypassRecord:
    """What the store holds about the current loss-limit bypass.

    ``active_at`` is the question a control asks; the rest is the audit trail.
    """

    enabled: bool
    expires_at_millis: float | None
    actor: str
    reason: str
    set_at_millis: float | None

    def active_at(self, now_millis: int) -> bool:
        """Whether the bypass is suspending the limit at this instant.

        A bypass with no expiry is never active. This module will not write
        one, so its absence means damaged or hand-edited state, and the
        fail-safe reading of that is that the limit is armed.
        """
        if not self.enabled or self.expires_at_millis is None:
            return False
        return now_millis < self.expires_at_millis


async def bypass_loss_limit(
    store: StateStore,
    *,
    duration_millis: int,
    actor: str,
    reason: str,
    clock: Clock | None = None,
) -> int:
    """Suspend the daily loss limit for a bounded period. Returns the expiry.

    ``duration_millis`` is required and bounded by
    :data:`MAX_BYPASS_DURATION_MILLIS`; ``actor`` and ``reason`` are required
    and must be non-empty. There is no way to express "suspend indefinitely"
    through this function, which is the point of it.

    Applies to both the live and simulated engines: an operator suspending a
    limit means the limit, not one leg of it.
    """
    if duration_millis <= 0:
        raise ValueError("a bypass must have a positive duration")
    if duration_millis > MAX_BYPASS_DURATION_MILLIS:
        raise ValueError(
            f"a bypass may not exceed {MAX_BYPASS_DURATION_MILLIS} ms; "
            "re-set the limit instead of suspending it for longer"
        )
    if not actor.strip():
        raise ValueError("a bypass must record who authorised it")
    if not reason.strip():
        raise ValueError("a bypass must record why it was authorised")

    now = (clock or SystemClock()).now_millis()
    expires_at = now + duration_millis
    await store.set_many(
        {
            keys.LOSS_LIMIT_BYPASS: encode_bool(True),
            keys.LOSS_LIMIT_BYPASS_EXPIRES_AT: encode_float(float(expires_at)),
            keys.LOSS_LIMIT_BYPASS_ACTOR: actor,
            keys.LOSS_LIMIT_BYPASS_REASON: reason,
            keys.LOSS_LIMIT_BYPASS_SET_AT: encode_float(float(now)),
        }
    )
    return expires_at


async def clear_loss_limit_bypass(
    store: StateStore, *, actor: str, clock: Clock | None = None
) -> None:
    """Re-arm the loss limit immediately, before its bypass would have lapsed.

    The attribution fields are rewritten rather than deleted, so the record
    shows who stood the limit back up as well as who stood it down.
    """
    if not actor.strip():
        raise ValueError("clearing a bypass must record who did it")
    now = (clock or SystemClock()).now_millis()
    await store.set_many(
        {
            keys.LOSS_LIMIT_BYPASS: encode_bool(False),
            keys.LOSS_LIMIT_BYPASS_EXPIRES_AT: "",
            keys.LOSS_LIMIT_BYPASS_ACTOR: actor,
            keys.LOSS_LIMIT_BYPASS_REASON: "bypass cleared",
            keys.LOSS_LIMIT_BYPASS_SET_AT: encode_float(float(now)),
        }
    )


async def read_loss_limit_bypass(store: StateStore) -> BypassRecord:
    """The bypass as it currently stands, including its audit fields."""
    raw = await store.get_many(
        (
            keys.LOSS_LIMIT_BYPASS,
            keys.LOSS_LIMIT_BYPASS_EXPIRES_AT,
            keys.LOSS_LIMIT_BYPASS_ACTOR,
            keys.LOSS_LIMIT_BYPASS_REASON,
            keys.LOSS_LIMIT_BYPASS_SET_AT,
        )
    )
    return BypassRecord(
        enabled=decode_bool(raw.get(keys.LOSS_LIMIT_BYPASS)),
        expires_at_millis=decode_float(raw.get(keys.LOSS_LIMIT_BYPASS_EXPIRES_AT)),
        actor=raw.get(keys.LOSS_LIMIT_BYPASS_ACTOR) or "",
        reason=raw.get(keys.LOSS_LIMIT_BYPASS_REASON) or "",
        set_at_millis=decode_float(raw.get(keys.LOSS_LIMIT_BYPASS_SET_AT)),
    )


async def reset_daily_loss_limit(store: StateStore) -> None:
    """Zero today's realized P&L AND both high water marks, re-arming the limit.

    Zeroing P&L alone does not clear a trailing limit. The floor is
    ``peak - limit``, so a banked peak holds the limit breached even at zero
    P&L: after a session that reached +30 with a 10 limit, the floor sits at
    +20, and P&L of 0 is still below it. Clearing the peaks drops the floor
    back to ``-limit``.

    The trading date and the day's notional are deliberately untouched — this
    re-arms the loss limit, it does not grant a fresh day.

    Only call this against a STOPPED engine. A running one holds these
    counters in memory and will persist straight over the reset.
    """
    await store.set_many(
        {
            keys.LIVE_REALIZED_PNL: encode_float(0.0),
            keys.SIMULATED_REALIZED_PNL: encode_float(0.0),
            keys.LIVE_PEAK_PNL: encode_float(0.0),
            keys.SIMULATED_PEAK_PNL: encode_float(0.0),
        }
    )


async def set_runtime_max_order_notional(store: StateStore, value: float | None) -> None:
    """Set the runtime per-order notional cap. ``None`` or ``<= 0`` clears it.

    A tuning knob, not a safety-loosening override: it takes effect on the
    engine's next refresh, in both live and simulated modes, without a
    restart. Cleared, the engine falls back to the configured limit.
    """
    await store.set(keys.RUNTIME_MAX_ORDER_NOTIONAL, _encode_override(value))


async def get_runtime_max_order_notional(store: StateStore) -> float | None:
    """The runtime per-order notional cap, or ``None`` when unset or unusable."""
    return decode_positive_float(await store.get(keys.RUNTIME_MAX_ORDER_NOTIONAL))


async def set_runtime_max_order_quantity(store: StateStore, value: float | None) -> None:
    """Set the runtime per-order quantity cap. ``None`` or ``<= 0`` clears it."""
    await store.set(keys.RUNTIME_MAX_ORDER_QUANTITY, _encode_override(value))


async def get_runtime_max_order_quantity(store: StateStore) -> float | None:
    """The runtime per-order quantity cap, or ``None`` when unset or unusable."""
    return decode_positive_float(await store.get(keys.RUNTIME_MAX_ORDER_QUANTITY))


def _encode_override(value: float | None) -> str:
    """Encode an optional override. The empty string is the cleared encoding."""
    if value is None or value <= 0:
        return ""
    return encode_float(float(value))
