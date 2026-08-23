"""Volume-farming engine tests."""

from __future__ import annotations

from decimal import Decimal

from deltabot.config import StrategyConfig, VolumeConfig
from deltabot.control import BotController
from deltabot.state import Phase, StateStore
from deltabot.strategy.farm import FarmEngine
from deltabot.venues.base import VenueUnavailable

from tests.fakes import FakeVenue

SYMBOLS = {"hibachi": "BTC/USDT-P", "phoenix": "BTC-PERP"}


def make_farm(tmp_path, style="pair", **farm_kwargs):
    strategy = StrategyConfig(poll_interval_s=0, cooldown_s=60)
    farm_cfg = VolumeConfig(
        style=style,
        cycle_notional=Decimal("650"),
        hold_s=0.0,
        pause_s=0.0,
        daily_volume_target=Decimal(str(farm_kwargs.pop("target", 1_000_000))),
        max_daily_fees=Decimal(str(farm_kwargs.pop("fee_budget", 100))),
    )
    hibachi = FakeVenue("hibachi", funding_rate="0.0001", mark="65000", step="0.0001")
    phoenix = FakeVenue("phoenix", funding_rate="0", mark="65000", step="0.0001")
    controller = BotController(paper=True, symbols=SYMBOLS)
    store = StateStore(tmp_path / "farm.json")
    engine = FarmEngine(hibachi, phoenix, SYMBOLS, strategy, farm_cfg, store,
                        controller=controller)
    return engine, hibachi, phoenix, controller


async def test_pair_cycle_generates_volume_and_ends_flat(tmp_path):
    engine, hibachi, phoenix, controller = make_farm(tmp_path)
    await engine.tick()
    assert engine.state.phase is Phase.FLAT
    assert hibachi.qty == 0 and phoenix.qty == 0  # always flat after a cycle
    farm = engine.state.farm
    assert farm.cycles == 1
    # ~$650 per leg, open+close = ~$1300 volume per venue
    assert farm.volume_usd["hibachi"] > 1200
    assert farm.volume_usd["phoenix"] > 1200
    assert farm.fees_usd > 0
    status = controller.status_payload()
    assert status["farm"]["cycles"] == 1
    assert status["farm"]["volume_today"] > 2400


async def test_fee_budget_stops_farming(tmp_path):
    engine, hibachi, phoenix, controller = make_farm(tmp_path, fee_budget=1)
    await engine.tick()  # ~ $1300*2 volume * 4.5/3.5bps ≈ $1.04 fees -> budget hit
    cycles_after_first = engine.state.farm.cycles
    assert cycles_after_first == 1
    await engine.tick()
    assert engine.state.farm.cycles == cycles_after_first  # no more cycles
    assert "fee budget" in controller.status_payload()["decision"]


async def test_volume_target_stops_farming(tmp_path):
    engine, hibachi, phoenix, controller = make_farm(tmp_path, target=2000)
    await engine.tick()  # ~2600 total volume >= 2000 target
    await engine.tick()
    assert engine.state.farm.cycles == 1
    assert "volume target" in controller.status_payload()["decision"]


async def test_solo_cycle_round_trips_one_venue(tmp_path):
    engine, hibachi, phoenix, controller = make_farm(tmp_path, style="solo")
    await engine.tick()
    assert hibachi.qty == 0  # opened and flattened
    assert phoenix.qty == 0  # untouched
    farm = engine.state.farm
    assert farm.cycles == 1
    assert "hibachi" in farm.volume_usd and "phoenix" not in farm.volume_usd


async def test_pause_blocks_cycles(tmp_path):
    engine, hibachi, phoenix, controller = make_farm(tmp_path)
    controller.paused = True
    await engine.tick()
    assert engine.state.farm.cycles == 0
    assert "paused" in controller.status_payload()["decision"]


async def test_crashed_cycle_residue_is_flattened(tmp_path):
    engine, hibachi, phoenix, controller = make_farm(tmp_path)
    controller.paused = True  # isolate the cleanup behavior
    hibachi.qty = Decimal("-0.01")  # residue from a crash
    await engine.tick()
    assert hibachi.qty == 0


async def test_failed_cycle_cools_down(tmp_path):
    engine, hibachi, phoenix, controller = make_farm(tmp_path)

    async def fail(request):
        raise VenueUnavailable("phoenix", "down")

    phoenix.place_order = fail
    await engine.tick()
    assert engine.state.phase is Phase.FLAT
    assert engine.state.farm.cycles == 0
    assert engine._next_cycle_at > 0
    assert engine.state.incidents
