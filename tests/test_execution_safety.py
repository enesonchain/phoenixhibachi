"""Failure-mode tests for the position-verified execution layer."""

from __future__ import annotations

from decimal import Decimal

import pytest

from deltabot.config import StrategyConfig
from deltabot.models import OrderRequest, OrderResult, OrderStatus, Side
from deltabot.state import Phase, StateStore
from deltabot.strategy.engine import Engine
from deltabot.strategy.execution import (
    ExecutionIncident,
    NakedLegError,
    PairExecutor,
)
from deltabot.venues.base import VenueUnavailable

from tests.fakes import FakeVenue

SYMBOLS = {"hibachi": "BTC/USDT-P", "phoenix": "BTC-PERP"}


def make_pair():
    hibachi = FakeVenue("hibachi", mark="65000", step="0.0001")
    phoenix = FakeVenue("phoenix", mark="65000", step="0.0001")
    executor = PairExecutor({"hibachi": hibachi, "phoenix": phoenix}, SYMBOLS)
    return executor, hibachi, phoenix


async def test_ambiguous_error_with_landed_fill_is_not_duplicated():
    """The venue times out on the response but the order actually landed:
    the retry loop must detect the position delta and NOT re-send."""
    executor, hibachi, phoenix = make_pair()

    real_place = phoenix.place_order
    calls = {"n": 0}

    async def landed_but_errored(request: OrderRequest) -> OrderResult:
        calls["n"] += 1
        await real_place(request)  # the fill lands...
        raise VenueUnavailable("phoenix", "timeout")  # ...but we never hear back

    phoenix.place_order = landed_but_errored
    fill = await executor.open_pair("hibachi", "phoenix", Decimal("0.01"), "phoenix")
    assert calls["n"] == 1  # no second market order
    assert phoenix.qty == Decimal("0.01")
    assert hibachi.qty == Decimal("-0.01")
    assert fill.qty == Decimal("0.01")


async def test_unknown_status_resolved_from_position():
    """UNKNOWN order result (e.g. unconfirmed Solana tx) with the fill
    actually applied: executor resolves via position delta."""
    executor, hibachi, phoenix = make_pair()
    real_place = phoenix.place_order

    async def unknown_result(request: OrderRequest) -> OrderResult:
        result = await real_place(request)
        return OrderResult(
            venue=result.venue, symbol=result.symbol, order_id=result.order_id,
            status=OrderStatus.UNKNOWN, filled_qty=Decimal(0),
        )

    phoenix.place_order = unknown_result
    fill = await executor.open_pair("hibachi", "phoenix", Decimal("0.01"), "phoenix")
    assert fill.qty == Decimal("0.01")
    assert phoenix.qty == Decimal("0.01") and hibachi.qty == Decimal("-0.01")


async def test_hedge_failure_restores_both_baselines():
    executor, hibachi, phoenix = make_pair()

    async def always_fail(request):
        raise VenueUnavailable("hibachi", "down")

    hibachi.place_order = always_fail
    with pytest.raises(ExecutionIncident):
        await executor.open_pair("hibachi", "phoenix", Decimal("0.01"), "phoenix")
    assert phoenix.qty == 0  # first leg unwound
    assert hibachi.qty == 0


async def test_partial_hedge_residual_is_cleaned_up():
    """Hedge leg partially fills then errors: the partial fill must be
    flattened along with the first leg."""
    executor, hibachi, phoenix = make_pair()
    real_place = hibachi.place_order
    calls = {"n": 0}

    async def partial_then_fail(request: OrderRequest):
        calls["n"] += 1
        if request.reduce_only:  # let cleanup orders through
            return await real_place(request)
        half = OrderRequest(
            symbol=request.symbol, side=request.side, qty=request.qty / 2,
            client_tag=request.client_tag,
        )
        await real_place(half)
        raise VenueUnavailable("hibachi", "lost connection mid-order")

    hibachi.place_order = partial_then_fail
    with pytest.raises(ExecutionIncident):
        await executor.open_pair("hibachi", "phoenix", Decimal("0.01"), "phoenix")
    assert hibachi.qty == 0  # partial hedge flattened
    assert phoenix.qty == 0  # first leg flattened


async def test_unflattenable_leg_raises_naked_leg_error():
    executor, hibachi, phoenix = make_pair()

    async def always_fail(request):
        raise VenueUnavailable("phoenix", "halted")

    # open a long on phoenix, then make every order (incl. reduce-only) fail
    await phoenix.place_order(
        OrderRequest(symbol="BTC-PERP", side=Side.BUY, qty=Decimal("0.01"))
    )
    phoenix.place_order = always_fail
    with pytest.raises(NakedLegError):
        await executor.flatten_to_baseline("phoenix", Decimal(0))


async def test_naked_leg_halts_engine(tmp_path):
    cfg = StrategyConfig(entry_apr=Decimal("0.10"), exit_apr=Decimal("0.02"),
                         target_notional=Decimal("1000"), poll_interval_s=0)
    hibachi = FakeVenue("hibachi", funding_rate="0.0002", mark="65000", step="0.0001")
    phoenix = FakeVenue("phoenix", funding_rate="0", mark="65000", step="0.0001")
    store = StateStore(tmp_path / "s.json")
    engine = Engine(hibachi, phoenix, SYMBOLS, cfg, store)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN

    async def refuse(request):
        raise VenueUnavailable("hibachi", "close refused")

    hibachi.place_order = refuse
    hibachi.funding_rate = Decimal("0")  # force exit
    await engine.tick()
    assert engine.state.phase is Phase.HALTED
    assert "hibachi" in (engine.state.halt_reason or "")


async def test_incident_while_open_preserves_pair(tmp_path):
    """A transient failure during a rebalance must not wipe the pair record
    or strand the hedge."""
    cfg = StrategyConfig(entry_apr=Decimal("0.10"), exit_apr=Decimal("0.02"),
                         target_notional=Decimal("1000"), poll_interval_s=0,
                         delta_rebalance_notional=Decimal("25"))
    hibachi = FakeVenue("hibachi", funding_rate="0.0002", mark="65000", step="0.0001")
    phoenix = FakeVenue("phoenix", funding_rate="0", mark="65000", step="0.0001")
    store = StateStore(tmp_path / "s.json")
    engine = Engine(hibachi, phoenix, SYMBOLS, cfg, store)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    pair = engine.state.pair

    # drift the delta and make the rebalance order fail transiently
    phoenix.qty += Decimal("0.001")

    async def flaky(request):
        raise VenueUnavailable("hibachi", "brief outage")

    real_place = hibachi.place_order
    hibachi.place_order = flaky
    await engine.tick()
    assert engine.state.phase is Phase.OPEN  # pair NOT wiped
    assert engine.state.pair == pair
    assert engine.state.incidents

    hibachi.place_order = real_place
    await engine.tick()  # rebalance succeeds now
    assert engine.state.phase is Phase.OPEN
    delta = hibachi.qty + phoenix.qty
    assert abs(delta) * hibachi.mark <= Decimal("25")


async def test_flat_with_recognizable_pair_adopts(tmp_path):
    """Lost state file + live hedged pair -> adopt and manage, not halt."""
    cfg = StrategyConfig(entry_apr=Decimal("0.10"), exit_apr=Decimal("0.02"),
                         poll_interval_s=0)
    hibachi = FakeVenue("hibachi", funding_rate="0.0002", mark="65000", step="0.0001")
    phoenix = FakeVenue("phoenix", funding_rate="0", mark="65000", step="0.0001")
    hibachi.qty = Decimal("-0.01")
    phoenix.qty = Decimal("0.01")
    store = StateStore(tmp_path / "fresh.json")
    engine = Engine(hibachi, phoenix, SYMBOLS, cfg, store)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    assert engine.state.pair.short_venue == "hibachi"
    assert engine.state.pair.long_venue == "phoenix"


async def test_flat_with_stray_single_leg_flattens_after_confirmation(tmp_path):
    cfg = StrategyConfig(entry_apr=Decimal("99"), exit_apr=Decimal("0.02"),
                         poll_interval_s=0)  # entry disabled
    hibachi = FakeVenue("hibachi", mark="65000", step="0.0001")
    phoenix = FakeVenue("phoenix", mark="65000", step="0.0001")
    phoenix.qty = Decimal("0.02")  # a leg we don't remember creating
    store = StateStore(tmp_path / "s.json")
    engine = Engine(hibachi, phoenix, SYMBOLS, cfg, store)
    await engine.tick()  # first observation: no action yet
    assert phoenix.qty == Decimal("0.02")
    assert engine.state.phase is Phase.FLAT
    await engine.tick()  # confirmed: flatten
    assert phoenix.qty == 0
    assert engine.state.phase is Phase.FLAT
    assert engine.state.incidents


async def test_flat_with_two_same_direction_legs_halts(tmp_path):
    cfg = StrategyConfig(poll_interval_s=0)
    hibachi = FakeVenue("hibachi", mark="65000", step="0.0001")
    phoenix = FakeVenue("phoenix", mark="65000", step="0.0001")
    hibachi.qty = Decimal("0.5")
    phoenix.qty = Decimal("0.5")
    store = StateStore(tmp_path / "s.json")
    engine = Engine(hibachi, phoenix, SYMBOLS, cfg, store)
    await engine.tick()
    assert engine.state.phase is Phase.HALTED


async def test_rebalance_rounds_to_step():
    executor, hibachi, phoenix = make_pair()
    hibachi.qty = Decimal("0.01")
    traded = await executor.rebalance_delta(
        "hibachi", Decimal("0.00012345"), step=Decimal("0.0001"),
        min_qty=Decimal("0.0001"),
    )
    assert traded == Decimal("0.0001")  # rounded down to the step grid
    tiny = await executor.rebalance_delta(
        "hibachi", Decimal("0.00005"), step=Decimal("0.0001"),
        min_qty=Decimal("0.0001"),
    )
    assert tiny is None  # below one step: no order sent
