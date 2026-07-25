"""The limit set: one field per control, every control off until configured.

Two conventions run through this whole structure.

**A limit of ``None`` means the control is not armed.** There are no implicit
defaults, because a default limit is a limit nobody chose, and a risk limit
nobody chose is one nobody owns. Configuring a field is the act of turning its
control on.

**An armed control that cannot be evaluated rejects the order.** If a price
band is configured and no reference price arrives, the engine returns
``MARKET_DATA_UNAVAILABLE`` rather than passing the order through unchecked.
The alternative — skipping quietly — is the failure mode where a desk believes
it is protected by a control that has not fired in months because its input
stopped arriving. If a control should not apply, leave its limit unset; do not
starve it of data.

Limits are immutable. Changing one means constructing a new
:class:`RiskLimits`, which keeps the limit set a value that can be logged,
compared and reproduced from a configuration file. The two limits a desk
routinely retunes intraday — the per-order caps — are additionally settable at
runtime through the operator control plane without a restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pretrade_risk.clock import MILLIS_PER_DAY
from pretrade_risk.order import SessionState


@dataclass(frozen=True)
class RiskLimits:
    """Every configurable threshold, grouped by the control family that owns it."""

    # -- System state ----------------------------------------------------
    #: While this file exists, every order is rejected. A file rather than a
    #: flag in the store so that it can be tripped by anyone with shell access
    #: — including a monitoring system, a scheduled job, or an operator whose
    #: console is the thing that has failed — with no dependency on the
    #: process being healthy enough to read its own configuration.
    #: ``None`` disables it.
    kill_switch_path: Path | None = None

    # -- Capital preservation ---------------------------------------------
    #: Maximum drawdown in realized P&L, measured from the session's high
    #: water mark, before trading halts for the day.
    daily_loss_limit_usd: float | None = None
    #: Maximum cumulative notional the desk may send in one trading day.
    daily_notional_limit_usd: float | None = None

    # -- Order validation (fat-finger) -------------------------------------
    max_order_quantity: float | None = None
    max_order_notional_usd: float | None = None

    # -- Price reasonableness ----------------------------------------------
    #: Price collar: the maximum fraction by which an order's price may
    #: deviate from the reference price, in either direction. 0.02 is 2%.
    #: Two-sided deliberately — an absurdly low buy is as much a sign of a
    #: broken price feed as an absurdly high one.
    price_band_fraction: float | None = None
    #: Maximum ADVERSE drift between the price the decision was taken at and
    #: the price now available. Distinct from the band above: the band asks
    #: "is this price sane?", this asks "is the opportunity still there?".
    #: One-sided, because a move in the desk's favour is not a risk event.
    max_execution_slippage: float | None = None
    #: Maximum age of the quotes accompanying the order.
    max_quote_age_millis: int | None = None

    # -- Message conduct ----------------------------------------------------
    #: Window within which an identical order counts as a resubmission.
    duplicate_window_millis: int | None = None
    #: Maximum orders sent within ``order_rate_window_millis``.
    max_orders_per_window: int | None = None
    order_rate_window_millis: int = 1_000
    #: Consecutive rejections before the strategy is throttled and must be
    #: re-armed deliberately. Stops a strategy from hammering a venue with
    #: orders that keep being refused — the pattern that turns one bad
    #: deployment into a compounding loss.
    max_consecutive_rejects: int | None = None
    #: Executions permitted before a human must re-arm the strategy. This is
    #: the repeated automated execution throttle: a strategy that keeps
    #: filling and re-entering the market unattended is stopped and held until
    #: somebody looks at it. Unlike the counters above it is not reset by the
    #: trading day rolling — only :meth:`PreTradeRiskEngine.rearm` clears it,
    #: because "without human intervention" means exactly that.
    max_executions_without_review: int | None = None

    # -- Position and exposure ----------------------------------------------
    max_working_orders: int | None = None
    max_open_positions: int | None = None
    #: Maximum absolute position in any single instrument after this order.
    max_position_quantity: float | None = None
    #: Maximum portfolio gross exposure after this order.
    max_gross_exposure_usd: float | None = None
    #: Reject orders that could trade against the firm's own resting orders.
    prevent_self_match: bool = False
    #: Reject short sales that do not carry a secured borrow.
    require_short_sale_locate: bool = False

    # -- Eligibility ---------------------------------------------------------
    #: The tradeable universe. ``None`` means no universe check; an empty set
    #: means nothing is tradeable, which is a valid way to stand a desk down.
    permitted_instruments: frozenset[str] | None = None
    #: Instruments the desk may not trade — a compliance restricted list,
    #: an issuer in a blackout, a name behind an information barrier.
    #: Checked after the universe, and it wins: presence here always blocks.
    restricted_instruments: frozenset[str] = field(default_factory=frozenset)
    #: Session phases in which orders may be entered. ``None`` means the
    #: control is not armed.
    tradeable_session_states: frozenset[SessionState] | None = None

    # -- Trading day ----------------------------------------------------------
    #: UTC time of day at which the trading day rolls, in milliseconds after
    #: UTC midnight. 0 is a UTC calendar day. A futures desk whose session
    #: opens at 17:00 US/Central sets this to 22 hours so that the daily loss
    #: limit does not reset in the middle of a live evening session.
    session_roll_millis: int = 0

    def __post_init__(self) -> None:
        """Reject an incoherent limit set at construction.

        A limit that is present but nonsensical — a negative cap, a zero-width
        window — is a configuration error, and the moment to fail is startup,
        not the first order of the day.
        """
        for name in (
            "daily_loss_limit_usd",
            "daily_notional_limit_usd",
            "max_order_quantity",
            "max_order_notional_usd",
            "price_band_fraction",
            "max_execution_slippage",
            "max_position_quantity",
            "max_gross_exposure_usd",
        ):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive when set, got {value!r}")

        for name in (
            "max_quote_age_millis",
            "duplicate_window_millis",
            "max_orders_per_window",
            "max_consecutive_rejects",
            "max_executions_without_review",
            "max_working_orders",
            "max_open_positions",
        ):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be a positive integer when set, got {value!r}")

        if self.order_rate_window_millis <= 0:
            raise ValueError("order_rate_window_millis must be positive")
        if not 0 <= self.session_roll_millis < MILLIS_PER_DAY:
            raise ValueError("session_roll_millis must be within [0, 86_400_000)")
