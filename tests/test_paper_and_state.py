from decimal import Decimal

from deltabot.models import OrderRequest, Side
from deltabot.state import BotState, OpenPair, Phase, StateStore
from deltabot.venues.paper import PaperVenue

from tests.fakes import FakeVenue


async def test_paper_venue_round_trip_realizes_pnl_and_fees():
    real = FakeVenue("phoenix", mark="100")
    paper = PaperVenue(real, start_balance=Decimal("1000"), taker_fee_bps=Decimal("10"))

    await paper.place_order(OrderRequest(symbol="X", side=Side.BUY, qty=Decimal("1")))
    position = await paper.get_position("X")
    assert position.qty == 1
    # bought at the ask (100.01); fee 10bps of 100.01
    assert position.entry_price == Decimal("100.01")

    real.mark = Decimal("110")
    balance = await paper.get_balance()
    assert balance.equity > Decimal("1000")  # unrealized gain visible in equity

    await paper.place_order(
        OrderRequest(symbol="X", side=Side.SELL, qty=Decimal("1"), reduce_only=True)
    )
    position = await paper.get_position("X")
    assert position.qty == 0
    balance = await paper.get_balance()
    # ~+9.98 price pnl minus ~0.21 fees
    assert Decimal("1009") < balance.equity < Decimal("1010")


async def test_paper_reduce_only_cannot_increase():
    real = FakeVenue("phoenix", mark="100")
    paper = PaperVenue(real, start_balance=Decimal("1000"))
    import pytest

    from deltabot.venues.base import OrderRejected

    with pytest.raises(OrderRejected):
        await paper.place_order(
            OrderRequest(symbol="X", side=Side.BUY, qty=Decimal("1"), reduce_only=True)
        )


async def test_paper_short_position_pnl():
    real = FakeVenue("hibachi", mark="100")
    paper = PaperVenue(real, start_balance=Decimal("1000"), taker_fee_bps=Decimal("0"))
    await paper.place_order(OrderRequest(symbol="X", side=Side.SELL, qty=Decimal("2")))
    real.mark = Decimal("90")
    position = await paper.get_position("X")
    assert position.qty == -2
    balance = await paper.get_balance()
    # short 2 @ ~99.99, mark 90 -> ~+20
    assert balance.equity > Decimal("1019")


def test_state_store_round_trip(tmp_path):
    store = StateStore(tmp_path / "s.json")
    state = BotState(
        phase=Phase.OPEN,
        pair=OpenPair(
            short_venue="hibachi",
            long_venue="phoenix",
            qty=Decimal("0.015"),
            entry_spread_apr=Decimal("0.42"),
        ),
        cooldown_until=123.0,
    )
    state.record_incident("test incident")
    store.save(state)

    loaded = store.load()
    assert loaded.phase is Phase.OPEN
    assert loaded.pair.qty == Decimal("0.015")
    assert loaded.pair.entry_spread_apr == Decimal("0.42")
    assert loaded.pair.short_venue == "hibachi"
    assert loaded.cooldown_until == 123.0
    assert loaded.incidents and "test incident" in loaded.incidents[0]


def test_state_store_missing_file_defaults(tmp_path):
    store = StateStore(tmp_path / "missing.json")
    state = store.load()
    assert state.phase is Phase.FLAT
    assert state.pair is None
