"""Paper venue: live market data from a real adapter, simulated account.

Wraps any PerpVenue. Reads (funding, top-of-book, market spec) pass through
to the real venue; writes (orders) fill instantly at top-of-book plus taker
fees, against a local balance. Lets the full engine run unfunded and keyless.
"""

from __future__ import annotations

import itertools
import logging
from decimal import Decimal

from deltabot.models import (
    Balance,
    FundingSnapshot,
    MarketSpec,
    OrderRequest,
    OrderResult,
    OrderStatus,
    PositionState,
    Side,
    TopOfBook,
)
from deltabot.venues.base import OrderRejected, PerpVenue

log = logging.getLogger(__name__)


class PaperVenue(PerpVenue):
    def __init__(
        self,
        real: PerpVenue,
        start_balance: Decimal = Decimal("10000"),
        taker_fee_bps: Decimal = Decimal("4"),
    ):
        self.real = real
        self.name = real.name  # keep the underlying name so pair logic is unchanged
        self.cash = start_balance
        self.taker_fee_bps = taker_fee_bps
        self._qty: dict[str, Decimal] = {}
        self._entry_notional: dict[str, Decimal] = {}
        self._order_ids = itertools.count(1)

    async def start(self) -> None:
        await self.real.start()

    async def close(self) -> None:
        await self.real.close()

    # ---------------------------------------------------------- pass-through

    async def get_market(self, symbol: str) -> MarketSpec:
        return await self.real.get_market(symbol)

    async def get_funding(self, symbol: str) -> FundingSnapshot:
        return await self.real.get_funding(symbol)

    async def get_top_of_book(self, symbol: str) -> TopOfBook:
        return await self.real.get_top_of_book(symbol)

    async def healthcheck(self) -> bool:
        return await self.real.healthcheck()

    # ------------------------------------------------------------- simulated

    async def get_balance(self) -> Balance:
        equity = self.cash
        for symbol, qty in self._qty.items():
            if qty == 0:
                continue
            book = await self.real.get_top_of_book(symbol)
            equity += qty * book.mid - self._entry_notional.get(symbol, Decimal(0))
        return Balance(venue=self.name, equity=equity, available=equity)

    async def get_position(self, symbol: str) -> PositionState:
        qty = self._qty.get(symbol, Decimal(0))
        if qty == 0:
            return PositionState(venue=self.name, symbol=symbol, qty=Decimal(0))
        book = await self.real.get_top_of_book(symbol)
        entry_notional = self._entry_notional.get(symbol, Decimal(0))
        entry_price = entry_notional / qty if qty != 0 else None
        return PositionState(
            venue=self.name,
            symbol=symbol,
            qty=qty,
            entry_price=entry_price,
            mark_price=book.mid,
            unrealized_pnl=qty * book.mid - entry_notional,
        )

    async def place_order(self, request: OrderRequest) -> OrderResult:
        book = await self.real.get_top_of_book(request.symbol)
        price = book.ask if request.side is Side.BUY else book.bid
        if price <= 0:
            raise OrderRejected(self.name, "no liquidity in simulated fill")
        signed = request.qty * request.side.sign

        current = self._qty.get(request.symbol, Decimal(0))
        if request.reduce_only:
            if current == 0 or current * signed > 0:
                raise OrderRejected(self.name, "reduce-only order would increase position")
            # Real venues clamp reduce-only size to the open position; a
            # reduce-only order must never flip through zero.
            if abs(signed) > abs(current):
                signed = -current
                request = OrderRequest(
                    symbol=request.symbol, side=request.side, qty=abs(signed),
                    order_type=request.order_type, price=request.price,
                    reduce_only=True, client_tag=request.client_tag,
                )

        fee = request.qty * price * self.taker_fee_bps / Decimal(10_000)
        self.cash -= fee
        # Realize PnL on the portion that closes existing exposure.
        if current != 0 and current * signed < 0:
            closing = min(abs(signed), abs(current))
            avg_entry = self._entry_notional[request.symbol] / current
            direction = Decimal(1 if current > 0 else -1)
            self.cash += (price - avg_entry) * closing * direction
            self._entry_notional[request.symbol] = avg_entry * (current + signed) if abs(signed) < abs(current) else Decimal(0)
            if abs(signed) > abs(current):
                # flipped through zero; remainder opens the other way
                remainder = signed + current
                self._entry_notional[request.symbol] = remainder * price
        else:
            self._entry_notional[request.symbol] = (
                self._entry_notional.get(request.symbol, Decimal(0)) + signed * price
            )
        self._qty[request.symbol] = current + signed

        log.info(
            "[paper:%s] %s %s %s @ %s (fee %.4f) -> pos %s",
            self.name, request.side.value, request.qty, request.symbol, price, fee,
            self._qty[request.symbol],
        )
        return OrderResult(
            venue=self.name,
            symbol=request.symbol,
            order_id=f"paper-{next(self._order_ids)}",
            status=OrderStatus.FILLED,
            filled_qty=request.qty,
            avg_price=price,
        )

    async def cancel_all(self, symbol: str) -> None:
        return None
