"""Shared control surface between the engine and the dashboard server.

The engine is the only writer of status and the only consumer of control
flags; the dashboard server is the only writer of control flags and the only
reader of status. Everything runs on one event loop, so plain attributes are
safe — no locking needed.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# Strategy fields the dashboard may tune at runtime.
TUNABLE_FIELDS = ("entry_apr", "exit_apr", "target_notional", "max_notional")

HISTORY_MAX_POINTS = 2880  # 12h of 15s ticks


@dataclass
class BotController:
    paper: bool
    symbols: dict[str, str]

    paused: bool = False
    restart_requested: bool = False
    _close_requested: bool = False
    _enter_requested: bool = False
    _clear_halt_requested: bool = False
    _pending_config: dict[str, Decimal] = field(default_factory=dict)

    status: dict = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_MAX_POINTS))
    started_at: float = field(default_factory=time.time)

    # ------------------------------------------------------------- controls

    def request_close(self) -> None:
        self._close_requested = True

    def consume_close_request(self) -> bool:
        requested, self._close_requested = self._close_requested, False
        return requested

    def request_enter(self) -> None:
        self._enter_requested = True

    def consume_enter_request(self) -> bool:
        requested, self._enter_requested = self._enter_requested, False
        return requested

    def request_clear_halt(self) -> None:
        self._clear_halt_requested = True

    def consume_clear_halt(self) -> bool:
        requested, self._clear_halt_requested = self._clear_halt_requested, False
        return requested

    def set_config(self, updates: dict[str, str]) -> dict[str, str]:
        """Stage strategy-config overrides; returns {field: error} rejects."""
        errors: dict[str, str] = {}
        for key, raw in updates.items():
            if key not in TUNABLE_FIELDS:
                errors[key] = "not tunable"
                continue
            try:
                value = Decimal(str(raw))
            except InvalidOperation:
                errors[key] = "not a number"
                continue
            if value < 0:
                errors[key] = "must be >= 0"
                continue
            self._pending_config[key] = value
        return errors

    def apply_pending_config(self, cfg) -> None:
        """Called by the engine at tick start; applies staged overrides."""
        if not self._pending_config:
            return
        for key, value in self._pending_config.items():
            setattr(cfg, key, value)
        self._pending_config.clear()

    # --------------------------------------------------------------- status

    def publish(self, status: dict) -> None:
        """Engine pushes a fresh status snapshot after every tick."""
        status["paused"] = self.paused
        status["paper"] = self.paper
        status["symbols"] = self.symbols
        status["started_at"] = self.started_at
        self.status = status
        spread = (status.get("spread") or {}).get("annualized")
        point = {
            "ts": status.get("ts", time.time()),
            "spread_apr": spread,
            "carry_apr": status.get("carry_apr"),
            "equity": status.get("total_equity"),
        }
        self.history.append(point)

    def status_payload(self) -> dict:
        return {**self.status, "history": list(self.history)}
