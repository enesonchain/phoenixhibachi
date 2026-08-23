"""In-memory fake venue for engine tests: scripted market data, instant fills."""

from __future__ import annotations

from decimal import Decimal

from deltabot.models import (
    Balance,
    FundingSnapshot,
    MarketSpec,
    OrderRequest,
    OrderResult,
    OrderStatus,
    PositionState,
    TopOfBook,
)
from deltabot.venues.base import OrderRejected, PerpVenue


class FakeVenue(PerpVenue):
    def __init__(
        self,
        name: str,
        funding_rate: str = "0",
        mark: str = "100",
        equity: str = "10000",
        step: str = "0.001",
        interval_hours: int = 1,
    ):
        self.name = name
        self.funding_rate = Decimal(funding_rate)
        self.mark = Decimal(mark)
        self.equity = Decimal(equity)
        self.step = Decimal(step)
        self.interval_hours = Decimal(interval_hours)
        self.qty = Decimal(0)
        self.orders: list[OrderRequest] = []
        self.fail_next_order: Exception | None = None
        self.reject_reduce_only = False

    async def get_market(self, symbol: str) -> MarketSpec:
        return MarketSpec(
            venue=self.name, symbol=symbol, step_size=self.step,
            tick_size=Decimal("0.1"), min_order_size=self.step,
            min_notional=Decimal("10"),
        )

    async def get_funding(self, symbol: str) -> FundingSnapshot:
        return FundingSnapshot(
            venue=self.name, symbol=symbol, rate=self.funding_rate,
            interval_hours=self.interval_hours, mark_price=self.mark,
        )

    async def get_top_of_book(self, symbol: str) -> TopOfBook:
        half_spread = self.mark * Decimal("0.0001")
        return TopOfBook(
            venue=self.name, symbol=symbol,
            bid=self.mark - half_spread, ask=self.mark + half_spread,
        )

    async def get_balance(self) -> Balance:
        return Balance(venue=self.name, equity=self.equity, available=self.equity)

    async def get_position(self, symbol: str) -> PositionState:
        return PositionState(
            venue=self.name, symbol=symbol, qty=self.qty,
            mark_price=self.mark,
        )

    async def place_order(self, request: OrderRequest) -> OrderResult:
        if self.fail_next_order is not None:
            error, self.fail_next_order = self.fail_next_order, None
            raise error
        if request.reduce_only and self.reject_reduce_only:
            raise OrderRejected(self.name, "reduce-only rejected (test)")
        self.orders.append(request)
        self.qty += request.qty * request.side.sign
        return OrderResult(
            venue=self.name, symbol=request.symbol, order_id=f"{self.name}-{len(self.orders)}",
            status=OrderStatus.FILLED, filled_qty=request.qty, avg_price=self.mark,
        )

    async def cancel_all(self, symbol: str) -> None:
        return None
