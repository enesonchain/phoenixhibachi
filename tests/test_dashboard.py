"""Controller, engine-control, and dashboard-server tests."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from aiohttp.test_utils import TestClient, TestServer

from deltabot.config import StrategyConfig
from deltabot.control import BotController
from deltabot.dashboard.server import create_app
from deltabot.state import Phase, StateStore
from deltabot.strategy.engine import Engine

from tests.fakes import FakeVenue

SYMBOLS = {"hibachi": "BTC/USDT-P", "phoenix": "BTC-PERP"}


def make_engine(tmp_path, controller, hib_rate="0.0002", phx_rate="0"):
    cfg = StrategyConfig(
        entry_apr=Decimal("0.10"), exit_apr=Decimal("0.02"),
        target_notional=Decimal("1000"), first_leg="phoenix", poll_interval_s=0,
    )
    hibachi = FakeVenue("hibachi", funding_rate=hib_rate, mark="65000", step="0.0001")
    phoenix = FakeVenue("phoenix", funding_rate=phx_rate, mark="65000", step="0.0001")
    store = StateStore(tmp_path / "state.json")
    engine = Engine(hibachi, phoenix, SYMBOLS, cfg, store, controller=controller)
    return engine, hibachi, phoenix


# ---------------------------------------------------------- engine controls


async def test_pause_blocks_entry(tmp_path):
    controller = BotController(paper=True, symbols=SYMBOLS)
    controller.paused = True
    engine, hibachi, phoenix = make_engine(tmp_path, controller)
    await engine.tick()
    assert engine.state.phase is Phase.FLAT
    assert hibachi.qty == 0 and phoenix.qty == 0

    controller.paused = False
    await engine.tick()
    assert engine.state.phase is Phase.OPEN


async def test_close_request_exits_pair(tmp_path):
    controller = BotController(paper=True, symbols=SYMBOLS)
    engine, hibachi, phoenix = make_engine(tmp_path, controller)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    controller.request_close()
    controller.paused = True  # keep it from instantly re-entering
    await engine.tick()
    assert engine.state.phase is Phase.FLAT
    assert hibachi.qty == 0 and phoenix.qty == 0


async def test_manual_enter_below_threshold(tmp_path):
    """'Open pair now' forces entry even when the spread is under entry_apr —
    while risk and sizing gates still apply."""
    controller = BotController(paper=True, symbols=SYMBOLS)
    # tiny spread: ~0.9% APR, far below the 10% entry threshold
    engine, hibachi, phoenix = make_engine(tmp_path, controller, hib_rate="0.000001", phx_rate="0")
    await engine.tick()
    assert engine.state.phase is Phase.FLAT
    assert "waiting for entry" in controller.status_payload()["decision"]

    controller.request_enter()
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    assert engine.state.pair.short_venue == "hibachi"
    assert hibachi.qty < 0 and phoenix.qty > 0


async def test_manual_enter_ignored_while_open(tmp_path):
    controller = BotController(paper=True, symbols=SYMBOLS)
    engine, hibachi, phoenix = make_engine(tmp_path, controller)
    await engine.tick()
    assert engine.state.phase is Phase.OPEN
    qty_before = (hibachi.qty, phoenix.qty)
    controller.request_enter()
    await engine.tick()
    assert (hibachi.qty, phoenix.qty) == qty_before  # no doubling
    assert controller.consume_enter_request() is False  # flag was consumed


async def test_decision_line_reflects_pause_and_hold(tmp_path):
    controller = BotController(paper=True, symbols=SYMBOLS)
    engine, hibachi, phoenix = make_engine(tmp_path, controller, hib_rate="0.000001")
    controller.paused = True
    await engine.tick()
    assert "paused" in controller.status_payload()["decision"]
    controller.paused = False
    engine2, *_ = make_engine(tmp_path, controller)
    await engine2.tick()  # enters
    await engine2.tick()  # manages the open pair
    assert "holding" in controller.status_payload()["decision"]


async def test_clear_halt_resumes(tmp_path):
    controller = BotController(paper=True, symbols=SYMBOLS)
    engine, hibachi, phoenix = make_engine(tmp_path, controller)
    engine.state.phase = Phase.HALTED
    engine.state.halt_reason = "test halt"
    controller.paused = True
    await engine.tick()
    assert engine.state.phase is Phase.HALTED  # no clear requested yet
    controller.request_clear_halt()
    await engine.tick()
    assert engine.state.phase is Phase.FLAT
    assert engine.state.halt_reason is None


async def test_config_override_applied_next_tick(tmp_path):
    controller = BotController(paper=True, symbols=SYMBOLS)
    engine, hibachi, phoenix = make_engine(tmp_path, controller)
    errors = controller.set_config({"entry_apr": "0.55", "bogus": "1"})
    assert "bogus" in errors
    controller.paused = True
    await engine.tick()
    assert engine.cfg.entry_apr == Decimal("0.55")


async def test_status_published_after_tick(tmp_path):
    controller = BotController(paper=True, symbols=SYMBOLS)
    engine, hibachi, phoenix = make_engine(tmp_path, controller)
    await engine.tick()
    status = controller.status_payload()
    assert status["phase"] == "OPEN"
    assert status["paper"] is True
    assert set(status["venues"]) == {"hibachi", "phoenix"}
    assert status["spread"]["short_venue"] == "hibachi"
    assert status["venues"]["hibachi"]["position_qty"] < 0
    assert status["total_equity"] > 0
    assert len(status["history"]) == 1
    assert status["history"][0]["spread_apr"] is not None
    # everything must be JSON-serializable
    json.dumps(status)


# --------------------------------------------------------------- http server


@pytest.fixture
async def client():
    controller = BotController(paper=True, symbols=SYMBOLS)
    controller.publish({"ts": 123.0, "phase": "FLAT", "spread": None})
    app = create_app(controller)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client, controller
    await client.close()


async def test_dashboard_serves_ui(client):
    http, _ = client
    res = await http.get("/")
    assert res.status == 200
    text = await res.text()
    assert "phoenixhibachi" in text
    assert "Funding spread" in text


async def test_status_endpoint(client):
    http, _ = client
    res = await http.get("/api/status")
    body = await res.json()
    assert body["phase"] == "FLAT"
    assert body["paper"] is True
    assert "history" in body


async def test_control_endpoint(client):
    http, controller = client
    res = await http.post("/api/control", json={"action": "pause"})
    assert res.status == 200
    assert controller.paused is True
    await http.post("/api/control", json={"action": "resume"})
    assert controller.paused is False
    await http.post("/api/control", json={"action": "close"})
    assert controller.consume_close_request() is True
    res = await http.post("/api/control", json={"action": "self_destruct"})
    assert res.status == 400


async def test_config_endpoint(client):
    http, controller = client
    res = await http.post("/api/config", json={"entry_apr": "0.2", "exit_apr": "abc"})
    assert res.status == 422  # one field invalid -> nothing silently applied
    res = await http.post("/api/config", json={"entry_apr": "0.2"})
    assert res.status == 200
    cfg = StrategyConfig()
    controller.apply_pending_config(cfg)
    assert cfg.entry_apr == Decimal("0.2")
