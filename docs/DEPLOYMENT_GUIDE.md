# Light Memory Challenge — Deployment Guide

## Current Deployment

The project team reports that the application is deployed on Render from the GitHub repository using the Free plan and that the deployed application is working.

- Render service URL: **https://light-memory-challenge.onrender.com**
- GitHub repository: **https://github.com/clairejerseyferrer/light-memory-challenge**
- Supabase project: configured separately; do not publish private credentials.

## Backend Configuration

Configure the following environment variables in the Render service's Environment settings:

| Variable | Value |
|---|---|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Private service-role key |
| `DEVICE_API_TOKEN` | Strong random token shared with the ESP32 configuration |
| `DEFAULT_DEVICE_ID` | `ESP32-001` unless the team intentionally changed it |
| `LOG_LEVEL` | `INFO` (optional) |

Optional reliability variables are documented in `.env.example` and `RUNNING.md`.

Do not include actual secret values in this file, screenshots, README, or GitHub commits.

## Database Preparation

1. Open the Supabase SQL Editor.
2. Review `database/schema.sql`.
3. Apply the schema to the intended project.
4. Apply `database/migrations/migration_readable_session_ids.sql` if the migration has not already been applied.
5. Confirm the `game_sessions` and `game_results` tables and required columns exist, including the readable `session_number` column and unique sequence-backed values.

Do not blindly reinitialize a database containing existing project data.

## ESP32 Hosted API URL

In the local, untracked `firmware/src/arduino_secrets.h`, set `SECRET_API_BASE_URL` to the actual deployed backend URL, without a trailing slash. Use the same `SECRET_DEVICE_ID` and `SECRET_DEVICE_TOKEN` values configured on the backend.

## Verify the Deployment

1. Open the Render service URL in a browser.
2. Open `<RENDER_URL>/api/health`.
3. Confirm the backend responds and inspect the database status in the JSON response.
4. Create a test session through the dashboard.
5. Confirm the ESP32 receives the start command.
6. Complete a test game and confirm the result appears in the dashboard and database.
7. Capture screenshots with all credentials and private information hidden.

## Free Plan Considerations

The application is deployed using Render's Free plan. Free-plan services may have resource limits, inactivity behavior, or cold starts depending on the provider's current policy. A delay after inactivity should not automatically be treated as a firmware or database defect. Check the live service behavior and provider documentation when diagnosing availability.

## Security Notes

- Keep the Supabase service-role key on the backend only.
- Keep the device token and Wi-Fi credentials private.
- Rotate credentials if they are exposed.
- The current firmware uses `secureClient.setInsecure()`, which skips TLS certificate validation. Document this as a limitation; use certificate validation before production deployment.
