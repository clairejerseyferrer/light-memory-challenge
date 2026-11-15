"""Gameplay adaptive-difficulty events and the explanations shown to players.

This is the IN-GAME rule implemented by the ESP32 firmware (NOT the post-game
Intelligent System suggestion in intelligent_system.py):

    Two failed sequences on the same level reduce the active difficulty by
    exactly one level. Level 1 is the floor (no change, so no event).

The firmware records each real adjustment and uploads it with the result. The
backend re-validates every event against that rule and against the per-level
telemetry before showing it, so the website can never claim an adjustment the
game did not make.
"""
from __future__ import annotations

from typing import Any

TRIGGER = "two_mistakes_same_level"
MISTAKES_REQUIRED = 2
MAX_LEVEL = 20  # matches MAX_LEVEL in the firmware


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def explain_event(event: dict[str, Any]) -> str:
    return (
        f"Difficulty adjusted: You made two mistakes at Level {event['from_level']}. "
        f"The game has reduced the difficulty to Level {event['to_level']} "
        f"to help you regain consistency."
    )


def validate_events(events: Any, round_data: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return explained, rule-consistent events; drop anything that breaks the rule."""
    if not isinstance(events, list):
        return []
    level_mistakes: dict[int, int] = {}
    for row in round_data or []:
        if isinstance(row, dict) and _int(row.get("level")) is not None:
            level_mistakes[_int(row["level"])] = int(row.get("mistakes") or 0)

    valid: list[dict[str, Any]] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        from_level, to_level = _int(raw.get("from_level")), _int(raw.get("to_level"))
        round_no = _int(raw.get("round"))
        mistakes = _int(raw.get("mistakes_at_level", MISTAKES_REQUIRED))
        if from_level is None or to_level is None:
            continue
        if not (2 <= from_level <= MAX_LEVEL) or to_level != from_level - 1:
            continue  # Level 1 is the floor and a drop is exactly one level.
        if mistakes != MISTAKES_REQUIRED:
            continue
        if level_mistakes and level_mistakes.get(from_level, 0) < MISTAKES_REQUIRED:
            continue  # telemetry shows fewer than two failed sequences on that level
        event = {
            "round": round_no,
            "from_level": from_level,
            "to_level": to_level,
            "mistakes_at_level": MISTAKES_REQUIRED,
            "trigger": TRIGGER,
        }
        event["message"] = explain_event(event)
        valid.append(event)
    return valid
