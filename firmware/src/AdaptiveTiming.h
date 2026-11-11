// =================================================================
// AdaptiveTiming.h  -  pure, hardware-independent gameplay rules
//
// Kept free of Arduino dependencies so the same code that runs on the ESP32
// is unit-tested on a PC (tests/firmware/test_adaptive_timing.cpp).
//
// RULES THIS FILE ENFORCES
//   1. Sequence length is NEVER adaptive. After every successfully completed
//      level (except the last) level += 1 and sequenceLength += 1.
//   2. The ONLY adaptive parameter is a signed response-time ADJUSTMENT (ms)
//      added to the original level-based time T(L) = MIN(Tmax, T0 + k*(L-1)).
//   3. The adjusted base time is bounded; the grace period is unchanged.
//   4. LED_ON_MS / LED_GAP_MS / LED_BEEP_MS live in the .ino and are not
//      touched by anything here.
// =================================================================
#ifndef LMC_ADAPTIVE_TIMING_H
#define LMC_ADAPTIVE_TIMING_H

// ---- Original level-based response time (unchanged from the working version) ----
const unsigned long BASE_TIME_MS      = 15000;  // T0   initial time limit
const unsigned long TIME_PER_LEVEL_MS = 1000;   // k    extra time per level
const unsigned long MAX_BASE_TIME_MS  = 30000;  // Tmax
const unsigned long GRACE_MS          = 5000;   // Grace (added on top of the base time)

// Scoring window: full points for a quick response, then linear decay to zero.
// This is separate from GRACE_MS, which remains part of the response deadline.
const unsigned long FULL_SCORE_WINDOW_BASE_MS = 5000;  // Level 1: first 5 seconds
const unsigned long FULL_SCORE_WINDOW_PER_LEVEL_MS = 1000; // +1 second per level
const int BASE_POINTS = 100;

// ---- Adaptive limits (must match backend/intelligent_system.py) ----
const long          ADAPT_MIN_ADJUSTMENT_MS = -4000;   // tightest: 4 s less than normal
const long          ADAPT_MAX_ADJUSTMENT_MS = 10000;   // most generous: 10 s more than normal
const unsigned long ADAPT_MIN_BASE_MS       = 10000;   // adjusted base time never below this
const unsigned long ADAPT_MAX_BASE_MS       = 40000;   // adjusted base time never above this

// Original calculation. Do not make this adaptive: it is the baseline/fallback.
inline unsigned long baseTimeForLevel(int lvl) {
  unsigned long t = BASE_TIME_MS + TIME_PER_LEVEL_MS * (unsigned long)(lvl - 1);
  if (t > MAX_BASE_TIME_MS) t = MAX_BASE_TIME_MS;
  return t;
}

inline bool adaptiveAdjustmentIsValid(long adjustmentMs) {
  return adjustmentMs >= ADAPT_MIN_ADJUSTMENT_MS && adjustmentMs <= ADAPT_MAX_ADJUSTMENT_MS;
}

// Baseline + bounded adjustment. adjustment == 0 returns exactly baseTimeForLevel().
inline unsigned long adaptedBaseTimeForLevel(int lvl, long adjustmentMs) {
  if (!adaptiveAdjustmentIsValid(adjustmentMs)) adjustmentMs = 0;   // never apply garbage
  long base = (long)baseTimeForLevel(lvl) + adjustmentMs;
  if (adjustmentMs != 0) {
    if (base < (long)ADAPT_MIN_BASE_MS) base = (long)ADAPT_MIN_BASE_MS;
    if (base > (long)ADAPT_MAX_BASE_MS) base = (long)ADAPT_MAX_BASE_MS;
  }
  return (unsigned long)base;
}

// Total time the player has for one level (base + grace). Used for the countdown,
// the timeout check and the scoring cut-off, so all three always agree.
inline unsigned long responseLimitForLevel(int lvl, long adjustmentMs) {
  return adaptedBaseTimeForLevel(lvl, adjustmentMs) + GRACE_MS;
}

// Full score is awarded inside the level's quick-response window.
// After that, points decay linearly to zero at the SAME deadline used by timeout.
// Adaptive response-time changes affect the deadline, not the full-score window.
inline unsigned long fullScoreWindowForLevel(int lvl) {
  if (lvl < 1) lvl = 1;
  return FULL_SCORE_WINDOW_BASE_MS +
         FULL_SCORE_WINDOW_PER_LEVEL_MS * (unsigned long)(lvl - 1);
}

inline int scoreForResponseTime(int lvl, unsigned long responseMs, long adjustmentMs) {
  const unsigned long deadline = responseLimitForLevel(lvl, adjustmentMs);
  const unsigned long fullScoreWindow = fullScoreWindowForLevel(lvl);

  if (responseMs >= deadline) return 0;
  if (responseMs <= fullScoreWindow) return BASE_POINTS;
  if (deadline <= fullScoreWindow) return 0; // defensive guard

  const unsigned long decayDuration = deadline - fullScoreWindow;
  const unsigned long remaining = deadline - responseMs;
  // Integer rounding to the nearest point.
  const unsigned long points = (remaining * (unsigned long)BASE_POINTS +
                                decayDuration / 2UL) / decayDuration;
  return points > BASE_POINTS ? BASE_POINTS : (int)points;
}

// The original per-level progression, plus (optionally) a validated adaptive
// adjustment for the NEXT level. Returns true if the game advanced.
// NOTE: there is intentionally no parameter that can influence sequenceLength.
struct LevelProgress {
  int  level;
  int  sequenceLength;
  long adjustmentMs;
};

inline bool advanceAfterSuccess(LevelProgress &p, int maxLevel,
                                bool haveDecision, long decisionAdjustmentMs) {
  if (p.level >= maxLevel) return false;      // last level cleared: nothing to advance
  p.level++;                                  // displayed level +1
  p.sequenceLength++;                         // exactly ONE more button (original rule)
  if (haveDecision && adaptiveAdjustmentIsValid(decisionAdjustmentMs)) {
    p.adjustmentMs = decisionAdjustmentMs;    // absolute value => re-applying is a no-op
  }
  return true;
}

#endif
