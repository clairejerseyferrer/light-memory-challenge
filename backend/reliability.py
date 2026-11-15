"""Database error classification, safe error messages, and bounded retries.

This module deliberately imports neither FastAPI, httpx, postgrest nor supabase,
so it can be unit-tested anywhere. Exceptions are recognised by class name along
their MRO (httpx's ``ConnectError``/``ReadTimeout``/... and postgrest's
``APIError``), which keeps it working across library versions.

Retry policy (see ``with_retries``):
  * Reads and other idempotent calls are retried on transient failures.
  * Writes are retried ONLY when the request provably never reached the server
    (connect-phase failures). A write that may have been processed is never
    blindly replayed; callers that need replay safety use a client-supplied
    primary key / idempotency key and read back (see data_access.create_waiting_session).
  * Permanent errors (schema, auth, constraint, validation) are never retried.
  * Exponential backoff with jitter, a fixed attempt cap, and a total time budget.
"""
from __future__ import annotations

import logging
import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

logger = logging.getLogger("lmc")
T = TypeVar("T")

# ---- exception recognition (by class name, version independent) -------------
_CONNECT_PHASE_NAMES = {"ConnectError", "ConnectTimeout", "PoolTimeout"}
_TRANSIENT_TRANSPORT_NAMES = _CONNECT_PHASE_NAMES | {
    "TimeoutException", "ReadTimeout", "WriteTimeout",
    "NetworkError", "ReadError", "WriteError", "CloseError",
    "RemoteProtocolError",
}
_TRANSIENT_BUILTINS = {"ConnectionError", "ConnectionResetError", "ConnectionAbortedError",
                       "ConnectionRefusedError", "BrokenPipeError", "TimeoutError", "gaierror"}
# Postgres / PostgREST / gateway codes that indicate a temporary condition.
_TRANSIENT_CODES = {"57014", "53300", "53400", "08000", "08001", "08003", "08004", "08006",
                    "57P01", "57P03", "PGRST000", "PGRST001", "PGRST002",
                    "502", "503", "504", "520", "521", "522", "523", "524"}
# Codes that mean the database schema differs from what this server expects.
_SCHEMA_CODES = {"42P01", "42703", "42883", "PGRST200", "PGRST204", "PGRST205"}
_AUTH_CODES = {"PGRST301", "PGRST302", "401", "403", "42501"}
UNIQUE_VIOLATION = "23505"


@dataclass(frozen=True)
class DbErrorInfo:
    kind: str            # connectivity | timeout | transient_db | schema | auth | unique_violation | database | application
    status: int          # HTTP status to return
    message: str         # safe for end users; never contains credentials or stack traces
    retryable: bool      # safe to retry when the operation is idempotent
    connect_phase: bool  # request never reached the server, so even writes may be retried


class ApiError(Exception):
    """Framework-independent HTTP error; main.py converts it to a JSON response."""

    def __init__(self, status_code: int, detail: str, headers: dict[str, str] | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers


def _mro_names(exc: BaseException) -> set[str]:
    return {cls.__name__ for cls in type(exc).__mro__}


def error_code(exc: BaseException) -> str | None:
    """Best-effort extraction of a PostgREST/Postgres error code."""
    code = getattr(exc, "code", None)
    if code is None and exc.args and isinstance(exc.args[0], dict):
        code = exc.args[0].get("code")
    return str(code) if code is not None else None


def _error_text(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    if message is None and exc.args and isinstance(exc.args[0], dict):
        message = exc.args[0].get("message")
    return str(message or "")


def is_unique_violation(exc: BaseException) -> bool:
    return error_code(exc) == UNIQUE_VIOLATION


def classify_db_error(exc: BaseException) -> DbErrorInfo:
    names = _mro_names(exc)
    code = error_code(exc)
    text = _error_text(exc).lower()

    if names & _TRANSIENT_TRANSPORT_NAMES or names & _TRANSIENT_BUILTINS:
        timeout = bool(names & {"TimeoutException", "ConnectTimeout", "ReadTimeout",
                                "WriteTimeout", "PoolTimeout", "TimeoutError"})
        return DbErrorInfo(
            "timeout" if timeout else "connectivity", 503,
            "The database took too long to respond. Please try again in a moment." if timeout
            else "The database could not be reached. This is usually temporary; please try again in a moment.",
            retryable=True,
            connect_phase=bool(names & _CONNECT_PHASE_NAMES),
        )
    if code == UNIQUE_VIOLATION:
        return DbErrorInfo("unique_violation", 409, "That record already exists.", False, False)
    if code in _SCHEMA_CODES:
        return DbErrorInfo(
            "schema", 500,
            "The database structure does not match this version of the server. "
            "An administrator may need to apply the latest database migration.",
            False, False)
    if code in _AUTH_CODES or "invalid api key" in text or "jwt" in text:
        return DbErrorInfo(
            "auth", 503,
            "The server could not authenticate with the database. An administrator should check the server configuration.",
            False, False)
    if code in _TRANSIENT_CODES:
        return DbErrorInfo("transient_db", 503,
                           "The database is temporarily unavailable. Please try again in a moment.",
                           True, False)
    if "APIError" in names:
        return DbErrorInfo("database", 500, "The database rejected the request.", False, False)
    return DbErrorInfo("application", 500, "Unexpected server error.", False, False)


def new_error_id() -> str:
    return uuid.uuid4().hex[:8]


# ---- retries -----------------------------------------------------------------
def _env_number(name: str, default: float, cast=float):
    try:
        return cast(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


MAX_ATTEMPTS = _env_number("DB_RETRY_ATTEMPTS", 3, int)
BASE_DELAY = _env_number("DB_RETRY_BASE_DELAY", 0.25)
MAX_DELAY = 2.0
TOTAL_BUDGET = _env_number("DB_RETRY_BUDGET_SECONDS", 12.0)


def with_retries(fn: Callable[[], T], *, idempotent: bool = True, attempts: int | None = None,
                 base_delay: float | None = None, max_delay: float = MAX_DELAY,
                 budget: float | None = None, op: str = "db",
                 sleep: Callable[[float], Any] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 rng: Callable[[], float] = random.random) -> T:
    attempts = max(1, attempts if attempts is not None else MAX_ATTEMPTS)
    base_delay = BASE_DELAY if base_delay is None else base_delay
    budget = TOTAL_BUDGET if budget is None else budget
    started = clock()
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified below, always re-raised
            info = classify_db_error(exc)
            may_retry = info.retryable if idempotent else (info.retryable and info.connect_phase)
            if not may_retry or attempt >= attempts:
                if may_retry:
                    logger.warning("%s failed after %d attempts (%s: %s)", op, attempt, type(exc).__name__, info.kind)
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = delay * (0.5 + 0.5 * rng())  # equal jitter
            if clock() - started + delay > budget:
                logger.warning("%s retry budget exhausted after %d attempt(s)", op, attempt)
                raise
            logger.warning("%s transient failure (%s: %s); retry %d/%d in %.2fs",
                           op, type(exc).__name__, info.kind, attempt, attempts - 1, delay)
            sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


# ---- transparent retry proxy around the Supabase client ------------------------
_WRITE_METHODS = {"insert", "update", "upsert", "delete"}


class ResilientQuery:
    """Wraps a postgrest query builder; ``execute()`` goes through ``with_retries``."""

    def __init__(self, query: Any, *, is_write: bool = False, retry: Callable[..., Any] = with_retries):
        self._query = query
        self._is_write = is_write
        self._retry = retry

    def __getattr__(self, name: str):
        attr = getattr(self._query, name)
        if not callable(attr):
            return attr
        if name == "execute":
            def execute(*args, **kwargs):
                return self._retry(lambda: self._query.execute(*args, **kwargs),
                                   idempotent=not self._is_write, op="supabase.execute")
            return execute

        def chain(*args, **kwargs):
            result = attr(*args, **kwargs)
            return ResilientQuery(result, is_write=self._is_write or name in _WRITE_METHODS, retry=self._retry)
        return chain


class ResilientClient:
    def __init__(self, client: Any, *, retry: Callable[..., Any] = with_retries):
        self._client = client
        self._retry = retry

    def table(self, name: str) -> ResilientQuery:
        return ResilientQuery(self._client.table(name), retry=self._retry)


# ---- database health ---------------------------------------------------------
def check_database(client: Any, *, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """One un-retried round trip, so the result reflects real connectivity."""
    started = clock()
    try:
        client.table("game_sessions").select("id").limit(1).execute()
    except Exception as exc:  # noqa: BLE001
        info = classify_db_error(exc)
        logger.warning("Health check database error (%s: %s)", type(exc).__name__, info.kind)
        status = {"connectivity": "unreachable", "timeout": "unreachable", "transient_db": "unreachable",
                  "schema": "schema_mismatch", "auth": "auth_error"}.get(info.kind, "error")
        return {"status": status, "detail": info.message, "latency_ms": int((clock() - started) * 1000)}
    return {"status": "ok", "detail": "Database responded.", "latency_ms": int((clock() - started) * 1000)}
