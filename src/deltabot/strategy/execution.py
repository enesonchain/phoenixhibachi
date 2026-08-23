"""Two-legged execution with one-legged-exposure protection.

Design principle: order responses are treated as hints, positions as truth.
Every leg is verified against the venue's *position delta* from a baseline
captured before the order, so an ambiguous failure (timeout, unconfirmed
transaction, lost status poll) never turns into a duplicate order or a
silently-ignored fill. Cleanup always drives the venue back to its baseline
position; if that flattening itself fails, ``NakedLegError`` is raised and
the engine halts rather than pretending to be flat.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from deltabot.models import OrderRequest, OrderResult, OrderStatus, Side
from deltabot.venues.base import OrderRejected, PerpVenue, VenueError

log = logging.getLogger(__name__)


class ExecutionIncident(Exception):
    """A leg could not be completed; the engine must cool down and reconcile."""


class NakedLegError(ExecutionIncident):
    """A position that should have been flattened could not be closed.

    This is the one state that needs a human: the engine halts on it.
    """


# Seconds to wait between order retries; tests shrink these timings.
RETRY_BACKOFF: tuple[float, ...] = (2.0, 4.0, 8.0)
# Position-poll cadence when resolving an ambiguous order outcome.
POSITION_POLL_S: float = 1.0
# How long to watch the position after an ambiguous placement error.
AMBIGUOUS_TIMEOUT_S: float = 10.0
# How long to watch after an inconclusive order status (UNKNOWN/partial).
UNCERTAIN_TIMEOUT_S: float = 15.0
# How long to wait for a flattening order to reflect in the position.
FLATTEN_TIMEOUT_S: float = 20.0
# Fill tolerance: venue step rounding may shave a sliver off the request.
FILL_TOLERANCE = Decimal("0.99")


@dataclass
class PairFill:
    short_qty: Decimal
    long_qty: Decimal

    @property
    def qty(self) -> Decimal:
        return min(self.short_qty, self.long_qty)


def _order_tag(suffix: str) -> str:
    # Hibachi clientId rules: 1-32 chars of [A-Za-z0-9-]; venues without
    # client-id support ignore the tag.
    return f"dn-{time.time_ns() // 1_000_000}-{suffix}"[:32]


class PairExecutor:
    """Executes and unwinds delta-neutral pairs across two venues."""

    def __init__(self, venues: dict[str, PerpVenue], symbols: dict[str, str]):
        self.venues = venues
        self.symbols = symbols  # venue name -> market symbol on that venue

    def _leg(self, venue_name: str) -> tuple[PerpVenue, str]:
        return self.venues[venue_name], self.symbols[venue_name]

    # ------------------------------------------------------------ primitives

    async def _position_qty(self, venue: PerpVenue, symbol: str) -> Decimal:
        last: Exception | None = None
        for _ in range(3):
            try:
                return (await venue.get_position(symbol)).qty
            except VenueError as e:
                last = e
                await asyncio.sleep(POSITION_POLL_S)
        raise ExecutionIncident(f"{venue.name}: cannot read position: {last}")

    async def _await_delta(
        self,
        venue: PerpVenue,
        symbol: str,
        baseline: Decimal,
        side: Side,
        qty: Decimal,
        timeout_s: float,
    ) -> Decimal:
        """Poll until the position delta from ``baseline`` reflects the order
        (within tolerance) or the timeout passes. Returns the observed delta
        in the order's direction (>= 0; counter-direction moves clamp to 0)."""
        deadline = time.monotonic() + timeout_s
        observed = Decimal(0)
        while True:
            try:
                current = (await venue.get_position(symbol)).qty
                observed = max(Decimal(0), (current - baseline) * side.sign)
                if observed >= qty * FILL_TOLERANCE:
                    return observed
            except VenueError as e:
                log.warning("%s: position poll failed: %s", venue.name, e)
            if time.monotonic() >= deadline:
                return observed
            await asyncio.sleep(POSITION_POLL_S)

    async def _execute_leg(
        self,
        venue: PerpVenue,
        symbol: str,
        side: Side,
        qty: Decimal,
        baseline: Decimal,
        tag: str,
        attempts: int = 3,
    ) -> Decimal:
        """Place a market order and return the quantity that verifiably
        filled (measured as position delta from ``baseline``). Never sends a
        retry while the previous attempt might still have landed unnoticed."""
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                result: OrderResult | None = await venue.place_order(
                    OrderRequest(symbol=symbol, side=side, qty=qty, client_tag=tag)
                )
            except OrderRejected as e:
                # Explicit rejection: nothing landed, but don't blindly retry a
                # request the venue just refused.
                raise ExecutionIncident(
                    f"{venue.name} rejected {side.value} {qty} {symbol}: {e}"
                ) from e
            except VenueError as e:
                # Ambiguous: the order may have landed. Resolve via position.
                last_error = e
                log.warning(
                    "%s order attempt %d/%d ambiguous failure: %s — checking position",
                    venue.name, attempt, attempts, e,
                )
                delta = await self._await_delta(
                    venue, symbol, baseline, side, qty, timeout_s=AMBIGUOUS_TIMEOUT_S
                )
                if delta > 0:
                    log.info(
                        "%s: order landed despite error (delta=%s)", venue.name, delta
                    )
                    return delta
                await asyncio.sleep(RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)])
                continue

            if result.status is OrderStatus.REJECTED:
                raise ExecutionIncident(
                    f"{venue.name} rejected {side.value} {qty} {symbol}: {result.raw}"
                )
            if result.status is OrderStatus.FILLED and result.filled_qty >= qty * FILL_TOLERANCE:
                return result.filled_qty
            # PARTIAL / CANCELLED / UNKNOWN / stuck PENDING: the response can't
            # be trusted either way — cancel anything resting, then let the
            # position tell us what actually happened.
            await self._safe_cancel(venue, symbol)
            delta = await self._await_delta(
                venue, symbol, baseline, side, qty, timeout_s=UNCERTAIN_TIMEOUT_S
            )
            return delta

        raise ExecutionIncident(
            f"{venue.name} {side.value} {qty} {symbol} failed after {attempts} attempts: {last_error}"
        )

    async def flatten_to_baseline(
        self, venue_name: str, baseline: Decimal = Decimal(0)
    ) -> None:
        """Cancel open orders and close any position beyond ``baseline``.
        Raises NakedLegError if the venue cannot be brought back."""
        venue, symbol = self._leg(venue_name)
        await self._safe_cancel(venue, symbol)
        residual = await self._position_qty(venue, symbol) - baseline
        if residual == 0:
            return
        side = Side.SELL if residual > 0 else Side.BUY
        qty = abs(residual)
        log.warning(
            "%s: flattening residual %s %s (reduce-only %s %s)",
            venue_name, residual, symbol, side.value, qty,
        )
        try:
            await venue.place_order(
                OrderRequest(
                    symbol=symbol, side=side, qty=qty, reduce_only=True,
                    client_tag=_order_tag("fl"),
                )
            )
        except VenueError as e:
            log.critical("FAILED TO UNWIND %s on %s: %s", symbol, venue_name, e)
            raise NakedLegError(
                f"could not flatten residual {residual} {symbol} on {venue_name}: {e}"
            ) from e
        remaining = await self._await_delta(
            venue, symbol, baseline + residual, side, qty, timeout_s=FLATTEN_TIMEOUT_S
        )
        if remaining < qty * FILL_TOLERANCE:
            still = await self._position_qty(venue, symbol) - baseline
            if still != 0:
                raise NakedLegError(
                    f"residual {still} {symbol} remains on {venue_name} after flatten"
                )

    # ------------------------------------------------------------ pair entry

    async def open_pair(
        self,
        short_venue: str,
        long_venue: str,
        qty: Decimal,
        first_leg: str,
    ) -> PairFill:
        """Open short on ``short_venue`` and long on ``long_venue``. The
        ``first_leg`` venue executes first; the hedge follows only once the
        first fill is verified. Any failure drives both venues back to their
        pre-entry baselines before the incident is raised."""
        legs = {short_venue: Side.SELL, long_venue: Side.BUY}
        if first_leg not in legs:
            first_leg = short_venue
        second_leg = long_venue if first_leg == short_venue else short_venue

        first_venue, first_symbol = self._leg(first_leg)
        second_venue, second_symbol = self._leg(second_leg)

        base_first = await self._position_qty(first_venue, first_symbol)
        base_second = await self._position_qty(second_venue, second_symbol)

        log.info(
            "opening pair: SELL %s / BUY %s qty=%s (first: %s)",
            short_venue, long_venue, qty, first_leg,
        )

        try:
            first_filled = await self._execute_leg(
                first_venue, first_symbol, legs[first_leg], qty, base_first,
                _order_tag("a"),
            )
        except NakedLegError:
            raise
        except ExecutionIncident:
            await self.flatten_to_baseline(first_leg, base_first)
            raise

        if first_filled < qty * FILL_TOLERANCE:
            await self.flatten_to_baseline(first_leg, base_first)
            raise ExecutionIncident(
                f"first leg on {first_leg} filled {first_filled}/{qty}; aborted"
            )

        hedge_qty = first_filled
        try:
            second_filled = await self._execute_leg(
                second_venue, second_symbol, legs[second_leg], hedge_qty,
                base_second, _order_tag("b"),
            )
            if second_filled < hedge_qty * FILL_TOLERANCE:
                raise ExecutionIncident(
                    f"hedge leg on {second_leg} filled {second_filled}/{hedge_qty}"
                )
        except NakedLegError:
            raise
        except ExecutionIncident:
            log.error("hedge leg failed; restoring both venues to baseline")
            # Order matters: clear the possibly-partial hedge first, then the
            # confirmed first leg. Either failure escalates to NakedLegError.
            await self.flatten_to_baseline(second_leg, base_second)
            await self.flatten_to_baseline(first_leg, base_first)
            raise

        fills = {first_leg: first_filled, second_leg: second_filled}
        return PairFill(short_qty=fills[short_venue], long_qty=fills[long_venue])

    # ------------------------------------------------------------- pair exit

    async def close_pair(self, short_venue: str, long_venue: str) -> None:
        """Flatten both legs completely, whatever their current sizes."""
        failures: list[NakedLegError] = []
        for venue_name in (short_venue, long_venue):
            try:
                await self.flatten_to_baseline(venue_name, Decimal(0))
            except NakedLegError as e:
                failures.append(e)
            except ExecutionIncident as e:
                failures.append(NakedLegError(str(e)))
        if failures:
            raise NakedLegError(
                "close_pair incomplete: " + "; ".join(str(f) for f in failures)
            )

    # ------------------------------------------------------------ single leg

    async def execute_single(
        self, venue_name: str, side: Side, qty: Decimal
    ) -> Decimal:
        """Place one position-verified market order on one venue. Returns the
        verified filled quantity."""
        venue, symbol = self._leg(venue_name)
        baseline = await self._position_qty(venue, symbol)
        return await self._execute_leg(
            venue, symbol, side, qty, baseline, _order_tag("s")
        )

    # ------------------------------------------------------------- rebalance

    async def rebalance_delta(
        self, venue_name: str, delta: Decimal, step: Decimal, min_qty: Decimal
    ) -> Decimal | None:
        """Trade away net delta on one venue. ``delta`` is the pair's signed
        net quantity (positive = net long). Returns the traded qty or None."""
        venue, symbol = self._leg(venue_name)
        qty = abs(delta)
        if step > 0:
            qty = (qty // step) * step
        if qty < min_qty or qty == 0:
            return None
        side = Side.SELL if delta > 0 else Side.BUY
        baseline = await self._position_qty(venue, symbol)
        log.info("rebalancing delta %s via %s %s on %s", delta, side.value, qty, venue_name)
        return await self._execute_leg(
            venue, symbol, side, qty, baseline, _order_tag("rb")
        )

    # -------------------------------------------------------------- helpers

    async def _safe_cancel(self, venue: PerpVenue, symbol: str) -> None:
        try:
            await venue.cancel_all(symbol)
        except VenueError as e:
            log.warning("cancel_all on %s failed: %s", venue.name, e)
