"""Rule-based Intelligent System for Light Memory Challenge.

Rules follow Intelligent_System.pdf (Part 2 and Part 3).
Per-level round_data contract (expanded by FastAPI from the compact ESP32 payload):
[
  {
    "level": 3,
    "sequences_attempted": 5,
    "correct_sequences": 4,
    "mistakes": 1,  # failed sequence attempts, including timeouts
    "incorrect_press_count": 2,  # individual wrong button presses
    "accuracy": 80.0,  # backend recalculates from correct / attempted
    "avg_response_time_ms": 12000,  # average time to complete a whole sequence
    "fastest_response_time_ms": 9000,
    "slowest_response_time_ms": 15000,
    "sequence_length": 4,
    "incorrect_press_details": [[2, 3, 2, 4, 4]]
    # detail tuple = [round, position, pressed_button, expected_button, sequence_length]
  }
]
"""
from __future__ import annotations

from collections import Counter
from typing import Any

RULES_VERSION = "pdf-proposal-1.0"
MAX_GAME_LEVEL = 20  # Matches MAX_LEVEL in the current ESP32 firmware.
CLASS_RANK = {"Struggling": 0, "Average": 1, "Good": 2, "Excellent": 3}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if result >= 0 else default
    except (TypeError, ValueError):
        return default


def _level_rows(round_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept the proposed per-level summary format; ignore malformed rows."""
    rows = []
    for item in round_data or []:
        if not isinstance(item, dict):
            continue
        # Accept either one summary per level or event-like rows carrying level.
        if "level" not in item or "sequences_attempted" not in item:
            continue
        rows.append(item)
    return sorted(rows, key=lambda row: _number(row.get("level")))


def _metrics(row: dict[str, Any]) -> dict[str, Any]:
    attempted = int(_number(row.get("sequences_attempted")))
    correct = int(_number(row.get("correct_sequences")))
    mistakes = int(_number(row.get("mistakes")))
    accuracy = (
        _number(row.get("accuracy"), -1)
        if row.get("accuracy") is not None
        else (correct / attempted * 100 if attempted else 0.0)
    )
    if accuracy < 0:
        accuracy = 0.0
    sequence_length = _number(row.get("sequence_length"))
    avg_ms = row.get("avg_response_time_ms")
    time_per_press = (
        _number(avg_ms) / 1000.0 / sequence_length
        if avg_ms is not None and sequence_length > 0
        else None
    )
    mistake_rate = mistakes / attempted if attempted else 0.0
    return {
        "level": int(_number(row.get("level"))),
        "sequences_attempted": attempted,
        "correct_sequences": correct,
        "mistakes": mistakes,
        "incorrect_press_count": int(_number(row.get("incorrect_press_count"))),
        "accuracy": accuracy,
        "time_per_button_press_seconds": time_per_press,
        "mistake_rate": mistake_rate,
        "sequence_length": int(sequence_length),
        "incorrect_press_details": (
            row.get("incorrect_press_details")
            if isinstance(row.get("incorrect_press_details"), list) else []
        ),
    }


def _band(value: float | None, excellent: float, good: float, average: float,
          lower_is_better: bool = False) -> str:
    if value is None:
        return "Average"
    if lower_is_better:
        if value <= excellent:
            return "Excellent"
        if value <= good:
            return "Good"
        if value <= average:
            return "Average"
        return "Struggling"
    if value >= excellent:
        return "Excellent"
    if value >= good:
        return "Good"
    if value >= average:
        return "Average"
    return "Struggling"


def classify_level(metrics: dict[str, Any]) -> dict[str, Any]:
    """Apply Part 2.1 and 2.2 to one level."""
    if metrics["sequences_attempted"] < 3:
        return {
            "classification": "Average",
            "difficulty_adjustment": 0,
            "reason": "Insufficient data: at least 3 sequences are required.",
            "metric_bands": {},
        }

    accuracy_band = _band(metrics["accuracy"], 90, 80, 60)
    speed_band = _band(
        metrics["time_per_button_press_seconds"], 1.0, 1.5, 2.5, True
    )
    mistake_band = _band(metrics["mistake_rate"], 0.10, 0.25, 0.50, True)

    # Accuracy establishes the base class, capped at Good unless all metrics
    # are Excellent. Conflicting lower speed AND mistake bands demote one band.
    if accuracy_band == speed_band == mistake_band == "Excellent":
        classification = "Excellent"
    else:
        classification = min(
            accuracy_band, "Good", key=lambda name: CLASS_RANK[name]
        )
        if (
            CLASS_RANK[speed_band] < CLASS_RANK[classification]
            and CLASS_RANK[mistake_band] < CLASS_RANK[classification]
        ):
            classification = next(
                name for name, rank in CLASS_RANK.items()
                if rank == max(0, CLASS_RANK[classification] - 1)
            )

    # Adaptive difficulty rules from Part 2.3.
    if classification == "Excellent":
        adjustment = (
            2 if metrics["accuracy"] >= 95 and metrics["mistake_rate"] <= 0.05
            else 1
        )
    elif classification == "Good":
        adjustment = 0 if "Struggling" in (speed_band, mistake_band) else 1
    elif classification == "Average":
        adjustment = 0
    elif metrics["accuracy"] >= 50:
        adjustment = 0
    elif metrics["accuracy"] >= 30:
        adjustment = -1
    else:
        adjustment = -2

    # Explicit safety rule: any struggling metric blocks an increase.
    if "Struggling" in (accuracy_band, speed_band, mistake_band):
        adjustment = min(0, adjustment)

    return {
        "classification": classification,
        "difficulty_adjustment": adjustment,
        "reason": "Classification based on accuracy, time per button press, and mistake rate.",
        "metric_bands": {
            "accuracy": accuracy_band,
            "speed": speed_band,
            "mistake_rate": mistake_band,
        },
    }


def _recommendations(round_data: list[dict[str, Any]], metrics: dict[str, Any],
                     decision: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    if metrics["sequences_attempted"] >= 3 and metrics["accuracy"] < 60:
        recommendations.append(
            "Focus on memorizing the complete sequence before inputting."
        )

    speed = metrics["time_per_button_press_seconds"]
    if speed is not None and speed > 2.5:
        recommendations.append(
            "Try improving reaction speed by responding faster after the sequence display."
        )

    # Rule 4: repeated mistakes on one specific button. Rule 3 (general
    # frequent mistakes) is only used when this more specific rule does not fire.
    button_counts: Counter[str] = Counter()
    all_mistake_positions: list[tuple[int, int]] = []
    for row in round_data or []:
        if not isinstance(row, dict):
            continue
        values = row.get("incorrect_buttons", [])
        if isinstance(values, list):
            button_counts.update(str(v) for v in values if v is not None)
        details = row.get("incorrect_press_details", [])
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, dict):
                    button = detail.get("pressed_button")
                    pos = int(_number(detail.get("position")))
                    seq_len = int(_number(detail.get("sequence_length")))
                elif isinstance(detail, list) and len(detail) >= 5:
                    pos = int(_number(detail[1]))
                    button = detail[2]
                    seq_len = int(_number(detail[4]))
                else:
                    continue
                if button is not None:
                    button_counts.update([str(button)])
                if pos > 0 and seq_len > 0:
                    all_mistake_positions.append((pos, seq_len))
    total_button_mistakes = sum(button_counts.values())
    repeated_button = None
    if total_button_mistakes >= 3 and button_counts:
        button, count = button_counts.most_common(1)[0]
        if count >= 3 and count / total_button_mistakes >= 0.40:
            repeated_button = button
            recommendations.append(
                f"Review and practice the {button} button, which accounts for "
                f"{count / total_button_mistakes:.0%} of recorded button mistakes."
            )
    if (
        not repeated_button
        and metrics["sequences_attempted"] >= 3
        and metrics["mistake_rate"] > 0.50
    ):
        recommendations.append(
            "Practice remembering button positions and sequence patterns."
        )

    # Rule 5: compare upper two played levels against lower levels.
    valid = [_metrics(row) for row in _level_rows(round_data)]
    valid = [m for m in valid if m["sequences_attempted"] > 0]
    if len({m["level"] for m in valid}) >= 4:
        by_level = {m["level"]: m for m in valid}
        levels = sorted(by_level)
        lower = [by_level[level] for level in levels[:-2]]
        upper = [by_level[level] for level in levels[-2:]]
        low_attempts = sum(m["sequences_attempted"] for m in lower)
        high_attempts = sum(m["sequences_attempted"] for m in upper)
        low_rate = sum(m["mistakes"] for m in lower) / low_attempts if low_attempts else 0
        high_rate = sum(m["mistakes"] for m in upper) / high_attempts if high_attempts else 0
        if high_rate >= 2 * low_rate and high_rate - low_rate >= 0.20:
            recommendations.append(
                "Practice longer sequences before attempting harder difficulty."
            )

    # Rule 6: fast but inaccurate replaces the ordinary low-accuracy message.
    if (
        metrics["accuracy"] < 60
        and speed is not None
        and speed <= 1.0
        and metrics["sequences_attempted"] >= 3
    ):
        recommendations = [
            item for item in recommendations
            if not item.startswith("Focus on memorizing")
        ]
        recommendations.insert(
            0, "Slow down a little and recall the whole sequence before pressing."
        )

    # Rule 7: late-sequence errors; requires position telemetry.
    # Rule 7: late-sequence errors, using the position of each individual
    # incorrect press supplied by the firmware.
    if len(all_mistake_positions) >= 5:
        late = sum(1 for pos, length in all_mistake_positions if pos > length / 2)
        if late / len(all_mistake_positions) >= 0.60:
            recommendations.append(
                "Try grouping the sequence into small chunks of 2 to 3 buttons."
            )

    # Rule 8: positive reinforcement only when no weakness recommendation fired.
    if not recommendations and decision["classification"] in ("Excellent", "Good"):
        recommendations.append("Great job! You are ready for a harder challenge.")
    return recommendations


def analyze_performance(accuracy, mistakes, avg_response_time_ms, round_data=None,
                        highest_level=None, max_level=MAX_GAME_LEVEL):
    """Return dashboard-ready IS classification, difficulty, and recommendations.

    The PDF specifies decisions per completed level. When level summaries are
    supplied, the latest/highest level is classified. `highest_level` remains
    accepted for call compatibility but is not used as the game-level cap;
    `max_level` is the configured game maximum. If level summaries are absent,
    the function returns an explicit insufficient-data result rather than
    silently treating session totals as per-level metrics.
    """
    round_data = round_data or []
    level_rows = _level_rows(round_data)
    if not level_rows:
        return {
            "classification": "Average",
            "proposed_level_adjustment": 0,
            "recommendations": [
                "IS classification is provisional: per-level sequence count, "
                "sequence length, response time, and mistake data are required."
            ],
            "rules_version": RULES_VERSION,
            "data_status": "insufficient_per_level_data",
            "missing_data": [
                "sequences_attempted_per_level",
                "sequence_length",
                "per_level_average_sequence_response_time",
                "per_level_mistakes",
                "incorrect_press_positions",
            ],
        }

    latest_row = max(level_rows, key=lambda row: _number(row.get("level")))
    metrics = _metrics(latest_row)
    decision = classify_level(metrics)
    # `highest_level` is the player's achieved level, not the game's cap.
    # The cap is the configured maximum level in the firmware.
    current_level = metrics["level"]
    target_level = current_level + decision["difficulty_adjustment"]
    target_level = min(int(_number(max_level, MAX_GAME_LEVEL)), target_level)
    target_level = max(1, target_level)

    recommendations = _recommendations(round_data, metrics, decision)
    per_level_analysis = []
    for row in level_rows:
        row_metrics = _metrics(row)
        row_decision = classify_level(row_metrics)
        per_level_analysis.append({
            **row_metrics,
            "classification": row_decision["classification"],
            "proposed_level_adjustment": row_decision["difficulty_adjustment"],
            "metric_bands": row_decision["metric_bands"],
            "data_sufficient": row_metrics["sequences_attempted"] >= 3,
        })
    if metrics["sequences_attempted"] < 3:
        recommendations = [
            "None. Wait until at least 3 sequences have been attempted in this level."
        ]

    return {
        "classification": decision["classification"],
        "proposed_level_adjustment": target_level - current_level,
        "recommended_level": target_level,
        "current_level": current_level,
        "reason": decision["reason"],
        "metric_bands": decision["metric_bands"],
        "metrics": metrics,
        "per_level_analysis": per_level_analysis,
        "recommendations": recommendations,
        "rules_version": RULES_VERSION,
        "data_status": (
            "complete_for_classification"
            if metrics["sequences_attempted"] >= 3
            else "insufficient_sequences_for_classification"
        ),
        "minimum_sequences_required": 3,
    }
