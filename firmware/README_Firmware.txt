LIGHT MEMORY CHALLENGE — FIRMWARE FOR CLAIRE'S BACKEND

Files
- LightMemoryChallenge_ClaireBackend.ino: adapted ESP32 firmware
- BACKEND_CHANGES_REQUIRED.md: two small FastAPI changes required

What changed from Zanti's original firmware
1. Removed ESP32-generated IDs. The session UUID comes from FastAPI.
2. Firmware polls GET /api/device/command?device_id=ESP32-001 for a session created and started on the phone.
3. Marks the session started through POST /api/device/started.
4. Uploads game results to POST /api/sessions/{session_id}/results using the field names in Claire's GameResult model.
5. Queues results in ESP32 Preferences when Wi-Fi/backend is unavailable and retries later.
6. Retries Wi-Fi while idle and reconnects before sending queued results.
7. Prevents repeated buttons at levels 1–3 and applies the specification's faster LED display by difficulty tier.
8. Highest level records the highest successfully completed level (does not mistakenly count the next level).
9. Uses the existing point formula; LATE_PENALTY is set to 0.5 so points decline from 100 to 50 during the grace period. Confirm this exact penalty with the team. Set 1.0 to decline to 0 by the end of grace; set 0.0 for no decline.

Setup
1. Open the .ino file in Arduino IDE.
2. Install libraries: Adafruit GFX, Adafruit SSD1306, ArduinoJson.
3. Select your actual ESP32 board and COM port.
4. Set WIFI_SSID and WIFI_PASSWORD.
5. Set API_BASE_URL to the reachable FastAPI URL. For cloud hosting, use the HTTPS URL. For LAN testing, use http://<PC-LAN-IP>:8000, with PC and ESP32 on the same Wi-Fi.
6. Set DEVICE_API_TOKEN to the exact DEVICE_API_TOKEN value in the backend .env file.
7. Make sure DEVICE_ID matches the backend DEFAULT_DEVICE_ID and the device_id used when the web app creates a session. This file uses ESP32-001.
8. Apply BACKEND_CHANGES_REQUIRED.md.
9. Confirm FastAPI is running/deployed and GET /api/health returns status ok.
10. Flash firmware. Create a player session on the phone, press Start, and watch Serial Monitor at 115200 baud.

Failure behavior / limitations
- A Wi-Fi outage during a game does not stop gameplay. Results are stored in ESP32 Preferences and retried after connectivity returns.
- The queue holds 8 unsent results. If it fills, this implementation drops the oldest entry; monitor Serial output and avoid long outages during testing. For a production build, prefer refusing new queue entries or a larger persistent queue rather than silently losing data.
- The browser/phone must be able to reach the same deployed FastAPI backend. The ESP32 must also be able to reach that backend; a phone's cellular connection does not automatically give the ESP32 internet unless it joins a hotspot/network that has internet.
- This version does not yet send live game progress to the phone; it sends the final result. The phone can refresh/poll the session endpoint to show completed results.
- The existing backend's leaderboard currently sorts score before highest level, whereas the game specification says highest level first and score as tiebreaker. Adjust that separately if the spec remains unchanged.
- Test on the exact ESP32 board and wiring. This source has not been compiled against your specific Arduino IDE/board package in this environment.
