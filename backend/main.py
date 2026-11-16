import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from supabase import Client, create_client
from .data_access import (  # noqa: F401 - build_leaderboard_entries/get_result stay importable from main
    build_leaderboard_entries, create_waiting_session, dt_utc, fetch_previous_results,
    get_result, queue_state, readable_id, search_sessions,
)
from .difficulty_events import validate_events
from .intelligent_system import analyze_performance
from .reliability import (
    ApiError, ResilientClient, check_database, classify_db_error, new_error_id,
)
from .telemetry import normalize_difficulty_events_input, normalize_round_data

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("lmc")
ROOT = Path(__file__).resolve().parent
WEB = ROOT.parent / "web"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
DEVICE_API_TOKEN = os.getenv("DEVICE_API_TOKEN", "").strip()
DEFAULT_DEVICE_ID = os.getenv("DEFAULT_DEVICE_ID", "ESP32-001").strip()
DB_TIMEOUT_SECONDS = float(os.getenv("DB_TIMEOUT_SECONDS", "10") or 10)
DEVICE_SEEN_WINDOW_SECONDS = 15


def _make_supabase() -> Client | None:
    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        logger.warning("Supabase is not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing).")
        return None
    try:  # bound every database call; the library default is far longer than a player will wait
        from supabase.lib.client_options import ClientOptions
        return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
                             options=ClientOptions(postgrest_client_timeout=DB_TIMEOUT_SECONDS))
    except Exception as exc:  # noqa: BLE001 - option names differ between supabase-py versions
        logger.warning("Could not apply the database timeout option (%s); using library defaults.", type(exc).__name__)
        return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


supabase: Client | None = _make_supabase()
app = FastAPI(title="Light Memory Challenge API", version="0.5.0")
_device_last_seen: dict[str, float] = {}  # in-memory, per process; see /api/health


def db() -> ResilientClient:
    if supabase is None:
        raise HTTPException(503, "Supabase is not configured. Check your .env file.")
    return ResilientClient(supabase)


def touch_device(device_id: str | None):
    _device_last_seen[device_id or DEFAULT_DEVICE_ID] = time.monotonic()


def require_device_token(token: str | None):
    if not DEVICE_API_TOKEN:
        raise HTTPException(503, "DEVICE_API_TOKEN is not configured on the server.")
    if token != DEVICE_API_TOKEN:
        raise HTTPException(401, "Invalid or missing device token.")

class CreateSession(BaseModel):
    codename: str = Field(min_length=2, max_length=24)
    device_id: str = Field(default=DEFAULT_DEVICE_ID, min_length=1, max_length=64)
    @field_validator("codename")
    @classmethod
    def validate_codename(cls, value):
        value = value.strip()
        if len(value) < 2: raise ValueError("Codename must have at least two non-space characters.")
        if not all(ch.isalnum() or ch in " _-" for ch in value):
            raise ValueError("Use letters, numbers, spaces, hyphens, or underscores only.")
        return value

class DeviceAbandon(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    session_id: UUID


class GameResult(BaseModel):
    score: int = Field(ge=0)
    highest_level: int = Field(ge=1)
    mistakes: int = Field(ge=0)
    total_rounds: int = Field(ge=0)
    correct_rounds: int = Field(ge=0)
    accuracy: float = Field(ge=0, le=100)
    game_duration_seconds: float = Field(ge=0)
    lives_remaining: int = Field(ge=0, le=3)
    avg_response_time_ms: float | None = Field(default=None, ge=0)
    fastest_response_time_ms: float | None = Field(default=None, ge=0)
    slowest_response_time_ms: float | None = Field(default=None, ge=0)
    round_data: list[dict[str, Any]] = Field(default_factory=list)
    difficulty_events: list[dict[str, Any]] = Field(default_factory=list)
    @field_validator("round_data", mode="before")
    @classmethod
    def normalize_round_data_field(cls, value):
        return normalize_round_data(value)

    @field_validator("difficulty_events", mode="before")
    @classmethod
    def normalize_difficulty_events_field(cls, value):
        return normalize_difficulty_events_input(value)

    @field_validator("correct_rounds")
    @classmethod
    def validate_correct_rounds(cls, value, info):
        total = info.data.get("total_rounds")
        if total is not None and value > total: raise ValueError("correct_rounds cannot exceed total_rounds.")
        return value

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """Log the real exception; return a safe, specific message (no stack trace, no secrets)."""
    info = classify_db_error(exc)
    error_id = new_error_id()
    logger.error("Unhandled error [%s] %s %s kind=%s", error_id, request.method, request.url.path, info.kind,
                 exc_info=(type(exc), exc, exc.__traceback__))
    return JSONResponse({"detail": f"{info.message} (ref {error_id})", "error_id": error_id},
                        status_code=info.status)


@app.get("/", include_in_schema=False)
def home(): return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

@app.get("/api/health")
def health():
    """Backend, configuration, real database reachability, and device last-seen.

    ``status`` is "ok" only when the database answered. The ESP32 is never
    reported as connected: the board has no heartbeat, so the best available
    evidence is "an authenticated request arrived recently" (it polls once a
    second while idle, but not during a game or while offline).
    """
    database = ({"status": "not_configured", "detail": "Supabase is not configured.", "latency_ms": None}
                if supabase is None else check_database(supabase))
    seen = _device_last_seen.get(DEFAULT_DEVICE_ID)
    age = None if seen is None else max(0, int(time.monotonic() - seen))
    recent = age is not None and age <= DEVICE_SEEN_WINDOW_SECONDS
    return {
        "status": "ok" if database["status"] == "ok" else "degraded",
        "backend": "ok",
        "supabase_configured": supabase is not None,
        "database": database,
        "device": {
            "status": "recently_seen" if recent else "unknown",
            "last_seen_seconds_ago": age,
            "note": "Connection to the ESP32 cannot be confirmed: the board sends no heartbeat while a game "
                    "is running or when it is offline.",
        },
        "device_id": DEFAULT_DEVICE_ID, "api_version": "0.5.0",
    }

@app.post("/api/sessions", status_code=201)
def create_session(payload: CreateSession, idempotency_key: str | None = Header(default=None)):
    client = db()
    session_id = None
    if idempotency_key:
        try:
            session_id = str(UUID(idempotency_key))
        except ValueError:
            raise HTTPException(422, "Idempotency-Key must be a valid UUID.")
    row, replayed = create_waiting_session(client, payload.codename, payload.device_id, session_id)
    return {
        "session_id": row["id"], "display_id": readable_id(row["session_number"]),
        "session_number": row["session_number"], "codename": row["codename"],
        "device_id": row["device_id"], "status": row["status"], "created_at": row["created_at"],
        "idempotent_replay": replayed,
        **queue_state(client, row["device_id"], row["id"])
    }

@app.post("/api/sessions/{session_id}/start")
def start_session(session_id: UUID):
    client, sid = db(), str(session_id)
    q = client.table("game_sessions").select("id,status,device_id").eq("id", sid).limit(1).execute()
    if not q.data:
        raise HTTPException(404, "Session not found.")
    session = q.data[0]
    if session["status"] != "waiting":
        raise HTTPException(409, f"Session cannot start from status '{session['status']}'.")
    state = queue_state(client, session["device_id"], sid)
    if state["active_session"]:
        raise HTTPException(409, "The physical game is already running another session.")
    if not state["is_next"]:
        raise HTTPException(409, "Please wait for your turn. Sessions start in queue order.")
    start_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    updated = client.table("game_sessions").update({
        "status": "countdown", "start_at": start_at.isoformat()
    }).eq("id", sid).eq("status", "waiting").execute()
    if not updated.data:
        raise HTTPException(409, "Session status changed before it could be started.")
    return {
        "session_id": sid,
        "status": "countdown",
        "countdown_seconds": 5,
        "start_at": start_at.isoformat(),
        "start_at_epoch": int(start_at.timestamp()),
        "countdown_remaining_ms": max(0, int((start_at - datetime.now(timezone.utc)).total_seconds() * 1000)),
    }


@app.post("/api/sessions/{session_id}/abandon")
def abandon_session(session_id: UUID):
    client, sid = db(), str(session_id)
    q = client.table("game_sessions").select("id,status,device_id").eq("id", sid).limit(1).execute()
    if not q.data:
        raise HTTPException(404, "Session not found.")
    session = q.data[0]
    if session["status"] not in ("waiting", "countdown", "playing"):
        raise HTTPException(409, f"Session cannot be abandoned from status '{session['status']}'.")
    updated = client.table("game_sessions").update({
        "status": "abandoned"
    }).eq("id", sid).in_("status", ["waiting", "countdown", "playing"]).execute()
    if not updated.data:
        raise HTTPException(409, "Session status changed before it could be abandoned.")
    return {
        "session_id": sid,
        "status": "abandoned",
        **queue_state(client, session["device_id"], sid),
    }


@app.post("/api/device/abandon")
def device_abandon(payload: DeviceAbandon, x_device_token: str | None = Header(default=None)):
    """Abandon only the exact active session requested by the authenticated ESP32."""
    require_device_token(x_device_token)
    touch_device(payload.device_id)
    client = db()
    sid = str(payload.session_id)
    q = (
        client.table("game_sessions")
        .select("id,status,device_id")
        .eq("id", sid)
        .limit(1)
        .execute()
    )
    if not q.data:
        raise HTTPException(404, "Session not found.")
    session = q.data[0]
    if session["device_id"] != payload.device_id:
        raise HTTPException(403, "Session does not belong to this device.")
    if session["status"] == "abandoned":
        return {"session_id": sid, "status": "abandoned", "idempotent": True}
    if session["status"] == "completed":
        return {"session_id": sid, "status": "completed", "ignored": True}
    if session["status"] not in ("waiting", "countdown", "playing"):
        raise HTTPException(409, f"Session cannot be abandoned from status '{session['status']}'.")
    updated = (
        client.table("game_sessions")
        .update({"status": "abandoned"})
        .eq("id", sid)
        .eq("device_id", payload.device_id)
        .in_("status", ["waiting", "countdown", "playing"])
        .execute()
    )
    if not updated.data:
        latest = client.table("game_sessions").select("status").eq("id", sid).limit(1).execute()
        if latest.data and latest.data[0]["status"] == "abandoned":
            return {"session_id": sid, "status": "abandoned", "idempotent": True}
        if latest.data and latest.data[0]["status"] == "completed":
            return {"session_id": sid, "status": "completed", "ignored": True}
        raise HTTPException(409, "Session changed before it could be abandoned.")
    return {"session_id": sid, "status": "abandoned"}


@app.get("/api/device/command")
def device_command(device_id: str = Query(default=DEFAULT_DEVICE_ID), x_device_token: str | None = Header(default=None)):
    require_device_token(x_device_token)
    touch_device(device_id)
    client = db()
    # The board's physical abandon button targets the active countdown/game,
    # or the oldest waiting session when nobody has started yet.
    target_q = (client.table("game_sessions")
        .select("id,session_number,codename,status,start_at")
        .eq("device_id", device_id)
        .in_("status", ["countdown", "playing"])
        .order("created_at").limit(1).execute())
    if not target_q.data:
        target_q = (client.table("game_sessions")
            .select("id,session_number,codename,status,start_at")
            .eq("device_id", device_id).eq("status", "waiting")
            .order("created_at").order("session_number").limit(1).execute())
    target = target_q.data[0] if target_q.data else None
    q = (client.table("game_sessions").select("id,session_number,codename,device_id,start_at")
         .eq("device_id", device_id).eq("status", "countdown")
         .order("created_at", desc=True).limit(1).execute())
    if not q.data:
        return {"command": "NONE", "abandon_session_id": target["id"] if target else None,
                "abandon_display_id": readable_id(target["session_number"]) if target else None,
                "abandon_status": target["status"] if target else None}
    row = q.data[0]
    start_at = dt_utc(row.get("start_at"))
    if not start_at:
        return {"command": "NONE", "abandon_session_id": target["id"] if target else None,
                "abandon_display_id": readable_id(target["session_number"]) if target else None,
                "abandon_status": target["status"] if target else None}
    countdown_remaining_ms = max(
        0,
        int((start_at - datetime.now(timezone.utc)).total_seconds() * 1000),
    )

    return {
        "command": "START",
        "session_id": row["id"],
        "display_id": readable_id(row["session_number"]),
        "codename": row["codename"],
        "device_id": row["device_id"],
        "start_at": start_at.isoformat(),
        "start_at_epoch": int(start_at.timestamp()),
        "countdown_remaining_ms": countdown_remaining_ms,
        "abandon_session_id": target["id"] if target else None,
        "abandon_display_id": readable_id(target["session_number"]) if target else None,
        "abandon_status": target["status"] if target else None,
    }
    

@app.post("/api/device/started")
def device_started(payload: dict[str, str], x_device_token: str | None = Header(default=None)):
    require_device_token(x_device_token)
    touch_device(DEFAULT_DEVICE_ID)
    sid = payload.get("session_id")
    if not sid: raise HTTPException(422, "session_id is required.")
    try: UUID(sid)
    except ValueError: raise HTTPException(422, "session_id must be a valid UUID.")
    client = db()
    q = client.table("game_sessions").select("id,status,start_at").eq("id", sid).limit(1).execute()
    if not q.data: raise HTTPException(404, "Session not found.")
    row = q.data[0]
    if row["status"] != "countdown": raise HTTPException(409, f"Session is not in countdown (status: {row['status']}).")
    start_at = dt_utc(row.get("start_at"))
    if start_at and datetime.now(timezone.utc) < start_at: raise HTTPException(409, "Scheduled start time has not arrived.")
    updated = client.table("game_sessions").update({"status": "playing", "started_at": datetime.now(timezone.utc).isoformat()}).eq("id", sid).eq("status", "countdown").execute()
    if not updated.data: raise HTTPException(409, "Session was already updated.")
    return {"session_id": sid, "status": "playing"}

@app.get("/api/leaderboard")
def leaderboard(limit: int = Query(default=100, ge=1, le=500)):
    """Return each codename's personal best; codenames are not authenticated identities."""
    client = db()
    entries = build_leaderboard_entries(client)[:limit]
    return {
        "entries": entries,
        "count": len(entries),
        "ranking_basis": "Highest level descending, score descending, then earliest saved result.",
        "identity_note": "One personal-best entry per codename; codenames are not unique or authenticated identities.",
    }

@app.get("/api/players/search")
def search_player(codename: str = Query(min_length=2, max_length=24)):
    codename = codename.strip()
    if len(codename) < 2: raise HTTPException(422, "Enter at least two characters.")
    if not all(ch.isalnum() or ch in " _-" for ch in codename):
        raise HTTPException(422, "Use letters, numbers, spaces, hyphens, or underscores only.")
    sessions = search_sessions(db(), codename)  # two queries total, however many sessions match
    return {"codename": codename, "count": len(sessions), "sessions": sessions}

@app.get("/api/sessions/{session_id}")
def get_session(session_id: UUID):
    client, sid = db(), str(session_id)
    q = client.table("game_sessions").select("*").eq("id", sid).limit(1).execute()
    if not q.data: raise HTTPException(404, "Session not found.")
    row = q.data[0]
    return {
        "session_id": row["id"], "display_id": readable_id(row["session_number"]),
        "session_number": row["session_number"], "codename": row["codename"],
        "device_id": row["device_id"], "status": row["status"], "created_at": row["created_at"],
        "start_at": row.get("start_at"),
        "countdown_remaining_ms": (
            max(0, int((dt_utc(row.get("start_at")) - datetime.now(timezone.utc)).total_seconds() * 1000))
            if row.get("status") == "countdown" and dt_utc(row.get("start_at")) else None
        ),
        "started_at": row.get("started_at"),
        "completed_at": row.get("completed_at"),
        "result": get_result(client, sid, codename=row["codename"]) if row["status"] == "completed" else None,
        **queue_state(client, row["device_id"], sid)
    }


@app.post("/api/sessions/{session_id}/results", status_code=201)
def save_results(session_id: UUID, payload: GameResult):
    client = db()
    sid = str(session_id)

    # 1. Confirm that the session exists.
    q = (
        client.table("game_sessions")
        .select("id,session_number,codename,status,completed_at")
        .eq("id", sid)
        .limit(1)
        .execute()
    )

    if not q.data:
        raise HTTPException(404, "Session not found.")

    session = q.data[0]
    if session["status"] == "abandoned":
        raise HTTPException(409, "This session was abandoned. Results will not be accepted.")

    # 2. Check whether a result was already saved.
    # This MUST happen before rejecting a non-playing session.
    existing_q = (
        client.table("game_results")
        .select("*")
        .eq("session_id", sid)
        .limit(1)
        .execute()
    )

    if existing_q.data:
        # The first upload succeeded, but the ESP32 may have
        # lost the HTTP response. Treat this retry as success.
        existing = existing_q.data[0]

        # Repair the session status if the result was saved
        # but the session update did not finish.
        if session["status"] != "completed":
            update_data = {"status": "completed"}
            if not session.get("completed_at"):
                update_data["completed_at"] = datetime.now(timezone.utc).isoformat()
            repaired = (
                client.table("game_sessions")
                .update(update_data)
                .eq("id", sid)
                .eq("status", "playing")
                .execute()
            )
            if not repaired.data:
                latest = (
                    client.table("game_sessions")
                    .select("status")
                    .eq("id", sid)
                    .limit(1)
                    .execute()
                )
                if latest.data and latest.data[0]["status"] == "abandoned":
                    client.table("game_results").delete().eq("session_id", sid).execute()
                    raise HTTPException(409, "This session was abandoned. Results will not be accepted.")
                if not latest.data or latest.data[0]["status"] != "completed":
                    raise HTTPException(409, "Session status changed before the result could be confirmed.")

        result = get_result(client, sid)

        return {
            "message": "Game results already saved.",
            "session_id": sid,
            "display_id": readable_id(session["session_number"]),
            "codename": session["codename"],
            "score": existing["score"],
            "ranking": result.get("ranking") if result else None,
            "status": "completed",
            "analysis": existing.get("analysis"),
            "result": result if result else existing,
        }

    # 3. No result exists yet, so a new result can only be
    # submitted while the session is playing.
    if session["status"] != "playing":
        raise HTTPException(
            409,
            "Results can only be saved when session status is playing."
        )

    # 4. Calculate the intelligent-system analysis.
    analysis = analyze_performance(
        payload.accuracy,
        payload.mistakes,
        payload.avg_response_time_ms,
        payload.round_data,
        highest_level=payload.highest_level,
        history=fetch_previous_results(client, session["codename"], sid),
    )
    # In-game adaptive-difficulty adjustments reported by the ESP32 (which alone
    # detects the consecutive mistakes). The backend only checks them for
    # plausibility against the per-level round data. Stored inside the existing
    # `analysis` JSONB, so no database migration is needed.
    analysis["gameplay_difficulty_events"] = validate_events(payload.difficulty_events, payload.round_data)

    row = {
        "session_id": sid,
        **payload.model_dump(exclude={"difficulty_events"}),
        "analysis": analysis,
    }

    # 5. Insert the result and mark the session completed.
    try:
        saved = client.table("game_results").insert(row).execute()

        if not saved.data:
            raise RuntimeError("No result row returned")

        updated = (
            client.table("game_sessions")
            .update({
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
            .eq("id", sid)
            .eq("status", "playing")
            .execute()
        )

        if not updated.data:
            client.table("game_results").delete().eq("session_id", sid).execute()
            raise RuntimeError("Session changed before completion; inserted result was rolled back.")

    except Exception as save_exc:
        logger.exception("Saving results for session %s failed", sid)
        # Re-read the session after a failed insert/status update. If physical
        # abandonment won the race, remove any result row and never rank it.
        try:
            latest_session_q = (
                client.table("game_sessions")
                .select("status,completed_at,session_number,codename")
                .eq("id", sid)
                .limit(1)
                .execute()
            )
            latest_session = latest_session_q.data[0] if latest_session_q.data else None
            retry_q = (
                client.table("game_results")
                .select("*")
                .eq("session_id", sid)
                .limit(1)
                .execute()
            )
            if latest_session and latest_session["status"] == "abandoned":
                if retry_q.data:
                    client.table("game_results").delete().eq("session_id", sid).execute()
                raise HTTPException(409, "This session was abandoned. Results will not be accepted.")

            if retry_q.data and latest_session:
                existing = retry_q.data[0]
                if latest_session["status"] == "playing":
                    repaired = (
                        client.table("game_sessions")
                        .update({
                            "status": "completed",
                            "completed_at": (
                                latest_session.get("completed_at")
                                or datetime.now(timezone.utc).isoformat()
                            ),
                        })
                        .eq("id", sid)
                        .eq("status", "playing")
                        .execute()
                    )
                    if not repaired.data:
                        refreshed = (
                            client.table("game_sessions")
                            .select("status")
                            .eq("id", sid)
                            .limit(1)
                            .execute()
                        )
                        if refreshed.data and refreshed.data[0]["status"] == "abandoned":
                            client.table("game_results").delete().eq("session_id", sid).execute()
                            raise HTTPException(409, "This session was abandoned. Results will not be accepted.")
                result = get_result(client, sid)
                return {
                    "message": "Game results already saved.",
                    "session_id": sid,
                    "display_id": readable_id(latest_session["session_number"]),
                    "codename": latest_session["codename"],
                    "score": existing["score"],
                    "ranking": result.get("ranking") if result else None,
                    "status": "completed",
                    "analysis": existing.get("analysis"),
                    "result": result if result else existing,
                }
        except HTTPException:
            raise
        except Exception:
            logger.exception("Recovery after a failed result save also failed for session %s", sid)

        info = classify_db_error(save_exc)
        if info.kind == "application":
            raise ApiError(500, "Could not save the results. The board keeps unsent results and will retry.")
        raise ApiError(info.status, info.message)

    # 6. Return the newly saved result.
    result = get_result(client, sid)

    return {
        "message": "Game results saved.",
        "session_id": sid,
        "display_id": readable_id(session["session_number"]),
        "codename": session["codename"],
        "score": payload.score,
        "ranking": result.get("ranking") if result else None,
        "leaderboard": result.get("leaderboard") if result else None,
        "status": "completed",
        "analysis": analysis,
        "result": result,
    }
