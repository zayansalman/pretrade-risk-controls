"""The library's stable identifiers, and what the engine returns.

Two enumerations here are public interface and may not change without a major
version: :class:`ControlId`, which names each control, and :class:`RejectCode`,
which names each way an order can be refused. Everything else in the library —
message wording, field ordering, internal structure — is free to change.


A free-text reason is enough for a human reading a log and useless for
everything else. Downstream systems have to route on the outcome — a stale
quote is retried, a breached loss limit stops the strategy, a compliance
rejection is escalated rather than retried — and none of that can key off
prose that changes when someone improves the wording. Supervision and
surveillance obligations then require that the same decision be reconstructable
from the record months later.

So every rejection carries three things:

``code``
    A stable identifier, safe to route on, alert on and count. It is part of
    the library's public interface: the wording of ``message`` may change in
    any release, ``code`` may not.

``limit`` / ``observed``
    The threshold that applied and the value that breached it. This is what
    makes a rejection actionable without re-deriving it from the message, and
    what lets a desk chart how close it runs to each limit.

``message``
    The operator's sentence, built from the two numbers above.

The codes are grouped by the control taxonomy a risk function actually uses —
system state, capital preservation, compliance, order validation, price
reasonableness, message conduct, position and exposure — because that grouping
is how limits get owned and escalated.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ControlId(str, Enum):
    """Stable identifier for each control, in evaluation order.

    Separate from the human name a rejection message carries, and for the same
    reason reject codes are separate from their messages: the wording is for
    people, the identifier is for configuration and code. Switching a control
    off is done with one of these rather than a bare string, so a typo is a
    startup error instead of a control that silently never runs.
    """

    # -- System state ---------------------------------------------------
    KILL_SWITCH = "KILL_SWITCH"

    # -- Capital preservation -------------------------------------------
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"

    # -- Eligibility and compliance --------------------------------------
    INSTRUMENT_UNIVERSE = "INSTRUMENT_UNIVERSE"
    RESTRICTED_LIST = "RESTRICTED_LIST"
    SESSION_STATE = "SESSION_STATE"
    SHORT_SALE_LOCATE = "SHORT_SALE_LOCATE"

    # -- Order well-formedness (never disableable) ------------------------
    ORDER_QUANTITY = "ORDER_QUANTITY"
    ORDER_PRICE = "ORDER_PRICE"
    ORDER_NOTIONAL = "ORDER_NOTIONAL"

    # -- Order size caps --------------------------------------------------
    MAX_ORDER_QUANTITY = "MAX_ORDER_QUANTITY"
    MAX_ORDER_NOTIONAL = "MAX_ORDER_NOTIONAL"

    # -- Price reasonableness ---------------------------------------------
    QUOTE_AGE = "QUOTE_AGE"
    PRICE_BAND = "PRICE_BAND"
    EXECUTION_SLIPPAGE = "EXECUTION_SLIPPAGE"

    # -- Message conduct ---------------------------------------------------
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    ORDER_RATE = "ORDER_RATE"
    CONSECUTIVE_REJECTS = "CONSECUTIVE_REJECTS"
    REPEATED_EXECUTION = "REPEATED_EXECUTION"

    # -- Position and exposure ----------------------------------------------
    WORKING_ORDERS = "WORKING_ORDERS"
    OPEN_POSITIONS = "OPEN_POSITIONS"
    POSITION_LIMIT = "POSITION_LIMIT"
    SELF_MATCH_PREVENTION = "SELF_MATCH_PREVENTION"
    GROSS_EXPOSURE = "GROSS_EXPOSURE"
    DAILY_NOTIONAL_LIMIT = "DAILY_NOTIONAL_LIMIT"


#: Controls that decide whether an order is well formed at all. A negative
#: quantity or a zero limit price is malformed whatever a desk's risk appetite
#: is, so these cannot be switched off — there is no configuration under which
#: sending one is the intended behaviour.
ALWAYS_ON: frozenset[ControlId] = frozenset(
    {ControlId.ORDER_QUANTITY, ControlId.ORDER_PRICE, ControlId.ORDER_NOTIONAL}
)


class RejectCode(str, Enum):
    """Stable, machine-routable rejection identifiers.

    Inherits from ``str`` so the value serialises directly into logs, metrics
    labels and FIX ``Text``/``OrdRejReason`` mappings without conversion. A
    port should treat these as the wire values, not the enum member names.
    """

    # -- System state ---------------------------------------------------
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"

    # -- Capital preservation -------------------------------------------
    LOSS_LIMIT_BREACHED = "LOSS_LIMIT_BREACHED"
    DAILY_NOTIONAL_LIMIT_EXCEEDED = "DAILY_NOTIONAL_LIMIT_EXCEEDED"

    # -- Compliance and eligibility -------------------------------------
    INSTRUMENT_NOT_PERMITTED = "INSTRUMENT_NOT_PERMITTED"
    INSTRUMENT_RESTRICTED = "INSTRUMENT_RESTRICTED"
    MARKET_SESSION_NOT_OPEN = "MARKET_SESSION_NOT_OPEN"
    SHORT_SALE_LOCATE_MISSING = "SHORT_SALE_LOCATE_MISSING"
    SELF_MATCH_PREVENTED = "SELF_MATCH_PREVENTED"

    # -- Order validation (fat-finger) ----------------------------------
    ORDER_QUANTITY_NOT_POSITIVE = "ORDER_QUANTITY_NOT_POSITIVE"
    ORDER_PRICE_NOT_POSITIVE = "ORDER_PRICE_NOT_POSITIVE"
    ORDER_NOTIONAL_NOT_POSITIVE = "ORDER_NOTIONAL_NOT_POSITIVE"
    MAX_ORDER_QUANTITY_EXCEEDED = "MAX_ORDER_QUANTITY_EXCEEDED"
    MAX_ORDER_NOTIONAL_EXCEEDED = "MAX_ORDER_NOTIONAL_EXCEEDED"

    # -- Price reasonableness -------------------------------------------
    MARKET_DATA_UNAVAILABLE = "MARKET_DATA_UNAVAILABLE"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    PRICE_BAND_EXCEEDED = "PRICE_BAND_EXCEEDED"
    EXECUTION_SLIPPAGE_EXCEEDED = "EXECUTION_SLIPPAGE_EXCEEDED"

    # -- Message conduct -------------------------------------------------
    DUPLICATE_ORDER = "DUPLICATE_ORDER"
    ORDER_RATE_EXCEEDED = "ORDER_RATE_EXCEEDED"
    CONSECUTIVE_REJECT_LIMIT_EXCEEDED = "CONSECUTIVE_REJECT_LIMIT_EXCEEDED"
    REPEATED_EXECUTION_THROTTLE = "REPEATED_EXECUTION_THROTTLE"

    # -- Position and exposure -------------------------------------------
    MAX_WORKING_ORDERS_EXCEEDED = "MAX_WORKING_ORDERS_EXCEEDED"
    MAX_OPEN_POSITIONS_EXCEEDED = "MAX_OPEN_POSITIONS_EXCEEDED"
    MAX_POSITION_EXCEEDED = "MAX_POSITION_EXCEEDED"
    GROSS_EXPOSURE_LIMIT_EXCEEDED = "GROSS_EXPOSURE_LIMIT_EXCEEDED"


@dataclass(frozen=True)
class RiskDecision:
    """The engine's verdict on one order.

    ``accepted`` is the only field a caller must read to decide whether to
    send. Everything else describes why not.
    """

    accepted: bool
    code: RejectCode | None = None
    control: str = ""
    message: str = ""
    limit: float | None = None
    observed: float | None = None

    def __bool__(self) -> bool:
        """``if decision:`` reads as "if the order may be sent"."""
        return self.accepted

    @classmethod
    def accept(cls) -> RiskDecision:
        return cls(accepted=True)

    @classmethod
    def reject(
        cls,
        code: RejectCode,
        control: str,
        message: str,
        *,
        limit: float | None = None,
        observed: float | None = None,
    ) -> RiskDecision:
        return cls(
            accepted=False,
            code=code,
            control=control,
            message=message,
            limit=limit,
            observed=observed,
        )


#: The single accepted verdict, shared rather than reallocated per order —
#: the decision is immutable and the accept path is the hot one.
ACCEPTED = RiskDecision(accepted=True)
