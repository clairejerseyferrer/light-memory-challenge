"""Database access helpers (no FastAPI / supabase imports, so they are unit-testable).

``client`` is anything shaped like the supabase-py client; production passes a
``ResilientClient`` so every ``execute()`` gets bounded retries.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from .reliability import ApiError, classify_db_error, is_unique_violation

logger = logging.getLogger("lmc")

MAX_WAITING = 3
PAGE_SIZE = int(os.getenv("DB_PAGE_SIZE", "1000") or 1000)  # keep <= Supabase "Max rows"
MAX_ROWS = 50_000  # safety stop for runaway pagination
RANKING_BASIS = (
    "Highest level descending, score descending, then earliest saved result; "
    "one personal-best entry per codename."
)
LEADERBOARD_LABEL = "Leaderboard standing for this codename (based on its best recorded entry)"


def readable_id(number: int) -> str:
    return f"Session #{int(number):03d}"


def dt_utc(value):
    if not value:
        return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def escape_like(value: str) -> str:
    """Make ``_`` literal in ILIKE (codenames may contain underscores)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---- pagination ---------------------------------------------------------------
def fetch_all(build_query: Callable[[], Any], *, page_size: int | None = None) -> list[dict[str, Any]]:
    """Read every row using .range() pages. ``build_query`` must return a fresh,
    deterministically ordered query each call."""
    page_size = page_size or PAGE_SIZE
    rows: list[dict[str, Any]] = []
    while len(rows) < MAX_ROWS:
        start = len(rows)
        data = build_query().range(start, start + page_size - 1).execute().data or []
        rows.extend(data)
        if len(data) < page_size:
            break
    else:
        logger.warning("fetch_all stopped at the %d-row safety limit", MAX_ROWS)
    return rows


# ---- leaderboard / standing -------------------------------------------------------
def _saved_sort_key(value) -> datetime:
    parsed = None
    try:
        parsed = dt_utc(value)
    except (TypeError, ValueError):
        pass
    return parsed or datetime.max.replace(tzinfo=timezone.utc)


def rank_key(row: dict[str, Any]):
    # One order for every displayed rank. Timestamps are parsed because Postgres
    # trims trailing fractional zeros, which makes plain string comparison unreliable.
    return (-row["highest_level"], -row["score"], _saved_sort_key(row["saved_at"]), row["session_id"] or "")


def build_leaderboard_entries(client) -> list[dict[str, Any]]:
    """Canonical leaderboard: one best result per codename, fully paginated."""
    sessions = fetch_all(lambda: client.table("game_sessions")
                         .select("id,codename").eq("status", "completed").order("id"))
    results = fetch_all(lambda: client.table("game_results")
                        .select("session_id,score,highest_level,accuracy,saved_at").order("session_id"))
    sessions_by_id = {row["id"]: row for row in sessions}
    best: dict[str, dict[str, Any]] = {}
    for result in results:
        session = sessions_by_id.get(result.get("session_id"))
        if not session:
            continue
        codename = session.get("codename") or "Unknown"
        candidate = {
            "codename": codename,
            "session_id": result.get("session_id"),
            "score": int(result.get("score") or 0),
            "highest_level": int(result.get("highest_level") or 0),
            "accuracy": float(result.get("accuracy") or 0),
            "saved_at": result.get("saved_at"),
        }
        key = codename.strip().casefold()
        if key not in best or rank_key(candidate) < rank_key(best[key]):
            best[key] = candidate
    entries = sorted(best.values(), key=rank_key)
    for index, entry in enumerate(entries, start=1):
        entry["rank"] = index
    return entries


def leaderboard_standing(entries: list[dict[str, Any]], codename: str | None, session_id: str | None):
    """The codename's leaderboard position, kept separate from any single game's result."""
    if not codename:
        return None
    key = codename.strip().casefold()
    entry = next((e for e in entries if e["codename"].strip().casefold() == key), None)
    if entry is None:
        return None
    return {
        "rank": entry["rank"],
        "label": LEADERBOARD_LABEL,
        "entry_session_id": entry["session_id"],
        "is_this_session": bool(session_id and entry["session_id"] == session_id),
        "entry_score": entry["score"],
        "entry_highest_level": entry["highest_level"],
        "basis": RANKING_BASIS,
    }


def get_result(client, sid: str, *, codename: str | None = None, entries: list | None = None):
    """The stored game result plus the codename's leaderboard standing.

    ``ranking`` is kept (the firmware reads it) but it is the codename's
    leaderboard rank, NOT a rank for this individual game. The new
    ``leaderboard`` object says so explicitly and says whether this very
    session is the leaderboard entry.
    """
    q = client.table("game_results").select("*").eq("session_id", sid).limit(1).execute()
    if not q.data:
        return None
    result = q.data[0]
    if codename is None:
        s = client.table("game_sessions").select("codename").eq("id", sid).limit(1).execute()
        codename = (s.data[0].get("codename") or "Unknown") if s.data else None
    if entries is None:
        entries = build_leaderboard_entries(client)
    standing = leaderboard_standing(entries, codename, sid)
    result["ranking"] = standing["rank"] if standing else None
    result["ranking_basis"] = RANKING_BASIS
    result["leaderboard"] = standing
    analysis = result.get("analysis") if isinstance(result.get("analysis"), dict) else {}
    result["difficulty_events"] = analysis.get("gameplay_difficulty_events") or []
    return result


# ---- queue / sessions ---------------------------------------------------------------
def queue_state(client, device_id: str, session_id: str | None = None):
    """Active game and FIFO waiting queue for one physical device."""
    rows = (client.table("game_sessions")
            .select("id,session_number,codename,device_id,status,created_at,start_at")
            .eq("device_id", device_id).in_("status", ["waiting", "countdown", "playing"])
            .order("created_at").order("session_number").execute().data or [])
    active = next((r for r in rows if r["status"] in ("countdown", "playing")), None)
    waiting = [r for r in rows if r["status"] == "waiting"]
    current = next((r for r in rows if r["id"] == session_id), None)
    position = next((i + 1 for i, r in enumerate(waiting) if r["id"] == session_id), None)
    return {
        "active_session": ({"session_id": active["id"], "display_id": readable_id(active["session_number"]),
                            "codename": active["codename"], "status": active["status"]} if active else None),
        "waiting_count": len(waiting),
        "max_waiting": MAX_WAITING,
        "queue_position": position,
        "is_next": bool(current and current["status"] == "waiting" and not active and waiting
                        and waiting[0]["id"] == session_id),
        "waiting_sessions": [{"session_id": r["id"], "display_id": readable_id(r["session_number"]),
                              "codename": r["codename"], "created_at": r["created_at"]} for r in waiting],
    }


def _session_by_id(client, session_id: str):
    q = client.table("game_sessions").select("*").eq("id", session_id).limit(1).execute()
    return q.data[0] if q.data else None


def create_waiting_session(client, codename: str, device_id: str, session_id: str | None = None):
    """Create a waiting session exactly once.

    ``session_id`` (from an Idempotency-Key header) or a server-generated UUID is
    the primary key. If the INSERT outcome is unknown (lost response, timeout) or
    the key already exists, the row is read back by that key: one session, never two.
    Returns ``(row, replayed)``.
    """
    sid = session_id or str(uuid4())
    if session_id:
        existing = _session_by_id(client, sid)
        if existing:
            if existing["codename"] != codename or existing["device_id"] != device_id:
                raise ApiError(409, "This request key was already used for a different session.")
            return existing, True
    if queue_state(client, device_id)["waiting_count"] >= MAX_WAITING:
        raise ApiError(409, "The queue is full (maximum 3 waiting players). "
                            "Please try again when a player starts or leaves.")
    try:
        response = client.table("game_sessions").insert(
            {"id": sid, "codename": codename, "device_id": device_id, "status": "waiting"}).execute()
        row = response.data[0] if response.data else None
    except Exception as exc:  # noqa: BLE001
        # Unknown outcome or duplicate key: find out whether the row exists.
        try:
            row = _session_by_id(client, sid)
        except Exception:  # noqa: BLE001
            row = None
        if row is None:
            raise
        logger.warning("Session insert reported %s but session exists; treating as created (%s)",
                       classify_db_error(exc).kind, sid)
        return row, True
    if not row:
        raise ApiError(500, "The session could not be created (the database returned no row).")
    if row.get("session_number") is None:
        raise ApiError(500, "The database did not return a session number. "
                            "Check that migration_readable_session_ids.sql has been applied.")
    return row, False


# ---- search -----------------------------------------------------------------------------
def fetch_results_for_sessions(client, session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """One query for many sessions' result summaries (no per-session reads)."""
    if not session_ids:
        return {}
    rows = client.table("game_results").select("session_id,score,highest_level,accuracy") \
        .in_("session_id", session_ids).execute().data or []
    return {row["session_id"]: row for row in rows}


def search_sessions(client, codename: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = (client.table("game_sessions")
            .select("id,session_number,codename,status,created_at,completed_at")
            .ilike("codename", escape_like(codename)).order("created_at", desc=True)
            .limit(limit).execute().data or [])
    results = fetch_results_for_sessions(client, [r["id"] for r in rows if r["status"] == "completed"])
    sessions = []
    for row in rows:
        result = results.get(row["id"]) if row["status"] == "completed" else None
        sessions.append({
            "session_id": row["id"], "display_id": readable_id(row["session_number"]),
            "codename": row["codename"], "status": row["status"], "created_at": row["created_at"],
            "completed_at": row.get("completed_at"),
            "score": result.get("score") if result else None,
            "highest_level": result.get("highest_level") if result else None,
            "accuracy": result.get("accuracy") if result else None,
        })
    return sessions


def fetch_previous_results(client, codename: str, exclude_session_id: str, limit: int = 3):
    """Most recent earlier completed results for the same codename (newest first).

    Optional context for the Intelligent System: any failure returns [] and is
    logged, because a missing comparison must never block saving a result.
    Codenames are not verified identities; callers must word comparisons accordingly.
    """
    try:
        sessions = (client.table("game_sessions").select("id,completed_at")
                    .ilike("codename", escape_like(codename)).eq("status", "completed")
                    .neq("id", exclude_session_id).order("completed_at", desc=True)
                    .limit(limit).execute().data or [])
        if not sessions:
            return []
        rows = client.table("game_results") \
            .select("session_id,accuracy,mistakes,highest_level,total_rounds,round_data") \
            .in_("session_id", [s["id"] for s in sessions]).execute().data or []
        by_id = {r["session_id"]: r for r in rows}
        return [by_id[s["id"]] for s in sessions if s["id"] in by_id]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load previous results for comparison (%s)", type(exc).__name__)
        return []
