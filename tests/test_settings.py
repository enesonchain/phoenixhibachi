"""Dashboard-managed settings: validation, persistence, restart plumbing."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from deltabot.config import BotConfig, HibachiConfig, PhoenixConfig, apply_overrides
from deltabot.control import BotController
from deltabot.dashboard.server import create_app
from deltabot.dashboard.settings import SettingsManager


def make_cfg(tmp_path) -> BotConfig:
    return BotConfig(
        hibachi=HibachiConfig(api_key="k", account_id=1, private_key="p"),
        phoenix=PhoenixConfig(),
        state_path=tmp_path / "state" / "bot_state.json",
    )


def test_settings_roundtrip_via_overrides(tmp_path):
    cfg = make_cfg(tmp_path)
    path = tmp_path / "state" / "settings_overrides.json"
    manager = SettingsManager(cfg, path)

    accepted, errors, needs_restart = manager.apply({
        "mode": "volume",
        "paper": False,
        "volume": {"cycle_notional": "350", "hold_s": "30", "style": "solo"},
    })
    assert not errors and needs_restart
    assert json.loads(path.read_text())["mode"] == "volume"

    # a fresh config load picks the overrides up
    cfg2 = make_cfg(tmp_path)
    apply_overrides(cfg2, path)
    assert cfg2.mode == "volume"
    assert cfg2.paper is False
    assert cfg2.volume.cycle_notional == Decimal("350")
    assert cfg2.volume.hold_s == 30.0
    assert cfg2.volume.style == "solo"


def test_settings_all_or_nothing_on_error(tmp_path):
    cfg = make_cfg(tmp_path)
    path = tmp_path / "overrides.json"
    manager = SettingsManager(cfg, path)
    accepted, errors, _ = manager.apply({
        "mode": "volume",
        "volume": {"cycle_notional": "not-a-number"},
    })
    assert "volume.cycle_notional" in errors
    assert not path.exists()  # nothing written


def test_settings_validation_bounds(tmp_path):
    manager = SettingsManager(make_cfg(tmp_path), tmp_path / "o.json")
    _, errors, _ = manager.apply({
        "mode": "yolo",
        "volume": {"hold_s": "-5", "style": "chaotic"},
    })
    assert set(errors) == {"mode", "volume.hold_s", "volume.style"}


@pytest.fixture
async def client(tmp_path):
    cfg = make_cfg(tmp_path)
    controller = BotController(paper=True, symbols={"hibachi": "X", "phoenix": "Y"})
    controller.publish({"ts": 1.0, "phase": "FLAT"})
    manager = SettingsManager(cfg, tmp_path / "overrides.json")
    app = create_app(controller, settings=manager)
    http = TestClient(TestServer(app))
    await http.start_server()
    yield http, controller, manager
    await http.close()


async def test_settings_endpoints(client):
    http, controller, manager = client
    res = await http.get("/api/settings")
    body = await res.json()
    assert body["mode"] == "funding"

    res = await http.post("/api/settings", json={"mode": "volume"})
    body = await res.json()
    assert body["ok"] and body["needs_restart"]

    res = await http.post("/api/settings", json={"mode": "nope"})
    assert res.status == 422


async def test_restart_action_sets_flag(client):
    http, controller, _ = client
    res = await http.post("/api/control", json={"action": "restart"})
    assert res.status == 200
    assert controller.restart_requested is True


async def test_restart_breaks_engine_loop(tmp_path):
    from deltabot.config import StrategyConfig
    from deltabot.state import StateStore
    from deltabot.strategy.engine import Engine
    from tests.fakes import FakeVenue

    controller = BotController(
        paper=True, symbols={"hibachi": "BTC/USDT-P", "phoenix": "BTC-PERP"}
    )
    controller.paused = True
    controller.restart_requested = True
    engine = Engine(
        FakeVenue("hibachi"), FakeVenue("phoenix"),
        {"hibachi": "BTC/USDT-P", "phoenix": "BTC-PERP"},
        StrategyConfig(poll_interval_s=0),
        StateStore(tmp_path / "s.json"),
        controller=controller,
    )
    await engine.run_forever()  # returns instead of looping forever
