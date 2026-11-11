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

RULES_VERSION = "pdf-proposal-1.1"
MIN_SEQUENCES_FOR_CLASSIFICATION = 1
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
    if metrics["sequences_attempted"] < MIN_SEQUENCES_FOR_CLASSIFICATION:
        return {
            "classification": "Average",
            "difficulty_adjustment": 0,
            "reason": "No sequence data is available for this level.",
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
    if metrics["sequences_attempted"] >= 1 and metrics["accuracy"] < 60:
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
        and metrics["sequences_attempted"] >= 1
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
        and metrics["sequences_attempted"] >= 1
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
    if len(all_mistake_positions) >= 2:
        late = sum(1 for pos, length in all_mistake_positions if pos > length / 2)
        if late / len(all_mistake_positions) >= 0.60:
            recommendations.append(
                "Try grouping the sequence into small chunks of 2 to 3 buttons."
            )

    # Rule 8: positive reinforcement only when no weakness recommendation fired.
    if not recommendations and decision["classification"] in ("Excellent", "Good"):
        recommendations.append("Great job! You are ready for a harder challenge.")
    return recommendations


def _explain_recommendations(recommendations: list[str], metrics: dict[str, Any],
                             decision: dict[str, Any]) -> list[dict[str, str]]:
    """Pair every recommendation with the observed data that triggered it.

    Additive companion to `recommendations` (which keeps its original string
    list shape so the dashboard is unaffected).
    """
    speed = metrics["time_per_button_press_seconds"]
    explanations = {
        "Focus on memorizing": f"Accuracy on the latest level was {metrics['accuracy']:.0f}% (below 60%).",
        "Try improving reaction speed": (
            f"Average time per button press was {speed:.1f} s (above 2.5 s)." if speed is not None else
            "Response time per button was slower than the 2.5 s guideline."),
        "Review and practice": "A single button accounted for a large share of recorded wrong presses.",
        "Practice remembering button positions": (
            f"{metrics['mistake_rate']:.0%} of sequence attempts on the latest level failed (above 50%)."),
        "Practice longer sequences": "Failure rate in the two highest levels played was at least double that of the lower levels.",
        "Slow down a little": (
            f"Presses were fast ({speed:.1f} s each) but accuracy was only {metrics['accuracy']:.0f}%."
            if speed is not None else "Presses were fast but accuracy was low."),
        "Try grouping": "At least 60% of wrong presses happened in the second half of the sequence.",
        "Great job": f"Classified {decision['classification']} with no weakness detected on the latest level.",
    }
    details = []
    for text in recommendations:
        reason = next((why for prefix, why in explanations.items() if text.startswith(prefix)),
                      "Generated by the recommendation rules from the observed level data.")
        details.append({"recommendation": text, "reason": reason})
    return details


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
            "data_sufficient": row_metrics["sequences_attempted"] >= 1,
            "confidence": "moderate" if row_metrics["sequences_attempted"] >= 2 else ("low" if row_metrics["sequences_attempted"] == 1 else "none"),
        })

    ordered_metrics = [_metrics(row) for row in level_rows]
    observed = [m for m in ordered_metrics if m["sequences_attempted"] > 0]
    strengths = []
    weaknesses = []
    for metric_name, band_key in (("accuracy", "accuracy"), ("response speed", "speed"), ("mistake control", "mistake_rate")):
        band = decision["metric_bands"].get(band_key)
        if band in ("Excellent", "Good"):
            strengths.append(metric_name)
        elif band == "Struggling":
            weaknesses.append(metric_name)
    accuracy_delta = None
    response_delta = None
    if len(observed) >= 2:
        accuracy_delta = round(observed[-1]["accuracy"] - observed[0]["accuracy"], 2)
        first_speed = observed[0]["time_per_button_press_seconds"]
        last_speed = observed[-1]["time_per_button_press_seconds"]
        if first_speed is not None and last_speed is not None:
            response_delta = round(last_speed - first_speed, 3)
    sequence_effect = None
    if len(observed) >= 2:
        short = [m for m in observed if m["sequence_length"] and m["sequence_length"] <= sorted(
            x["sequence_length"] for x in observed if x["sequence_length"]
        )[max(0, (len([x for x in observed if x["sequence_length"]]) - 1) // 2)]]
        long = [m for m in observed if m["sequence_length"] and m not in short]
        if short and long:
            short_acc = sum(m["accuracy"] for m in short) / len(short)
            long_acc = sum(m["accuracy"] for m in long) / len(long)
            sequence_effect = {
                "short_sequence_accuracy": round(short_acc, 2),
                "long_sequence_accuracy": round(long_acc, 2),
                "accuracy_change": round(long_acc - short_acc, 2),
            }

    return {
        "classification": decision["classification"],
        "strengths": strengths,
        "weaknesses": weaknesses,
        "performance_progression": {
            "levels_observed": [m["level"] for m in observed],
            "accuracy_change_percentage_points": accuracy_delta,
            "response_time_per_button_change_seconds": response_delta,
            "response_time_trend": (
                "improved" if response_delta is not None and response_delta < 0
                else "deteriorated" if response_delta is not None and response_delta > 0
                else "unchanged_or_insufficient_data" if response_delta is not None
                else "insufficient_data"
            ),
            "sequence_length_effect": sequence_effect,
            "consistency": (
                "consistent" if len(observed) >= 2 and max(m["accuracy"] for m in observed) - min(m["accuracy"] for m in observed) <= 15
                else "variable" if len(observed) >= 2
                else "insufficient_data"
            ),
        },
        "proposed_level_adjustment": target_level - current_level,
        "recommended_level": target_level,
        "current_level": current_level,
        "reason": decision["reason"],
        "metric_bands": decision["metric_bands"],
        "metrics": metrics,
        "per_level_analysis": per_level_analysis,
        "recommendations": recommendations,
        "recommendation_details": _explain_recommendations(recommendations, metrics, decision),
        "rules_version": RULES_VERSION,
        "data_status": (
            "complete_for_classification"
            if metrics["sequences_attempted"] >= 2
            else "provisional_single_sequence"
        ),
        "minimum_sequences_required": 1,
        "confidence": "moderate" if metrics["sequences_attempted"] >= 2 else "low",
        "confidence_note": "One sequence is enough for a provisional assessment; two or more provide a more reliable pattern. Interpret single-sequence recommendations cautiously.",
    }


# =====================================================================
# ADAPTIVE DIFFICULTY  -  response-time allowance ONLY
# =====================================================================
# The game's sequence length and displayed level follow the ORIGINAL fixed
# progression (level 1 = 3 buttons, +1 button per completed level). The
# Intelligent System never returns, changes or influences sequence length,
# LED illumination time, or the delay between LEDs. It only returns a bounded,
# signed adjustment (ms) to the ORIGINAL level-based response time.
#
#   limit(level) = clamp(baseTime(level) + adjustment) + GRACE
#
# These constants MUST match firmware/src/AdaptiveTiming.h.
POLICY_VERSION = "response-time-v2"
GRACE_MS = 5000
MIN_ADJUSTMENT_MS = -4000
MAX_ADJUSTMENT_MS = 10000
MIN_BASE_TIME_MS = 10000
MAX_BASE_TIME_MS = 40000
MIN_RESPONSE_ALLOWANCE_MS = MIN_BASE_TIME_MS + GRACE_MS   # 15 000
MAX_RESPONSE_ALLOWANCE_MS = MAX_BASE_TIME_MS + GRACE_MS   # 45 000 (matches DB check)
TIGHTEN_STEP_MS = 500        # per point of the existing PDF difficulty_adjustment (Excellent: 500-1000)
EASE_STEP_MS = 2000          # Struggling
EASE_STEP_SEVERE_MS = 3000   # Struggling with accuracy < 30 % (PDF adjustment -2)


def clamp_adjustment(value: Any) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return max(MIN_ADJUSTMENT_MS, min(MAX_ADJUSTMENT_MS, value))


def response_allowance_ms(baseline_allowance_ms: int, adjustment_ms: int) -> int:
    """Mirror of firmware responseLimitForLevel(): baseline + bounded adjustment.

    `baseline_allowance_ms` is the ORIGINAL level-based limit (base time + grace).
    An adjustment of 0 returns the baseline unchanged.
    """
    adjustment_ms = clamp_adjustment(adjustment_ms)
    base = int(baseline_allowance_ms) - GRACE_MS
    if adjustment_ms != 0:
        base = max(MIN_BASE_TIME_MS, min(MAX_BASE_TIME_MS, base + adjustment_ms))
    return base + GRACE_MS


def adaptive_difficulty_decision(
    level_row: dict[str, Any],
    current_response_time_adjustment_ms: int,
    next_level_baseline_allowance_ms: int,
    current_response_time_allowance_ms: int | None = None,
    previous_action: str | None = None,
) -> dict[str, Any]:
    """Evaluate one completed level and return a bounded response-time decision.

    Existing PDF classification thresholds remain authoritative:
      Excellent -> tighten (500 ms per PDF adjustment point, i.e. 500-1000 ms)
      Good      -> tighten 500 ms only when the PDF rules justify it
                   (adjustment > 0, i.e. no Struggling metric)
      Average   -> maintain the current setting
      Struggling-> ease (+2000 ms, +3000 ms when accuracy < 30 %)
    Tightening needs >= 2 sequences of evidence. Easing is never withheld.
    A tighten is deferred for one level right after an ease (anti-oscillation).
    The result contains NO sequence-length value by design.
    """
    metrics = _metrics(level_row)
    classification = classify_level(metrics)
    label = classification["classification"]
    pdf_adjustment = classification["difficulty_adjustment"]
    sufficient = metrics["sequences_attempted"] >= 2
    previous_adjustment = clamp_adjustment(current_response_time_adjustment_ms)
    baseline = max(MIN_RESPONSE_ALLOWANCE_MS, min(MAX_RESPONSE_ALLOWANCE_MS, int(next_level_baseline_allowance_ms)))

    step = 0
    notes: list[str] = []
    if label == "Excellent" and pdf_adjustment > 0:
        if sufficient:
            step = -TIGHTEN_STEP_MS * min(2, pdf_adjustment)
        else:
            notes.append("Fewer than two sequences were recorded; tightening is withheld.")
    elif label == "Good" and pdf_adjustment > 0:
        if sufficient:
            step = -TIGHTEN_STEP_MS
        else:
            notes.append("Fewer than two sequences were recorded; tightening is withheld.")
    elif label == "Struggling":
        step = EASE_STEP_SEVERE_MS if pdf_adjustment <= -2 else EASE_STEP_MS

    if step < 0 and previous_action == "ease":
        step = 0
        notes.append("Tightening was deferred for one level because the previous decision eased the time.")

    new_adjustment = clamp_adjustment(previous_adjustment + step)
    if step != 0 and new_adjustment == previous_adjustment:
        notes.append("The configured response-time limit was already reached.")
    applied_step = new_adjustment - previous_adjustment
    action = "tighten" if applied_step < 0 else "ease" if applied_step > 0 else "maintain"

    previous_allowance = (
        int(current_response_time_allowance_ms) if current_response_time_allowance_ms is not None
        else response_allowance_ms(baseline, previous_adjustment)
    )
    reason = " ".join([classification["reason"], *notes])
    return {
        "policy_version": POLICY_VERSION,
        "classification": label,
        "difficulty_action": action,
        "recommended_step_ms": applied_step,
        "previous_response_time_adjustment_ms": previous_adjustment,
        "new_response_time_adjustment_ms": new_adjustment,
        "previous_response_time_allowance_ms": previous_allowance,
        "baseline_response_time_allowance_ms": baseline,
        "new_response_time_allowance_ms": response_allowance_ms(baseline, new_adjustment),
        "completed_sequence_length": metrics["sequence_length"],
        "sequence_length_policy": "fixed_original_progression",
        "reason": reason,
        "data_sufficient": sufficient,
        "provisional": not sufficient,
        "metric_bands": classification["metric_bands"],
    }
