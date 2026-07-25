"""The order under evaluation, and the context the controls need to judge it.

One dataclass carries the order itself plus the surrounding state the risk
controls read: the touch, the session phase, the current position, the working
order count. That state is supplied BY THE CALLER on every request rather than
tracked here, and the reason is worth stating plainly: this library is not a
position keeper. The order management system already knows the firm's
positions and working orders, has already reconciled them against the venue's
drop copy, and is the only component entitled to be authoritative about them.
A risk layer that kept its own shadow copy would eventually disagree with the
book of record, and a risk control that disagrees with reality is worse than
no control at all.

Fields default to "not supplied". A control whose input is missing is not
silently skipped — if its limit is configured, the engine rejects the order
rather than waving it through unchecked. See
:class:`~pretrade_risk.limits.RiskLimits`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pretrade_risk.encoding import encode_float


class Side(str, Enum):
    """Order side.

    ``SELL_SHORT`` is distinguished from ``SELL`` because it carries
    obligations a long sale does not — a locate must be secured before a short
    sale is entered, which is a pre-order-entry regulatory requirement of
    exactly the kind the market access rules put on the pre-trade path.
    """

    BUY = "BUY"
    SELL = "SELL"
    SELL_SHORT = "SELL_SHORT"

    @property
    def is_sell(self) -> bool:
        return self in (Side.SELL, Side.SELL_SHORT)

    @property
    def signed_multiplier(self) -> float:
        """+1 for buys, -1 for sells — for projecting the resulting position."""
        return -1.0 if self.is_sell else 1.0


class SessionState(str, Enum):
    """The venue's trading phase for this instrument, as the caller sees it.

    ``UNKNOWN`` is explicit rather than implied by ``None`` so that "I could
    not determine the session" is a state the caller states deliberately, and
    one the session control can be configured to refuse.
    """

    OPEN = "OPEN"  # continuous trading
    AUCTION = "AUCTION"  # opening/closing/volatility auction
    PRE_OPEN = "PRE_OPEN"  # order entry accepted, no matching
    CLOSED = "CLOSED"
    HALTED = "HALTED"  # regulatory or volatility halt
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class OrderRequest:
    """One order, plus the market and book context the controls evaluate it against.

    Only ``symbol``, ``side`` and ``quantity`` are required. Everything else
    describes context that specific controls need; supply what the controls
    you have configured require.
    """

    symbol: str
    side: Side
    quantity: float

    # -- The order ------------------------------------------------------
    #: ``None`` denotes a market order.
    limit_price: float | None = None

    # -- Market context --------------------------------------------------
    #: The price the trading decision was taken against. The slippage control
    #: measures drift away from this; the notional valuation falls back to it.
    decision_price: float | None = None
    #: The prevailing touch at the moment of submission.
    best_bid: float | None = None
    best_ask: float | None = None
    #: The reference price the price band is measured around — typically the
    #: last trade or the mid. Falls back to the mid of the touch.
    reference_price: float | None = None
    #: Age of the quotes above. Stale market data is the input that makes a
    #: price control lie, so it is checked before the price controls run.
    quote_age_millis: int | None = None
    session_state: SessionState = SessionState.UNKNOWN

    # -- Book context, from the order management system --------------------
    #: Distinct instruments currently held, across the book.
    open_positions: int = 0
    #: Orders currently working at the venue, across the book.
    working_orders: int = 0
    #: Signed position in THIS instrument before this order: positive long,
    #: negative short.
    position_quantity: float = 0.0
    #: Portfolio gross exposure in USD before this order — the sum of the
    #: absolute value of every position, which is the exposure measure a limit
    #: is normally set against because it does not net long against short.
    gross_exposure_usd: float = 0.0
    #: The firm's OWN resting quantity on the opposite side of this
    #: instrument. Non-zero means this order could trade against the firm
    #: itself.
    opposing_resting_quantity: float = 0.0

    # -- Compliance context ------------------------------------------------
    #: Whether a borrow has been secured for a short sale.
    locate_secured: bool = False

    def valuation_price(self) -> float | None:
        """The price this order's notional is computed at, or ``None``.

        Precedence is fixed and documented so that the notional a limit is
        tested against is reproducible from the request alone: the limit price
        if there is one, else the decision price, else the far touch the order
        would cross (the ask for a buy, the bid for a sell).

        The far touch, not the near one, because a market order's cost is what
        it pays to cross — valuing a buy at the bid would understate it.
        """
        if self.limit_price is not None:
            return self.limit_price
        if self.decision_price is not None:
            return self.decision_price
        return self.best_bid if self.side.is_sell else self.best_ask

    def notional_usd(self) -> float | None:
        """Quantity times valuation price, or ``None`` if it cannot be priced."""
        price = self.valuation_price()
        if price is None:
            return None
        return self.quantity * price

    def band_reference_price(self) -> float | None:
        """The price the band control measures deviation from.

        The explicit reference if supplied, else the mid of a two-sided touch.
        A one-sided book gives no mid, so it yields ``None`` and the control
        reports its input as unavailable rather than inventing a reference.
        """
        if self.reference_price is not None:
            return self.reference_price
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return None

    def projected_position(self) -> float:
        """The signed position in this instrument if this order fully fills."""
        return self.position_quantity + self.side.signed_multiplier * self.quantity

    def fingerprint(self) -> str:
        """Identity for duplicate suppression: same instrument, side, size, price.

        Built from the encoded numbers rather than the host language's default
        formatting so two ports agree on whether two orders are the same one.
        A client order ID is deliberately NOT part of it — a resubmission
        carries a fresh ID, and catching resubmissions is the whole point.
        """
        price = "MKT" if self.limit_price is None else encode_float(self.limit_price)
        return f"{self.symbol}|{self.side.value}|{encode_float(self.quantity)}|{price}"
