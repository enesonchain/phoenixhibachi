from decimal import Decimal

import pytest

from deltabot.models import FundingSnapshot
from deltabot.strategy.funding import carry_of_position, compute_spread


def snap(venue: str, rate: str, interval: int = 1, mark: str = "100") -> FundingSnapshot:
    return FundingSnapshot(
        venue=venue,
        symbol="X",
        rate=Decimal(rate),
        interval_hours=Decimal(interval),
        mark_price=Decimal(mark),
    )


def test_annualization_hourly():
    s = snap("a", "0.0001", interval=1)  # 1bp per hour
    assert s.annualized == Decimal("0.0001") * 24 * 365  # 87.6% APR


def test_annualization_8h_interval():
    s = snap("a", "0.0008", interval=8)  # 8bp per 8h == 1bp/h
    assert s.annualized == Decimal("0.0001") * 24 * 365


def test_spread_orientation_short_the_higher_funding():
    a = snap("hibachi", "0.0002")
    b = snap("phoenix", "0.00005")
    spread = compute_spread(a, b)
    assert spread.short_venue == "hibachi"
    assert spread.long_venue == "phoenix"
    assert spread.annualized == (Decimal("0.00015")) * 24 * 365
    # symmetric: argument order must not matter
    spread2 = compute_spread(b, a)
    assert spread2.short_venue == "hibachi"
    assert spread2.annualized == spread.annualized


def test_spread_with_negative_funding_on_one_leg():
    # phoenix negative funding: being long there *earns*; short hibachi earns too
    a = snap("hibachi", "0.0001")
    b = snap("phoenix", "-0.0001")
    spread = compute_spread(a, b)
    assert spread.short_venue == "hibachi"
    assert spread.annualized == Decimal("0.0002") * 24 * 365


def test_spread_both_negative_shorts_the_less_negative():
    a = snap("hibachi", "-0.0003")
    b = snap("phoenix", "-0.0001")
    spread = compute_spread(a, b)
    # b is higher (less negative) -> short phoenix, long hibachi
    assert spread.short_venue == "phoenix"
    assert spread.annualized == Decimal("0.0002") * 24 * 365


def test_carry_of_position_sign():
    a = snap("hibachi", "0.0002")
    b = snap("phoenix", "0.00005")
    assert carry_of_position("hibachi", a, b) > 0
    assert carry_of_position("phoenix", a, b) < 0
    with pytest.raises(ValueError):
        carry_of_position("nope", a, b)


def test_carry_flips_when_funding_flips():
    a = snap("hibachi", "-0.0002")  # we are short hibachi; now shorts pay
    b = snap("phoenix", "0.0001")
    assert carry_of_position("hibachi", a, b) < 0
