"""Dashboard-managed persistent settings.

The dashboard writes validated overrides to a JSON file next to the state
file; ``load_config`` applies them on top of config.yaml at startup, so
choices made in the browser survive restarts without anyone editing YAML.
Mode and paper/live changes take effect on restart; the dashboard offers a
restart button for exactly that.
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from deltabot.config import BotConfig


def _decimal(value, lo: Decimal, hi: Decimal):
    try:
        v = Decimal(str(value))
    except InvalidOperation:
        return None, "not a number"
    if not (lo <= v <= hi):
        return None, f"must be between {lo} and {hi}"
    return v, None


class SettingsManager:
    """Validates and persists dashboard settings; reports what needs restart."""

    def __init__(self, cfg: BotConfig, path: Path):
        self.cfg = cfg
        self.path = path

    def current(self) -> dict:
        """Effective settings for prefilling the dashboard form."""
        c = self.cfg
        return {
            "mode": c.mode,
            "paper": c.paper,
            "strategy": {
                "entry_apr": str(c.strategy.entry_apr),
                "exit_apr": str(c.strategy.exit_apr),
                "target_notional": str(c.strategy.target_notional),
                "max_notional": str(c.strategy.max_notional),
            },
            "volume": {
                "style": c.volume.style,
                "solo_venue": c.volume.solo_venue,
                "cycle_notional": str(c.volume.cycle_notional),
                "hold_s": c.volume.hold_s,
                "pause_s": c.volume.pause_s,
                "daily_volume_target": str(c.volume.daily_volume_target),
                "max_daily_fees": str(c.volume.max_daily_fees),
            },
        }

    def apply(self, updates: dict) -> tuple[dict, dict[str, str], bool]:
        """Validate ``updates``, merge into the overrides file.

        Returns (accepted, errors, needs_restart). Nothing is written when
        any field fails validation — all-or-nothing keeps the file coherent.
        """
        errors: dict[str, str] = {}
        accepted: dict = {}

        if "mode" in updates:
            if updates["mode"] in ("funding", "volume"):
                accepted["mode"] = updates["mode"]
            else:
                errors["mode"] = "must be funding or volume"
        if "paper" in updates:
            if isinstance(updates["paper"], bool):
                accepted["paper"] = updates["paper"]
            else:
                errors["paper"] = "must be true or false"

        strategy_in = updates.get("strategy", {}) or {}
        strategy_out: dict = {}
        for key, lo, hi in (
            ("entry_apr", Decimal("0"), Decimal("10")),
            ("exit_apr", Decimal("0"), Decimal("10")),
            ("target_notional", Decimal("1"), Decimal("10000000")),
            ("max_notional", Decimal("1"), Decimal("10000000")),
        ):
            if key in strategy_in:
                v, err = _decimal(strategy_in[key], lo, hi)
                if err:
                    errors[f"strategy.{key}"] = err
                else:
                    strategy_out[key] = str(v)
        if strategy_out:
            accepted["strategy"] = strategy_out

        volume_in = updates.get("volume", {}) or {}
        volume_out: dict = {}
        if "style" in volume_in:
            if volume_in["style"] in ("pair", "solo"):
                volume_out["style"] = volume_in["style"]
            else:
                errors["volume.style"] = "must be pair or solo"
        if "solo_venue" in volume_in:
            if volume_in["solo_venue"] in ("hibachi", "phoenix"):
                volume_out["solo_venue"] = volume_in["solo_venue"]
            else:
                errors["volume.solo_venue"] = "must be hibachi or phoenix"
        for key, lo, hi in (
            ("cycle_notional", Decimal("1"), Decimal("1000000")),
            ("daily_volume_target", Decimal("0"), Decimal("1000000000")),
            ("max_daily_fees", Decimal("0"), Decimal("100000")),
            ("hold_s", Decimal("0"), Decimal("86400")),
            ("pause_s", Decimal("0"), Decimal("86400")),
        ):
            if key in volume_in:
                v, err = _decimal(volume_in[key], lo, hi)
                if err:
                    errors[f"volume.{key}"] = err
                elif key in ("hold_s", "pause_s"):
                    volume_out[key] = float(v)
                else:
                    volume_out[key] = str(v)
        if volume_out:
            accepted["volume"] = volume_out

        if errors or not accepted:
            return accepted, errors, False

        stored: dict = {}
        if self.path.exists():
            try:
                stored = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                stored = {}
        for key, value in accepted.items():
            if isinstance(value, dict):
                stored.setdefault(key, {}).update(value)
            else:
                stored[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(stored, indent=2))

        needs_restart = (
            "mode" in accepted
            or "paper" in accepted
            or "volume" in accepted  # farm engine reads these at startup
        )
        return accepted, {}, needs_restart
