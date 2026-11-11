"""Adaptive response-time service layer (framework-independent, unit-testable).

FastAPI routes in main.py are thin wrappers around these functions. The
functions take a Supabase-like `client` so tests can inject a fake.

Contract (see intelligent_system.adaptive_difficulty_decision):
  * The device sends the completed level's statistics plus its current
    response-time adjustment and the ORIGINAL baseline allowance of the next
    level.
  * The backend returns ONE bounded response-time adjustment. It never returns
    a sequence length, level, LED duration or inter-LED delay.
  * One immutable decision per (session, completed_level): repeated requests get
    the stored decision back, never a compounded one.
  * The device later acknowledges what it really applied; the recommended and
    actual values are stored side by side.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .intelligent_system import (
    MAX_ADJUSTMENT_MS,
    MAX_RESPONSE_ALLOWANCE_MS,
    MIN_ADJUSTMENT_MS,
    MIN_RESPONSE_ALLOWANCE_MS,
    POLICY_VERSION,
    adaptive_difficulty_decision,
)

MAX_LEVEL = 20
MAX_SEQUENCE_LENGTH = 30  # firmware MAX_SEQUENCE; level 20 uses 22 buttons


class AdaptiveError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _int(payload: dict[str, Any], key: str, lo: int, hi: int, default: int | None = None) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise AdaptiveError(422, f"{key} must be an integer.")
    value = int(value)
    if not lo <= value <= hi:
        raise AdaptiveError(422, f"{key} must be between {lo} and {hi}.")
    return value


def _authorized_session(client, sid: str, device_id: Any) -> dict[str, Any]:
    q = client.table("game_sessions").select("id,device_id,status").eq("id", sid).limit(1).execute()
    if not q.data:
        raise AdaptiveError(404, "Session not found.")
    session = q.data[0]
    if session["device_id"] != device_id:
        raise AdaptiveError(403, "Session does not belong to this device.")
    return session


def _event_for_level(client, sid: str, level: int) -> dict[str, Any] | None:
    q = (client.table("game_adaptive_events").select("*")
         .eq("session_id", sid).eq("completed_level", level).limit(1).execute())
    return q.data[0] if q.data else None


def _replay(prior: dict[str, Any]) -> dict[str, Any]:
    if prior.get("policy_version") != POLICY_VERSION:
        # Written by the earlier sequence-length design; never replay it to the board.
        raise AdaptiveError(409, "A legacy adaptive record exists for this level; keep current timing.")
    return {**prior["decision"], "idempotent": True}


def process_adaptive_decision(client, payload: dict[str, Any]) -> dict[str, Any]:
    sid = str(payload.get("session_id", ""))
    level = _int(payload, "completed_level", 1, MAX_LEVEL - 1)  # no "next level" after the last one
    attempted = _int(payload, "sequences_attempted", 1, 100)
    correct = _int(payload, "correct_sequences", 0, 100)
    mistakes = _int(payload, "mistakes", 0, 100)
    sequence_length = _int(payload, "sequence_length", 2, MAX_SEQUENCE_LENGTH)
    incorrect_presses = _int(payload, "incorrect_press_count", 0, 1000, default=0)
    current_adjustment = _int(payload, "current_response_time_adjustment_ms",
                              MIN_ADJUSTMENT_MS, MAX_ADJUSTMENT_MS, default=0)
    current_allowance = _int(payload, "current_response_time_allowance_ms", 1000, 120000)
    baseline = _int(payload, "next_level_baseline_allowance_ms",
                    MIN_RESPONSE_ALLOWANCE_MS, MAX_RESPONSE_ALLOWANCE_MS)
    avg_ms = payload.get("avg_response_time_ms")
    if avg_ms is not None:
        if isinstance(avg_ms, bool) or not isinstance(avg_ms, (int, float)) or not 0 <= avg_ms <= 120000:
            raise AdaptiveError(422, "avg_response_time_ms is invalid.")

    session = _authorized_session(client, sid, payload.get("device_id"))
    if session["status"] != "playing":
        raise AdaptiveError(409, "Adaptive decisions are only available during active gameplay.")

    try:
        prior = _event_for_level(client, sid, level)
    except Exception:
        raise AdaptiveError(503, "Adaptive event storage is unavailable. Apply local fallback.")
    if prior:
        return _replay(prior)

    previous_action = None
    try:
        prev_q = (client.table("game_adaptive_events").select("decision,policy_version")
                  .eq("session_id", sid).lt("completed_level", level)
                  .order("completed_level", desc=True).limit(1).execute())
        if prev_q.data and prev_q.data[0].get("policy_version") == POLICY_VERSION:
            previous_action = (prev_q.data[0].get("decision") or {}).get("difficulty_action")
    except Exception:
        previous_action = None  # hysteresis is best-effort; the decision itself stays bounded

    decision = adaptive_difficulty_decision(
        {
            "level": level,
            "sequences_attempted": attempted,
            "correct_sequences": min(correct, attempted),
            "mistakes": mistakes,
            "sequence_length": sequence_length,
            "avg_response_time_ms": avg_ms,
            "incorrect_press_count": incorrect_presses,
        },
        current_adjustment, baseline, current_allowance, previous_action,
    )
    event = {
        "session_id": sid,
        "completed_level": level,
        "policy_version": POLICY_VERSION,
        "classification": decision["classification"],
        "decision": decision,
        "recommended_adjustment_ms": decision["recommended_step_ms"],
        "previous_adjustment_ms": decision["previous_response_time_adjustment_ms"],
        "recommended_new_adjustment_ms": decision["new_response_time_adjustment_ms"],
        "previous_response_time_allowance_ms": decision["previous_response_time_allowance_ms"],
        "recommended_response_time_allowance_ms": decision["new_response_time_allowance_ms"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        client.table("game_adaptive_events").insert(event).execute()
    except Exception:
        # Two near-simultaneous retries: the first stored decision is canonical.
        try:
            prior = _event_for_level(client, sid, level)
        except Exception:
            prior = None
        if prior:
            return _replay(prior)
        raise AdaptiveError(503, "Could not persist adaptive decision.")
    return decision


def record_adaptive_applied(client, payload: dict[str, Any]) -> dict[str, Any]:
    sid = str(payload.get("session_id", ""))
    level = _int(payload, "completed_level", 1, MAX_LEVEL)
    actual_length = _int(payload, "actual_sequence_length", 2, MAX_SEQUENCE_LENGTH)
    actual_adjustment = _int(payload, "actual_response_time_adjustment_ms", MIN_ADJUSTMENT_MS, MAX_ADJUSTMENT_MS)
    actual_allowance = _int(payload, "actual_response_time_allowance_ms",
                            MIN_RESPONSE_ALLOWANCE_MS, MAX_RESPONSE_ALLOWANCE_MS)
    applied = payload.get("applied")
    if not isinstance(applied, bool):
        raise AdaptiveError(422, "applied must be true or false.")

    _authorized_session(client, sid, payload.get("device_id"))
    row = _event_for_level(client, sid, level)
    if not row:
        raise AdaptiveError(404, "Adaptive decision record not found.")
    if row.get("policy_version") != POLICY_VERSION:
        raise AdaptiveError(409, "Legacy adaptive record cannot be acknowledged.")
    if row.get("applied") is not None:
        return {"recorded": True, "idempotent": True}   # first acknowledgement wins

    completed_length = (row.get("decision") or {}).get("completed_sequence_length") or 0
    matches = (
        applied
        and actual_adjustment == row.get("recommended_new_adjustment_ms")
        and actual_allowance == row.get("recommended_response_time_allowance_ms")
    )
    update = {
        "applied": applied,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "actual_sequence_length": actual_length,
        "actual_response_time_adjustment_ms": actual_adjustment,
        "actual_response_time_allowance_ms": actual_allowance,
        "applied_matches_recommendation": bool(matches),
        # Evidence for the core rule: the board's next sequence is exactly one longer.
        "sequence_progression_verified": (actual_length == completed_length + 1) if completed_length else None,
    }
    result = (client.table("game_adaptive_events").update(update)
              .eq("session_id", sid).eq("completed_level", level).execute())
    if not result.data:
        raise AdaptiveError(404, "Adaptive decision record not found.")
    return {"recorded": True, "applied_matches_recommendation": bool(matches),
            "sequence_progression_verified": update["sequence_progression_verified"]}


def summarize_adaptive_history(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact, honest summary: unconfirmed events are counted, not guessed."""
    v2 = [e for e in events if e.get("policy_version") == POLICY_VERSION]
    return {
        "decisions": len(v2),
        "legacy_records": len(events) - len(v2),
        "applied": sum(1 for e in v2 if e.get("applied") is True),
        "not_applied": sum(1 for e in v2 if e.get("applied") is False),
        "unconfirmed": sum(1 for e in v2 if e.get("applied") is None),
        "matched_recommendation": sum(1 for e in v2 if e.get("applied_matches_recommendation") is True),
        "net_adjustment_ms": (
            v2[-1].get("actual_response_time_adjustment_ms")
            if v2 and v2[-1].get("actual_response_time_adjustment_ms") is not None else None
        ),
    }
