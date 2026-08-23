"""YAML config loading with ${ENV_VAR} interpolation for secrets."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(value):
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            var = m.group(1)
            if var not in os.environ:
                raise KeyError(f"config references ${{{var}}} but it is not set in the environment")
            return os.environ[var]
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


@dataclass
class HibachiConfig:
    api_key: str
    account_id: int
    private_key: str
    symbol: str = "BTC/USDT-P"
    api_url: str | None = None
    data_api_url: str | None = None
    # Decimal fraction, not percent: 0.0005 = 5 bps; must cover your taker fee.
    max_fees_percent: Decimal = Decimal("0.0005")
    # Fallback only — the adapter infers the actual cadence from live
    # settlement history (observed hourly; older docs said 8h).
    funding_interval_hours: Decimal = Decimal(1)


@dataclass
class PhoenixConfig:
    symbol: str = "BTC-PERP"
    data_api_url: str = "https://perp-api.phoenix.trade"
    # Execution backend: "wallet" signs Solana transactions locally;
    # "data-only" serves market data only (paper mode works fully).
    execution_backend: str = "wallet"
    rpc_url: str | None = None
    wallet_private_key: str | None = None  # base58 Solana keypair, wallet backend only


@dataclass
class StrategyConfig:
    # Enter when |net annualized funding spread| >= entry_apr (fraction: 0.10 = 10%).
    entry_apr: Decimal = Decimal("0.10")
    # Exit when carry of the open position drops below exit_apr (hysteresis).
    exit_apr: Decimal = Decimal("0.02")
    # Target notional per leg in settlement currency (USD).
    target_notional: Decimal = Decimal("200")
    max_notional: Decimal = Decimal("1000")
    # Refuse entry if a venue's top-of-book spread is wider than this (bps).
    max_book_spread_bps: Decimal = Decimal("20")
    # Rebalance when |net delta| * mark exceeds this (USD).
    delta_rebalance_notional: Decimal = Decimal("25")
    # Keep at least this fraction of equity free on each venue.
    min_free_collateral_frac: Decimal = Decimal("0.25")
    # Halt if venue marks diverge by more than this fraction (oracle sanity).
    max_mark_divergence: Decimal = Decimal("0.01")
    # Which venue's leg to place first on entry (less liquid first).
    first_leg: str = "phoenix"
    # Seconds between decision ticks.
    poll_interval_s: float = 15.0
    # After an execution incident, stay flat this long.
    cooldown_s: float = 300.0


@dataclass
class VolumeConfig:
    """Volume-farming mode: cycle delta-neutral pairs to generate venue
    volume at a controlled, budgeted cost."""

    # "pair" = hedged cycles across both venues (near-zero price risk).
    # "solo" = open+close on one venue only (seconds of directional exposure
    # per cycle) — for farming a venue before the other one is available.
    style: str = "pair"
    solo_venue: str = "hibachi"
    # Notional per cycle leg in USD.
    cycle_notional: Decimal = Decimal("200")
    # Hold the position this long before closing (some points programs
    # weight held open interest; longer hold also looks less bot-like).
    hold_s: float = 60.0
    # Pause between cycles.
    pause_s: float = 120.0
    # Hard daily stops (UTC day): whichever hits first ends farming for the
    # day. Fees are estimated from taker rates; budget them like a real cost.
    daily_volume_target: Decimal = Decimal("50000")  # summed across venues
    max_daily_fees: Decimal = Decimal("10")
    # Taker fee estimates per venue (fraction), used for budget accounting.
    taker_fee: dict = field(default_factory=lambda: {
        "hibachi": Decimal("0.00045"), "phoenix": Decimal("0.00035"),
    })


@dataclass
class DashboardConfig:
    enabled: bool = True
    # 127.0.0.1 only, deliberately: this port can close positions. Put real
    # auth in front before ever binding wider.
    host: str = "127.0.0.1"
    port: int = 8790


@dataclass
class BotConfig:
    hibachi: HibachiConfig
    phoenix: PhoenixConfig
    # "funding" = carry the funding-rate spread; "volume" = farm volume with
    # budgeted delta-neutral cycles.
    mode: str = "funding"
    volume: VolumeConfig = field(default_factory=VolumeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    state_path: Path = Path("state/bot_state.json")
    paper: bool = True
    paper_start_balance: Decimal = Decimal("10000")
    log_level: str = "INFO"


def _decimals(d: dict, keys: tuple[str, ...]) -> dict:
    return {k: (Decimal(str(v)) if k in keys and v is not None else v) for k, v in d.items()}


def load_config(path: str | Path) -> BotConfig:
    raw = yaml.safe_load(Path(path).read_text())
    raw = _interpolate(raw)

    h = _decimals(raw["hibachi"], ("max_fees_percent", "funding_interval_hours"))
    hibachi = HibachiConfig(**{**h, "account_id": int(h["account_id"])})

    phoenix = PhoenixConfig(**raw.get("phoenix", {}))

    s = _decimals(
        raw.get("strategy", {}),
        (
            "entry_apr", "exit_apr", "target_notional", "max_notional",
            "max_book_spread_bps", "delta_rebalance_notional",
            "min_free_collateral_frac", "max_mark_divergence",
        ),
    )
    strategy = StrategyConfig(**s)

    dashboard = DashboardConfig(**raw.get("dashboard", {}))

    v = _decimals(
        raw.get("volume", {}),
        ("cycle_notional", "daily_volume_target", "max_daily_fees"),
    )
    if "taker_fee" in v:
        v["taker_fee"] = {k: Decimal(str(x)) for k, x in v["taker_fee"].items()}
    volume = VolumeConfig(**v)

    return BotConfig(
        hibachi=hibachi,
        phoenix=phoenix,
        mode=str(raw.get("mode", "funding")),
        volume=volume,
        strategy=strategy,
        dashboard=dashboard,
        state_path=Path(raw.get("state_path", "state/bot_state.json")),
        paper=bool(raw.get("paper", True)),
        paper_start_balance=Decimal(str(raw.get("paper_start_balance", "10000"))),
        log_level=str(raw.get("log_level", "INFO")),
    )
