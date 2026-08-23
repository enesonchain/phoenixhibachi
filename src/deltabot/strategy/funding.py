"""Funding-spread math.

Convention: a positive funding rate means longs pay shorts. For a pair of
venues (A, B) the carry of holding short-A/long-B is
``annualized(A) - annualized(B)``; the sign of the spread picks the direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from deltabot.models import FundingSnapshot


@dataclass(frozen=True)
class FundingSpread:
    """The tradeable funding differential between two venues.

    ``short_venue`` is the venue whose funding we *receive* by being short
    there (the venue with the higher/more positive rate); ``long_venue`` is
    the other leg. ``annualized`` is the expected net carry (APR fraction,
    e.g. 0.15 = 15%) of that short/long pair, always >= 0.
    """

    short_venue: str
    long_venue: str
    annualized: Decimal
    snapshot_short: FundingSnapshot
    snapshot_long: FundingSnapshot

    @property
    def annualized_pct(self) -> Decimal:
        return self.annualized * 100


def compute_spread(a: FundingSnapshot, b: FundingSnapshot) -> FundingSpread:
    """Orient the pair so the higher-funding venue is the short leg."""
    diff = a.annualized - b.annualized
    if diff >= 0:
        return FundingSpread(
            short_venue=a.venue,
            long_venue=b.venue,
            annualized=diff,
            snapshot_short=a,
            snapshot_long=b,
        )
    return FundingSpread(
        short_venue=b.venue,
        long_venue=a.venue,
        annualized=-diff,
        snapshot_short=b,
        snapshot_long=a,
    )


def carry_of_position(
    short_leg_venue: str, a: FundingSnapshot, b: FundingSnapshot
) -> Decimal:
    """Current annualized carry of an *existing* position whose short leg is on
    ``short_leg_venue``. Negative means the position now bleeds funding."""
    if a.venue == short_leg_venue:
        return a.annualized - b.annualized
    if b.venue == short_leg_venue:
        return b.annualized - a.annualized
    raise ValueError(f"short leg venue {short_leg_venue!r} not in snapshots")
