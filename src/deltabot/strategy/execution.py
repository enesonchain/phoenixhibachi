"""Two-legged execution with one-legged-exposure protection.

Entry: place the harder (less liquid) leg first; only once it is confirmed
filled place the hedge leg. If the hedge leg fails, immediately unwind the
first leg so the book never carries naked directional risk longer than one
round-trip of retries.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal

from deltabot.models import OrderRequest, OrderResult, OrderStatus, Side
from deltabot.venues.base import PerpVenue, VenueError

log = logging.getLogger(__name__)


class ExecutionIncident(Exception):
    """A leg could not be completed; the engine must cool down and reconcile."""


# Seconds to wait between order retries; tests shrink this.
RETRY_BACKOFF: tuple[float, ...] = (2.0, 4.0, 8.0)


@dataclass
class PairFill:
    short_result: OrderResult
    long_result: OrderResult
    qty: Decimal


def _filled_enough(result: OrderResult, qty: Decimal) -> bool:
    # Market/IOC orders may fill fractionally short of the request due to
    # step rounding at the venue; tolerate 1% shortfall, reconcile later.
    return result.status is OrderStatus.FILLED or (
        result.filled_qty >= qty * Decimal("0.99")
    )


async def _place_market(
    venue: PerpVenue,
    symbol: str,
    side: Side,
    qty: Decimal,
    reduce_only: bool = False,
    attempts: int = 3,
) -> OrderResult:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = await venue.place_order(
                OrderRequest(symbol=symbol, side=side, qty=qty, reduce_only=reduce_only)
            )
            if result.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                raise ExecutionIncident(
                    f"{venue.name} rejected {side.value} {qty} {symbol}: {result.raw}"
                )
            return result
        except VenueError as e:
            last_error = e
            log.warning("%s order attempt %d/%d failed: %s", venue.name, attempt, attempts, e)
            await asyncio.sleep(RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)])
    raise ExecutionIncident(
        f"{venue.name} {side.value} {qty} {symbol} failed after {attempts} attempts: {last_error}"
    )


class PairExecutor:
    """Executes and unwinds delta-neutral pairs across two venues."""

    def __init__(self, venues: dict[str, PerpVenue], symbols: dict[str, str]):
        self.venues = venues
        self.symbols = symbols  # venue name -> market symbol on that venue

    def _leg(self, venue_name: str) -> tuple[PerpVenue, str]:
        return self.venues[venue_name], self.symbols[venue_name]

    async def open_pair(
        self,
        short_venue: str,
        long_venue: str,
        qty: Decimal,
        first_leg: str,
    ) -> PairFill:
        """Open short on ``short_venue`` and long on ``long_venue``, first_leg first."""
        legs = {
            short_venue: Side.SELL,
            long_venue: Side.BUY,
        }
        if first_leg not in legs:
            first_leg = short_venue
        second_leg = long_venue if first_leg == short_venue else short_venue

        first_venue, first_symbol = self._leg(first_leg)
        second_venue, second_symbol = self._leg(second_leg)

        log.info(
            "opening pair: %s %s / %s %s qty=%s (first: %s)",
            legs[short_venue].value, short_venue, legs[long_venue].value, long_venue,
            qty, first_leg,
        )

        first_result = await _place_market(first_venue, first_symbol, legs[first_leg], qty)
        if not _filled_enough(first_result, qty):
            # Nothing (or almost nothing) filled — cancel and abort cleanly.
            await self._safe_cancel(first_venue, first_symbol)
            if first_result.filled_qty > 0:
                await self._unwind(first_venue, first_symbol, legs[first_leg], first_result.filled_qty)
            raise ExecutionIncident(
                f"first leg on {first_leg} filled {first_result.filled_qty}/{qty}; aborted"
            )

        # Hedge exactly what the first leg actually filled.
        hedge_qty = first_result.filled_qty if first_result.filled_qty > 0 else qty
        try:
            second_result = await _place_market(
                second_venue, second_symbol, legs[second_leg], hedge_qty
            )
            if not _filled_enough(second_result, hedge_qty):
                raise ExecutionIncident(
                    f"hedge leg on {second_leg} filled {second_result.filled_qty}/{hedge_qty}"
                )
        except ExecutionIncident:
            log.error("hedge leg failed; unwinding first leg on %s", first_leg)
            await self._unwind(first_venue, first_symbol, legs[first_leg], hedge_qty)
            raise

        if first_leg == short_venue:
            return PairFill(short_result=first_result, long_result=second_result, qty=hedge_qty)
        return PairFill(short_result=second_result, long_result=first_result, qty=hedge_qty)

    async def close_pair(self, short_venue: str, long_venue: str) -> None:
        """Flatten both legs (reduce-only) based on live position size."""
        errors: list[str] = []
        for venue_name, closing_side in ((short_venue, Side.BUY), (long_venue, Side.SELL)):
            venue, symbol = self._leg(venue_name)
            try:
                await self._safe_cancel(venue, symbol)
                position = await venue.get_position(symbol)
                if position.qty == 0:
                    continue
                qty = abs(position.qty)
                side = Side.SELL if position.qty > 0 else Side.BUY
                if side is not closing_side:
                    log.warning(
                        "%s position sign unexpected (qty=%s); closing anyway",
                        venue_name, position.qty,
                    )
                await _place_market(venue, symbol, side, qty, reduce_only=True)
            except (VenueError, ExecutionIncident) as e:
                errors.append(f"{venue_name}: {e}")
        if errors:
            raise ExecutionIncident("close_pair incomplete: " + "; ".join(errors))

    async def rebalance_delta(
        self, venue_name: str, delta: Decimal, min_qty: Decimal
    ) -> OrderResult | None:
        """Trade away net delta on one venue. ``delta`` is the pair's signed net
        quantity (positive = net long); we sell when long, buy when short."""
        qty = abs(delta)
        if qty < min_qty:
            return None
        venue, symbol = self._leg(venue_name)
        side = Side.SELL if delta > 0 else Side.BUY
        log.info("rebalancing delta %s via %s %s on %s", delta, side.value, qty, venue_name)
        return await _place_market(venue, symbol, side, qty)

    async def _unwind(
        self, venue: PerpVenue, symbol: str, opened_side: Side, qty: Decimal
    ) -> None:
        try:
            await _place_market(venue, symbol, opened_side.opposite, qty, reduce_only=True)
        except ExecutionIncident as e:
            # This is the one state that genuinely needs a human: a naked leg
            # we could not close. Surface loudly; the engine halts.
            log.critical("FAILED TO UNWIND %s leg on %s: %s", opened_side.value, venue.name, e)
            raise

    async def _safe_cancel(self, venue: PerpVenue, symbol: str) -> None:
        try:
            await venue.cancel_all(symbol)
        except VenueError as e:
            log.warning("cancel_all on %s failed: %s", venue.name, e)
