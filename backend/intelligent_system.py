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

RULES_VERSION = "pdf-proposal-1.2"
MIN_SEQUENCES_FOR_CLASSIFICATION = 1
MAX_GAME_LEVEL = 20  # Matches MAX_LEVEL in the current ESP32 firmware.
CLASS_RANK = {"Struggling": 0, "Average": 1, "Good": 2, "Excellent": 3}

# Evidence thresholds for the personalised insights. The repeated-button values
# are the original rule (unchanged).
MIN_BUTTON_MISTAKES = 3       # recorded incorrect presses needed to look at buttons
MIN_BUTTON_COUNT = 3          # presses of the single most-mistaken button
MIN_BUTTON_SHARE = 0.40       # and its share of all incorrect presses
MIN_POSITION_SAMPLES = 4      # incorrect-press positions needed for position analysis
POSITION_MIN_SHARE = 0.50     # a third of the sequence must hold >= half of the mistakes...
POSITION_MARGIN = 0.20        # ...and exceed its expected share by this much
MIN_GROUP_ATTEMPTS = 2        # sequences per level-group for the difficulty trend
DECLINE_MIN_GAP = 0.20        # original rule: upper failure rate >= 2x lower and +20 points
SLOW_SECONDS_PER_PRESS = 2.5  # original rule
RUSH_SECONDS_PER_PRESS = 1.0  # original rule
TIME_LIMIT_SHARE = 0.75       # average sequence time vs. the level's time limit
IMPROVEMENT_POINTS = 5.0      # accuracy gain vs previous attempt worth mentioning
REGRESSION_POINTS = 10.0
MIN_HISTORY_ATTEMPTS = 3      # sequences each game needs for an accuracy comparison

# Mirrors firmware responseLimitForLevel(): BASE_TIME_MS + TIME_PER_LEVEL_MS*(L-1),
# capped at MAX_BASE_TIME_MS, plus GRACE_MS. tests/test_integration.py checks these
# against the .ino so they cannot drift silently.
FW_BASE_TIME_MS = 15000
FW_TIME_PER_LEVEL_MS = 1000
FW_MAX_BASE_TIME_MS = 30000
FW_GRACE_MS = 5000


def response_limit_ms(level: int) -> int:
    base = min(FW_MAX_BASE_TIME_MS, FW_BASE_TIME_MS + FW_TIME_PER_LEVEL_MS * (max(1, level) - 1))
    return base + FW_GRACE_MS


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



# ---------------------------------------------------------------------------
# Personalised insights (rule based; evidence-backed; no machine learning)
# ---------------------------------------------------------------------------
def _levels_text(levels: list[int]) -> str:
    levels = sorted(set(levels))
    if not levels:
        return ""
    if len(levels) == 1:
        return f"Level {levels[0]}"
    names = [str(v) for v in levels]
    return "Levels " + ", ".join(names[:-1]) + " and " + names[-1]


def _range_text(levels: list[int]) -> str:
    levels = sorted(levels)
    return f"Level {levels[0]}" if len(levels) == 1 else f"Levels {levels[0]}–{levels[-1]}"


def _confidence(evidence_count: int, moderate_from: int) -> str:
    """Per-insight confidence. Capped at 'moderate', like the overall rating."""
    return "moderate" if evidence_count >= moderate_from else "low"


def _incorrect_presses(round_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten per-level incorrect-press telemetry (list or dict form)."""
    presses: list[dict[str, Any]] = []
    for row in round_data or []:
        if not isinstance(row, dict):
            continue
        level = int(_number(row.get("level")))
        details = row.get("incorrect_press_details")
        if isinstance(details, list) and details:
            for d in details:
                if isinstance(d, dict):
                    rnd, pos = d.get("round"), d.get("position")
                    pressed, expected = d.get("pressed_button"), d.get("expected_button")
                    length = d.get("sequence_length")
                elif isinstance(d, (list, tuple)) and len(d) >= 5:
                    rnd, pos, pressed, expected, length = d[0], d[1], d[2], d[3], d[4]
                else:
                    continue
                pressed_i = int(_number(pressed))
                if pressed_i <= 0:
                    continue
                presses.append({"level": level, "round": int(_number(rnd)),
                                "position": int(_number(pos)), "pressed": pressed_i,
                                "expected": int(_number(expected)),
                                "length": int(_number(length))})
        else:  # legacy telemetry: only the wrongly pressed buttons
            legacy = row.get("incorrect_buttons")
            if isinstance(legacy, list):
                for b in legacy:
                    if int(_number(b)) > 0:
                        presses.append({"level": level, "round": 0, "position": 0,
                                        "pressed": int(_number(b)), "expected": 0, "length": 0})
    return presses


def _button_analysis(presses: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(presses)
    out: dict[str, Any] = {"total": total, "qualifies": False}
    if total == 0:
        return out
    counts = Counter(p["pressed"] for p in presses)
    button, count = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    out.update(button=button, count=count, share=count / total, counts=dict(counts))
    mine = [p for p in presses if p["pressed"] == button]
    out["levels"] = sorted({p["level"] for p in mine if p["level"] > 0})
    expected = Counter(p["expected"] for p in mine if p["expected"] > 0)
    if expected:
        exp_button, exp_count = sorted(expected.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        if exp_count >= 2 and exp_count / count >= 0.5:
            out["usually_expected"] = exp_button
    out["qualifies"] = (total >= MIN_BUTTON_MISTAKES and count >= MIN_BUTTON_COUNT
                        and count / total >= MIN_BUTTON_SHARE)
    return out


def _segment(position: int, length: int) -> str:
    third = length / 3.0
    if position <= third:
        return "first"
    return "last" if position > 2 * third else "middle"


def _position_analysis(presses: list[dict[str, Any]]) -> dict[str, Any]:
    samples = [p for p in presses if p["position"] > 0 and p["length"] > 0]
    n = len(samples)
    out: dict[str, Any] = {"samples": n, "pattern": None}
    if n < MIN_POSITION_SAMPLES:
        return out
    observed = Counter(_segment(p["position"], p["length"]) for p in samples)
    expected = Counter()
    for p in samples:  # share of a sequence's positions that fall in each third
        sizes = Counter(_segment(i, p["length"]) for i in range(1, p["length"] + 1))
        for seg in ("first", "middle", "last"):
            expected[seg] += sizes[seg] / p["length"] / n
    best = max(("first", "middle", "last"), key=lambda s: observed[s] / n - expected[s])
    share = observed[best] / n
    out.update(observed=dict(observed), mean_length=sum(p["length"] for p in samples) / n)
    if share >= POSITION_MIN_SHARE and share - expected[best] >= POSITION_MARGIN:
        out.update(pattern=best, count=observed[best], share=share, expected_share=expected[best])
    return out


def _level_trend(valid: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"levels_played": len(valid), "decline": False, "consistent_low": False,
                           "consistent_all": False}
    if len(valid) < 3:
        return out
    upper = valid[-2:] if len(valid) >= 4 else valid[-1:]
    lower = valid[: len(valid) - len(upper)]
    low_a = sum(m["sequences_attempted"] for m in lower)
    high_a = sum(m["sequences_attempted"] for m in upper)
    if low_a < MIN_GROUP_ATTEMPTS or high_a < MIN_GROUP_ATTEMPTS:
        return out
    low_rate = sum(m["mistakes"] for m in lower) / low_a
    high_rate = sum(m["mistakes"] for m in upper) / high_a
    low_acc = 100 * (1 - low_rate)
    out.update(lower_levels=[m["level"] for m in lower], upper_levels=[m["level"] for m in upper],
               low_rate=low_rate, high_rate=high_rate, low_attempts=low_a, high_attempts=high_a,
               low_len=max((m["sequence_length"] for m in lower), default=0),
               high_len=max((m["sequence_length"] for m in upper), default=0))
    out["decline"] = high_rate >= 2 * low_rate and high_rate - low_rate >= DECLINE_MIN_GAP
    out["consistent_low"] = low_acc >= 80
    out["consistent_all"] = (not out["decline"]
                             and all(m["accuracy"] >= 80 for m in valid if m["sequences_attempted"] >= 2))
    return out


def _history_comparison(cur: dict[str, Any], history: list[dict[str, Any]] | None) -> dict[str, Any]:
    basis = ("Compared with the previous completed session that used the same codename. "
             "Codenames are not verified identities, so this may be a different player.")
    if not history:
        return {"available": False, "reason": "No earlier completed session with this codename was found."}
    prev = history[0]
    prev_rows = _level_rows(prev.get("round_data") or [])
    prev_metrics = [_metrics(r) for r in prev_rows]
    prev_attempts = sum(m["sequences_attempted"] for m in prev_metrics) or int(_number(prev.get("total_rounds")))
    improvements: list[dict[str, str]] = []
    regressions: list[dict[str, str]] = []

    if cur["attempts"] >= MIN_HISTORY_ATTEMPTS and prev_attempts >= MIN_HISTORY_ATTEMPTS:
        prev_acc = _number(prev.get("accuracy"))
        delta = cur["accuracy"] - prev_acc
        if delta >= IMPROVEMENT_POINTS:
            improvements.append({"id": "improvement_accuracy", "text":
                f"Accuracy improved from {prev_acc:.0f}% to {cur['accuracy']:.0f}% compared with your previous attempt."})
        elif delta <= -REGRESSION_POINTS:
            regressions.append({"id": "regression_accuracy", "text":
                f"Accuracy fell from {prev_acc:.0f}% to {cur['accuracy']:.0f}% compared with your previous attempt."})

        prev_btn = _button_analysis(_incorrect_presses(prev.get("round_data") or []))
        if prev_btn["qualifies"]:
            now = cur["button_counts"].get(prev_btn["button"], 0)
            if now < prev_btn["count"]:
                improvements.append({"id": "improvement_button", "text":
                    f"Mistakes on Button {prev_btn['button']} fell from {prev_btn['count']} to {now} "
                    f"compared with your previous attempt."})

        prev_by_level = {m["level"]: m for m in prev_metrics if m["sequences_attempted"] >= MIN_GROUP_ATTEMPTS}
        common = sorted(l for l in cur["by_level"]
                        if l in prev_by_level and cur["by_level"][l]["sequences_attempted"] >= MIN_GROUP_ATTEMPTS)
        if common:
            top = common[-2:]
            prev_rate = (sum(prev_by_level[l]["mistakes"] for l in top)
                         / sum(prev_by_level[l]["sequences_attempted"] for l in top))
            cur_rate = (sum(cur["by_level"][l]["mistakes"] for l in top)
                        / sum(cur["by_level"][l]["sequences_attempted"] for l in top))
            if prev_rate - cur_rate >= 0.20:
                improvements.append({"id": "improvement_consistency", "text":
                    f"You were more consistent at {_range_text(top)}: failed sequences dropped from "
                    f"{prev_rate:.0%} to {cur_rate:.0%} compared with your previous attempt."})
    return {"available": True, "basis": basis, "previous_session_id": prev.get("session_id"),
            "improvements": improvements, "regressions": regressions}


def _build_insights(round_data, metrics, decision, target_level, history, game_accuracy,
                    avg_press_ms) -> dict[str, Any]:
    level = metrics["level"]
    attempts = metrics["sequences_attempted"]
    accuracy = metrics["accuracy"]
    speed = metrics["time_per_button_press_seconds"]
    lvl_conf = _confidence(attempts, 2)
    strengths: list[dict[str, Any]] = []
    weaknesses: list[dict[str, Any]] = []
    recs: list[dict[str, Any]] = []
    notes: list[str] = []

    def rec(rid, title, detected, evidence, action, confidence, weakness=True):
        provisional = confidence == "low"
        recs.append({"id": rid, "title": title, "detected": ("Early signal: " + detected[0].lower() + detected[1:]) if provisional else detected,
                     "evidence": evidence, "action": action, "confidence": confidence,
                     "provisional": provisional})
        if weakness:
            weaknesses.append({"id": rid, "title": title, "evidence": evidence, "confidence": confidence})

    valid = [m for m in (_metrics(r) for r in _level_rows(round_data)) if m["sequences_attempted"] > 0]
    presses = _incorrect_presses(round_data)

    # --- A. button-specific ------------------------------------------------
    buttons = _button_analysis(presses)
    if buttons["qualifies"]:
        b, c, t = buttons["button"], buttons["count"], buttons["total"]
        where = f" ({_levels_text(buttons['levels'])})" if buttons["levels"] else ""
        extra = (f", most often when Button {buttons['usually_expected']} was expected"
                 if buttons.get("usually_expected") else "")
        rec("repeated_button", f"Frequent mistakes on Button {b}",
            f"You made the most mistakes on Button {b}.",
            f"It accounts for {buttons['share']:.0%} ({c} of {t}) of your recorded incorrect button presses{where}{extra}.",
            f"Pay closer attention to Button {b} when recalling sequences: note where it appears in the pattern "
            f"before you start pressing.", _confidence(t, 5))
    elif buttons["total"] > 0:
        if buttons["total"] < MIN_BUTTON_MISTAKES:
            notes.append(f"Only {buttons['total']} incorrect button press(es) were recorded, which is not enough "
                         f"(minimum {MIN_BUTTON_MISTAKES}) to identify a problem button.")
        else:
            notes.append("Incorrect presses were spread across several buttons, so no single button stood out.")

    # --- B. sequence position ---------------------------------------------
    pos = _position_analysis(presses)
    if pos["pattern"]:
        n, k = pos["samples"], pos["count"]
        longer = "longer " if pos["mean_length"] >= 5 else ""
        third = {"first": "first", "middle": "middle", "last": "last"}[pos["pattern"]]
        advice = {
            "last": "Try dividing sequences into smaller groups of 2–3 buttons to improve recall.",
            "first": "Rehearse the first few lights silently while the rest of the pattern plays so they don't fade.",
            "middle": "Keep a steady rhythm in your head as you recall the pattern so the middle doesn't blur.",
        }[pos["pattern"]]
        where = {"last": f"You made more mistakes toward the end of {longer}sequences.",
                 "first": f"You made more mistakes at the beginning of {longer}sequences.",
                 "middle": f"You made more mistakes in the middle of {longer}sequences."}[pos["pattern"]]
        rec("position_pattern", f"Mistakes cluster in the {third} third of sequences", where,
            f"{k} of {n} recorded incorrect presses ({k / n:.0%}) were in the {third} third of the sequence, "
            f"versus about {pos['expected_share']:.0%} expected if mistakes were evenly spread.",
            advice, _confidence(n, 6))
    elif 0 < pos["samples"] < MIN_POSITION_SAMPLES:
        notes.append(f"Only {pos['samples']} incorrect-press position(s) were recorded; at least "
                     f"{MIN_POSITION_SAMPLES} are needed to judge whether mistakes cluster at a part of the sequence.")

    # --- C. difficulty / level trend ----------------------------------------
    trend = _level_trend(valid)
    if trend["decline"]:
        lens = ""
        if trend["low_len"] and trend["high_len"] and trend["high_len"] > trend["low_len"]:
            lens = f" as sequences grew from {trend['low_len']} to {trend['high_len']} buttons"
        rec("higher_level_decline", "Accuracy drops at higher levels",
            "Your accuracy decreased considerably at higher levels.",
            f"Failed sequences rose from {trend['low_rate']:.0%} at {_range_text(trend['lower_levels'])} to "
            f"{trend['high_rate']:.0%} at {_range_text(trend['upper_levels'])}{lens}.",
            "Consider practicing shorter sequences until they feel automatic before attempting more difficult ones.",
            _confidence(min(trend["low_attempts"], trend["high_attempts"]), 4))
        if trend["consistent_low"]:
            strengths.append({"id": "consistent_lower_levels",
                              "title": f"Consistent at {_range_text(trend['lower_levels'])}",
                              "evidence": f"{1 - trend['low_rate']:.0%} of sequences completed at "
                                          f"{_range_text(trend['lower_levels'])}.",
                              "confidence": _confidence(trend["low_attempts"], 4)})
    elif trend["consistent_all"]:
        strengths.append({"id": "consistent_levels",
                          "title": f"Steady performance across {_range_text([m['level'] for m in valid])}",
                          "evidence": "Accuracy stayed at 80% or higher on every level with at least two sequences.",
                          "confidence": _confidence(min(m["sequences_attempted"] for m in valid), 4)})
    if len(valid) < 3 and attempts:
        notes.append("Fewer than three levels were played, so level-to-level trends could not be assessed.")

    # --- D. response time (whole-sequence input time, not pure reaction speed) -
    avg_ms = metrics.get("avg_sequence_ms")
    limit_ms = response_limit_ms(level)
    near_limit = avg_ms is not None and avg_ms >= TIME_LIMIT_SHARE * limit_ms
    pace_note = ("This measures the whole input phase at that level (including pauses to recall and the short "
                 "feedback flash after each press), not pure reaction speed.")
    game_pace = (f" Across the whole game the average time per button press was {avg_press_ms / 1000:.1f} s."
                 if avg_press_ms else "")
    rushing = attempts >= 1 and accuracy < 60 and speed is not None and speed <= RUSH_SECONDS_PER_PRESS
    if rushing:
        rec("rushing", "Answering faster than you can recall",
            "You may be answering faster than you can reliably recall the pattern.",
            f"At {_range_text([level])} input pace was {speed:.1f} s per button while accuracy was {accuracy:.0f}%. "
            + pace_note,
            "Slow down a little and recall the whole sequence before pressing.", lvl_conf)
    elif attempts >= 1 and accuracy < 60:
        rec("low_accuracy", f"Low accuracy at Level {level}",
            f"You completed few sequences correctly at Level {level}.",
            f"{metrics['correct_sequences']} of {attempts} sequence(s) completed ({accuracy:.0f}%), below the 60% "
            f"level the system flags.",
            "Before pressing, silently replay the whole pattern once and start only when you can recall every light in order.",
            lvl_conf)
    if speed is not None and speed > SLOW_SECONDS_PER_PRESS:
        limit_txt = (f" That is {avg_ms / limit_ms:.0%} of the {limit_ms / 1000:.0f} s limit (including grace time)."
                     if near_limit else "")
        rec("slow_pace", f"Slow input pace at Level {level}",
            "Your input pace was slow at this level.",
            f"Sequences took {avg_ms / 1000:.1f} s on average, {speed:.1f} s per button (over "
            f"{SLOW_SECONDS_PER_PRESS} s).{limit_txt} {pace_note}{game_pace}",
            "Start entering the pattern as soon as the lights stop and keep a steady rhythm, for example by grouping buttons.",
            lvl_conf)
    elif near_limit:
        rec("time_pressure", f"Close to the time limit at Level {level}",
            "You used most of the available time at this level.",
            f"Sequences took {avg_ms / 1000:.1f} s on average, {avg_ms / limit_ms:.0%} of the "
            f"{limit_ms / 1000:.0f} s limit (including grace time). {pace_note}",
            "Aim to begin pressing sooner; running close to the limit risks timeouts, which cost a life.",
            lvl_conf)
    bands = decision.get("metric_bands") or {}
    if attempts and accuracy >= 80:
        strengths.append({"id": "high_accuracy", "title": f"Strong accuracy at Level {level}",
                          "evidence": f"{metrics['correct_sequences']} of {attempts} sequence(s) completed ({accuracy:.0f}%).",
                          "confidence": lvl_conf})
    if attempts and accuracy >= 80 and bands.get("speed") in ("Excellent", "Good") and speed is not None:
        strengths.append({"id": "steady_pace", "title": "Comfortable input pace",
                          "evidence": f"About {speed:.1f} s per button at Level {level} with {accuracy:.0f}% accuracy. "
                                      + pace_note,
                          "confidence": lvl_conf})

    # --- E. history ---------------------------------------------------------
    by_level = {m["level"]: m for m in valid}
    cur = {"attempts": sum(m["sequences_attempted"] for m in valid), "accuracy": game_accuracy,
           "button_counts": buttons.get("counts", {}), "by_level": by_level}
    history_result = _history_comparison(cur, history)
    if history_result["available"]:
        for item in history_result["improvements"]:
            strengths.append({"id": item["id"], "title": "Improvement since last attempt",
                              "evidence": item["text"], "confidence": "moderate"})
        for item in history_result["regressions"]:
            weaknesses.append({"id": item["id"], "title": "Lower than last attempt",
                               "evidence": item["text"], "confidence": "moderate"})

    # --- fallbacks / positive reinforcement ------------------------------------
    if not recs and decision["classification"] in ("Excellent", "Good"):
        rec("ready_for_harder", "Ready for more", "No weaknesses were detected in this game.",
            f"{decision['classification']} performance at Level {level} ({accuracy:.0f}% accuracy).",
            f"You are ready for a harder challenge; the suggested next level is Level {target_level}."
            if target_level > level else "Keep playing at this level to build a longer track record.",
            lvl_conf, weakness=False)
    elif not recs:
        rec("keep_playing", "No clear weakness yet", "No specific weakness stood out in this game.",
            f"{attempts} sequence(s) were analysed at Level {level}.",
            "Keep playing; more sequences give the system more evidence for specific feedback.",
            lvl_conf, weakness=False)

    seen_actions: set[str] = set()
    unique = []
    for item in recs:
        if item["action"] not in seen_actions:
            seen_actions.add(item["action"])
            unique.append(item)
    return {"strengths": strengths, "weaknesses": weaknesses, "recommendations": unique,
            "notes": notes, "history": history_result, "buttons": buttons, "positions": pos}


def analyze_performance(accuracy, mistakes, avg_response_time_ms, round_data=None,
                        highest_level=None, max_level=MAX_GAME_LEVEL, history=None):
    """Return dashboard-ready IS classification, difficulty, and recommendations.

    The classification and difficulty rules (PDF Part 2) are unchanged. This is
    the POST-GAME suggestion; it never changes the game in progress (the in-game
    adaptive rule lives in the firmware, see difficulty_events.py).

    ``avg_response_time_ms`` is the firmware's per-button-press average over the
    whole game; per-level ``avg_response_time_ms`` is whole-sequence input time.
    ``history`` is an optional list of earlier results (newest first) used only
    for comparisons; nothing is compared when it is empty.
    """
    round_data = round_data or []
    level_rows = _level_rows(round_data)
    if not level_rows:
        msg = ("IS classification is provisional: per-level sequence count, "
               "sequence length, response time, and mistake data are required.")
        return {
            "classification": "Average",
            "proposed_level_adjustment": 0,
            "recommendations": [msg],
            "rules_version": RULES_VERSION,
            "data_status": "insufficient_per_level_data",
            "missing_data": [
                "sequences_attempted_per_level", "sequence_length",
                "per_level_average_sequence_response_time", "per_level_mistakes",
                "incorrect_press_positions",
            ],
            "overall_assessment": {"summary": msg, "classification": "Average", "confidence": "low",
                                   "sequences_assessed": 0},
            "strengths": [], "weaknesses": [], "personalized_recommendations": [],
            "suggested_difficulty": {"adjustment": 0, "recommended_level": None, "provisional": True,
                                     "explanation": "No difficulty suggestion can be made without per-level data."},
            "history_comparison": {"available": False, "reason": "No per-level data."},
            "data_notes": [msg],
        }

    latest_row = max(level_rows, key=lambda row: _number(row.get("level")))
    metrics = _metrics(latest_row)
    metrics["avg_sequence_ms"] = (_number(latest_row.get("avg_response_time_ms"))
                                  if latest_row.get("avg_response_time_ms") is not None else None)
    decision = classify_level(metrics)
    current_level = metrics["level"]
    target_level = current_level + decision["difficulty_adjustment"]
    target_level = min(int(_number(max_level, MAX_GAME_LEVEL)), target_level)
    target_level = max(1, target_level)

    avg_press_ms = _number(avg_response_time_ms, 0) or None
    game_accuracy = _number(accuracy)
    insights = _build_insights(round_data, metrics, decision, target_level, history, game_accuracy, avg_press_ms)

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
            "confidence": "moderate" if row_metrics["sequences_attempted"] >= 2 else (
                "low" if row_metrics["sequences_attempted"] == 1 else "none"),
        })

    attempts = metrics["sequences_attempted"]
    confidence = "moderate" if attempts >= 2 else "low"
    played = max((int(_number(r.get("level"))) for r in level_rows), default=current_level)
    peak = int(_number(highest_level, played)) or played
    summary = (f"At Level {current_level} your performance was classified as {decision['classification']} "
               f"({confidence} confidence, based on {attempts} sequence{'s' if attempts != 1 else ''}). "
               f"You reached Level {peak} with {game_accuracy:.0f}% overall accuracy.")
    if confidence == "low":
        summary += " This is a provisional assessment."
    bands = decision.get("metric_bands") or {}
    adj = target_level - current_level
    basis = (f"accuracy {bands.get('accuracy', '—')}, input pace {bands.get('speed', '—')}, "
             f"failed sequences {bands.get('mistake_rate', '—')}")
    if adj > 0:
        why = f"Level {current_level} was classified {decision['classification']} ({basis}), so the system suggests Level {target_level} next time."
    elif adj < 0:
        why = f"Level {current_level} was classified {decision['classification']} ({basis}), so the system suggests Level {target_level} next time."
    else:
        why = f"Level {current_level} was classified {decision['classification']} ({basis}); the system suggests staying at Level {current_level}."
    if "Struggling" in bands.values():
        why += " Difficulty is never raised while any metric is Struggling."
    if confidence == "low":
        why += " Only one sequence was available, so treat this as provisional."

    flat = [" ".join(p for p in (r["detected"], r["evidence"], r["action"]) if p)
            for r in insights["recommendations"]]
    return {
        "classification": decision["classification"],
        "proposed_level_adjustment": adj,
        "recommended_level": target_level,
        "current_level": current_level,
        "reason": decision["reason"],
        "metric_bands": decision["metric_bands"],
        "metrics": metrics,
        "per_level_analysis": per_level_analysis,
        "recommendations": flat,
        "overall_assessment": {"summary": summary, "classification": decision["classification"],
                               "confidence": confidence, "level": current_level,
                               "sequences_assessed": attempts, "highest_level": peak,
                               "game_accuracy": round(game_accuracy, 1),
                               "avg_time_per_press_ms": avg_press_ms},
        "strengths": insights["strengths"],
        "weaknesses": insights["weaknesses"],
        "personalized_recommendations": insights["recommendations"],
        "suggested_difficulty": {"current_level": current_level, "recommended_level": target_level,
                                 "adjustment": adj, "explanation": why,
                                 "provisional": confidence == "low",
                                 "scope": "A suggestion for your next game. It does not change the game in progress."},
        "history_comparison": insights["history"],
        "data_notes": insights["notes"],
        "rules_version": RULES_VERSION,
        "data_status": "complete_for_classification" if attempts >= 2 else "provisional_single_sequence",
        "minimum_sequences_required": 1,
        "confidence": confidence,
        "confidence_note": "One sequence is enough for a provisional assessment; two or more provide a more reliable pattern. Interpret single-sequence recommendations cautiously.",
    }
