"""Crash-safe persistent bot state.

The engine is written so that on restart it reconciles this record against
live venue positions; the state file is the bot's memory of *why* a position
exists, not the source of truth for *whether* it exists.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path


class Phase(enum.Enum):
    FLAT = "FLAT"
    ENTERING = "ENTERING"
    OPEN = "OPEN"
    EXITING = "EXITING"
    HALTED = "HALTED"


@dataclass
class OpenPair:
    short_venue: str
    long_venue: str
    qty: Decimal  # per-leg quantity (positive)
    entry_spread_apr: Decimal
    entry_ts: float = field(default_factory=time.time)


@dataclass
class FarmDay:
    """Volume-farming counters for one UTC day."""

    day: str = ""  # YYYY-MM-DD (UTC)
    cycles: int = 0
    volume_usd: dict = field(default_factory=dict)  # venue -> Decimal-as-str ok
    fees_usd: Decimal = Decimal(0)

    def total_volume(self) -> Decimal:
        return sum((Decimal(str(v)) for v in self.volume_usd.values()), Decimal(0))


@dataclass
class BotState:
    phase: Phase = Phase.FLAT
    pair: OpenPair | None = None
    cooldown_until: float = 0.0
    halt_reason: str | None = None
    incidents: list[str] = field(default_factory=list)
    farm: FarmDay = field(default_factory=FarmDay)

    def record_incident(self, message: str) -> None:
        self.incidents.append(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}")
        # keep the file bounded
        self.incidents = self.incidents[-50:]


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            # A corrupt state file must not brick the bot: park the evidence
            # and start fresh — the engine's reconcile pass re-discovers any
            # live positions from the venues (adopt / flatten / halt).
            backup = self.path.with_suffix(f".corrupt-{int(time.time())}")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            logging.getLogger(__name__).error(
                "state file was corrupt; moved to %s and starting fresh", backup
            )
            return BotState()
        pair = None
        if data.get("pair"):
            p = data["pair"]
            pair = OpenPair(
                short_venue=p["short_venue"],
                long_venue=p["long_venue"],
                qty=Decimal(p["qty"]),
                entry_spread_apr=Decimal(p["entry_spread_apr"]),
                entry_ts=float(p.get("entry_ts", 0)),
            )
        farm_data = data.get("farm") or {}
        farm = FarmDay(
            day=str(farm_data.get("day", "")),
            cycles=int(farm_data.get("cycles", 0)),
            volume_usd={k: Decimal(str(v)) for k, v in (farm_data.get("volume_usd") or {}).items()},
            fees_usd=Decimal(str(farm_data.get("fees_usd", "0"))),
        )
        return BotState(
            phase=Phase(data.get("phase", "FLAT")),
            pair=pair,
            cooldown_until=float(data.get("cooldown_until", 0)),
            halt_reason=data.get("halt_reason"),
            incidents=list(data.get("incidents", [])),
            farm=farm,
        )

    def save(self, state: BotState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "phase": state.phase.value,
            "pair": (
                {**asdict(state.pair), "qty": str(state.pair.qty),
                 "entry_spread_apr": str(state.pair.entry_spread_apr)}
                if state.pair
                else None
            ),
            "cooldown_until": state.cooldown_until,
            "halt_reason": state.halt_reason,
            "incidents": state.incidents,
            "farm": {
                "day": state.farm.day,
                "cycles": state.farm.cycles,
                "volume_usd": {k: str(v) for k, v in state.farm.volume_usd.items()},
                "fees_usd": str(state.farm.fees_usd),
            },
        }
        # atomic write so a crash mid-save can't corrupt the file
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
