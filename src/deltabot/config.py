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
    funding_interval_hours: Decimal = Decimal(1)


@dataclass
class PhoenixConfig:
    symbol: str = "BTC-PERP"
    data_api_url: str = "https://perp-api.phoenix.trade"
    ws_url: str | None = None
    funding_interval_hours: Decimal = Decimal(1)
    # Execution backend: "wallet" signs Solana transactions locally;
    # "unsupported" runs Phoenix as data-only (bot refuses live entry).
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
class BotConfig:
    hibachi: HibachiConfig
    phoenix: PhoenixConfig
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
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

    p = _decimals(raw.get("phoenix", {}), ("funding_interval_hours",))
    phoenix = PhoenixConfig(**p)

    s = _decimals(
        raw.get("strategy", {}),
        (
            "entry_apr", "exit_apr", "target_notional", "max_notional",
            "max_book_spread_bps", "delta_rebalance_notional",
            "min_free_collateral_frac", "max_mark_divergence",
        ),
    )
    strategy = StrategyConfig(**s)

    return BotConfig(
        hibachi=hibachi,
        phoenix=phoenix,
        strategy=strategy,
        state_path=Path(raw.get("state_path", "state/bot_state.json")),
        paper=bool(raw.get("paper", True)),
        paper_start_balance=Decimal(str(raw.get("paper_start_balance", "10000"))),
        log_level=str(raw.get("log_level", "INFO")),
    )
