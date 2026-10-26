/*
  LIGHT MEMORY CHALLENGE - ESP32 firmware for Claire's FastAPI/Supabase backend

  IMPORTANT:
  - Backend creates the session UUID. This firmware NEVER creates session IDs.
  - A player creates a session and presses START on the phone/web app.
  - Firmware polls GET /api/device/command?device_id=... and starts the game
    when the backend command arrives.
  - Game results are POSTed to /api/sessions/{UUID}/results.
  - Unsent results are kept in ESP32 Preferences and retried when connectivity
    returns. The game itself does not require Wi-Fi after it starts.

  Arduino libraries required:
    - Adafruit GFX Library
    - Adafruit SSD1306
    - ArduinoJson (v6 or v7)
  ESP32 board package provides WiFi, HTTPClient, Preferences, Wire.

  Before use, edit Wi-Fi credentials, API_BASE_URL, DEVICE_API_TOKEN and pins.
  API_BASE_URL must be the publicly reachable base URL if FastAPI is cloud-hosted,
  e.g. https://your-api.example.com (no trailing slash).
*/

#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <ArduinoJson.h>

// ========================== SETTINGS =============================
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* API_BASE_URL = "https://YOUR-FASTAPI-HOST";
const char* DEVICE_API_TOKEN = "SET_THE_SAME_DEVICE_API_TOKEN_AS_BACKEND";
const char* DEVICE_ID = "ESP32-001"; // Must match backend DEFAULT_DEVICE_ID / session device_id

const int BASE_POINTS = 100;
const float LEVEL_BONUS_RATE = 0.0f;
const unsigned long BASE_TIME_MS = 15000;
const unsigned long TIME_PER_LEVEL_MS = 1000;
const unsigned long MAX_BASE_TIME_MS = 30000;
const unsigned long GRACE_MS = 5000;
// 0.5 means points fall linearly from 100 to 50 during the grace period.
// Set 1.0 if they should fall from 100 to 0. The formula itself is preserved.
const float LATE_PENALTY = 0.5f;

const unsigned long WIFI_RETRY_MS = 10000;
const unsigned long COMMAND_POLL_MS = 1500;
const unsigned long HTTP_TIMEOUT_MS = 3000;
const int MAX_SEQUENCE = 30;
const int QUEUE_SIZE = 8;
const size_t QUEUE_JSON_MAX = 1200;

#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

const int LED_PINS[7] = {4, 5, 13, 14, 16, 17, 18};
const int BUTTON_PINS[7] = {19, 23, 25, 26, 27, 32, 33};
const int BUZZER_PIN = 2;

Preferences prefs;
int score = 0, lives = 3, level = 1, sequenceLength = 3;
int mistakes = 0, totalRounds = 0, correctRounds = 0, highestLevel = 1;
unsigned long responseTime = 0, totalResponseMs = 0;
unsigned long fastestResponseMs = 0, slowestResponseMs = 0;
int answeredRounds = 0;
unsigned long gameStartMs = 0;
bool inputTimedOut = false;
int sequence[MAX_SEQUENCE], playerInput[MAX_SEQUENCE];
char sessionId[40] = "";       // backend UUID, not the display ID
char displaySessionId[32] = ""; // e.g. Session #001
char playerCodename[25] = "";
unsigned long lastWifiAttempt = 0, lastCommandPoll = 0;
bool gameRunning = false;

// ========================== DECLARATIONS =========================
void showMessage(String line1, String line2 = "");
void playTone(int frequency, int duration);
void serviceWiFi();
bool connectWiFi(unsigned long timeoutMs);
void pollForStartCommand();
bool requestGameStarted();
void runGame();
void resetGameStats();
unsigned long baseTimeForLevel(int lvl);
int calcPoints(int lvl, unsigned long respMs);
void generateRandomSequence();
void displaySequenceUsingLEDs();
void getPlayerInput();
int waitForButtonPress(unsigned long roundStart, unsigned long limitMs, bool &timedOut);
void checkInput();
void updateOnCorrect();
void updateOnMistake(bool wasTimeout);
void displayResults();
void endGame();
String buildQueueRecord();
bool queuePush(const char* json);
bool queuePeek(char* out, size_t n);
void queuePop();
uint8_t queueCount();
bool postResultRecord(const char* record);
bool flushQueue(int maxRecords);

// ========================== SETUP / MAIN LOOP ====================
void setup() {
  Serial.begin(115200);
  for (int i = 0; i < 7; i++) {
    pinMode(LED_PINS[i], OUTPUT);
    digitalWrite(LED_PINS[i], LOW);
    pinMode(BUTTON_PINS[i], INPUT_PULLUP);
  }
  pinMode(BUZZER_PIN, OUTPUT);
  if (!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) Serial.println("OLED not found");
  showMessage("LIGHT MEMORY", "CHALLENGE");
  randomSeed(esp_random());
  WiFi.mode(WIFI_STA);
  serviceWiFi();
  showMessage("SCAN / OPEN", "PLAYER WEB APP");
  Serial.println("Ready. Create a session and press START on the phone.");
}

void loop() {
  serviceWiFi();
  if (!gameRunning) {
    if (WiFi.status() == WL_CONNECTED && millis() - lastCommandPoll >= COMMAND_POLL_MS) {
      lastCommandPoll = millis();
      pollForStartCommand();
    }
    if (queueCount() > 0 && WiFi.status() == WL_CONNECTED) flushQueue(1);
    delay(5);
    return;
  }
}

// ========================== WIFI / COMMANDS ======================
void serviceWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  if (millis() - lastWifiAttempt < WIFI_RETRY_MS) return;
  lastWifiAttempt = millis();
  Serial.println("Wi-Fi disconnected; reconnecting...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

bool connectWiFi(unsigned long timeoutMs) {
  if (WiFi.status() == WL_CONNECTED) return true;
  lastWifiAttempt = millis();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < timeoutMs) {
    delay(100);
  }
  return WiFi.status() == WL_CONNECTED;
}

void pollForStartCommand() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  String url = String(API_BASE_URL) + "/api/device/command?device_id=" + DEVICE_ID;
  if (!http.begin(url)) return;
  http.addHeader("X-Device-Token", DEVICE_API_TOKEN);
  int code = http.GET();
  if (code == 200) {
    String body = http.getString();
    DynamicJsonDocument doc(1024);
    DeserializationError err = deserializeJson(doc, body);
    const char* command = doc["command"] | "NONE";
    if (!err && String(command) == "START") {
      const char* sid = doc["session_id"] | "";
      const char* did = doc["display_id"] | "Session";
      const char* codename = doc["codename"] | "Player";
      if (strlen(sid) > 0 && strlen(sid) < sizeof(sessionId)) {
        strlcpy(sessionId, sid, sizeof(sessionId));
        strlcpy(displaySessionId, did, sizeof(displaySessionId));
        strlcpy(playerCodename, codename, sizeof(playerCodename));
        int countdownSeconds = doc["countdown_seconds"] | 0;
        http.end();
        showMessage("SESSION READY", String(playerCodename));
        // Backend supplies the remaining countdown, avoiding dependence on
        // the ESP32 having a synchronized system clock.
        unsigned long waitUntil = millis() + (unsigned long)max(0, countdownSeconds) * 1000UL;
        while ((long)(millis() - waitUntil) < 0) {
          serviceWiFi();
          delay(25);
        }
        if (requestGameStarted()) runGame();
        else showMessage("START FAILED", "Retry from phone");
        return;
      }
    }
  }
  http.end();
}

bool requestGameStarted() {
  if (!connectWiFi(5000)) return false;
  HTTPClient http;
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  String url = String(API_BASE_URL) + "/api/device/started";
  if (!http.begin(url)) return false;
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Token", DEVICE_API_TOKEN);
  DynamicJsonDocument doc(256);
  doc["session_id"] = sessionId;
  String body;
  serializeJson(doc, body);
  int code = http.POST(body);
  http.end();
  if (code >= 200 && code < 300) return true;
  Serial.printf("Could not mark session started; HTTP %d\n", code);
  return false;
}

// ========================== GAME FLOW ============================
void runGame() {
  resetGameStats();
  gameRunning = true;
  gameStartMs = millis();
  showMessage("GET READY!", "3  2  1  GO!");
  playTone(800, 150); delay(250);
  playTone(1000, 150); delay(250);
  playTone(1300, 250); delay(250);

  while (lives > 0) {
    generateRandomSequence();
    displaySequenceUsingLEDs();
    getPlayerInput();
    checkInput();
    // Game remains playable even if Wi-Fi drops during a round.
    if (lives > 0) delay(100);
  }
  endGame();
  gameRunning = false;
  sessionId[0] = '\0';
  showMessage("SCAN / OPEN", "PLAYER WEB APP");
}

void resetGameStats() {
  score = 0; lives = 3; level = 1; sequenceLength = 3;
  mistakes = 0; totalRounds = 0; correctRounds = 0; highestLevel = 1;
  responseTime = 0; totalResponseMs = 0; fastestResponseMs = 0;
  slowestResponseMs = 0; answeredRounds = 0; inputTimedOut = false;
}

unsigned long baseTimeForLevel(int lvl) {
  unsigned long t = BASE_TIME_MS + TIME_PER_LEVEL_MS * (unsigned long)(lvl - 1);
  return (t > MAX_BASE_TIME_MS) ? MAX_BASE_TIME_MS : t;
}

int calcPoints(int lvl, unsigned long respMs) {
  unsigned long base = baseTimeForLevel(lvl);
  if (respMs > base + GRACE_MS) return 0;
  float pts = BASE_POINTS * (1.0f + LEVEL_BONUS_RATE * (lvl - 1));
  if (respMs > base && GRACE_MS > 0) {
    float late = (float)(respMs - base) / (float)GRACE_MS;
    pts *= (1.0f - LATE_PENALTY * late);
  }
  if (pts < 0) pts = 0;
  return (int)(pts + 0.5f);
}

void generateRandomSequence() {
  if (sequenceLength > MAX_SEQUENCE) sequenceLength = MAX_SEQUENCE;
  for (int i = 0; i < sequenceLength; i++) {
    int candidate;
    bool duplicate;
    do {
      candidate = random(0, 7);
      duplicate = false;
      // Specification: repeated buttons are not allowed in Easy (levels 1-3).
      if (level <= 3) {
        for (int j = 0; j < i; j++) if (sequence[j] == candidate) duplicate = true;
      }
    } while (duplicate);
    sequence[i] = candidate;
  }
}

void displaySequenceUsingLEDs() {
  showMessage("WATCH", "THE LIGHTS");
  delay(400);
  unsigned long onTime = (level <= 3) ? 700 : (level <= 6) ? 500 : (level <= 10) ? 350 : 275;
  for (int i = 0; i < sequenceLength; i++) {
    int ledIndex = sequence[i];
    digitalWrite(LED_PINS[ledIndex], HIGH);
    playTone(800, 100);
    delay(onTime);
    digitalWrite(LED_PINS[ledIndex], LOW);
    delay(180);
  }
}

void getPlayerInput() {
  showMessage("YOUR TURN!", "Level " + String(level));
  unsigned long limitMs = baseTimeForLevel(level) + GRACE_MS;
  unsigned long startTime = millis();
  bool timedOut = false;
  for (int i = 0; i < sequenceLength; i++) {
    int pressed = waitForButtonPress(startTime, limitMs, timedOut);
    if (timedOut || pressed < 0) break;
    playerInput[i] = pressed;
    digitalWrite(LED_PINS[pressed], HIGH);
    playTone(600, 60);
    delay(80);
    digitalWrite(LED_PINS[pressed], LOW);
  }
  responseTime = millis() - startTime;
  inputTimedOut = timedOut;
  if (!timedOut) {
    totalResponseMs += responseTime;
    answeredRounds++;
    if (fastestResponseMs == 0 || responseTime < fastestResponseMs) fastestResponseMs = responseTime;
    if (responseTime > slowestResponseMs) slowestResponseMs = responseTime;
  }
}

int waitForButtonPress(unsigned long roundStart, unsigned long limitMs, bool &timedOut) {
  while (true) {
    if (millis() - roundStart >= limitMs) { timedOut = true; return -1; }
    for (int i = 0; i < 7; i++) {
      if (digitalRead(BUTTON_PINS[i]) == LOW) {
        delay(25);
        if (digitalRead(BUTTON_PINS[i]) == LOW) {
          while (digitalRead(BUTTON_PINS[i]) == LOW) delay(5);
          return i;
        }
      }
    }
    delay(1);
  }
}

void checkInput() {
  totalRounds++;
  bool inputCorrect = !inputTimedOut;
  if (inputCorrect) {
    for (int i = 0; i < sequenceLength; i++) {
      if (playerInput[i] != sequence[i]) { inputCorrect = false; break; }
    }
  }
  if (inputCorrect) updateOnCorrect();
  else updateOnMistake(inputTimedOut);
  displayResults();
}

void updateOnCorrect() {
  correctRounds++;
  int points = calcPoints(level, responseTime);
  score += points;
  // Highest level means the highest level successfully completed.
  highestLevel = max(highestLevel, level);
  playTone(1200, 150);
  showMessage("CORRECT! +" + String(points), "Score: " + String(score));
  level++;
  sequenceLength++;
  delay(700);
}

void updateOnMistake(bool wasTimeout) {
  lives--;
  mistakes++;
  playTone(300, 350);
  showMessage(wasTimeout ? "TIME'S UP!" : "WRONG SEQUENCE!", "Lives: " + String(lives));
  delay(700);
  if (lives > 0) {
    showMessage("LEVEL RESTART", "New sequence");
    delay(500);
  }
}

void displayResults() {
  showMessage("Score: " + String(score), "Level: " + String(level) + " Lives: " + String(lives));
  delay(500);
}

// ========================== RESULTS / OFFLINE QUEUE ===============
String buildQueueRecord() {
  float accuracy = totalRounds > 0 ? (100.0f * correctRounds / totalRounds) : 0.0f;
  float avgMs = answeredRounds > 0 ? ((float)totalResponseMs / answeredRounds) : 0.0f;
  float durationSec = (millis() - gameStartMs) / 1000.0f;
  DynamicJsonDocument doc(1024);
  doc["session_id"] = sessionId;
  JsonObject result = doc.createNestedObject("result");
  result["score"] = score;
  result["highest_level"] = highestLevel;
  result["mistakes"] = mistakes;
  result["total_rounds"] = totalRounds;
  result["correct_rounds"] = correctRounds;
  result["accuracy"] = roundf(accuracy * 100.0f) / 100.0f;
  result["game_duration_seconds"] = roundf(durationSec * 100.0f) / 100.0f;
  result["lives_remaining"] = lives;
  result["avg_response_time_ms"] = avgMs;
  if (fastestResponseMs > 0) result["fastest_response_time_ms"] = fastestResponseMs;
  if (slowestResponseMs > 0) result["slowest_response_time_ms"] = slowestResponseMs;
  result.createNestedArray("round_data");
  String output;
  serializeJson(doc, output);
  return output;
}

void endGame() {
  showMessage("GAME OVER", "Saving results...");
  playTone(200, 500);
  String record = buildQueueRecord();
  Serial.println("Result queue record:");
  Serial.println(record);
  bool saved = queuePush(record.c_str());
  bool delivered = false;
  if (saved) delivered = flushQueue(2);
  else delivered = postResultRecord(record.c_str());

  float accuracy = totalRounds > 0 ? (100.0f * correctRounds / totalRounds) : 0.0f;
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println("GAME OVER");
  display.println(displaySessionId);
  display.print("Player: "); display.println(playerCodename);
  display.print("Score: "); display.println(score);
  display.print("Level: "); display.println(highestLevel);
  display.print("Accuracy: "); display.print(accuracy, 1); display.println("%");
  display.println(delivered ? "Results uploaded" : "Saved offline");
  display.display();
  delay(5000);
}

bool postResultRecord(const char* record) {
  if (!connectWiFi(5000)) return false;
  DynamicJsonDocument stored(1400);
  if (deserializeJson(stored, record)) return false;
  const char* sid = stored["session_id"] | "";
  if (strlen(sid) < 10) return false;
  JsonObject result = stored["result"].as<JsonObject>();
  if (result.isNull()) return false;

  String url = String(API_BASE_URL) + "/api/sessions/" + sid + "/results";
  String body;
  serializeJson(result, body);
  HTTPClient http;
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(url)) return false;
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  String responseBody = http.getString();
  http.end();
  Serial.printf("POST results -> HTTP %d\n", code);
  if (code >= 200 && code < 300) return true;
  // 409 can mean a prior retry already saved the result. Verify the session
  // is completed before considering the queued item delivered.
  if (code == 409) {
    HTTPClient verify;
    verify.setConnectTimeout(HTTP_TIMEOUT_MS);
    verify.setTimeout(HTTP_TIMEOUT_MS);
    if (verify.begin(String(API_BASE_URL) + "/api/sessions/" + sid)) {
      int vcode = verify.GET();
      String vb = verify.getString();
      verify.end();
      if (vcode == 200) {
        DynamicJsonDocument vdoc(2048);
        if (!deserializeJson(vdoc, vb)) {
          const char* verifiedStatus = vdoc["status"] | "";
          if (String(verifiedStatus) == "completed") return true;
        }
      }
    }
  }
  Serial.println(responseBody);
  return false;
}

uint8_t queueCount() {
  prefs.begin("lmcq", true);
  uint8_t c = prefs.getUChar("count", 0);
  prefs.end();
  return c;
}

bool queuePush(const char* json) {
  prefs.begin("lmcq", false);
  uint8_t head = prefs.getUChar("head", 0);
  uint8_t count = prefs.getUChar("count", 0);
  if (count >= QUEUE_SIZE) {
    char oldKey[8]; snprintf(oldKey, sizeof(oldKey), "p%u", head);
    prefs.remove(oldKey);
    head = (head + 1) % QUEUE_SIZE;
    count--;
    Serial.println("WARNING: queue full; oldest unsent result discarded.");
  }
  uint8_t slot = (head + count) % QUEUE_SIZE;
  char key[8]; snprintf(key, sizeof(key), "p%u", slot);
  size_t wrote = prefs.putString(key, json);
  if (wrote > 0) {
    prefs.putUChar("head", head);
    prefs.putUChar("count", count + 1);
  }
  prefs.end();
  return wrote > 0;
}

bool queuePeek(char* out, size_t n) {
  prefs.begin("lmcq", true);
  uint8_t count = prefs.getUChar("count", 0);
  if (count == 0) { prefs.end(); return false; }
  uint8_t head = prefs.getUChar("head", 0);
  char key[8]; snprintf(key, sizeof(key), "p%u", head);
  String s = prefs.getString(key, "");
  prefs.end();
  if (s.length() == 0 || s.length() >= n) return false;
  strlcpy(out, s.c_str(), n);
  return true;
}

void queuePop() {
  prefs.begin("lmcq", false);
  uint8_t head = prefs.getUChar("head", 0);
  uint8_t count = prefs.getUChar("count", 0);
  if (count > 0) {
    char key[8]; snprintf(key, sizeof(key), "p%u", head);
    prefs.remove(key);
    prefs.putUChar("head", (head + 1) % QUEUE_SIZE);
    prefs.putUChar("count", count - 1);
  }
  prefs.end();
}

bool flushQueue(int maxRecords) {
  char record[QUEUE_JSON_MAX];
  int sentCount = 0;
  while (sentCount < maxRecords && queueCount() > 0) {
    if (!queuePeek(record, sizeof(record))) {
      Serial.println("Queue item unreadable; removing it.");
      queuePop();
      continue;
    }
    if (postResultRecord(record)) {
      queuePop();
      sentCount++;
      Serial.println("Queued result delivered.");
    } else {
      Serial.println("Result still queued; will retry after connectivity returns.");
      return false;
    }
  }
  return queueCount() == 0;
}

// ========================== DISPLAY / BUZZER =====================
void showMessage(String line1, String line2) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 8);
  display.println(line1);
  if (line2.length()) { display.setCursor(0, 32); display.println(line2); }
  display.display();
}

void playTone(int frequency, int duration) {
  if (frequency <= 0) return;
  unsigned long halfPeriod = 500000UL / (unsigned long)frequency;
  unsigned long endAt = millis() + duration;
  while ((long)(millis() - endAt) < 0) {
    digitalWrite(BUZZER_PIN, HIGH); delayMicroseconds(halfPeriod);
    digitalWrite(BUZZER_PIN, LOW); delayMicroseconds(halfPeriod);
  }
}
