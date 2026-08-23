"""The delta-neutral decision loop.

Phases:  FLAT -> ENTERING -> OPEN -> EXITING -> FLAT, plus HALTED for
incidents that need a human. Every tick re-reads live venue state first, so
the engine survives restarts and reconciles drift (e.g. one leg liquidated).
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from deltabot.config import StrategyConfig
from deltabot.models import FundingSnapshot
from deltabot.state import BotState, OpenPair, Phase, StateStore
from deltabot.strategy.execution import ExecutionIncident, PairExecutor
from deltabot.strategy.funding import carry_of_position, compute_spread
from deltabot.strategy.risk import check_entry, size_pair
from deltabot.venues.base import PerpVenue, VenueError

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        venue_a: PerpVenue,
        venue_b: PerpVenue,
        symbols: dict[str, str],
        cfg: StrategyConfig,
        store: StateStore,
    ):
        self.venues = {venue_a.name: venue_a, venue_b.name: venue_b}
        self.symbols = symbols
        self.cfg = cfg
        self.store = store
        self.state: BotState = store.load()
        self.executor = PairExecutor(self.venues, symbols)

    # ------------------------------------------------------------- lifecycle

    async def run_forever(self) -> None:
        log.info("engine starting in phase %s", self.state.phase.value)
        while True:
            await self.tick()
            if self.state.phase is Phase.HALTED:
                log.error("engine HALTED: %s — fix, then delete/repair the state file", self.state.halt_reason)
                return
            await asyncio.sleep(self.cfg.poll_interval_s)

    # ----------------------------------------------------------------- tick

    async def tick(self) -> None:
        """One full observe-decide-act cycle. Never raises: incidents cool the
        bot down, unknown errors halt it, and state is always persisted."""
        try:
            snapshot = await self._observe()
            positions = snapshot["positions"]

            live_open = any(not p.is_flat for p in positions.values())
            self._reconcile(live_open)

            if self.state.phase is Phase.OPEN:
                await self._manage_open(snapshot)
            elif self.state.phase is Phase.FLAT:
                await self._consider_entry(snapshot)
            # ENTERING/EXITING are transient inside a single tick; if we crashed
            # mid-way, _reconcile has already mapped us back onto FLAT or OPEN.
        except VenueError as e:
            log.warning("tick skipped, venue unavailable: %s", e)
        except ExecutionIncident as e:
            self._on_incident(str(e))
        except Exception:
            log.exception("unexpected error in tick; halting for safety")
            self.state.phase = Phase.HALTED
            self.state.halt_reason = "unexpected exception (see logs)"
        finally:
            self.store.save(self.state)

    async def _observe(self) -> dict:
        names = list(self.venues)
        results = await asyncio.gather(
            *[self.venues[n].get_funding(self.symbols[n]) for n in names],
            *[self.venues[n].get_top_of_book(self.symbols[n]) for n in names],
            *[self.venues[n].get_position(self.symbols[n]) for n in names],
            *[self.venues[n].get_balance() for n in names],
        )
        k = len(names)
        return {
            "fundings": dict(zip(names, results[0:k])),
            "books": dict(zip(names, results[k : 2 * k])),
            "positions": dict(zip(names, results[2 * k : 3 * k])),
            "balances": dict(zip(names, results[3 * k : 4 * k])),
        }

    # ------------------------------------------------------------ reconcile

    def _reconcile(self, live_open: bool) -> None:
        """Map recorded phase onto observed reality after restarts/crashes."""
        phase = self.state.phase
        if phase in (Phase.ENTERING, Phase.EXITING):
            # We died mid-execution; trust live positions.
            self.state.phase = Phase.OPEN if live_open else Phase.FLAT
            if self.state.phase is Phase.FLAT:
                self.state.pair = None
            log.warning("recovered from %s -> %s", phase.value, self.state.phase.value)
        elif phase is Phase.OPEN and not live_open:
            log.warning("state says OPEN but venues are flat; going FLAT")
            self.state.phase = Phase.FLAT
            self.state.pair = None
        elif phase is Phase.FLAT and live_open:
            # Positions exist that we don't remember creating (manual trade,
            # lost state file). Adopt them if they look like our pair.
            adopted = self._try_adopt_pair()
            if not adopted:
                self.state.phase = Phase.HALTED
                self.state.halt_reason = (
                    "live positions exist but state is FLAT and they don't form "
                    "a recognizable delta-neutral pair; refusing to trade"
                )

    def _try_adopt_pair(self) -> bool:
        # Filled in by tick's snapshot on the next pass; conservative default.
        return False

    # ---------------------------------------------------------------- entry

    async def _consider_entry(self, snapshot: dict) -> None:
        if time.time() < self.state.cooldown_until:
            return

        fundings: dict[str, FundingSnapshot] = snapshot["fundings"]
        names = list(fundings)
        spread = compute_spread(fundings[names[0]], fundings[names[1]])

        log.info(
            "funding: %s=%.2f%% APR, %s=%.2f%% APR -> spread %.2f%% (short %s / long %s)",
            names[0], fundings[names[0]].annualized * 100,
            names[1], fundings[names[1]].annualized * 100,
            spread.annualized_pct, spread.short_venue, spread.long_venue,
        )

        if spread.annualized < self.cfg.entry_apr:
            return

        verdict = check_entry(self.cfg, snapshot["books"], snapshot["balances"], fundings)
        if not verdict.ok:
            log.info("entry blocked by risk: %s", "; ".join(verdict.reasons))
            return

        specs = {}
        for name, venue in self.venues.items():
            specs[name] = await venue.get_market(self.symbols[name])
        marks = {n: f.mark_price for n, f in fundings.items()}
        qty, refusal = size_pair(self.cfg, marks, snapshot["balances"], specs)
        if refusal:
            log.info("entry refused by sizing: %s", refusal)
            return

        self.state.phase = Phase.ENTERING
        self.store.save(self.state)
        fill = await self.executor.open_pair(
            spread.short_venue, spread.long_venue, qty, first_leg=self.cfg.first_leg
        )
        self.state.pair = OpenPair(
            short_venue=spread.short_venue,
            long_venue=spread.long_venue,
            qty=fill.qty,
            entry_spread_apr=spread.annualized,
        )
        self.state.phase = Phase.OPEN
        log.info(
            "pair OPEN: short %s / long %s qty=%s @ %.2f%% APR",
            spread.short_venue, spread.long_venue, fill.qty, spread.annualized_pct,
        )

    # ------------------------------------------------------------ open mgmt

    async def _manage_open(self, snapshot: dict) -> None:
        pair = self.state.pair
        positions = snapshot["positions"]
        fundings: dict[str, FundingSnapshot] = snapshot["fundings"]

        if pair is None:
            # OPEN without a pair record: reconstruct from live positions.
            pair = self._pair_from_positions(positions)
            if pair is None:
                self.state.phase = Phase.HALTED
                self.state.halt_reason = "OPEN with unrecognizable positions"
                return
            self.state.pair = pair
            log.warning("reconstructed pair record from live positions: %s", pair)

        # One leg vanished (liquidation/manual close) -> flatten the survivor now.
        short_pos = positions[pair.short_venue]
        long_pos = positions[pair.long_venue]
        if short_pos.is_flat or long_pos.is_flat:
            log.error("one leg missing (short=%s long=%s); flattening survivor",
                      short_pos.qty, long_pos.qty)
            await self._exit("one leg missing — emergency flatten")
            self.state.record_incident("one leg was missing while OPEN")
            self.state.cooldown_until = time.time() + self.cfg.cooldown_s
            return

        # Delta drift beyond tolerance -> rebalance on the short venue's book.
        delta = short_pos.qty + long_pos.qty
        mark = fundings[pair.short_venue].mark_price
        if abs(delta) * mark > self.cfg.delta_rebalance_notional:
            spec = await self.venues[pair.short_venue].get_market(self.symbols[pair.short_venue])
            await self.executor.rebalance_delta(pair.short_venue, delta, spec.min_order_size)

        # Exit on carry collapse (hysteresis) or flip.
        names = list(fundings)
        carry = carry_of_position(pair.short_venue, fundings[names[0]], fundings[names[1]])
        log.info("open pair carry %.2f%% APR (entry %.2f%%)",
                 carry * 100, pair.entry_spread_apr * 100)
        if carry < self.cfg.exit_apr:
            await self._exit(f"carry {carry * 100:.2f}% APR below exit threshold")

    async def _exit(self, reason: str) -> None:
        pair = self.state.pair
        if pair is None:
            self.state.phase = Phase.FLAT
            return
        log.info("exiting pair: %s", reason)
        self.state.phase = Phase.EXITING
        self.store.save(self.state)
        await self.executor.close_pair(pair.short_venue, pair.long_venue)
        self.state.phase = Phase.FLAT
        self.state.pair = None
        log.info("pair closed: %s", reason)

    def _pair_from_positions(self, positions: dict) -> OpenPair | None:
        shorts = [n for n, p in positions.items() if p.qty < 0]
        longs = [n for n, p in positions.items() if p.qty > 0]
        if len(shorts) == 1 and len(longs) == 1:
            qty_short = abs(positions[shorts[0]].qty)
            qty_long = positions[longs[0]].qty
            if qty_long > 0 and abs(qty_short - qty_long) / qty_long < Decimal("0.05"):
                return OpenPair(
                    short_venue=shorts[0],
                    long_venue=longs[0],
                    qty=max(qty_short, qty_long),
                    entry_spread_apr=Decimal(0),
                )
        return None

    # ------------------------------------------------------------ incidents

    def _on_incident(self, message: str) -> None:
        log.error("execution incident: %s", message)
        self.state.record_incident(message)
        self.state.cooldown_until = time.time() + self.cfg.cooldown_s
        # If an unwind failed we may be carrying a naked leg — that specific
        # case raises from close/unwind paths and is caught here: halt.
        if "FAILED TO UNWIND" in message or "close_pair incomplete" in message:
            self.state.phase = Phase.HALTED
            self.state.halt_reason = message
        else:
            self.state.phase = Phase.FLAT
            self.state.pair = None
