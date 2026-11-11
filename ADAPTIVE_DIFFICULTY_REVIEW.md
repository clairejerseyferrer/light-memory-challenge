# Light Memory Challenge - Intelligent System Integration Review

## 1. Comparison: original vs IS-integrated version

| Area | Original working | IS-integrated (as received) | Verdict |
|---|---|---|---|
| Sequence progression | `level++; sequenceLength++` | Backend returned `new_sequence_length`; firmware did `sequenceLength = (int)newLength` | **Regression - fixed** |
| Response time | `baseTimeForLevel(level) + GRACE_MS` | Backend returned an absolute allowance (starting at 20 000 ms) that replaced the level-based formula and stopped growing with level | **Wrong - fixed** (now baseline + bounded adjustment) |
| Countdown display | `baseTimeForLevel(level)` | Still original while timeout/scoring used the adaptive value - the TFT countdown disagreed with the real limit | **Bug - fixed** (one source for display, timeout, scoring) |
| LED timing | 550 / 200 / 150 / 500 ms | unchanged | OK, locked by tests |
| Lives, scoring formula, mistake handling | - | unchanged | OK, locked by tests |
| `adaptive-applied` ack | - | Sent *before* level advanced, so "actual" values were not the real next-level values | **Fixed** (sent after progression) |
| Fallback on backend failure | - | Reset to legacy | Replaced: keep current adjustment (0 = original) |
| Level 19-20 | - | Backend/DB capped sequence length at 20, but level 20 = 22 buttons (3 + 19) -> 422 / check violations | **Fixed** (cap 30 = firmware MAX_SEQUENCE) |
| `schema.sql` | - | Stray literal `\n` before the adaptive block (invalid SQL) | **Fixed** |
| Analysis / recommendations / `performance_progression` | basic | Strengths, weaknesses, progression added | Retained; added `recommendation_details` (reason per recommendation) |
| `game_adaptive_events`, endpoints, `requestAdaptiveDecision()`, `adaptive_difficulty_decision()`, idempotency per session/level | - | present | Retained and corrected |

**Root cause:** the IS treated "difficulty" as `sequence_length`. `adaptive_difficulty_decision()` could shrink a struggling player's next sequence (and grow it for good players), and the firmware copied that value straight into `sequenceLength`, bypassing the original `+1 per level` rule. It also replaced the level-based timer with a flat allowance.

## 2. Corrected design

```
limit(level) = clamp( baseTimeForLevel(level) + adjustment ) + GRACE_MS     adjustment in [-4000, +10000] ms
```
* `AdaptiveTiming.h` (new, hardware-free, PC-tested): `baseTimeForLevel()` unchanged, `adaptedBaseTimeForLevel()`, `responseLimitForLevel()`, and `advanceAfterSuccess()` - the original progression; it has **no input that can change sequence length**.
* Policy (`intelligent_system.adaptive_difficulty_decision`, existing PDF classification reused):
  Excellent -> -0.5/-1 s - Good -> -0.5 s only if the PDF rules allow an increase, else 0 - Average -> keep - Struggling -> +2 s (+3 s if accuracy < 30 %).
  Tighten needs >= 2 sequences; a tighten is deferred one level after an ease; easing is never withheld. Hitting a limit is reported, not hidden.
* Flow: level cleared -> ESP32 posts stats (`current adjustment`, current limit, next-level baseline) -> backend stores one immutable decision (`policy_version = response-time-v2`) -> ESP32 does the original level/length advance, then applies the validated adjustment -> ESP32 acks the actual settings -> backend stores recommended vs actual, `applied_matches_recommendation`, and `sequence_progression_verified` (next length == completed length + 1).
* Idempotency: decisions carry the **absolute** adjustment, repeats return the stored decision, and the firmware asks once per level - nothing compounds.
* Choices you may want to change: Average *holds* the current adjustment (instead of snapping back to 0, which would oscillate); on backend failure the board *keeps* its last adjustment.

## 3. Database (additive, nothing dropped/deleted)
`database/migrations/migration_adaptive_response_time.sql` adds nullable columns (`policy_version`, `classification`, recommended/previous/actual adjustment and allowance, `applied_at`, `applied_matches_recommendation`, `sequence_progression_verified`), relaxes the `actual_sequence_length` check from 2..20 to 2..30 (needed because level 20 = 22 buttons), adds range checks and an index. Existing rows keep `policy_version = NULL` and remain readable; the backend never replays them to a board. Re-runnable. `schema.sql` now creates the final shape for fresh installs. Existing `game_sessions`/`game_results` are untouched.

## 4. Test results (software only)
`python3 -m unittest discover -s tests -t .` -> **44 tests, all pass** (original IS tests, 10 adaptive-policy tests, 17 service/integration tests against a fake Supabase client, 11 firmware tests).

* Firmware host test compiles the real `AdaptiveTiming.h`: levels 1..20 give 3,4,5...22 buttons for every decision kind (none / tighten / ease / invalid); no advance past level 20; `adjustment 0` == original timing for all 20 levels; limits bounded for every level x adjustment; invalid values ignored; duplicate decision is a no-op; C++ and Python produce identical limits (580 combinations).
* Contract tests: `updateOnMistake`, `displaySequenceUsingLEDs`, `generateRandomSequence`, `checkInput`, `addRoundLog` and the LED constants are compared with the original ZIP (fixture `tests/firmware/original_reference.json`); `sequenceLength` is only written by the original rule; the decision request precedes and the ack follows progression.
* Negative controls: the firmware tests fail on the as-received IS firmware (5 failures + 1 error) and on a firmware with an injected sequence-length overwrite.

**Not verified (no hardware / toolchain here):** the sketch was not compiled for the ESP32 and nothing ran on the board - real button timing, TFT countdown, Wi-Fi/HTTP behaviour, LED/buzzer output are untested. FastAPI/Supabase were not installed in the sandbox, so routes were tested through the framework-independent service layer with a fake client, not over HTTP or against your live Supabase. Lives/game-over and scoring are verified as *unchanged code*, not by playing.

## 5. Deployment
1. Supabase SQL editor: run `database/migrations/migration_adaptive_response_time.sql`.
2. Deploy `backend/` (new `adaptive_service.py`; `main.py`, `intelligent_system.py` updated). Keep `DEVICE_API_TOKEN` == `SECRET_DEVICE_TOKEN`. Deploy the backend **before** flashing.
3. Flash `firmware/src/` (both `LightMemoryChallenge.ino` and the new `AdaptiveTiming.h`). Play-test levels 1-5 and watch the Serial Monitor line `Next level N: sequence S ...` (S must be N+2).
4. Check `game_adaptive_events`: `sequence_progression_verified` should be `true` on every row; `applied_matches_recommendation` should be `true` unless a limit clamped the value.

## 6. Modified / new files
Modified: `firmware/src/LightMemoryChallenge.ino`, `backend/intelligent_system.py`, `backend/main.py`, `database/schema.sql`, `tests/test_intelligent_system.py`, `UPDATE_NOTES.md`, `firmware/README_Firmware.txt`.
New: `firmware/src/AdaptiveTiming.h`, `backend/adaptive_service.py`, `database/migrations/migration_adaptive_response_time.sql`, `tests/fakes.py`, `tests/test_adaptive_service.py`, `tests/test_firmware.py`, `tests/firmware/test_adaptive_timing.cpp`, `tests/firmware/original_reference.json`, `tests/__init__.py`, `tests/firmware/__init__.py`, this file.
Unchanged: `web/`, the applied migration `migration_adaptive_difficulty.sql`, `RUNNING.md`.
