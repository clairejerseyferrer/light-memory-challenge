import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from supabase import Client, create_client
from .intelligent_system import analyze_performance

load_dotenv()
ROOT = Path(__file__).resolve().parent
WEB = ROOT.parent / "web"
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
DEVICE_API_TOKEN = os.getenv("DEVICE_API_TOKEN", "").strip()
DEFAULT_DEVICE_ID = os.getenv("DEFAULT_DEVICE_ID", "ESP32-001").strip()
supabase: Client | None = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY) if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY else None
app = FastAPI(title="Light Memory Challenge API", version="0.3.0")


def db() -> Client:
    if supabase is None:
        raise HTTPException(503, "Supabase is not configured. Check your .env file.")
    return supabase

def require_device_token(token: str | None):
    if DEVICE_API_TOKEN and token != DEVICE_API_TOKEN:
        raise HTTPException(401, "Invalid or missing device token.")

def dt_utc(value):
    if not value: return None
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def readable_id(number: int) -> str:
    return f"Session #{int(number):03d}"

def get_result(client: Client, sid: str):
    q = client.table("game_results").select("*").eq("session_id", sid).limit(1).execute()
    if not q.data: return None
    result = q.data[0]
    all_results = client.table("game_results").select("session_id,score,highest_level,saved_at").order("score", desc=True).order("highest_level", desc=True).order("saved_at").execute()
    result["ranking"] = next((i for i, row in enumerate(all_results.data or [], 1) if row["session_id"] == sid), None)
    result["ranking_basis"] = "Score descending, then highest level descending, then earliest saved result."
    return result

def queue_state(client: Client, device_id: str, session_id: str | None = None):
    """Return the active game and FIFO waiting queue for one physical device."""
    q = (
        client.table("game_sessions")
        .select("id,session_number,codename,device_id,status,created_at,start_at")
        .eq("device_id", device_id)
        .in_("status", ["waiting", "countdown", "playing"])
        .order("created_at")
        .order("session_number")
        .execute()
    )
    rows = q.data or []
    active = next((row for row in rows if row["status"] in ("countdown", "playing")), None)
    waiting = [row for row in rows if row["status"] == "waiting"]
    current = next((row for row in rows if row["id"] == session_id), None)
    queue_position = next((i + 1 for i, row in enumerate(waiting) if row["id"] == session_id), None)
    return {
        "active_session": ({
            "session_id": active["id"],
            "display_id": readable_id(active["session_number"]),
            "codename": active["codename"],
            "status": active["status"],
        } if active else None),
        "waiting_count": len(waiting),
        "max_waiting": 3,
        "queue_position": queue_position,
        "is_next": bool(current and current["status"] == "waiting" and not active and waiting and waiting[0]["id"] == session_id),
        "waiting_sessions": [
            {
                "session_id": row["id"],
                "display_id": readable_id(row["session_number"]),
                "codename": row["codename"],
                "created_at": row["created_at"],
            }
            for row in waiting
        ],
    }

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
    @field_validator("correct_rounds")
    @classmethod
    def validate_correct_rounds(cls, value, info):
        total = info.data.get("total_rounds")
        if total is not None and value > total: raise ValueError("correct_rounds cannot exceed total_rounds.")
        return value

@app.get("/", include_in_schema=False)
def home(): return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-store"})

@app.get("/api/health")
def health():
    return {"status": "ok", "supabase_configured": supabase is not None, "device_id": DEFAULT_DEVICE_ID, "api_version": "0.3.0"}

@app.post("/api/sessions", status_code=201)
def create_session(payload: CreateSession):
    client = db()
    # Waiting sessions are capped at three; countdown/playing is the one active game.
    current_queue = queue_state(client, payload.device_id)
    if current_queue["waiting_count"] >= 3:
        raise HTTPException(409, "The queue is full (maximum 3 waiting players). Please try again when a player starts or leaves.")
    try:
        response = client.table("game_sessions").insert({
            "id": str(uuid4()), "codename": payload.codename,
            "device_id": payload.device_id, "status": "waiting"
        }).execute()
    except Exception:
        raise HTTPException(500, "Could not create session. Did you run migration_readable_session_ids.sql in Supabase?")
    if not response.data: raise HTTPException(500, "Session was not created.")
    row = response.data[0]
    if row.get("session_number") is None:
        raise HTTPException(500, "No session_number returned. Run the database migration.")
    return {
        "session_id": row["id"], "display_id": readable_id(row["session_number"]),
        "session_number": row["session_number"], "codename": row["codename"],
        "device_id": row["device_id"], "status": row["status"], "created_at": row["created_at"],
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
def device_abandon(device_id: str = Query(default=DEFAULT_DEVICE_ID), x_device_token: str | None = Header(default=None)):
    """Allow the authenticated physical device to abandon its current active game."""
    require_device_token(x_device_token)
    client = db()
    q = (
        client.table("game_sessions")
        .select("id,status")
        .eq("device_id", device_id)
        .in_("status", ["countdown", "playing"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not q.data:
        return {"status": "no_active_session"}
    sid = q.data[0]["id"]
    updated = (
        client.table("game_sessions")
        .update({"status": "abandoned"})
        .eq("id", sid)
        .in_("status", ["countdown", "playing"])
        .execute()
    )
    if not updated.data:
        raise HTTPException(409, "Active session changed before it could be abandoned.")
    return {"session_id": sid, "status": "abandoned"}


@app.get("/api/device/command")
def device_command(device_id: str = Query(default=DEFAULT_DEVICE_ID), x_device_token: str | None = Header(default=None)):
    require_device_token(x_device_token)
    q = db().table("game_sessions").select("id,session_number,codename,device_id,start_at").eq("device_id", device_id).eq("status", "countdown").order("created_at", desc=True).limit(1).execute()
    if not q.data: return {"command": "NONE"}
    row = q.data[0]
    start_at = dt_utc(row.get("start_at"))
    if not start_at: return {"command": "NONE"}
    remaining_seconds = max(
    0,
    int((start_at - datetime.now(timezone.utc)).total_seconds() + 0.999),
    )

    return {
        "command": "START",
        "session_id": row["id"],
        "display_id": readable_id(row["session_number"]),
        "codename": row["codename"],
        "device_id": row["device_id"],
        "start_at": start_at.isoformat(),
        "start_at_epoch": int(start_at.timestamp()),
        "countdown_seconds": remaining_seconds,
    }
    

@app.post("/api/device/started")
def device_started(payload: dict[str, str], x_device_token: str | None = Header(default=None)):
    require_device_token(x_device_token)
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

@app.get("/api/players/search")
def search_player(codename: str = Query(min_length=2, max_length=24)):
    codename = codename.strip()
    if len(codename) < 2: raise HTTPException(422, "Enter at least two characters.")
    client = db()
    try:
        q = client.table("game_sessions").select("id,session_number,codename,status,created_at,completed_at").ilike("codename", codename).order("created_at", desc=True).limit(50).execute()
    except Exception: raise HTTPException(500, "Could not search sessions.")
    sessions = []
    for row in q.data or []:
        result = get_result(client, row["id"]) if row["status"] == "completed" else None
        sessions.append({
            "session_id": row["id"], "display_id": readable_id(row["session_number"]),
            "codename": row["codename"], "status": row["status"], "created_at": row["created_at"],
            "completed_at": row.get("completed_at"), "score": result.get("score") if result else None,
            "highest_level": result.get("highest_level") if result else None,
            "accuracy": result.get("accuracy") if result else None
        })
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
        "result": get_result(client, sid) if row["status"] == "completed" else None,
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

            # Preserve an existing completion timestamp.
            if not session.get("completed_at"):
                update_data["completed_at"] = (
                    datetime.now(timezone.utc).isoformat()
                )

            client.table("game_sessions").update(
                update_data
            ).eq("id", sid).execute()

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
    )

    row = {
        "session_id": sid,
        **payload.model_dump(),
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
            raise RuntimeError("Could not mark session completed")

    except Exception:
        # A concurrent retry may have inserted the same result
        # after our initial existence check. Recheck before
        # reporting a failure.
        try:
            retry_q = (
                client.table("game_results")
                .select("*")
                .eq("session_id", sid)
                .limit(1)
                .execute()
            )

            if retry_q.data:
                existing = retry_q.data[0]

                client.table("game_sessions").update({
                    "status": "completed",
                    "completed_at": (
                        session.get("completed_at")
                        or datetime.now(timezone.utc).isoformat()
                    ),
                }).eq("id", sid).execute()

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

        except Exception:
            pass

        raise HTTPException(
            500,
            "Could not save results. Check Supabase columns and server logs."
        )

    # 6. Return the newly saved result.
    result = get_result(client, sid)

    return {
        "message": "Game results saved.",
        "session_id": sid,
        "display_id": readable_id(session["session_number"]),
        "codename": session["codename"],
        "score": payload.score,
        "ranking": result.get("ranking") if result else None,
        "status": "completed",
        "analysis": analysis,
        "result": result,
    }
