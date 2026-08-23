"""Hibachi implementation of the PerpVenue interface."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from deltabot.models import (
    Balance,
    FundingSnapshot,
    MarketSpec,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    PositionState,
    Side,
    TopOfBook,
)
from deltabot.venues.base import PerpVenue, VenueError
from deltabot.venues.hibachi.client import HibachiClient

log = logging.getLogger(__name__)

# Hibachi settles funding every 8 hours at 00:00/08:00/16:00 UTC (per
# docs.hibachi.xyz; confirmed by settlement timestamps on 8h boundaries in
# /market/data/funding-rates). The estimate from /market/data/prices is the
# rate for the next settlement. The adapter additionally infers the actual
# interval from settlement history at runtime, so a venue-side change to the
# cadence corrects itself; this constant is only the fallback.
DEFAULT_FUNDING_INTERVAL_HOURS = Decimal(8)

_STATUS_MAP = {
    "PENDING": OrderStatus.PENDING,
    "CHILD_PENDING": OrderStatus.PENDING,
    "PLACED": OrderStatus.PLACED,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}


class HibachiVenue(PerpVenue):
    name = "hibachi"

    def __init__(
        self,
        api_key: str,
        account_id: int,
        private_key: str,
        api_url: str | None = None,
        data_api_url: str | None = None,
        # Max fee rate accepted per order, as a decimal fraction (0.0005 = 5 bps).
        # Must be >= your account's taker fee rate or orders are rejected.
        max_fees_percent: Decimal = Decimal("0.0005"),
        funding_interval_hours: Decimal = DEFAULT_FUNDING_INTERVAL_HOURS,
    ):
        kwargs = {}
        if api_url:
            kwargs["api_url"] = api_url
        if data_api_url:
            kwargs["data_api_url"] = data_api_url
        self.client = HibachiClient(api_key, account_id, private_key, **kwargs)
        self.max_fees_percent = max_fees_percent
        self.funding_interval_hours = funding_interval_hours
        self._inferred_intervals: dict[str, Decimal] = {}

    async def close(self) -> None:
        await self.client.close()

    async def get_market(self, symbol: str) -> MarketSpec:
        c = await self.client.contract(symbol)
        return MarketSpec(
            venue=self.name,
            symbol=symbol,
            step_size=Decimal(str(c["stepSize"])),
            tick_size=Decimal(str(c["tickSize"])),
            min_order_size=Decimal(str(c["minOrderSize"])),
            min_notional=Decimal(str(c["minNotional"])),
            extra={
                "contract_id": int(c["id"]),
                "underlying_decimals": int(c["underlyingDecimals"]),
                "settlement_decimals": int(c["settlementDecimals"]),
                "maintenance_margin_rate": str(c["maintenanceMarginRate"]),
                "initial_margin_rate": str(c["initialMarginRate"]),
            },
        )

    async def _funding_interval(self, symbol: str) -> Decimal:
        """Infer the settlement interval from consecutive funding timestamps;
        fall back to the configured value when history is unavailable."""
        if symbol in self._inferred_intervals:
            return self._inferred_intervals[symbol]
        interval = self.funding_interval_hours
        try:
            history = await self.client.funding_rates(symbol, limit=10)
            timestamps = sorted(
                float(e["fundingTimestamp"]) for e in history
                if e.get("fundingTimestamp") is not None
            )
            diffs = [
                b - a for a, b in zip(timestamps, timestamps[1:]) if b - a > 0
            ]
            if diffs:
                diffs.sort()
                inferred = Decimal(str(diffs[len(diffs) // 2])) / 3600  # median
                # accept only sane cadences (15min..24h)
                if Decimal("0.25") <= inferred <= 24:
                    if inferred != self.funding_interval_hours:
                        log.warning(
                            "hibachi: funding interval inferred as %sh "
                            "(configured %sh); using inferred",
                            inferred, self.funding_interval_hours,
                        )
                    interval = inferred
        except VenueError as e:
            log.debug("hibachi: funding interval inference skipped: %s", e)
        self._inferred_intervals[symbol] = interval
        return interval

    async def get_funding(self, symbol: str) -> FundingSnapshot:
        prices = await self.client.prices(symbol)
        est = prices.get("fundingRateEstimation") or {}
        next_ts = est.get("nextFundingTimestamp")
        return FundingSnapshot(
            venue=self.name,
            symbol=symbol,
            rate=Decimal(str(est.get("estimatedFundingRate", "0"))),
            interval_hours=await self._funding_interval(symbol),
            mark_price=Decimal(str(prices["markPrice"])),
            next_funding_ts=float(next_ts) if next_ts is not None else None,
        )

    async def get_top_of_book(self, symbol: str) -> TopOfBook:
        prices = await self.client.prices(symbol)
        return TopOfBook(
            venue=self.name,
            symbol=symbol,
            bid=Decimal(str(prices["bidPrice"])),
            ask=Decimal(str(prices["askPrice"])),
        )

    async def get_balance(self) -> Balance:
        info = await self.client.account_info()
        # "balance" is settled cash; add unrealized PnL so equity reflects the
        # account's true liquidation-relevant value.
        equity = Decimal(str(info["balance"])) + Decimal(
            str(info.get("totalUnrealizedPnl", "0"))
        )
        # Free collateral: maximalWithdraw is the venue's own number for what
        # is not consumed by position/order margin.
        available = Decimal(str(info.get("maximalWithdraw", equity)))
        return Balance(venue=self.name, equity=equity, available=available)

    async def get_position(self, symbol: str) -> PositionState:
        info = await self.client.account_info()
        for p in info.get("positions", []):
            if p.get("symbol") != symbol:
                continue
            qty = Decimal(str(p["quantity"]))
            direction = str(p.get("direction", "")).lower()
            if direction in ("short", "sell", "ask") and qty > 0:
                qty = -qty
            funding_pnl = Decimal(str(p.get("unrealizedFundingPnl", "0")))
            trading_pnl = Decimal(str(p.get("unrealizedTradingPnl", "0")))
            return PositionState(
                venue=self.name,
                symbol=symbol,
                qty=qty,
                entry_price=Decimal(str(p["openPrice"])) if p.get("openPrice") else None,
                mark_price=Decimal(str(p["markPrice"])) if p.get("markPrice") else None,
                unrealized_pnl=funding_pnl + trading_pnl,
            )
        return PositionState(venue=self.name, symbol=symbol, qty=Decimal(0))

    async def place_order(self, request: OrderRequest) -> OrderResult:
        body = await self.client.place_order(
            symbol=request.symbol,
            quantity=request.qty,
            is_buy=request.side is Side.BUY,
            max_fees_percent=self.max_fees_percent,
            price=request.price if request.order_type is OrderType.LIMIT else None,
            reduce_only=request.reduce_only,
            client_id=request.client_tag,
        )
        order_id = body.get("orderId")
        if order_id is None:
            raise VenueError(self.name, f"no orderId in order response: {body}")
        # Market orders fill (or die) server-side but not always instantly:
        # poll briefly until the status is terminal so a still-PENDING fill
        # isn't misread as a failed leg by the pair executor.
        status_body: dict = {}
        status = OrderStatus.UNKNOWN
        for attempt in range(6):
            if attempt:
                await asyncio.sleep(0.5)
            try:
                status_body = await self.client.order_status(order_id)
            except VenueError:
                log.warning("hibachi: could not fetch status for order %s", order_id)
                continue
            status = _STATUS_MAP.get(str(status_body.get("status", "")), OrderStatus.UNKNOWN)
            if status.is_terminal:
                break
            if request.order_type is OrderType.LIMIT and status is OrderStatus.PLACED:
                break  # resting limit order: PLACED is its steady state
        total = status_body.get("totalQuantity")
        available = status_body.get("availableQuantity")
        filled = Decimal(0)
        if total is not None and available is not None:
            filled = Decimal(str(total)) - Decimal(str(available))
        elif status is OrderStatus.FILLED:
            filled = request.qty
        price = status_body.get("price")
        return OrderResult(
            venue=self.name,
            symbol=request.symbol,
            order_id=str(order_id),
            status=status,
            filled_qty=filled,
            avg_price=Decimal(str(price)) if price is not None else None,
            raw={"place": body, "status": status_body},
        )

    async def cancel_all(self, symbol: str) -> None:
        await self.client.cancel_all_orders(symbol)

    async def healthcheck(self) -> bool:
        try:
            await self.client.exchange_info()
            return True
        except VenueError:
            return False
