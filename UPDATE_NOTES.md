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


## Follow-up update: mobile IS display, confidence, and idle abandonment

- Replaced the wide per-level HTML table with responsive per-level metric cards and horizontal bars for accuracy and average sequence time. Detailed incorrect press positions are expandable per level.
- Changed the IS minimum from 3 sequences to 1. A single sequence produces a **low-confidence/provisional** assessment; 2 or more produce **moderate confidence**. No data still produces no classification.
- Updated recommendations so applicable observations can be surfaced after one sequence, while retaining multi-observation requirements for repeated-button patterns and using at least two recorded incorrect positions for the late-sequence pattern.
- Expanded authenticated `/api/device/abandon` to accept `waiting` as well as `countdown` and `playing`.
- `/api/device/command` now returns the session UUID the physical button should target: active countdown/game first, otherwise the oldest waiting session.
- Firmware stores that target separately from the active gameplay session and checks the physical button while idle, so it can cancel a queued session even before the game starts.
- No new database columns or migration are required; session status remains `abandoned`.


## TFT display and revised ESP32 pin map

The firmware now targets a typical 1.8-inch ST7735 SPI TFT in landscape orientation. The display driver changed from Adafruit SSD1306 to Adafruit ST7735; game, session, Wi-Fi, backend upload, and telemetry logic were otherwise retained.

| Component | ESP32 GPIO |
|---|---|
| LED 1–7 | 4, 5, 13, 14, 16, 17, 19 |
| Game buttons 1–7 | 34, 35, 25, 26, 27, 32, 33 |
| Physical abandon/reset | 15 |
| Buzzer | 2 |
| TFT SCK | 18 |
| TFT MOSI / SDA | 23 |
| TFT DC / A0 | 21 |
| TFT RST / RES | 22 |
| TFT CS | GND (CS=-1 in the driver) |

GPIO34 and GPIO35 require separate external 10kΩ pull-up resistors to 3.3V; the corresponding buttons connect those GPIOs to GND when pressed. The firmware configures those two pins as `INPUT`, not `INPUT_PULLUP`.

The TFT uses the ESP32 hardware SPI bus with SCK=18, MOSI=23, and MISO disabled. This is intentional: GPIO19 is therefore available for LED 7. This pin map assumes a classic ESP32 DevKit / ESP32-WROOM board and a 1.8-inch ST7735 128x160 module. Verify the exact TFT controller, tab initialization, voltage, and backlight requirements before wiring. The firmware has not been compiled or tested on the team's physical hardware.


## Dashboard and TFT presentation update

- Reworked `web/index.html` into a responsive dashboard with a desktop sidebar and mobile bottom navigation, keeping the existing API endpoints and session/scoring flow intact.
- Added a dashboard overview based on leaderboard records, quick actions, a compact game guide, backend status refresh, and a leaderboard refresh control.
- Preserved the existing session create/start/abandon, queue polling, session-history lookup, leaderboard, result, and per-level Intelligent System analysis behavior.
- Improved the ST7735 presentation: large colored pre-game countdown, large live turn timer (green in normal time and red during grace time), larger total score and round feedback, and a more readable final score screen.
- No LED playback timing or scoring formulas were changed in this update. The supplied firmware snapshot still defines `LED_ON_MS` as 550 ms; compare it with the latest GitHub version before merging if the intended current value is 500 ms.
- Hardware verification is still required on the actual ST7735 module. If colors are shifted or incorrect, the module's initialization tab (`INITR_BLACKTAB`, `INITR_GREENTAB`, or `INITR_REDTAB`) may need to match the specific screen.

## Follow-up update: TFT start countdown and adaptive gameplay assistance

- The TFT now displays the initial 5 immediately, then follows the backend-scheduled countdown so it stays aligned with the dashboard even if the ESP32 receives the START command partway through the countdown. If NTP time is unavailable, the TFT runs a local five-second countdown instead. The backend's scheduled start confirmation remains in place.
- Added a same-level mistake counter. Two failed sequences on the current level reduce the active game difficulty by exactly one level and shorten the sequence accordingly. Level 1 remains the minimum. A correct sequence resets the counter as the player advances.
- Added a one-attempt LED viewing-time assist after a lost life: each LED remains lit for an additional 120 ms on the next sequence playback. The normal 550 ms duration resumes after that playback unless another life is lost.
- No dashboard, backend API, database schema, scoring, or result telemetry changes are required for these gameplay rules; they run on the ESP32 firmware.

### Files changed in this follow-up

- `firmware/src/LightMemoryChallenge.ino`
  - Complete five-number TFT countdown on START.
  - Two-mistakes-on-the-same-level difficulty reduction, with a level-1 floor.
  - One-attempt +120 ms LED viewing-time assistance after losing a life.
- `UPDATE_NOTES.md`
  - Documented the new behavior and replacement file.

No database migration is required. Flash the updated firmware to the ESP32; the dashboard and backend files do not need to be replaced for these three changes.


## TFT countdown reliability fix
- The TFT now displays a complete one-second-per-number countdown.
- If the ESP32 receives the START command after the scheduled server deadline,
  or its NTP clock makes the remaining time implausible, it falls back to a
  complete local countdown instead of flashing `5` and immediately showing `GO!`.
- Physical abandon checks remain active throughout the countdown.
