"""Pre-trade and in-position risk checks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from deltabot.config import StrategyConfig
from deltabot.models import Balance, FundingSnapshot, MarketSpec, TopOfBook

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskVerdict:
    ok: bool
    reasons: tuple[str, ...] = ()

    @staticmethod
    def fail(*reasons: str) -> "RiskVerdict":
        return RiskVerdict(False, tuple(reasons))

    @staticmethod
    def passed() -> "RiskVerdict":
        return RiskVerdict(True)


def check_entry(
    cfg: StrategyConfig,
    books: dict[str, TopOfBook],
    balances: dict[str, Balance],
    fundings: dict[str, FundingSnapshot],
) -> RiskVerdict:
    reasons: list[str] = []

    for venue, book in books.items():
        if book.bid <= 0 or book.ask <= 0 or book.ask < book.bid:
            reasons.append(f"{venue}: bad top of book ({book.bid}/{book.ask})")
        elif book.spread_bps > cfg.max_book_spread_bps:
            reasons.append(
                f"{venue}: book spread {book.spread_bps:.1f}bps > {cfg.max_book_spread_bps}bps"
            )

    marks = [f.mark_price for f in fundings.values() if f.mark_price > 0]
    if len(marks) == 2 and min(marks) > 0:
        divergence = abs(marks[0] - marks[1]) / min(marks)
        if divergence > cfg.max_mark_divergence:
            reasons.append(f"mark divergence {divergence:.4f} > {cfg.max_mark_divergence}")

    for venue, balance in balances.items():
        if balance.equity <= 0:
            reasons.append(f"{venue}: no equity")

    return RiskVerdict(not reasons, tuple(reasons))


def size_pair(
    cfg: StrategyConfig,
    marks: dict[str, Decimal],
    balances: dict[str, Balance],
    specs: dict[str, MarketSpec],
) -> tuple[Decimal, str | None]:
    """Choose a per-leg quantity. Returns (qty, refusal_reason)."""
    mark = max(marks.values())
    if mark <= 0:
        return Decimal(0), "no valid mark price"

    notional = min(cfg.target_notional, cfg.max_notional)
    # Cap by collateral: leave min_free_collateral_frac of equity untouched and
    # treat the rest as usable margin at 1x (conservative — ignores leverage).
    for venue, balance in balances.items():
        usable = balance.available - balance.equity * cfg.min_free_collateral_frac
        if usable <= 0:
            return Decimal(0), f"{venue}: no usable collateral above reserve"
        notional = min(notional, usable)

    qty = notional / mark
    # Round down to every venue's step so both legs can hold the same quantity.
    for spec in specs.values():
        qty = spec.round_qty(qty)
    if qty <= 0:
        return Decimal(0), "quantity rounds to zero at venue step size"

    for venue, spec in specs.items():
        if qty < spec.min_order_size:
            return Decimal(0), f"{venue}: qty {qty} < min order size {spec.min_order_size}"
        if qty * marks[venue] < spec.min_notional:
            return Decimal(0), f"{venue}: notional below min {spec.min_notional}"

    return qty, None
