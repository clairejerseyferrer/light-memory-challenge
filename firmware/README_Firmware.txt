LIGHT MEMORY CHALLENGE — ESP32 FIRMWARE

Files
- src/LightMemoryChallenge.ino: current firmware
- src/arduino_secrets.example.h: safe configuration template
- README_Firmware.txt: setup and behavior notes

Physical controls
- GPIO15 is the ABANDON/RESET button. It is not a start button.
- Hold GPIO15 for 2 seconds during countdown or gameplay to abandon the active session.
- The player starts a queued session from the website/phone only.
- The seven gameplay buttons remain on GPIO 19, 23, 25, 26, 27, 32, and 33.

Backend integration
1. The ESP32 polls GET /api/device/command?device_id=ESP32-001.
2. The website creates and starts sessions; the backend sends the scheduled START command.
3. The ESP32 confirms game start through POST /api/device/started.
4. The ESP32 submits completed results through POST /api/sessions/{session_id}/results.
5. The physical abandon control calls authenticated POST /api/device/abandon with the exact device_id and session_id.
6. If the backend is unreachable during abandonment, the ESP32 stores that exact session ID in Preferences and retries before accepting another start command.
7. An abandoned session is not uploaded as a completed result and is excluded from the leaderboard.

Intelligent System telemetry
The firmware records cumulative per-level telemetry:
- sequences_attempted: all sequence attempts at that level
- correct_sequences: successful sequences
- mistakes: failed sequences/timeouts (one failed sequence = one mistake)
- incorrect_press_count: individual wrong button presses
- accuracy: backend-calculated from correct_sequences / sequences_attempted
- avg_response_time_ms, fastest_response_time_ms, slowest_response_time_ms: whole-sequence times
- sequence_length: number of buttons in the sequence
- incorrect_press_details: compact [round, position, pressed_button, expected_button, sequence_length] entries for every incorrect press

FastAPI expands the firmware's compact JSON keys into descriptive names before analysis and database storage. The existing game_results.round_data JSONB column is sufficient; no database migration is needed for this telemetry.

Setup
1. Open src/LightMemoryChallenge.ino in Arduino IDE.
2. Install Adafruit GFX and Adafruit SSD1306. WiFi, HTTPClient, Preferences, and time are supplied by the ESP32 board package.
3. Copy src/arduino_secrets.example.h to src/arduino_secrets.h.
4. Fill in Wi-Fi name/password, API base URL, DEVICE_ID, and DEVICE_TOKEN. Do not commit arduino_secrets.h.
5. Set DEVICE_API_TOKEN in the backend .env to the same token and DEFAULT_DEVICE_ID to the same device ID.
6. Make sure the backend is deployed/reachable by both the phone and ESP32. The ESP32 must be able to reach the API URL over its own Wi-Fi.
7. Run the backend and verify GET /api/health returns status ok.
8. Flash the firmware and open Serial Monitor at 115200 baud.
9. Create a session on the website and press START GAME there. The physical board begins when it receives the backend command.
10. To cancel from the board, hold GPIO15 for 2 seconds.

Important hardware note
GPIO15 is an ESP32 strapping pin on some board variants. Verify boot behavior with the actual board and wiring. Do not hold the button while powering on or resetting the board. Confirm the switch is wired between GPIO15 and GND because the firmware uses INPUT_PULLUP.

Known limitations / testing
- The firmware source has not been compiled against the exact ESP32 board package or tested with the team's physical wiring in this environment.
- The browser receives completed results, not live per-round progress.
- Wi-Fi loss does not stop gameplay. Completed results are kept in ESP32 Preferences and retried later.
- Abandonment is retried before the board accepts another session, preventing a stale offline abandon from cancelling a later player's session.
- The backend remains authoritative for queue order and session status.
