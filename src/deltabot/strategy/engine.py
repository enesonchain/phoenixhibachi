"""The delta-neutral decision loop.

Phases:  FLAT -> ENTERING -> OPEN -> EXITING -> FLAT, plus HALTED for
incidents that need a human. Every tick re-reads live venue state first, so
the engine survives restarts and reconciles drift: recognizable delta-neutral
pairs are adopted, a stray single leg is flattened (after two consecutive
observations, to ride out indexer lag), and anything unrecognizable halts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from deltabot.config import StrategyConfig
from deltabot.models import FundingSnapshot, PositionState
from deltabot.state import BotState, OpenPair, Phase, StateStore
from deltabot.strategy.execution import (
    ExecutionIncident,
    NakedLegError,
    PairExecutor,
)
from deltabot.strategy.funding import carry_of_position, compute_spread
from deltabot.strategy.risk import check_entry, size_pair
from deltabot.venues.base import PerpVenue, VenueError

log = logging.getLogger(__name__)

# A lone leg (or missing leg) must be observed on this many consecutive ticks
# before the engine acts on it — a single stale read (e.g. indexer lag right
# after a fill) must not trigger an emergency flatten.
STRAY_CONFIRMATION_TICKS = 2


class Engine:
    def __init__(
        self,
        venue_a: PerpVenue,
        venue_b: PerpVenue,
        symbols: dict[str, str],
        cfg: StrategyConfig,
        store: StateStore,
        controller=None,  # deltabot.control.BotController | None
    ):
        self.venues = {venue_a.name: venue_a, venue_b.name: venue_b}
        self.symbols = symbols
        self.cfg = cfg
        self.store = store
        self.state: BotState = store.load()
        self.executor = PairExecutor(self.venues, symbols)
        self.controller = controller
        self._stray_streak = 0
        self._last_snapshot: dict | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------- lifecycle

    async def run_forever(self) -> None:
        log.info("engine starting in phase %s", self.state.phase.value)
        while True:
            await self.tick()
            if self.state.phase is Phase.HALTED:
                if self.controller is None:
                    log.error(
                        "engine HALTED: %s — fix, then delete/repair the state file",
                        self.state.halt_reason,
                    )
                    return
                # With a dashboard attached, stay alive so the operator can
                # inspect the halt and clear it; trading stays frozen.
                log.error("engine HALTED (dashboard attached): %s", self.state.halt_reason)
            await asyncio.sleep(self.cfg.poll_interval_s)

    # ----------------------------------------------------------------- tick

    async def tick(self) -> None:
        """One full observe-decide-act cycle. Never raises: incidents cool the
        bot down, naked-leg failures and unknown errors halt it, and state is
        always persisted."""
        try:
            if self.controller is not None:
                self.controller.apply_pending_config(self.cfg)
                if (
                    self.state.phase is Phase.HALTED
                    and self.controller.consume_clear_halt()
                ):
                    log.warning("halt cleared from dashboard; reconciling")
                    self.state.record_incident("halt cleared via dashboard")
                    self.state.phase = Phase.FLAT
                    self.state.halt_reason = None
            snapshot = await self._observe()
            self._last_snapshot = snapshot
            self._last_error = None
            await self._reconcile(snapshot["positions"])
            if (
                self.controller is not None
                and self.controller.consume_close_request()
                and self.state.phase is Phase.OPEN
            ):
                await self._exit("manual close requested via dashboard")
            elif self.state.phase is Phase.OPEN:
                await self._manage_open(snapshot)
            elif self.state.phase is Phase.FLAT:
                await self._consider_entry(snapshot)
        except VenueError as e:
            log.warning("tick skipped, venue unavailable: %s", e)
            self._last_error = str(e)
        except NakedLegError as e:
            log.critical("naked-leg failure: %s", e)
            self.state.record_incident(str(e))
            self.state.phase = Phase.HALTED
            self.state.halt_reason = str(e)
        except ExecutionIncident as e:
            self._on_incident(str(e))
        except Exception:
            log.exception("unexpected error in tick; halting for safety")
            self.state.phase = Phase.HALTED
            self.state.halt_reason = "unexpected exception (see logs)"
        finally:
            self.store.save(self.state)
            if self.controller is not None:
                try:
                    self.controller.publish(self._status_dict())
                except Exception:
                    log.exception("failed to publish dashboard status")

    async def _refresh_positions(self) -> None:
        """Re-read positions/balances into the last snapshot after an action
        changed them, so the published status isn't a tick stale."""
        if self._last_snapshot is None:
            return
        try:
            names = list(self.venues)
            results = await asyncio.gather(
                *[self.venues[n].get_position(self.symbols[n]) for n in names],
                *[self.venues[n].get_balance() for n in names],
            )
            k = len(names)
            self._last_snapshot["positions"] = dict(zip(names, results[0:k]))
            self._last_snapshot["balances"] = dict(zip(names, results[k : 2 * k]))
        except VenueError as e:
            log.debug("post-action position refresh skipped: %s", e)

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

    async def _reconcile(self, positions: dict[str, PositionState]) -> None:
        """Map recorded phase onto observed venue reality."""
        live = {n: p for n, p in positions.items() if not p.is_flat}
        phase = self.state.phase

        if phase in (Phase.ENTERING, Phase.EXITING):
            # We died mid-execution; live positions are the truth.
            log.warning("recovering from crash during %s", phase.value)
            self.state.phase = Phase.OPEN if live else Phase.FLAT
            if not live:
                self.state.pair = None
            phase = self.state.phase

        if phase is Phase.OPEN and not live:
            log.warning("state says OPEN but venues are flat; going FLAT")
            self.state.phase = Phase.FLAT
            self.state.pair = None
            self._stray_streak = 0
            return

        if phase is Phase.OPEN and self.state.pair is None:
            pair = self._pair_from_positions(positions)
            if pair is not None:
                log.warning("reconstructed pair record from live positions: %s", pair)
                self.state.pair = pair
            else:
                await self._handle_unrecognized(live, "OPEN with unrecognizable positions")
            return

        if phase is Phase.FLAT and live:
            pair = self._pair_from_positions(positions)
            if pair is not None:
                log.warning("adopting live positions as an open pair: %s", pair)
                self.state.pair = pair
                self.state.phase = Phase.OPEN
            else:
                await self._handle_unrecognized(live, "FLAT with live positions")
            return

        if phase is Phase.FLAT and not live:
            self._stray_streak = 0

    async def _handle_unrecognized(
        self, live: dict[str, PositionState], context: str
    ) -> None:
        """A position set that isn't a delta-neutral pair. A single stray leg
        is flattened once confirmed on consecutive ticks; anything more
        complex halts for a human."""
        if len(live) == 1:
            self._stray_streak += 1
            if self._stray_streak < STRAY_CONFIRMATION_TICKS:
                log.warning(
                    "%s: single leg observed (%d/%d confirmations); waiting",
                    context, self._stray_streak, STRAY_CONFIRMATION_TICKS,
                )
                return
            venue_name = next(iter(live))
            log.error("%s: flattening confirmed stray leg on %s", context, venue_name)
            await self.executor.flatten_to_baseline(venue_name, Decimal(0))
            self.state.record_incident(f"flattened stray leg on {venue_name} ({context})")
            self.state.cooldown_until = time.time() + self.cfg.cooldown_s
            self.state.phase = Phase.FLAT
            self.state.pair = None
            self._stray_streak = 0
            return
        self.state.phase = Phase.HALTED
        self.state.halt_reason = (
            f"{context}: positions {[(n, str(p.qty)) for n, p in live.items()]} "
            "don't form a recognizable delta-neutral pair; refusing to trade"
        )

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

    # ---------------------------------------------------------------- entry

    async def _consider_entry(self, snapshot: dict) -> None:
        if self.controller is not None and self.controller.paused:
            return
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
        await self._refresh_positions()

    # ------------------------------------------------------------ open mgmt

    async def _manage_open(self, snapshot: dict) -> None:
        pair = self.state.pair
        positions = snapshot["positions"]
        fundings: dict[str, FundingSnapshot] = snapshot["fundings"]
        if pair is None:  # _reconcile guarantees this, but stay defensive
            return

        # One leg vanished (liquidation / manual close). Confirm on
        # consecutive ticks before flattening the survivor: a single stale
        # read from a lagging indexer must not destroy a healthy hedge.
        short_pos = positions[pair.short_venue]
        long_pos = positions[pair.long_venue]
        if short_pos.is_flat or long_pos.is_flat:
            self._stray_streak += 1
            if self._stray_streak < STRAY_CONFIRMATION_TICKS:
                log.warning(
                    "one leg reads flat (short=%s long=%s); awaiting confirmation %d/%d",
                    short_pos.qty, long_pos.qty,
                    self._stray_streak, STRAY_CONFIRMATION_TICKS,
                )
                return
            log.error(
                "one leg missing (short=%s long=%s); flattening survivor",
                short_pos.qty, long_pos.qty,
            )
            await self._exit("one leg missing — emergency flatten")
            self.state.record_incident("one leg was missing while OPEN")
            self.state.cooldown_until = time.time() + self.cfg.cooldown_s
            self._stray_streak = 0
            return
        self._stray_streak = 0

        # Delta drift beyond tolerance -> rebalance on the short venue's book.
        delta = short_pos.qty + long_pos.qty
        mark = fundings[pair.short_venue].mark_price
        if abs(delta) * mark > self.cfg.delta_rebalance_notional:
            spec = await self.venues[pair.short_venue].get_market(
                self.symbols[pair.short_venue]
            )
            await self.executor.rebalance_delta(
                pair.short_venue, delta, spec.step_size, spec.min_order_size
            )

        # Exit on carry collapse (hysteresis) or flip.
        names = list(fundings)
        carry = carry_of_position(pair.short_venue, fundings[names[0]], fundings[names[1]])
        log.info(
            "open pair carry %.2f%% APR (entry %.2f%%)",
            carry * 100, pair.entry_spread_apr * 100,
        )
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
        await self._refresh_positions()

    # ------------------------------------------------------------ dashboard

    def _status_dict(self) -> dict:
        """JSON-safe snapshot of everything the dashboard shows."""

        def num(value):
            return float(value) if value is not None else None

        status: dict = {
            "ts": time.time(),
            "phase": self.state.phase.value,
            "halt_reason": self.state.halt_reason,
            "cooldown_until": self.state.cooldown_until,
            "incidents": list(self.state.incidents[-20:]),
            "error": self._last_error,
            "config": {
                "entry_apr": num(self.cfg.entry_apr),
                "exit_apr": num(self.cfg.exit_apr),
                "target_notional": num(self.cfg.target_notional),
                "max_notional": num(self.cfg.max_notional),
                "poll_interval_s": self.cfg.poll_interval_s,
            },
            "pair": None,
            "venues": {},
            "spread": None,
            "carry_apr": None,
            "total_equity": None,
            "net_delta_usd": None,
        }
        pair = self.state.pair
        if pair is not None:
            status["pair"] = {
                "short_venue": pair.short_venue,
                "long_venue": pair.long_venue,
                "qty": num(pair.qty),
                "entry_spread_apr": num(pair.entry_spread_apr),
                "entry_ts": pair.entry_ts,
            }
        snapshot = self._last_snapshot
        if snapshot is None:
            return status

        fundings = snapshot["fundings"]
        books = snapshot["books"]
        positions = snapshot["positions"]
        balances = snapshot["balances"]
        for name in self.venues:
            funding = fundings[name]
            book = books[name]
            position = positions[name]
            balance = balances[name]
            status["venues"][name] = {
                "symbol": self.symbols[name],
                "funding_rate": num(funding.rate),
                "interval_hours": num(funding.interval_hours),
                "annualized": num(funding.annualized),
                "mark": num(funding.mark_price),
                "bid": num(book.bid),
                "ask": num(book.ask),
                "book_spread_bps": num(book.spread_bps),
                "position_qty": num(position.qty),
                "entry_price": num(position.entry_price),
                "unrealized_pnl": num(position.unrealized_pnl),
                "equity": num(balance.equity),
                "available": num(balance.available),
            }
        names = list(fundings)
        spread = compute_spread(fundings[names[0]], fundings[names[1]])
        status["spread"] = {
            "annualized": num(spread.annualized),
            "short_venue": spread.short_venue,
            "long_venue": spread.long_venue,
        }
        if pair is not None:
            status["carry_apr"] = num(
                carry_of_position(pair.short_venue, fundings[names[0]], fundings[names[1]])
            )
        status["total_equity"] = sum(num(b.equity) or 0 for b in balances.values())
        delta = sum((p.qty for p in positions.values()), Decimal(0))
        mark = max((f.mark_price for f in fundings.values()), default=Decimal(0))
        status["net_delta_usd"] = num(delta * mark)
        return status

    # ------------------------------------------------------------ incidents

    def _on_incident(self, message: str) -> None:
        log.error("execution incident: %s", message)
        self.state.record_incident(message)
        self.state.cooldown_until = time.time() + self.cfg.cooldown_s
        # An incident during entry (pair not yet recorded) means the executor
        # restored both venues to baseline: we are flat. An incident while a
        # pair is open (e.g. a failed rebalance) must NOT wipe the pair —
        # the hedge is still on and keeps being managed next tick.
        if self.state.pair is not None:
            self.state.phase = Phase.OPEN
        else:
            self.state.phase = Phase.FLAT
