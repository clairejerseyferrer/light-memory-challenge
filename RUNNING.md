# Light Memory Challenge — Local Run Guide

## 1. Keep the project structure intact

Run the backend from the repository root (the folder containing `backend`, `web`, and `database`):

```text
light-memory-challenge-main/
├── backend/
│   ├── __init__.py
│   ├── main.py
│   ├── intelligent_system.py
│   └── requirements.txt
├── database/
├── firmware/
└── web/
    └── index.html
```

The backend imports the Intelligent System as `backend.intelligent_system`, and serves the dashboard from `web/index.html`. Do not move `main.py` or `intelligent_system.py` out of `backend/` unless you also update the imports.

## 2. Create the virtual environment and install dependencies (PowerShell)

Open PowerShell in `light-memory-challenge-main` and run:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r backend\requirements.txt
```

If Python 3.12 is not installed, check `py -0p` to see which Python versions are available. The import error in the reported traceback is caused by the module import path; it is not evidence by itself of a Python-version problem.

If PowerShell blocks activation, run this once for the current terminal, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 3. Configure Supabase

1. Copy `.env.example` to a new file named `.env` in the repository root.
2. Fill in `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` with your project's values. Keep the service-role key private; never commit `.env`.
3. In the Supabase SQL Editor, apply `database/schema.sql` and then `database/migrations/migration_readable_session_ids.sql` if the readable session-number migration has not already been applied.
4. Set a long random `DEVICE_API_TOKEN` and use the same token in the ESP32 firmware configuration if its device requests require authentication.

Do not replace your existing database with the schema blindly if it already contains project data; review the SQL first and apply only the missing changes.

## 4. Start the server

From the repository root, with `.venv` activated:

```powershell
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Do not append a backslash to `8000`. In PowerShell, a backslash is not the line-continuation character. If the server starts, open `http://127.0.0.1:8000` on the same computer and `http://YOUR_COMPUTER_LAN_IP:8000` on a phone connected to the same Wi-Fi. Windows Firewall may ask you to allow Python on private networks.

`0.0.0.0` makes the server listen on network interfaces; it is not the address to type into the browser. A phone on a different network cannot access this local server without a suitable deployment or secure network tunnel.

## 5. If Uvicorn still fails

- `ModuleNotFoundError: No module named 'intelligent_system'`: ensure you are in the repository root and use `python -m uvicorn backend.main:app ...`.
- `Supabase is not configured`: check that `.env` is in the repository root and that both Supabase values are set.
- `No session_number returned` or session creation fails: confirm the readable-session migration has been applied.
- `Could not save results` / missing columns: compare the actual Supabase table columns against `database/schema.sql`; do not assume the live database matches the local schema.

## Notes on this update

- Session ID is stored in browser `localStorage`, then fetched from the backend after reload so the backend remains the source of truth.
- The queue allows at most three sessions in `waiting` status per device, and only the first waiting player can start when no game is active.
- GPIO15 is now the physical abandon/reset button only. Hold it for 2 seconds during countdown or gameplay; it never starts a game. The website remains responsible for starting a queued session.
- The ESP32 calls authenticated `POST /api/device/abandon` with the exact `device_id` and `session_id`. If Wi-Fi is unavailable, that exact session ID is persisted and retried before the board accepts another start command.
- The website countdown uses a monotonic browser timer and server-provided remaining milliseconds rather than comparing the phone's wall clock directly to a UTC timestamp. Network delay or a backgrounded mobile tab can still affect what the player sees, while the backend's scheduled start remains authoritative.


## Intelligent System telemetry contract

The firmware now sends cumulative per-level summaries in `round_data`. For
each level the summary includes:
- `sequences_attempted`: every sequence shown and attempted at that level.
- `correct_sequences`: successfully completed sequences.
- `mistakes`: failed sequences, including timeouts (one failed attempt = one mistake).
- `incorrect_press_count`: count of individual wrong button presses; kept separate
  from failed-sequence count so the IS mistake-rate denominator remains sequences.
- `accuracy`: recalculated by the backend from correct / attempted sequences.
- `avg_response_time_ms`, `fastest_response_time_ms`, and
  `slowest_response_time_ms`: whole-sequence response-time statistics.
- `sequence_length`: the number of buttons in that level's sequence.
- `incorrect_press_details`: compact entries `[round, position, pressed_button,
  expected_button, sequence_length]` for every incorrect press.

The firmware uses compact JSON keys to keep the offline ESP32 result queue
small. FastAPI expands them to descriptive field names before storing the
result and running the Intelligent System. Average sequence time is divided by
sequence length to derive average time per button press.

The IS does not infer a level classification until at least three sequences
have been attempted at that level. The dashboard displays all level summaries,
the per-level classification sufficiency, and exact incorrect press positions.
No database column migration is required: the existing `game_results.round_data`
JSONB column stores the expanded telemetry.
