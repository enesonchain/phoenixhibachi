"""Volume-farming mode: budgeted delta-neutral cycles.

Instead of waiting for a funding spread, this engine repeatedly opens a
hedged pair (or a single-venue round trip in "solo" style), holds briefly,
and closes — generating venue volume at a known, budgeted cost. Hard daily
stops: a volume target and a fee budget; whichever hits first ends the
UTC day. All of the execution safety machinery (position-verified fills,
flatten-to-baseline cleanup, naked-leg halts) is reused unchanged.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

from deltabot.config import StrategyConfig, VolumeConfig
from deltabot.models import Side
from deltabot.state import BotState, FarmDay, OpenPair, Phase, StateStore
from deltabot.strategy.execution import (
    ExecutionIncident,
    NakedLegError,
    PairExecutor,
)
from deltabot.strategy.funding import compute_spread
from deltabot.strategy.risk import check_entry, size_pair
from deltabot.venues.base import PerpVenue, VenueError

log = logging.getLogger(__name__)


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class FarmEngine:
    """Cycle loop with the same external surface as the funding Engine:
    run_forever / tick / state / controller."""

    def __init__(
        self,
        venue_a: PerpVenue,
        venue_b: PerpVenue,
        symbols: dict[str, str],
        strategy_cfg: StrategyConfig,
        farm_cfg: VolumeConfig,
        store: StateStore,
        controller=None,
    ):
        self.venues = {venue_a.name: venue_a, venue_b.name: venue_b}
        self.symbols = symbols
        self.cfg = strategy_cfg
        self.farm_cfg = farm_cfg
        self.store = store
        self.state: BotState = store.load()
        self.executor = PairExecutor(self.venues, symbols)
        self.controller = controller
        self._last_snapshot: dict | None = None
        self._last_error: str | None = None
        self._decision = "starting up"
        self._next_cycle_at = 0.0

    # ------------------------------------------------------------- lifecycle

    async def run_forever(self) -> None:
        log.info(
            "volume farming (%s style): %s notional/cycle, hold %ss, "
            "target $%s/day, fee budget $%s/day",
            self.farm_cfg.style, self.farm_cfg.cycle_notional, self.farm_cfg.hold_s,
            self.farm_cfg.daily_volume_target, self.farm_cfg.max_daily_fees,
        )
        while True:
            await self.tick()
            if self.state.phase is Phase.HALTED and self.controller is None:
                log.error("farm HALTED: %s", self.state.halt_reason)
                return
            await asyncio.sleep(self.cfg.poll_interval_s)

    # ----------------------------------------------------------------- tick

    async def tick(self) -> None:
        try:
            if self.controller is not None:
                self.controller.apply_pending_config(self.cfg)
                if (
                    self.state.phase is Phase.HALTED
                    and self.controller.consume_clear_halt()
                ):
                    self.state.record_incident("halt cleared via dashboard")
                    self.state.phase = Phase.FLAT
                    self.state.halt_reason = None
            self._roll_day()
            snapshot = await self._observe()
            self._last_snapshot = snapshot
            self._last_error = None

            if self.state.phase is Phase.HALTED:
                return

            # Farm cycles always end flat: any live position at tick start is
            # residue from a crashed cycle — flatten it before anything else.
            live = {n: p for n, p in snapshot["positions"].items() if not p.is_flat}
            if live:
                log.warning("live positions at tick start (%s); flattening",
                            {n: str(p.qty) for n, p in live.items()})
                for name in live:
                    await self.executor.flatten_to_baseline(name, Decimal(0))
                self.state.pair = None
                self.state.phase = Phase.FLAT

            force = (
                self.controller is not None
                and self.controller.consume_enter_request()
            )
            # dashboard "close" is meaningless between cycles; consume quietly
            if self.controller is not None:
                self.controller.consume_close_request()

            if not force:
                if self.controller is not None and self.controller.paused:
                    self._decision = "farming paused from the dashboard"
                    return
                if time.time() < self._next_cycle_at:
                    self._decision = (
                        f"next cycle in {int(self._next_cycle_at - time.time())}s"
                    )
                    return
                stop = self._budget_stop()
                if stop:
                    self._decision = stop
                    return

            await self._run_cycle(snapshot, force)
        except VenueError as e:
            log.warning("tick skipped, venue unavailable: %s", e)
            self._last_error = str(e)
        except NakedLegError as e:
            log.critical("naked-leg failure: %s", e)
            self.state.record_incident(str(e))
            self.state.phase = Phase.HALTED
            self.state.halt_reason = str(e)
        except ExecutionIncident as e:
            log.error("cycle incident: %s", e)
            self.state.record_incident(str(e))
            self.state.phase = Phase.FLAT
            self.state.pair = None
            self._next_cycle_at = time.time() + self.cfg.cooldown_s
            self._decision = f"cycle failed, cooling down: {e}"
        except Exception:
            log.exception("unexpected error in farm tick; halting for safety")
            self.state.phase = Phase.HALTED
            self.state.halt_reason = "unexpected exception (see logs)"
        finally:
            self.store.save(self.state)
            if self.controller is not None:
                try:
                    self.controller.publish(self._status_dict())
                except Exception:
                    log.exception("failed to publish dashboard status")

    # ---------------------------------------------------------------- cycle

    async def _run_cycle(self, snapshot: dict, force: bool) -> None:
        fundings = snapshot["fundings"]
        verdict = check_entry(self.cfg, snapshot["books"], snapshot["balances"], fundings)
        if not verdict.ok:
            self._decision = "cycle blocked by risk: " + "; ".join(verdict.reasons)
            return

        specs = {}
        for name, venue in self.venues.items():
            specs[name] = await venue.get_market(self.symbols[name])
        marks = {n: f.mark_price for n, f in fundings.items()}
        sizing_cfg = dataclasses.replace(
            self.cfg, target_notional=self.farm_cfg.cycle_notional
        )
        if self.farm_cfg.style == "solo":
            await self._solo_cycle(marks, specs, snapshot)
        else:
            qty, refusal = size_pair(sizing_cfg, marks, snapshot["balances"], specs)
            if refusal:
                self._decision = "cycle refused by sizing: " + refusal
                return
            await self._pair_cycle(qty, fundings, marks)
        self._next_cycle_at = time.time() + self.farm_cfg.pause_s

    async def _pair_cycle(self, qty: Decimal, fundings: dict, marks: dict) -> None:
        names = list(fundings)
        # Orient like the funding strategy: short the higher-funding venue, so
        # any funding accrued during the hold works for us, not against us.
        spread = compute_spread(fundings[names[0]], fundings[names[1]])
        log.info("cycle %d: opening pair qty=%s (short %s / long %s)",
                 self.state.farm.cycles + 1, qty, spread.short_venue, spread.long_venue)
        self.state.phase = Phase.ENTERING
        self.store.save(self.state)
        fill = await self.executor.open_pair(
            spread.short_venue, spread.long_venue, qty, first_leg=self.cfg.first_leg
        )
        self.state.pair = OpenPair(
            short_venue=spread.short_venue, long_venue=spread.long_venue,
            qty=fill.qty, entry_spread_apr=spread.annualized,
        )
        self.state.phase = Phase.OPEN
        self._decision = f"cycle open, holding {self.farm_cfg.hold_s:.0f}s"
        self.store.save(self.state)
        if self.controller is not None:
            self.controller.publish(self._status_dict())

        await asyncio.sleep(self.farm_cfg.hold_s)

        self.state.phase = Phase.EXITING
        self.store.save(self.state)
        await self.executor.close_pair(spread.short_venue, spread.long_venue)
        self.state.phase = Phase.FLAT
        self.state.pair = None

        for name in self.venues:
            self._record_volume(name, fill.qty * marks[name] * 2)
        self.state.farm.cycles += 1
        self._decision = self._progress_line()
        log.info("cycle complete: %s", self._decision)

    async def _solo_cycle(self, marks: dict, specs: dict, snapshot: dict) -> None:
        venue_name = self.farm_cfg.solo_venue
        if venue_name not in self.venues:
            self._decision = f"solo venue {venue_name!r} not configured"
            return
        mark = marks[venue_name]
        spec = specs[venue_name]
        qty = spec.round_qty(self.farm_cfg.cycle_notional / mark)
        if qty < spec.min_order_size or qty * mark < spec.min_notional:
            self._decision = "cycle refused by sizing: below venue minimums"
            return
        log.info("solo cycle %d on %s: qty=%s", self.state.farm.cycles + 1,
                 venue_name, qty)
        self.state.phase = Phase.ENTERING
        self.store.save(self.state)
        filled = await self.executor.execute_single(venue_name, Side.BUY, qty)
        self.state.phase = Phase.OPEN
        try:
            await asyncio.sleep(self.farm_cfg.hold_s)
        finally:
            # whatever happens, drive the venue flat again
            await self.executor.flatten_to_baseline(venue_name, Decimal(0))
        self.state.phase = Phase.FLAT
        self._record_volume(venue_name, filled * mark * 2)
        self.state.farm.cycles += 1
        self._decision = self._progress_line()
        log.info("solo cycle complete: %s", self._decision)

    # -------------------------------------------------------------- accounting

    def _roll_day(self) -> None:
        today = _utc_day()
        if self.state.farm.day != today:
            if self.state.farm.day:
                log.info(
                    "farm day %s closed: %d cycles, $%.0f volume, $%.2f fees",
                    self.state.farm.day, self.state.farm.cycles,
                    self.state.farm.total_volume(), self.state.farm.fees_usd,
                )
            self.state.farm = FarmDay(day=today)

    def _record_volume(self, venue: str, volume: Decimal) -> None:
        farm = self.state.farm
        farm.volume_usd[venue] = farm.volume_usd.get(venue, Decimal(0)) + volume
        fee_rate = self.farm_cfg.taker_fee.get(venue, Decimal("0.0005"))
        farm.fees_usd += volume * fee_rate

    def _budget_stop(self) -> str | None:
        farm = self.state.farm
        if farm.fees_usd >= self.farm_cfg.max_daily_fees:
            return (
                f"done for the day: fee budget spent "
                f"(${farm.fees_usd:.2f} / ${self.farm_cfg.max_daily_fees})"
            )
        if farm.total_volume() >= self.farm_cfg.daily_volume_target:
            return (
                f"done for the day: volume target hit "
                f"(${farm.total_volume():.0f} / ${self.farm_cfg.daily_volume_target})"
            )
        return None

    def _progress_line(self) -> str:
        farm = self.state.farm
        return (
            f"cycle {farm.cycles} done — ${farm.total_volume():,.0f} volume, "
            f"${farm.fees_usd:.2f} fees today"
        )

    # ---------------------------------------------------------------- status

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

    def _status_dict(self) -> dict:
        def num(value):
            return float(value) if value is not None else None

        farm = self.state.farm
        status: dict = {
            "ts": time.time(),
            "phase": self.state.phase.value,
            "decision": (
                "halted: " + (self.state.halt_reason or "")
                if self.state.phase is Phase.HALTED
                else self._decision
            ),
            "halt_reason": self.state.halt_reason,
            "cooldown_until": self._next_cycle_at,
            "incidents": list(self.state.incidents[-20:]),
            "error": self._last_error,
            "config": {
                "entry_apr": num(self.cfg.entry_apr),
                "exit_apr": num(self.cfg.exit_apr),
                "target_notional": num(self.farm_cfg.cycle_notional),
                "max_notional": num(self.cfg.max_notional),
                "poll_interval_s": self.cfg.poll_interval_s,
            },
            "farm": {
                "style": self.farm_cfg.style,
                "cycles": farm.cycles,
                "volume_by_venue": {k: num(v) for k, v in farm.volume_usd.items()},
                "volume_today": num(farm.total_volume()),
                "volume_target": num(self.farm_cfg.daily_volume_target),
                "fees_today": num(farm.fees_usd),
                "fee_budget": num(self.farm_cfg.max_daily_fees),
                "cycle_notional": num(self.farm_cfg.cycle_notional),
                "next_cycle_ts": self._next_cycle_at,
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
                "short_venue": pair.short_venue, "long_venue": pair.long_venue,
                "qty": num(pair.qty), "entry_spread_apr": num(pair.entry_spread_apr),
                "entry_ts": pair.entry_ts,
            }
        snapshot = self._last_snapshot
        if snapshot is None:
            return status
        fundings = snapshot["fundings"]
        for name in self.venues:
            funding = fundings[name]
            book = snapshot["books"][name]
            position = snapshot["positions"][name]
            balance = snapshot["balances"][name]
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
        status["total_equity"] = sum(
            num(b.equity) or 0 for b in snapshot["balances"].values()
        )
        delta = sum((p.qty for p in snapshot["positions"].values()), Decimal(0))
        mark = max((f.mark_price for f in fundings.values()), default=Decimal(0))
        status["net_delta_usd"] = num(delta * mark)
        return status
