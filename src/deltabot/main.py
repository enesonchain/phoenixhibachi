"""CLI entrypoint.

Commands:
    phxbot run --config config.yaml          # run the strategy loop
    phxbot status --config config.yaml       # one-shot: funding, positions, spread
    phxbot close --config config.yaml        # flatten both venues and exit
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from deltabot.config import BotConfig, load_config
from deltabot.state import StateStore
from deltabot.strategy.engine import Engine
from deltabot.strategy.execution import PairExecutor
from deltabot.strategy.funding import compute_spread
from deltabot.telemetry import setup_logging
from deltabot.venues.base import PerpVenue
from deltabot.venues.hibachi.adapter import HibachiVenue
from deltabot.venues.paper import PaperVenue
from deltabot.venues.phoenix.adapter import PhoenixVenue

log = logging.getLogger(__name__)


def build_venues(cfg: BotConfig) -> tuple[PerpVenue, PerpVenue, dict[str, str]]:
    hibachi: PerpVenue = HibachiVenue(
        api_key=cfg.hibachi.api_key,
        account_id=cfg.hibachi.account_id,
        private_key=cfg.hibachi.private_key,
        api_url=cfg.hibachi.api_url,
        data_api_url=cfg.hibachi.data_api_url,
        max_fees_percent=cfg.hibachi.max_fees_percent,
        funding_interval_hours=cfg.hibachi.funding_interval_hours,
    )
    phoenix: PerpVenue = PhoenixVenue.from_config(cfg.phoenix, allow_data_only=cfg.paper)
    if cfg.paper:
        # Taker fees: Hibachi default 4.5 bps, Phoenix 3.5 bps.
        from decimal import Decimal

        hibachi = PaperVenue(hibachi, start_balance=cfg.paper_start_balance,
                             taker_fee_bps=Decimal("4.5"))
        phoenix = PaperVenue(phoenix, start_balance=cfg.paper_start_balance,
                             taker_fee_bps=Decimal("3.5"))
    symbols = {"hibachi": cfg.hibachi.symbol, "phoenix": cfg.phoenix.symbol}
    return hibachi, phoenix, symbols


def _acquire_instance_lock(cfg: BotConfig):
    """One bot process per state file: two engines trading the same accounts
    would double positions. Returns the held lock file object."""
    import fcntl

    lock_path = cfg.state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            f"another bot instance already holds {lock_path}; refusing to start"
        ) from None
    return lock_file


async def cmd_run(cfg: BotConfig) -> int:
    lock = _acquire_instance_lock(cfg)
    hibachi, phoenix, symbols = build_venues(cfg)

    controller = None
    dashboard_runner = None
    if cfg.dashboard.enabled:
        from deltabot.config import overrides_path
        from deltabot.control import BotController
        from deltabot.dashboard.server import start_dashboard
        from deltabot.dashboard.settings import SettingsManager

        controller = BotController(paper=cfg.paper, symbols=symbols)
        dashboard_runner = await start_dashboard(
            controller, host=cfg.dashboard.host, port=cfg.dashboard.port,
            settings=SettingsManager(cfg, overrides_path(cfg)),
        )
        log.info(
            "dashboard: http://%s:%d (local only — it can close positions)",
            cfg.dashboard.host, cfg.dashboard.port,
        )

    if cfg.mode == "volume":
        from deltabot.strategy.farm import FarmEngine

        engine = FarmEngine(
            hibachi, phoenix, symbols, cfg.strategy, cfg.volume,
            StateStore(cfg.state_path), controller=controller,
        )
    else:
        engine = Engine(
            hibachi, phoenix, symbols, cfg.strategy, StateStore(cfg.state_path),
            controller=controller,
        )
    if cfg.paper:
        log.info("PAPER MODE: live market data, simulated fills — no real orders")
    try:
        await engine.run_forever()
    finally:
        if dashboard_runner is not None:
            await dashboard_runner.cleanup()
        await hibachi.close()
        await phoenix.close()
        lock.close()
    if controller is not None and controller.restart_requested:
        return 42  # the launcher loop restarts us on exactly this code
    return 1 if engine.state.halt_reason else 0


async def cmd_status(cfg: BotConfig) -> int:
    hibachi, phoenix, symbols = build_venues(cfg)
    try:
        for venue in (hibachi, phoenix):
            symbol = symbols[venue.name]
            funding = await venue.get_funding(symbol)
            book = await venue.get_top_of_book(symbol)
            position = await venue.get_position(symbol)
            print(
                f"{venue.name:>8} {symbol:<12} mark={funding.mark_price} "
                f"bid/ask={book.bid}/{book.ask} "
                f"funding={funding.rate} per {funding.interval_hours}h "
                f"({funding.annualized * 100:.2f}% APR) position={position.qty}"
            )
        funding_a = await hibachi.get_funding(symbols["hibachi"])
        funding_b = await phoenix.get_funding(symbols["phoenix"])
        spread = compute_spread(funding_a, funding_b)
        print(
            f"\nspread: {spread.annualized_pct:.2f}% APR "
            f"(short {spread.short_venue} / long {spread.long_venue})"
        )
    finally:
        await hibachi.close()
        await phoenix.close()
    return 0


async def cmd_close(cfg: BotConfig) -> int:
    if cfg.paper:
        print(
            "paper mode: simulated positions live only inside a running bot "
            "process — nothing to close. Set paper: false to close real positions."
        )
        return 0
    hibachi, phoenix, symbols = build_venues(cfg)
    try:
        executor = PairExecutor(
            {hibachi.name: hibachi, phoenix.name: phoenix}, symbols
        )
        # close_pair flattens whatever exists on both venues regardless of side
        await executor.close_pair("hibachi", "phoenix")
        print("both venues flat")
    finally:
        await hibachi.close()
        await phoenix.close()
    return 0


def cli() -> None:
    parser = argparse.ArgumentParser(prog="phxbot", description=__doc__)
    parser.add_argument("command", choices=["run", "status", "close"])
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg.log_level)
    command = {"run": cmd_run, "status": cmd_status, "close": cmd_close}[args.command]
    sys.exit(asyncio.run(command(cfg)))


if __name__ == "__main__":
    cli()
