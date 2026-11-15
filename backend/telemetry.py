"""Expansion of the ESP32's compact result payload (pure Python, no web deps)."""
from __future__ import annotations

from typing import Any

ROUND_DATA_ALIASES = {
    "l": "level",
    "a": "sequences_attempted",
    "c": "correct_sequences",
    "m": "mistakes",
    "ip": "incorrect_press_count",
    "acc": "accuracy",
    "avg": "avg_response_time_ms",
    "fast": "fastest_response_time_ms",
    "slow": "slowest_response_time_ms",
    "n": "sequence_length",
    "d": "incorrect_press_details",
}

DIFFICULTY_EVENT_ALIASES = {
    "r": "round",
    "f": "from_level",
    "t": "to_level",
    "m": "mistakes_at_level",
}


def _expand(item: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    row = dict(item)
    for short, full in aliases.items():
        if full not in row and short in row:
            row[full] = row.pop(short)
    return row


def normalize_round_data(value: Any) -> list[dict[str, Any]]:
    """Expand compact keys; per-level accuracy is recomputed from the counts."""
    if not isinstance(value, list):
        raise ValueError("round_data must be a list.")
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each round_data entry must be an object.")
        row = _expand(item, ROUND_DATA_ALIASES)
        attempted = row.get("sequences_attempted")
        correct = row.get("correct_sequences")
        if isinstance(attempted, (int, float)) and attempted > 0 and isinstance(correct, (int, float)):
            row["accuracy"] = round(100.0 * correct / attempted, 2)
        normalized.append(row)
    return normalized


def normalize_difficulty_events_input(value: Any) -> list[dict[str, Any]]:
    """Expand compact gameplay difficulty events; absent/None means no events."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("difficulty_events must be a list.")
    events = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Each difficulty_events entry must be an object.")
        events.append(_expand(item, DIFFICULTY_EVENT_ALIASES))
    return events
