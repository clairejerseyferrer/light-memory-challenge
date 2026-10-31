# Firmware, Backend, IS, and Dashboard Update

## Main behavior changes

- The physical GPIO15 control is now **Abandon/Reset**, not Start.
- Hold GPIO15 for 2 seconds during countdown or gameplay to abandon the exact active session.
- The website remains the place where the player creates a session and presses **Start Game**.
- If the ESP32 cannot reach the backend when the button is held, it stores the exact session UUID in Preferences and retries that abandonment before accepting another start command.
- Abandoned sessions are not submitted as completed results and cannot be added to the leaderboard.

## Intelligent System telemetry

The ESP32 now tracks cumulative statistics for every level played:

1. Total sequences attempted.
2. Total successful sequences.
3. Failed sequences/timeouts (the IS mistake-rate unit).
4. Individual incorrect button presses, separately counted.
5. Per-level accuracy, recomputed by FastAPI from successful sequences / attempts.
6. Average, fastest, and slowest whole-sequence response times.
7. Sequence length.
8. Every incorrect press position, with round number, actual pressed button, expected button, and sequence length.

The firmware sends compact JSON keys to keep the ESP32 offline queue manageable. `backend/main.py` expands those keys before storing the `round_data` JSONB value and passing it to the Intelligent System. The dashboard displays the per-level summary and individual incorrect press details.

The IS still requires at least 3 attempted sequences in a level before making a meaningful classification. It reports insufficient data instead of inventing a classification.

## Files changed

- `firmware/src/LightMemoryChallenge.ino`
  - Replaced physical start-button configuration with GPIO15 long-press abandon/reset.
  - Added exact-session abandonment request and offline retry persistence.
  - Added complete per-level aggregates and incorrect press position capture.
  - Expanded result payload to include cumulative per-level telemetry.
  - Corrected highest-level tracking so it records the highest level actually completed.

- `backend/main.py`
  - Changed device abandonment to accept an authenticated request containing the exact `device_id` and `session_id`.
  - Added safe/idempotent handling for already-abandoned and already-completed sessions.
  - Prevents result uploads for abandoned sessions and removes a result if abandonment wins a completion race.
  - Expands compact ESP32 telemetry keys and recalculates per-level accuracy from counts.
  - Requires `DEVICE_API_TOKEN` to be configured for device endpoints.

- `backend/intelligent_system.py`
  - Reads the new per-level summaries and individual incorrect press details.
  - Returns a classification/sufficiency result for every level, while retaining the existing latest-level recommendation and adjustment output.
  - Uses incorrect press positions for late-sequence recommendations.

- `web/index.html`
  - Clarified that game start happens on the phone/site and physical GPIO15 is for reset/abandon only.
  - Added a horizontally scrollable per-level performance table with sequence attempts, failed sequences, incorrect presses, accuracy, average/fastest/slowest response times, and incorrect press positions.
  - Added per-level IS classification/sufficiency labels.

- `database/schema.sql`
  - Documented the existing JSONB telemetry contract. No new table or column is needed.

- `tests/test_intelligent_system.py`
  - Added coverage for per-level telemetry and late-sequence incorrect press positions.

- `RUNNING.md`
- `firmware/README_Firmware.txt`
  - Updated setup, endpoint, telemetry, and hardware notes.

## Files added

- `UPDATE_NOTES.md` — this change summary.

## Database migration

No new SQL migration is required for the telemetry because the current `game_results.round_data` column is already JSONB. Keep your existing Supabase data; do not recreate the tables. The schema file only adds a column comment describing the JSONB contents.

## Replace these files

Copy the changed files above into the matching paths in your current repository. Also replace the two documentation files. Keep your own `.env` and `firmware/src/arduino_secrets.h`; do not overwrite them with example credentials.

## Hardware and configuration checks

- Wire the GPIO15 button between GPIO15 and GND (the pin uses `INPUT_PULLUP`).
- GPIO15 is a strapping pin on some ESP32 variants. Verify the board boots reliably and do not hold the button during power-on/reset.
- Make sure `DEVICE_API_TOKEN` in the backend `.env` exactly matches `SECRET_DEVICE_TOKEN` in your local `arduino_secrets.h`.
- Make sure `DEFAULT_DEVICE_ID` matches `SECRET_DEVICE_ID`.
- The website still needs its Start Game button; that is intentional because the phone/web app starts sessions.

## Validation performed

- Intelligent System unit tests: 5 passed.
- Python syntax compilation: passed.
- Dashboard JavaScript syntax check with Node: passed.
- Backend compact-telemetry normalization: checked with a representative payload.
- The Arduino firmware was not compiled against the team's exact ESP32 board package and libraries here. Flash and test it on the actual board before relying on it at the event.
