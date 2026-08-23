"""Venue-agnostic domain models shared by all exchange adapters and the strategy engine.

All quantities and prices are Decimal end-to-end. Signed quantity convention:
positive = long, negative = short.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from decimal import Decimal

HOURS_PER_YEAR = Decimal(24 * 365)


class Side(enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(enum.Enum):
    PENDING = "PENDING"
    PLACED = "PLACED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


@dataclass(frozen=True)
class MarketSpec:
    """Static per-market metadata needed to size and round orders."""

    venue: str
    symbol: str
    step_size: Decimal  # quantity increment
    tick_size: Decimal  # price increment
    min_order_size: Decimal
    min_notional: Decimal
    # Venue-specific extras (e.g. Hibachi contract id / decimals) live here so
    # adapters can round-trip them without widening the shared model.
    extra: dict = field(default_factory=dict, hash=False, compare=False)

    def round_qty(self, qty: Decimal) -> Decimal:
        """Round a quantity down to the market's step size."""
        if self.step_size <= 0:
            return qty
        return (qty // self.step_size) * self.step_size

    def round_price(self, price: Decimal) -> Decimal:
        if self.tick_size <= 0:
            return price
        return (price // self.tick_size) * self.tick_size


@dataclass(frozen=True)
class FundingSnapshot:
    """A venue's funding rate for one market at a point in time.

    ``rate`` is the rate paid per funding interval (fraction, not percent):
    positive means longs pay shorts, negative means shorts pay longs.
    """

    venue: str
    symbol: str
    rate: Decimal
    interval_hours: Decimal
    mark_price: Decimal
    next_funding_ts: float | None = None  # unix seconds
    ts: float = field(default_factory=time.time)

    @property
    def hourly_rate(self) -> Decimal:
        if self.interval_hours <= 0:
            return Decimal(0)
        return self.rate / self.interval_hours

    @property
    def annualized(self) -> Decimal:
        """Simple (non-compounded) annualized rate for cross-venue comparison."""
        return self.hourly_rate * HOURS_PER_YEAR


@dataclass(frozen=True)
class TopOfBook:
    venue: str
    symbol: str
    bid: Decimal
    ask: Decimal
    ts: float = field(default_factory=time.time)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        mid = self.mid
        if mid == 0:
            return Decimal(0)
        return (self.ask - self.bid) / mid * 10_000


@dataclass(frozen=True)
class PositionState:
    venue: str
    symbol: str
    qty: Decimal  # signed: + long, - short; 0 = flat
    entry_price: Decimal | None = None
    mark_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def notional(self) -> Decimal | None:
        if self.mark_price is None:
            return None
        return abs(self.qty) * self.mark_price


@dataclass(frozen=True)
class Balance:
    venue: str
    equity: Decimal  # total account value (settlement currency, USD-ish)
    available: Decimal  # free collateral for new positions


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    qty: Decimal  # always positive; direction comes from side
    order_type: OrderType = OrderType.MARKET
    price: Decimal | None = None  # required for LIMIT
    reduce_only: bool = False
    # Best-effort client tag; venues that support client ids echo it back.
    client_tag: str | None = None

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError(f"order qty must be positive, got {self.qty}")
        if self.order_type is OrderType.LIMIT and self.price is None:
            raise ValueError("LIMIT order requires a price")


@dataclass(frozen=True)
class OrderResult:
    venue: str
    symbol: str
    order_id: str
    status: OrderStatus
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    raw: dict = field(default_factory=dict, hash=False, compare=False)
