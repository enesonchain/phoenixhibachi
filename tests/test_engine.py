"""Engine state-machine tests over fake venues."""

from __future__ import annotations

from decimal import Decimal

import pytest

from deltabot.config import StrategyConfig
from deltabot.state import BotState, OpenPair, Phase, StateStore
from deltabot.strategy.engine import Engine
from deltabot.strategy.execution import ExecutionIncident, PairExecutor
from deltabot.venues.base import VenueUnavailable

from tests.fakes import FakeVenue

SYMBOLS = {"hibachi": "BTC/USDT-P", "phoenix": "BTC-PERP"}


def make_engine(tmp_path, hib_rate="0.0002", phx_rate="0.0000", **cfg_kwargs):
    cfg = StrategyConfig(
        entry_apr=Decimal("0.10"),
        exit_apr=Decimal("0.02"),
        target_notional=Decimal("1000"),
        first_leg="phoenix",
        poll_interval_s=0,
        **cfg_kwargs,
    )
    hibachi = FakeVenue("hibachi", funding_rate=hib_rate, mark="65000", step="0.0001")
    phoenix = FakeVenue("phoenix", funding_rate=phx_rate, mark="65000", step="0.0001")
    store = StateStore(tmp_path / "state.json")
    engine = Engine(hibachi, phoenix, SYMBOLS, cfg, store)
    return engine, hibachi, phoenix


async def test_enters_when_spread_above_threshold(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path)  # hibachi 0.02%/h vs 0 => ~175% APR
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    assert engine.state.pair.short_venue == "hibachi"
    assert engine.state.pair.long_venue == "phoenix"
    assert hibachi.qty < 0 and phoenix.qty > 0
    assert abs(hibachi.qty) == phoenix.qty
    # phoenix (first_leg) got its order first
    assert phoenix.orders and hibachi.orders


async def test_stays_flat_below_threshold(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path, hib_rate="0.000001", phx_rate="0")
    await engine.tick()
    assert engine.state.phase is Phase.FLAT
    assert hibachi.qty == 0 and phoenix.qty == 0


async def test_direction_flips_when_phoenix_pays_more(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path, hib_rate="0", phx_rate="0.0002")
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    assert engine.state.pair.short_venue == "phoenix"
    assert phoenix.qty < 0 and hibachi.qty > 0


async def test_failed_hedge_unwinds_first_leg(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path)
    # hedge leg is hibachi (short venue); make it fail
    hibachi.fail_next_order = VenueUnavailable("hibachi", "down")
    # retries: fail each attempt
    original_place = hibachi.place_order

    async def always_fail(request):
        raise VenueUnavailable("hibachi", "down")

    hibachi.place_order = always_fail
    await engine.tick()
    # first leg (phoenix long) must have been unwound: net flat
    assert phoenix.qty == 0
    assert engine.state.phase is Phase.FLAT
    assert engine.state.cooldown_until > 0
    assert engine.state.incidents


async def test_exits_when_carry_collapses(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    hibachi.funding_rate = Decimal("0.0000001")  # spread collapses
    await engine.tick()
    assert engine.state.phase is Phase.FLAT
    assert hibachi.qty == 0 and phoenix.qty == 0


async def test_holds_open_within_hysteresis_band(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    # drop spread below entry (10%) but above exit (2%): ~0.000006/h*8760 = 5.3%
    hibachi.funding_rate = Decimal("0.000006")
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    assert hibachi.qty < 0


async def test_missing_leg_triggers_emergency_flatten(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    hibachi.qty = Decimal(0)  # simulate liquidation of the short leg
    # first observation: engine waits for confirmation (indexer-lag guard)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    assert phoenix.qty > 0  # survivor untouched after a single stale read
    # second consecutive observation: emergency flatten fires
    await engine.tick()
    assert engine.state.phase is Phase.FLAT
    assert phoenix.qty == 0  # survivor closed
    assert engine.state.incidents


async def test_missing_leg_single_blip_does_not_flatten(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    real_qty = hibachi.qty
    hibachi.qty = Decimal(0)  # stale read
    await engine.tick()
    hibachi.qty = real_qty  # indexer caught up
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    assert phoenix.qty > 0 and hibachi.qty < 0  # pair intact


async def test_restart_recovers_open_position(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    pair = engine.state.pair

    # New engine instance, same store (simulates restart) with live positions.
    cfg = engine.cfg
    store = StateStore(tmp_path / "state.json")
    engine2 = Engine(hibachi, phoenix, SYMBOLS, cfg, store)
    assert engine2.state.phase is Phase.OPEN
    assert engine2.state.pair.short_venue == pair.short_venue
    await engine2.tick()  # spread still wide -> stays open
    assert engine2.state.phase is Phase.OPEN


async def test_entering_crash_reconciles_to_flat_when_no_positions(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path)
    engine.state = BotState(phase=Phase.ENTERING)
    await engine.tick()
    # tick reconciles ENTERING->FLAT, then (spread is wide) may enter again;
    # either way the state must be internally consistent:
    assert engine.state.phase in (Phase.FLAT, Phase.OPEN)
    if engine.state.phase is Phase.OPEN:
        assert engine.state.pair is not None


async def test_unknown_positions_halt(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path)
    hibachi.qty = Decimal("0.5")  # a long we don't know about, same-direction both
    phoenix.qty = Decimal("0.5")
    await engine.tick()
    assert engine.state.phase is Phase.HALTED


async def test_delta_rebalance_when_drifted(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path, delta_rebalance_notional=Decimal("25"))
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    # introduce drift on the long leg: extra 0.001 BTC (~$65 > $25 tolerance)
    phoenix.qty += Decimal("0.001")
    orders_before = len(hibachi.orders) + len(phoenix.orders)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    delta = hibachi.qty + phoenix.qty
    assert abs(delta) * hibachi.mark <= Decimal("25")
    assert len(hibachi.orders) + len(phoenix.orders) > orders_before


async def test_cooldown_blocks_reentry(tmp_path):
    import time as time_mod

    engine, hibachi, phoenix = make_engine(tmp_path)
    engine.state.cooldown_until = time_mod.time() + 3600
    await engine.tick()
    assert engine.state.phase is Phase.FLAT
    assert hibachi.qty == 0


async def test_close_pair_failure_halts(tmp_path):
    engine, hibachi, phoenix = make_engine(tmp_path)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN

    async def refuse(request):
        raise VenueUnavailable("hibachi", "close refused")

    hibachi.place_order = refuse
    hibachi.funding_rate = Decimal("0")  # force exit condition
    await engine.tick()
    assert engine.state.phase is Phase.HALTED
