"""Gameplay adaptive-difficulty events and the explanations shown to players.

This is the IN-GAME rule implemented by the ESP32 firmware (NOT the post-game
Intelligent System suggestion in intelligent_system.py):

    Two failed sequences on the same level reduce the active difficulty by
    exactly one level. Level 1 is the floor (no change, so no event).

The firmware records each real adjustment (it alone detects the consecutive
mistakes) and uploads it with the result. The backend only runs a plausibility
and consistency check of each event against the submitted per-level round data
before showing it; it cannot independently verify that the mistakes were
consecutive.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("lmc")

TRIGGER = "two_mistakes_same_level"
MISTAKES_REQUIRED = 2
MAX_LEVEL = 20  # matches MAX_LEVEL in the firmware


def _int(value: Any) -> int | None:
    """Strict integer parsing: no bools, no truncated floats."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def explain_event(event: dict[str, Any]) -> str:
    return (
        f"Difficulty adjusted: You made two mistakes at Level {event['from_level']}. "
        f"The game has reduced the difficulty to Level {event['to_level']} "
        f"to help you regain consistency."
    )


REQUIRED_FIELDS = ("round", "from_level", "to_level", "mistakes_at_level")


def _level_mistakes(round_data: Any) -> dict[int, int]:
    totals: dict[int, int] = {}
    if isinstance(round_data, list):
        for row in round_data:
            level = _int(row.get("level")) if isinstance(row, dict) else None
            if level is not None:
                totals[level] = totals.get(level, 0) + (_int(row.get("mistakes")) or 0)
    return totals


def check_event(raw: Any, level_mistakes: dict[int, int],
                total_rounds: Any = None) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(event, None)`` if plausible, else ``(None, reason)``."""
    if not isinstance(raw, dict):
        return None, "event is not an object"
    missing = [k for k in REQUIRED_FIELDS if raw.get(k) is None]
    if missing:
        return None, "missing field(s): " + ", ".join(missing)  # nothing is defaulted
    values = {k: _int(raw[k]) for k in REQUIRED_FIELDS}
    bad = [k for k, v in values.items() if v is None]
    if bad:
        return None, "not a valid integer: " + ", ".join(bad)
    round_no, from_level = values["round"], values["from_level"]
    to_level, mistakes = values["to_level"], values["mistakes_at_level"]
    if round_no < 1:
        return None, "round must be a positive number"
    total = _int(total_rounds)
    if total is None:
        return None, "total_rounds was not provided"
    if total < 1:
        return None, "the game has no completed rounds"
    if round_no > total:
        return None, f"round {round_no} is beyond the game's {total} round(s)"
    if from_level <= to_level:
        return None, "from_level must be greater than to_level"
    if to_level != from_level - 1:
        return None, "the drop must be exactly one level"
    if not (2 <= from_level <= MAX_LEVEL):
        return None, "from_level is outside 2..max level (Level 1 is the floor)"
    if mistakes != MISTAKES_REQUIRED:
        return None, f"mistakes_at_level must be {MISTAKES_REQUIRED}"
    if not level_mistakes:
        return None, "no supporting round data"
    if from_level not in level_mistakes:
        return None, f"no round data for Level {from_level}"
    if level_mistakes[from_level] < MISTAKES_REQUIRED:
        return None, f"round data shows fewer than {MISTAKES_REQUIRED} mistakes at Level {from_level}"
    event = {"round": round_no, "from_level": from_level, "to_level": to_level,
             "mistakes_at_level": mistakes, "trigger": TRIGGER}
    event["message"] = explain_event(event)
    return event, None


def validate_events(events: Any, round_data: list[dict[str, Any]] | None = None,
                    total_rounds: Any = None) -> list[dict[str, Any]]:
    """Return explained events that pass a plausibility check; reject the rest.

    This is a consistency check against the submitted per-level totals, not
    independent proof. The aggregate data can show that at least two failed
    sequences happened at ``from_level`` but NOT that they were consecutive:
    the ESP32 detects that condition and remains responsible for it. An event
    without supporting round data is rejected, as is one whose round is not within
    ``1 <= round <= total_rounds`` (``total_rounds`` is required), and no field is
    ever defaulted.
    Rejections are logged with the reason.
    """
    if not isinstance(events, list):
        return []
    level_mistakes = _level_mistakes(round_data)
    valid: list[dict[str, Any]] = []
    for raw in events:
        event, reason = check_event(raw, level_mistakes, total_rounds)
        if event is None:
            logger.warning("Rejected difficulty event: %s", reason)
        else:
            valid.append(event)
    return valid
