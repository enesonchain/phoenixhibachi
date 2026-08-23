"""Config loading and CLI wiring tests."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from deltabot.config import load_config
from deltabot.venues.phoenix.adapter import PhoenixVenue
from deltabot.config import PhoenixConfig

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"

ENV = {
    "HIBACHI_API_KEY": "k",
    "HIBACHI_ACCOUNT_ID": "42",
    "HIBACHI_PRIVATE_KEY": "0x" + "ab" * 32,
    "SOLANA_RPC_URL": "https://rpc.example",
    "PHOENIX_WALLET_PRIVATE_KEY": "wallet-key",
}


def test_example_config_loads_with_env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    cfg = load_config(EXAMPLE)
    assert cfg.paper is True  # example must default to paper
    assert cfg.hibachi.account_id == 42
    assert cfg.hibachi.api_key == "k"
    assert cfg.hibachi.max_fees_percent == Decimal("0.0005")
    assert cfg.hibachi.funding_interval_hours == Decimal("8")
    assert cfg.phoenix.rpc_url == "https://rpc.example"
    assert cfg.strategy.entry_apr == Decimal("0.10")
    assert cfg.strategy.exit_apr < cfg.strategy.entry_apr  # hysteresis sanity


def test_missing_env_var_fails_loudly(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HIBACHI_API_KEY")
    with pytest.raises(KeyError, match="HIBACHI_API_KEY"):
        load_config(EXAMPLE)


def test_wallet_backend_without_key_fails_when_live():
    cfg = PhoenixConfig(execution_backend="wallet", wallet_private_key=None)
    with pytest.raises(ValueError, match="wallet_private_key"):
        PhoenixVenue.from_config(cfg, allow_data_only=False)


async def test_wallet_backend_without_key_ok_in_paper():
    cfg = PhoenixConfig(execution_backend="wallet", wallet_private_key=None)
    venue = PhoenixVenue.from_config(cfg, allow_data_only=True)
    assert venue.trading is None  # data-only, as paper mode expects
    await venue.close()


def test_build_venues_paper_mode(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    # wallet key empty: allowed because paper is true in the example config
    monkeypatch.setenv("PHOENIX_WALLET_PRIVATE_KEY", "")
    from deltabot.main import build_venues

    cfg = load_config(EXAMPLE)
    hibachi, phoenix, symbols = build_venues(cfg)
    from deltabot.venues.paper import PaperVenue

    assert isinstance(hibachi, PaperVenue)
    assert isinstance(phoenix, PaperVenue)
    assert symbols == {"hibachi": "BTC/USDT-P", "phoenix": "BTC-PERP"}
