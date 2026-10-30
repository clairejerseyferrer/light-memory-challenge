/*
  ================================================================
  LIGHT MEMORY CHALLENGE  -  ESP32 firmware v3 (LMC Player Portal API)
  ================================================================
  How a game works with the web app / backend:
    1. Player opens the web app on a phone, types a codename, taps
       "Create Session", then "START GAME".
    2. The backend schedules the start 5 seconds ahead.
    3. THIS ESP32 polls  GET /api/device/command  once per second.
       When it receives START it shows the countdown, then confirms
       with  POST /api/device/started.
    4. The player plays the game on the board.
    5. At game over the ESP32 saves the result to flash, then sends it
       with  POST /api/sessions/{session_id}/results  and shows
       rank / session ID / codename / score on the OLED.

  Failure handling (nothing is deleted until the server says OK):
    - Result is saved to flash BEFORE sending, so a power cut or no
      Wi-Fi loses nothing (up to 8 unsent games are kept).
    - Network errors, timeouts and 5xx replies -> keep and retry
      (2 tries at game over, then every 30 s while idle, and at boot).
    - Wi-Fi is reconnected automatically while idle.
    - If the server says "409" the ESP32 asks the server for the
      session status and fixes it (late start-confirm or already saved).
    - Replies that can never succeed (404, 422...) are printed to the
      Serial Monitor and dropped so they do not block newer games.

  Game rules (meeting): Levels 1-20, 3 lives. A mistake or timeout costs
  1 life and restarts the SAME level with a new sequence. Scoring and
  time limits are in the SCORING + TIME SETTINGS block below.

  Hardware:
    - 7 LEDs         -> GPIO 4, 5, 13, 14, 16, 17, 18
    - 7 Game Buttons -> GPIO 19, 23, 25, 26, 27, 32, 33
    - Start Button   -> GPIO 15 (wired but not used: the web app starts the game)
    - Buzzer         -> GPIO 2
    - OLED (I2C)     -> SDA 21, SCL 22

  Libraries (Arduino Library Manager): Adafruit GFX, Adafruit SSD1306
  (WiFi, HTTPClient, Preferences come with the ESP32 board package)
  ================================================================
*/

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <time.h>
#include "arduino_secrets.h"

// =================================================================
// SETTINGS YOU MUST EDIT
// =================================================================
const char* WIFI_SSID     = SECRET_SSID;
const char* WIFI_PASSWORD = SECRET_OPTIONAL_PASS;
// Address of the computer running:  uvicorn main:app --host 0.0.0.0 --port 8000
// (no slash at the end; use https://... if the backend is hosted online)
const char* SERVER_BASE   = SECRET_API_BASE_URL;
// Must match DEFAULT_DEVICE_ID and DEVICE_API_TOKEN in the backend .env file.
const char* DEVICE_ID     = SECRET_DEVICE_ID;
const char* DEVICE_TOKEN  = SECRET_DEVICE_TOKEN;

// =================================================================
// SCORING + TIME SETTINGS  (same values as LMC_Point_and_Time_Formula.xlsx)
//   Levels 1-20, 100 points per level.
//   Completion time (whole seconds) of 10 s or more -> 100 points.
//   Every whole second BELOW 10 s -> minus 10 points (9 s = 90 ... 1 s = 10).
//   Time limit  T(L) = MIN(Tmax, T0 + k*(L-1)), starting at 15 s, plus Grace.
//   Not finished by T(L) + Grace -> 0 points and a lost life.
// =================================================================
const int           MAX_LEVEL           = 20;
const int           BASE_POINTS         = 100;
const int           FAST_LIMIT_SECONDS  = 10;     // 10 s or longer = no deduction
const int           FAST_PENALTY        = 10;     // points lost per second under the limit
const unsigned long BASE_TIME_MS        = 15000;  // T0   initial time limit
const unsigned long TIME_PER_LEVEL_MS   = 1000;   // k    extra time per level
const unsigned long MAX_BASE_TIME_MS    = 30000;  // Tmax
const unsigned long GRACE_MS            = 5000;   // Grace

// =================================================================
// LED PLAYBACK SPEED  (tune after prototype testing)
// =================================================================
const unsigned long WATCH_PAUSE_MS = 500;   // pause after "WATCH THE LIGHTS"
const unsigned long LED_ON_MS      = 550;   // how long each LED stays lit
const unsigned long LED_GAP_MS     = 200;   // dark pause between two LEDs
const int           LED_BEEP_MS    = 150;   // beep at the start of each LED

// ---------------- OLED ----------------
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// ---------------- PINS ----------------
const int LED_PINS[7]    = {4, 5, 13, 14, 16, 17, 18};
const int BUTTON_PINS[7] = {19, 23, 25, 26, 27, 32, 33};
const int START_BUTTON   = 15;
const int BUZZER_PIN     = 2;

// ---------------- CURRENT SESSION (from the backend) ----------------
char currentSessionId[40] = "";   // UUID
char currentDisplayId[16] = "";   // "Session #001"
char currentCodename[28]  = "";
long currentStartAtEpoch  = 0;
char ignoredSessionId[40] = "";   // a session the server rejected; do not retry it

// ---------------- GAME VARIABLES ----------------
int score = 0;
int lives = 3;
int level = 1;
int sequenceLength = 3;
int mistakes = 0;
int totalRounds = 0;
int correctRounds = 0;
int highestLevel = 1;
unsigned long responseTime = 0;     // ms the whole round took (used for points)
bool inputTimedOut = false;
bool allLevelsCleared = false;     // true after Level 20 is completed
int lastTimerKey = 0;               // remembers which countdown value is on the OLED
int answeredInRound = 0;            // buttons the player pressed this round
unsigned long gameStartMs = 0;

// per-button reaction times (sent to the Intelligent System)
int pressCount = 0;
unsigned long pressSumMs = 0, pressMinMs = 0, pressMaxMs = 0;

// per-round log (the last 16 rounds are sent as round_data; 16 always fits the buffer)
const int MAX_LOGGED_ROUNDS = 16;
struct RoundLog {
  uint8_t  round, level, seqLen;
  bool     correct, timedOut;
  uint32_t responseMs;
  uint8_t  wrongMask;               // bit i set = button i+1 was expected but pressed wrong
};
RoundLog roundLog[MAX_LOGGED_ROUNDS];
int roundLogCount = 0;

const int MAX_SEQUENCE = 30;
int sequence[MAX_SEQUENCE];
int playerInput[MAX_SEQUENCE];

// ---------------- UPLOAD / QUEUE SETTINGS ----------------
const int  PAYLOAD_MAX = 3072;
const int  ENTRY_MAX = PAYLOAD_MAX + 48;           // "uuid|json"
const int  QUEUE_SIZE = 8;                         // unsent games kept in flash
const unsigned long POLL_INTERVAL_MS  = 1000;      // ask the server for a start command
const unsigned long RETRY_INTERVAL_MS = 30000;     // re-send unsent games while idle
char jsonBuf[PAYLOAD_MAX];
char entryBuf[ENTRY_MAX];
Preferences prefs;
WiFiClient plainClient;
WiFiClientSecure secureClient;

enum Outcome { OUT_DELIVERED, OUT_RETRY, OUT_DISCARD };
enum StartResult { START_OK, START_UNREACHABLE, START_REJECTED };
enum IdleStatus { IDLE_NONE, IDLE_READY, IDLE_NO_WIFI, IDLE_NO_SERVER, IDLE_BAD_TOKEN };
Outcome lastOutcome = OUT_RETRY;
int lastRank = -1;
IdleStatus idleShown = IDLE_NONE;

// ---------------- FUNCTION PROTOTYPES ----------------
unsigned long baseTimeForLevel(int lvl);
int calcPoints(int lvl, unsigned long respMs);
void resetGame();
void playSession();
void runCountdown();
void generateRandomSequence();
void displaySequenceUsingLEDs();
void getPlayerInput();
int waitForButtonPress(unsigned long roundStart, unsigned long limitMs, bool &timedOut, unsigned long &detectedAt);
void checkInput();
void addRoundLog(bool correct, bool timedOut, uint8_t wrongMask);
void updateOnCorrect();
void updateOnMistake(bool wasTimeout);
void endGame();
void showFinalScreen();
void showCountdown(long n);
void showTurn(long secs, bool grace);
void refreshTurnTimer(unsigned long roundStart);
void showIdle(IdleStatus s);
void showMessage(String line1, String line2);
void playTone(int frequency, int duration);
bool connectWiFi(unsigned long timeoutMs);
bool syncTime(unsigned long timeoutMs);
bool timeIsSynced();
int httpRequest(const char* method, const char* path, const char* body, String* resp);
bool isSuccess(int code);
bool pollForStart();
StartResult confirmStarted(const char* sid, int attempts);
Outcome deliverResult(const char* sid, const char* json, int attempts);
Outcome reconcileAfter409(const char* sid, const char* json);
void buildPayload(char* buf, size_t n);
bool queuePush(const char* sid, const char* json);
bool queuePeek(char* out, size_t n);
void queuePop();
uint8_t queueCount();
bool flushQueue(int attempts);
bool jsonGetString(const char* json, const char* key, char* out, size_t n);
bool jsonGetLong(const char* json, const char* key, long &out);

// =================================================================
// INITIALIZE
// =================================================================
void setup() {
  Serial.begin(115200);

  for (int i = 0; i < 7; i++) {
    pinMode(LED_PINS[i], OUTPUT);
    digitalWrite(LED_PINS[i], LOW);
    pinMode(BUTTON_PINS[i], INPUT_PULLUP);
  }
  pinMode(START_BUTTON, INPUT_PULLUP);
  pinMode(BUZZER_PIN, OUTPUT);

  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("OLED not found");
  }
  display.clearDisplay();
  showMessage("LIGHT MEMORY", "CHALLENGE");
  randomSeed(esp_random());
  delay(1500);

  showMessage("CONNECTING", "Wi-Fi...");
  bool online = connectWiFi(8000);
  Serial.println(online ? "Wi-Fi connected" : "Wi-Fi not connected yet (will keep trying)");
  if (online) {
    syncTime(8000);            // needed to line up with the server's start time
    flushQueue(1);             // send games left in flash from before a reboot
  }
  idleShown = IDLE_NONE;
}

// =================================================================
// MAIN LOOP  -  idle: keep Wi-Fi up, re-send unsent games, wait for START
// =================================================================
void loop() {
  static unsigned long lastPoll = 0, lastFlush = 0, lastWifiTry = 0, lastNtp = 0;
  unsigned long now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    showIdle(IDLE_NO_WIFI);
    if (lastWifiTry == 0 || now - lastWifiTry > 10000) {
      connectWiFi(5000);
      lastWifiTry = millis();
    }
    delay(100);
    return;
  }

  if (!timeIsSynced() && now - lastNtp > 30000) {
    syncTime(3000);
    lastNtp = millis();
  }
  if (queueCount() > 0 && now - lastFlush > RETRY_INTERVAL_MS) {
    flushQueue(1);
    lastFlush = millis();
  }
  if (now - lastPoll >= POLL_INTERVAL_MS) {
    lastPoll = now;
    if (pollForStart()) {
      playSession();
      idleShown = IDLE_NONE;     // force the idle screen to redraw
    }
  }
  delay(20);
}

// =================================================================
// ONE FULL GAME SESSION
// =================================================================
void playSession() {
  Serial.printf("START received: %s (%s) codename=%s\n", currentDisplayId, currentSessionId, currentCodename);

  runCountdown();

  // Tell the server the game really started. 'not arrived' replies mean
  // our clock is a little early, so we wait and try again.
  StartResult sr = confirmStarted(currentSessionId, 8);
  if (sr == START_REJECTED) {
    showMessage("SESSION ERROR", "Ask the operator");
    playTone(300, 400);
    strncpy(ignoredSessionId, currentSessionId, sizeof(ignoredSessionId) - 1);
    delay(3000);
    return;
  }
  if (sr == START_UNREACHABLE) {
    // Play anyway. The start is confirmed again when the result is sent.
    Serial.println("Could not confirm start yet - playing anyway");
  }

  resetGame();
  gameStartMs = millis();

  while (lives > 0 && !allLevelsCleared) {
    generateRandomSequence();
    displaySequenceUsingLEDs();
    getPlayerInput();
    checkInput();
  }
  endGame();
}

void resetGame() {
  score = 0;
  lives = 3;
  level = 1;
  sequenceLength = 3;
  mistakes = 0;
  totalRounds = 0;
  correctRounds = 0;
  highestLevel = 1;
  responseTime = 0;
  answeredInRound = 0;
  pressCount = 0;
  pressSumMs = 0;
  pressMinMs = 0;
  pressMaxMs = 0;
  roundLogCount = 0;
  allLevelsCleared = false;
}

// =================================================================
// COUNTDOWN (lines up with the server's start time using NTP time)
// =================================================================
void runCountdown() {
  char name[22];
  strncpy(name, currentCodename, sizeof(name) - 1);
  name[sizeof(name) - 1] = 0;
  showMessage("HELLO!", name);
  delay(1200);

  if (timeIsSynced()) {
    while (true) {
      long rem = currentStartAtEpoch - (long)time(nullptr);
      if (rem <= 0 || rem > 15) break;
      showCountdown(rem);
      playTone(1000, 120);
      delay(850);
    }
  } else {
    for (int s = 5; s > 0; s--) {            // clock not synced: plain 5 s countdown
      showCountdown(s);
      playTone(1000, 120);
      delay(850);
    }
  }
  playTone(1500, 300);
  showMessage("GO!", "");
}

// =================================================================
// FORMULAS (time per level and points)
// =================================================================
unsigned long baseTimeForLevel(int lvl) {
  unsigned long t = BASE_TIME_MS + TIME_PER_LEVEL_MS * (unsigned long)(lvl - 1);
  if (t > MAX_BASE_TIME_MS) t = MAX_BASE_TIME_MS;
  return t;
}

// Revised scoring: 100 points, minus 10 for every whole second under 10 s.
// (completion time is counted in whole seconds: 8.9 s counts as 8 s)
int calcPoints(int lvl, unsigned long respMs) {
  if (respMs > baseTimeForLevel(lvl) + GRACE_MS) return 0;   // out of time

  int secs = (int)(respMs / 1000UL);
  if (secs >= FAST_LIMIT_SECONDS) return BASE_POINTS;
  int pts = BASE_POINTS - FAST_PENALTY * (FAST_LIMIT_SECONDS - secs);
  return pts < 0 ? 0 : pts;
}

// =================================================================
// GAME STEPS
// =================================================================
void generateRandomSequence() {
  if (sequenceLength > MAX_SEQUENCE) sequenceLength = MAX_SEQUENCE;
  for (int i = 0; i < sequenceLength; i++) {
    sequence[i] = random(0, 7);
  }
}

void displaySequenceUsingLEDs() {
  showMessage("WATCH", "THE LIGHTS");
  delay(WATCH_PAUSE_MS);
  for (int i = 0; i < sequenceLength; i++) {
    int ledIndex = sequence[i];
    digitalWrite(LED_PINS[ledIndex], HIGH);
    playTone(800, LED_BEEP_MS);
    if (LED_ON_MS > (unsigned long)LED_BEEP_MS) delay(LED_ON_MS - LED_BEEP_MS);
    digitalWrite(LED_PINS[ledIndex], LOW);
    delay(LED_GAP_MS);
  }
}

void getPlayerInput() {
  lastTimerKey = 0;                    // forces the countdown screen to draw

  unsigned long limitMs = baseTimeForLevel(level) + GRACE_MS;
  unsigned long startTime = millis();
  unsigned long waitStart = startTime;
  bool timedOut = false;
  answeredInRound = 0;

  for (int i = 0; i < sequenceLength; i++) {
    unsigned long detectedAt = 0;
    int pressed = waitForButtonPress(startTime, limitMs, timedOut, detectedAt);
    if (timedOut) break;

    unsigned long reaction = detectedAt - waitStart;     // time for this one button
    pressCount++;
    pressSumMs += reaction;
    if (pressCount == 1 || reaction < pressMinMs) pressMinMs = reaction;
    if (reaction > pressMaxMs) pressMaxMs = reaction;

    playerInput[i] = pressed;
    answeredInRound = i + 1;
    digitalWrite(LED_PINS[pressed], HIGH);
    playTone(600, 100);
    delay(150);
    digitalWrite(LED_PINS[pressed], LOW);
    waitStart = millis();
  }

  responseTime = millis() - startTime;
  inputTimedOut = timedOut;
}

// Returns button index 0-6, or -1 if time ran out.
int waitForButtonPress(unsigned long roundStart, unsigned long limitMs, bool &timedOut, unsigned long &detectedAt) {
  while (true) {
    if (millis() - roundStart > limitMs) {
      timedOut = true;
      return -1;
    }
    refreshTurnTimer(roundStart);        // OLED countdown (redraws once per second)
    for (int i = 0; i < 7; i++) {
      if (digitalRead(BUTTON_PINS[i]) == LOW) {
        unsigned long t = millis();
        delay(30);
        if (digitalRead(BUTTON_PINS[i]) == LOW) {
          detectedAt = t;
          while (digitalRead(BUTTON_PINS[i]) == LOW) delay(10);
          return i;
        }
      }
    }
  }
}

void checkInput() {
  totalRounds++;
  bool inputCorrect = !inputTimedOut;
  uint8_t wrongMask = 0;

  for (int i = 0; i < answeredInRound; i++) {
    if (playerInput[i] != sequence[i]) {
      inputCorrect = false;
      wrongMask |= (uint8_t)(1 << sequence[i]);   // remember which button was missed
    }
  }

  addRoundLog(inputCorrect, inputTimedOut, wrongMask);   // before level changes

  if (inputCorrect) {
    updateOnCorrect();
  } else {
    updateOnMistake(inputTimedOut);
  }
  showMessage("Score: " + String(score), "Level: " + String(level));
  delay(1000);
}

void addRoundLog(bool correct, bool timedOut, uint8_t wrongMask) {
  if (roundLogCount >= MAX_LOGGED_ROUNDS) {           // keep only the most recent rounds
    memmove(&roundLog[0], &roundLog[1], sizeof(RoundLog) * (MAX_LOGGED_ROUNDS - 1));
    roundLogCount = MAX_LOGGED_ROUNDS - 1;
  }
  RoundLog &r = roundLog[roundLogCount++];
  r.round = (uint8_t)totalRounds;
  r.level = (uint8_t)level;
  r.seqLen = (uint8_t)sequenceLength;
  r.correct = correct;
  r.timedOut = timedOut;
  r.responseMs = (uint32_t)responseTime;
  r.wrongMask = wrongMask;
}

void updateOnCorrect() {
  correctRounds++;
  int points = calcPoints(level, responseTime);
  score += points;

  if (level >= MAX_LEVEL) {
    allLevelsCleared = true;             // Level 20 done: the game ends here
  } else {
    level++;
    sequenceLength++;
  }
  if (level > highestLevel) highestLevel = level;

  playTone(1200, 200);
  showMessage("CORRECT!", "+" + String(points) + "  Score: " + String(score));
  delay(1000);
  if (allLevelsCleared) {
    showMessage("ALL LEVELS", "CLEARED!");
    delay(1500);
  }
}

// Lose a life. Level and sequence length stay the same, so the next round
// restarts the SAME level with a NEW random sequence.
void updateOnMistake(bool wasTimeout) {
  lives--;
  mistakes++;

  playTone(300, 400);
  if (wasTimeout) {
    showMessage("TIME'S UP!", "Lives: " + String(lives));
  } else {
    showMessage("WRONG SEQUENCE!", "Lives: " + String(lives));
  }
  delay(800);
  if (lives > 0) {
    showMessage("LEVEL RESTART", "New Sequence");
    delay(1000);
  }
}

// =================================================================
// END GAME  ->  save to flash  ->  send to backend  ->  show result
// =================================================================
void endGame() {
  showMessage("GAME OVER", "");
  playTone(200, 800);
  delay(1500);

  buildPayload(jsonBuf, sizeof(jsonBuf));
  Serial.println("----- RESULT JSON -----");
  Serial.println(jsonBuf);

  bool saved = queuePush(currentSessionId, jsonBuf);       // 1) flash first
  if (!saved) Serial.println("WARNING: could not save to flash, sending directly");

  showMessage("SAVING SCORE", "Please wait...");
  lastRank = -1;
  lastOutcome = OUT_RETRY;
  if (saved) flushQueue(2);                                // 2) then send
  else lastOutcome = deliverResult(currentSessionId, jsonBuf, 2);

  showFinalScreen();
  delay(6000);
}

void showFinalScreen() {
  bool delivered = (lastOutcome == OUT_DELIVERED && queueCount() == 0);
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println("GAME OVER");
  display.println(currentCodename);
  display.println(currentDisplayId);
  display.print("Score: "); display.println(score);
  display.print("Rank: ");
  if (delivered && lastRank > 0) { display.print("#"); display.println(lastRank); }
  else display.println("--");
  display.print("Level: "); display.println(highestLevel);
  if (delivered) display.println("Saved to server");
  else if (lastOutcome == OUT_DISCARD) display.println("Server rejected");
  else display.println("Saved offline");
  display.display();
}

// =================================================================
// JSON RESULT PAYLOAD  (matches the backend's GameResult model)
// =================================================================
static void appendf(char* buf, size_t n, size_t &len, const char* fmt, ...) {
  if (len >= n - 1) return;
  va_list ap;
  va_start(ap, fmt);
  int w = vsnprintf(buf + len, n - len, fmt, ap);
  va_end(ap);
  if (w > 0) len += (size_t)w;
  if (len > n - 1) len = n - 1;
}

void buildPayload(char* buf, size_t n) {
  float accuracy = totalRounds > 0 ? 100.0f * correctRounds / totalRounds : 0.0f;
  float seconds = (millis() - gameStartMs) / 1000.0f;
  size_t len = 0;
  buf[0] = 0;

  appendf(buf, n, len,
    "{\"score\":%d,\"highest_level\":%d,\"mistakes\":%d,\"total_rounds\":%d,"
    "\"correct_rounds\":%d,\"accuracy\":%.2f,\"game_duration_seconds\":%.2f,"
    "\"lives_remaining\":%d,",
    score, highestLevel, mistakes, totalRounds, correctRounds, accuracy, seconds, lives);

  if (pressCount > 0) {
    appendf(buf, n, len,
      "\"avg_response_time_ms\":%.1f,\"fastest_response_time_ms\":%lu,\"slowest_response_time_ms\":%lu,",
      (float)pressSumMs / pressCount, pressMinMs, pressMaxMs);
  } else {
    appendf(buf, n, len,
      "\"avg_response_time_ms\":null,\"fastest_response_time_ms\":null,\"slowest_response_time_ms\":null,");
  }

  appendf(buf, n, len, "\"round_data\":[");
  for (int r = 0; r < roundLogCount; r++) {
    if (n - len < 220) break;                       // always leave room to close the JSON
    const RoundLog &g = roundLog[r];
    if (r > 0) appendf(buf, n, len, ",");
    appendf(buf, n, len,
      "{\"round\":%u,\"level\":%u,\"sequence_length\":%u,\"correct\":%s,\"timed_out\":%s,"
      "\"response_ms\":%lu,\"incorrect_buttons\":[",
      g.round, g.level, g.seqLen, g.correct ? "true" : "false", g.timedOut ? "true" : "false",
      (unsigned long)g.responseMs);
    bool first = true;
    for (int b = 0; b < 7; b++) {
      if (g.wrongMask & (1 << b)) {
        appendf(buf, n, len, "%s\"%d\"", first ? "" : ",", b + 1);
        first = false;
      }
    }
    appendf(buf, n, len, "]}");
  }
  appendf(buf, n, len, "]}");
}

// =================================================================
// WI-FI, TIME, HTTP
// =================================================================
bool connectWiFi(unsigned long timeoutMs) {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
    delay(250);
  }
  return WiFi.status() == WL_CONNECTED;
}

bool timeIsSynced() {
  return time(nullptr) > 1700000000L;
}

bool syncTime(unsigned long timeoutMs) {
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  unsigned long start = millis();
  while (!timeIsSynced() && millis() - start < timeoutMs) {
    delay(250);
  }
  Serial.println(timeIsSynced() ? "Time synced (NTP)" : "Time NOT synced");
  return timeIsSynced();
}

bool isSuccess(int code) {
  return code >= 200 && code < 300;
}

// Sends one request. Returns the HTTP status code, or a value <= 0 when
// the server could not be reached. The reply body is copied into *resp.
int httpRequest(const char* method, const char* path, const char* body, String* resp) {
  if (WiFi.status() != WL_CONNECTED) return -1;

  String url = String(SERVER_BASE) + path;
  HTTPClient http;
  bool ok;
  if (strncmp(SERVER_BASE, "https", 5) == 0) {
    secureClient.setInsecure();            // school demo: skips certificate check
    ok = http.begin(secureClient, url);
  } else {
    ok = http.begin(plainClient, url);
  }
  if (!ok) return -2;

  http.setConnectTimeout(5000);
  http.setTimeout(5000);
  if (strlen(DEVICE_TOKEN) > 0) http.addHeader("X-Device-Token", DEVICE_TOKEN);

  int code;
  if (strcmp(method, "POST") == 0) {
    http.addHeader("Content-Type", "application/json");
    code = http.POST(String(body ? body : "{}"));
  } else {
    code = http.GET();
  }
  if (resp != NULL && code > 0) *resp = http.getString();
  http.end();
  return code;
}

// =================================================================
// TALKING TO THE BACKEND
// =================================================================

// GET /api/device/command  ->  true when there is a START for this board.
bool pollForStart() {
  char path[96];
  snprintf(path, sizeof(path), "/api/device/command?device_id=%s", DEVICE_ID);
  String body;
  int code = httpRequest("GET", path, NULL, &body);

  if (code == 401 || code == 403) { showIdle(IDLE_BAD_TOKEN); return false; }
  if (code != 200)                { showIdle(IDLE_NO_SERVER); return false; }
  showIdle(IDLE_READY);

  char cmd[12];
  if (!jsonGetString(body.c_str(), "command", cmd, sizeof(cmd))) return false;
  if (strcmp(cmd, "START") != 0) return false;

  char sid[40] = "", did[16] = "", name[28] = "";
  long startAt = 0;
  if (!jsonGetString(body.c_str(), "session_id", sid, sizeof(sid))) return false;
  jsonGetString(body.c_str(), "display_id", did, sizeof(did));
  jsonGetString(body.c_str(), "codename", name, sizeof(name));
  jsonGetLong(body.c_str(), "start_at_epoch", startAt);

  if (strcmp(sid, ignoredSessionId) == 0) return false;   // already rejected once

  strncpy(currentSessionId, sid, sizeof(currentSessionId) - 1);
  strncpy(currentDisplayId, did, sizeof(currentDisplayId) - 1);
  strncpy(currentCodename, name, sizeof(currentCodename) - 1);
  currentStartAtEpoch = startAt;
  return true;
}

// POST /api/device/started
StartResult confirmStarted(const char* sid, int attempts) {
  char body[96];
  snprintf(body, sizeof(body), "{\"session_id\":\"%s\"}", sid);

  for (int a = 1; a <= attempts; a++) {
    String resp;
    int code = httpRequest("POST", "/api/device/started", body, &resp);
    Serial.printf("started attempt %d -> %d\n", a, code);

    if (isSuccess(code)) return START_OK;
    if (code == 409) {
      const char* r = resp.c_str();
      if (strstr(r, "not arrived")) { delay(500); continue; }          // our clock is a bit early
      if (strstr(r, "status: playing") || strstr(r, "already updated")) return START_OK;
      return START_REJECTED;                                           // completed / abandoned ...
    }
    if (code == 404 || code == 422) return START_REJECTED;
    delay(700);                                                        // no connection / 5xx: try again
  }
  return START_UNREACHABLE;
}

// POST /api/sessions/{id}/results  (+ fixes a 409 by asking for the session status)
Outcome deliverResult(const char* sid, const char* json, int attempts) {
  char path[96];
  snprintf(path, sizeof(path), "/api/sessions/%s/results", sid);
  Outcome out = OUT_RETRY;

  for (int a = 1; a <= attempts; a++) {
    if (WiFi.status() != WL_CONNECTED) connectWiFi(6000);

    String resp;
    int code = httpRequest("POST", path, json, &resp);
    Serial.printf("results attempt %d -> %d\n", a, code);

    if (isSuccess(code)) {
      long rank = -1;
      if (!jsonGetLong(resp.c_str(), "ranking", rank)) rank = -1;   // top-level ranking comes first
      lastRank = (int)rank;
      out = OUT_DELIVERED;
      break;
    }
    if (code == 409) { out = reconcileAfter409(sid, json); break; }
    if (code == 404 || code == 422 ||
        (code >= 400 && code < 500 && code != 401 && code != 403 && code != 408 && code != 429)) {
      out = OUT_DISCARD;                  // retrying the same data can never work
      break;
    }
    if (a < attempts) delay(1000UL * a);  // wait 1 s, 2 s ... before the next try
  }
  lastOutcome = out;
  return out;
}

// The server said 409 (session is not in the 'playing' state). Ask what state it is in.
Outcome reconcileAfter409(const char* sid, const char* json) {
  char path[96];
  snprintf(path, sizeof(path), "/api/sessions/%s", sid);
  String body;
  int code = httpRequest("GET", path, NULL, &body);
  if (code == 404) return OUT_DISCARD;
  if (!isSuccess(code)) return OUT_RETRY;

  char status[16] = "";
  jsonGetString(body.c_str(), "status", status, sizeof(status));
  Serial.printf("409 -> server says session status = %s\n", status);

  if (strcmp(status, "completed") == 0) {        // our earlier send worked, only the reply was lost
    lastRank = -1;
    return OUT_DELIVERED;
  }
  if (strcmp(status, "countdown") == 0) {        // the 'started' confirmation never arrived
    if (confirmStarted(sid, 4) != START_OK) return OUT_RETRY;
    char rpath[96];
    snprintf(rpath, sizeof(rpath), "/api/sessions/%s/results", sid);
    String resp;
    int c2 = httpRequest("POST", rpath, json, &resp);
    Serial.printf("results after late start-confirm -> %d\n", c2);
    if (isSuccess(c2)) {
      long rank = -1;
      if (!jsonGetLong(resp.c_str(), "ranking", rank)) rank = -1;
      lastRank = (int)rank;
      return OUT_DELIVERED;
    }
    return OUT_RETRY;
  }
  if (strcmp(status, "playing") == 0) return OUT_RETRY;
  return OUT_DISCARD;                            // waiting / abandoned: cannot accept results
}

// =================================================================
// FLASH QUEUE (unsent games survive power loss)   entry = "<uuid>|<json>"
// =================================================================
uint8_t queueCount() {
  prefs.begin("lmcq", false);
  uint8_t c = prefs.getUChar("count", 0);
  prefs.end();
  return c;
}

bool queuePush(const char* sid, const char* json) {
  if (strlen(sid) + strlen(json) + 2 > (size_t)ENTRY_MAX) return false;
  snprintf(entryBuf, sizeof(entryBuf), "%s|%s", sid, json);

  prefs.begin("lmcq", false);
  uint8_t head  = prefs.getUChar("head", 0);
  uint8_t count = prefs.getUChar("count", 0);

  if (count >= QUEUE_SIZE) {                 // full: drop the oldest entry
    char oldKey[8];
    snprintf(oldKey, sizeof(oldKey), "p%u", head);
    prefs.remove(oldKey);
    head = (head + 1) % QUEUE_SIZE;
    count--;
    Serial.println("Queue full - oldest unsent game dropped");
  }

  uint8_t slot = (head + count) % QUEUE_SIZE;
  char key[8];
  snprintf(key, sizeof(key), "p%u", slot);
  bool ok = prefs.putString(key, entryBuf) > 0;
  if (ok) {
    prefs.putUChar("head", head);
    prefs.putUChar("count", count + 1);
  }
  prefs.end();
  return ok;
}

bool queuePeek(char* out, size_t n) {
  prefs.begin("lmcq", false);
  uint8_t count = prefs.getUChar("count", 0);
  if (count == 0) { prefs.end(); return false; }
  uint8_t head = prefs.getUChar("head", 0);
  char key[8];
  snprintf(key, sizeof(key), "p%u", head);
  String s = prefs.getString(key, "");
  prefs.end();
  if (s.length() == 0 || s.length() >= n) return false;
  strcpy(out, s.c_str());
  return true;
}

void queuePop() {
  prefs.begin("lmcq", false);
  uint8_t head  = prefs.getUChar("head", 0);
  uint8_t count = prefs.getUChar("count", 0);
  if (count > 0) {
    char key[8];
    snprintf(key, sizeof(key), "p%u", head);
    prefs.remove(key);
    prefs.putUChar("head", (head + 1) % QUEUE_SIZE);
    prefs.putUChar("count", count - 1);
  }
  prefs.end();
}

// Sends unsent games oldest-first. Returns true when nothing is left waiting.
bool flushQueue(int attempts) {
  while (queueCount() > 0) {
    if (!queuePeek(entryBuf, sizeof(entryBuf))) { queuePop(); continue; }   // unreadable: skip
    char* sep = strchr(entryBuf, '|');
    if (sep == NULL) { queuePop(); continue; }
    *sep = 0;
    const char* sid = entryBuf;
    const char* json = sep + 1;

    Outcome o = deliverResult(sid, json, attempts);
    if (o == OUT_DELIVERED) {
      Serial.println("Result delivered");
      queuePop();
    } else if (o == OUT_DISCARD) {
      Serial.println("Server will not accept this result, dropping it. Data was:");
      Serial.println(json);
      queuePop();
    } else {
      Serial.println("Server not reachable - result kept, will retry later");
      return false;
    }
  }
  return true;
}

// =================================================================
// TINY JSON READERS (the replies are small and flat)
// =================================================================
static const char* jsonFindValue(const char* json, const char* key) {
  char pat[40];
  snprintf(pat, sizeof(pat), "\"%s\"", key);
  const char* p = strstr(json, pat);
  if (!p) return NULL;
  p += strlen(pat);
  while (*p == ' ') p++;
  if (*p != ':') return NULL;
  p++;
  while (*p == ' ') p++;
  return p;
}

bool jsonGetString(const char* json, const char* key, char* out, size_t n) {
  const char* p = jsonFindValue(json, key);
  if (!p || *p != '"') return false;
  p++;
  size_t i = 0;
  while (*p && *p != '"' && i < n - 1) out[i++] = *p++;
  out[i] = 0;
  return true;
}

bool jsonGetLong(const char* json, const char* key, long &out) {
  const char* p = jsonFindValue(json, key);
  if (!p || (*p != '-' && (*p < '0' || *p > '9'))) return false;   // also rejects null
  out = atol(p);
  return true;
}

// =================================================================
// OLED + BUZZER HELPERS
// =================================================================
// Big pre-game countdown number.
void showCountdown(long n) {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(2);
  display.setCursor(0, 0);
  display.print("GET READY!");
  display.setTextSize(4);
  display.setCursor(52, 28);
  display.print((int)n);
  display.display();
}

// Live countdown while the player enters the sequence.
void showTurn(long secs, bool grace) {
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print("LEVEL ");
  display.print(level);
  display.setTextSize(2);
  display.setCursor(0, 12);
  display.print("YOUR TURN!");
  display.setTextSize(3);
  display.setCursor(0, 38);
  display.print(grace ? "+" : "");
  display.print((int)secs);
  display.print("s");
  display.display();
}

// Seconds left in the base time; after that, seconds left in the grace period.
void refreshTurnTimer(unsigned long roundStart) {
  unsigned long elapsed = millis() - roundStart;
  unsigned long base = baseTimeForLevel(level);
  bool grace = elapsed >= base;
  unsigned long leftMs = grace ? (base + GRACE_MS - elapsed) : (base - elapsed);
  long secs = (long)((leftMs + 999UL) / 1000UL);
  int key = grace ? -(int)secs - 1 : (int)secs + 1;     // never 0, differs between modes
  if (key == lastTimerKey) return;
  lastTimerKey = key;
  showTurn(secs, grace);
}

void showIdle(IdleStatus s) {
  if (s == idleShown) return;                // only redraw when something changed
  idleShown = s;
  if (s == IDLE_READY)          showMessage("WAITING FOR", "PLAYER - tap START on phone");
  else if (s == IDLE_NO_WIFI)   showMessage("NO WI-FI", "Reconnecting...");
  else if (s == IDLE_NO_SERVER) showMessage("NO SERVER", "Retrying...");
  else if (s == IDLE_BAD_TOKEN) showMessage("BAD TOKEN", "Check DEVICE_TOKEN");
}

void showMessage(String line1, String line2) {
  display.clearDisplay();
  display.setTextSize(2);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 10);
  display.println(line1);
  if (line2 != "") {
    display.setTextSize(1);
    display.setCursor(0, 40);
    display.println(line2);
  }
  display.display();
}

// Beep by toggling the pin: works on any ESP32 board-package version and
// with both active and passive buzzers.
void playTone(int frequency, int duration) {
  unsigned long halfPeriod = 500000UL / (unsigned long)frequency;
  unsigned long end = millis() + duration;
  while (millis() < end) {
    digitalWrite(BUZZER_PIN, HIGH);
    delayMicroseconds(halfPeriod);
    digitalWrite(BUZZER_PIN, LOW);
    delayMicroseconds(halfPeriod);
  }
}
