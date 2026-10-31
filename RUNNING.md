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
- Abandonment is supported by the web API. The firmware must call the authenticated `POST /api/device/abandon` endpoint for a physical-device abandon button to trigger the same backend state change.
- The website countdown uses a monotonic browser timer and server-provided remaining milliseconds rather than comparing the phone's wall clock directly to a UTC timestamp. Network delay or a backgrounded mobile tab can still affect what the player sees, while the backend's scheduled start remains authoritative.


## Intelligent System telemetry contract

The proposed rules in `Intelligent_System.pdf` make decisions after each
completed level. To classify a level correctly, the ESP32 must submit
`round_data` as per-level summaries, for example:

```json
[
  {
    "level": 3,
    "sequences_attempted": 5,
    "correct_sequences": 4,
    "mistakes": 2,
    "accuracy": 80,
    "avg_response_time_ms": 12000,
    "sequence_length": 4,
    "incorrect_buttons": ["green", "green"],
    "mistake_positions": [3, 4]
  }
]
```

`avg_response_time_ms` here means average time to complete a whole sequence;
the IS divides it by `sequence_length` to calculate seconds per button press.
`mistakes` is the number of mistakes for that level. `incorrect_buttons` and
`mistake_positions` support the optional recommendation rules. The IS needs
at least three attempted sequences in a level before classifying it.

If per-level summaries are not supplied, the IS deliberately reports
`insufficient_per_level_data` and does not guess from session-wide totals.
The firmware currently needs to be checked/updated to send this exact
information. Late-sequence mistake recommendations additionally require
the position of each mistake.
