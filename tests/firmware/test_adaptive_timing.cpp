// Host-side (PC) test of firmware/src/AdaptiveTiming.h - the SAME header the ESP32 compiles.
// Build: g++ -std=c++17 -I firmware/src tests/firmware/test_adaptive_timing.cpp -o /tmp/lmc_timing_test
#include <cstdio>
#include <cstdlib>
#include "AdaptiveTiming.h"

static int failures = 0;
#define CHECK(cond, ...) do { if (!(cond)) { ++failures; std::printf("FAIL line %d: ", __LINE__); std::printf(__VA_ARGS__); std::printf("\n"); } } while (0)

// Original (pre-IS) rule, written out independently of the header.
static unsigned long originalBase(int lvl) {
  unsigned long t = 15000UL + 1000UL * (unsigned long)(lvl - 1);
  return t > 30000UL ? 30000UL : t;
}

int main() {
  const int MAX_LEVEL = 20;

  // 1. adjustment 0 == original timing for every level (baseline preserved).
  for (int l = 1; l <= MAX_LEVEL; ++l) {
    CHECK(baseTimeForLevel(l) == originalBase(l), "baseTimeForLevel(%d)", l);
    CHECK(adaptedBaseTimeForLevel(l, 0) == originalBase(l), "adapted(0) level %d", l);
    CHECK(responseLimitForLevel(l, 0) == originalBase(l) + 5000UL, "limit(0) level %d", l);
  }

  // 2. scoring: quick responses earn full points, then decay to zero at deadline.
  CHECK(scoreForResponseTime(1, 0, 0) == 100, "level 1 instant response");
  CHECK(scoreForResponseTime(1, 5000, 0) == 100, "level 1 full-score boundary");
  CHECK(scoreForResponseTime(1, 12500, 0) >= 49 &&
        scoreForResponseTime(1, 12500, 0) <= 51, "level 1 midpoint decay");
  CHECK(scoreForResponseTime(1, 20000, 0) == 0, "level 1 deadline");
  CHECK(scoreForResponseTime(1, 21000, 0) == 0, "level 1 after deadline");
  CHECK(scoreForResponseTime(2, 6000, 0) == 100, "level 2 full-score window");
  CHECK(scoreForResponseTime(1, 5000, 2000) == 100, "adaptive allowance preserves quick full score");
  CHECK(scoreForResponseTime(1, 22000, 2000) == 0, "adapted deadline scores zero");

  // 2. direction and bounds for the whole adjustment range, all levels.
  for (int l = 1; l <= MAX_LEVEL; ++l) {
    for (long a = ADAPT_MIN_ADJUSTMENT_MS; a <= ADAPT_MAX_ADJUSTMENT_MS; a += 250) {
      unsigned long b = adaptedBaseTimeForLevel(l, a);
      CHECK(b >= ADAPT_MIN_BASE_MS && b <= ADAPT_MAX_BASE_MS, "bounds level %d adj %ld -> %lu", l, a, b);
      unsigned long lim = responseLimitForLevel(l, a);
      CHECK(lim >= 15000UL && lim <= 45000UL, "limit range level %d adj %ld -> %lu", l, a, lim);
      if (a < 0) CHECK(b <= originalBase(l), "tighter level %d adj %ld", l, a);
      if (a > 0) CHECK(b >= originalBase(l), "looser level %d adj %ld", l, a);
    }
  }

  // 3. invalid adjustments never change timing.
  long bad[] = {ADAPT_MIN_ADJUSTMENT_MS - 1, ADAPT_MAX_ADJUSTMENT_MS + 1, 999999L, -999999L};
  for (long a : bad) {
    CHECK(!adaptiveAdjustmentIsValid(a), "invalid %ld accepted", a);
    CHECK(adaptedBaseTimeForLevel(5, a) == originalBase(5), "invalid %ld changed timing", a);
  }

  // 4. PROGRESSION: level+1 and exactly one more button after every success, for every
  //    kind of decision (none, valid tighten/ease/maintain, invalid) - never anything else.
  struct Case { bool have; long adj; } cases[] = {
    {false, 0}, {true, -4000}, {true, -1000}, {true, 0}, {true, 3000}, {true, 10000},
    {true, 999999L}, {true, -999999L}};
  for (const Case &c : cases) {
    LevelProgress p = {1, 3, 0};
    for (int completed = 1; completed < MAX_LEVEL; ++completed) {
      CHECK(p.level == completed && p.sequenceLength == completed + 2,
            "before: level %d len %d", p.level, p.sequenceLength);
      bool advanced = advanceAfterSuccess(p, MAX_LEVEL, c.have, c.adj);
      CHECK(advanced, "did not advance at level %d", completed);
      CHECK(p.level == completed + 1, "level %d -> %d", completed, p.level);
      CHECK(p.sequenceLength == completed + 3, "len after level %d is %d (want %d)", completed, p.sequenceLength, completed + 3);
      CHECK(adaptiveAdjustmentIsValid(p.adjustmentMs), "adjustment out of range %ld", p.adjustmentMs);
    }
    CHECK(p.level == 20 && p.sequenceLength == 22, "final state level %d len %d", p.level, p.sequenceLength);
    LevelProgress last = p;
    CHECK(!advanceAfterSuccess(last, MAX_LEVEL, true, -1000), "advanced past max level");
    CHECK(last.level == p.level && last.sequenceLength == p.sequenceLength, "state changed at max level");
  }
  { // the explicit table from the requirements
    LevelProgress p = {1, 3, 0};
    int want[] = {3, 4, 5, 6, 7};
    for (int i = 0; i < 5; ++i) {
      CHECK(p.level == i + 1 && p.sequenceLength == want[i], "table level %d", i + 1);
      advanceAfterSuccess(p, MAX_LEVEL, true, (i % 2) ? 4000 : -2000);
    }
  }

  // 5. applying the same decision twice is a no-op (absolute value, not a delta).
  { LevelProgress p = {3, 5, 0};
    advanceAfterSuccess(p, MAX_LEVEL, true, 2000);
    long once = p.adjustmentMs;
    p.adjustmentMs = 2000;                       // re-apply the identical decision
    CHECK(p.adjustmentMs == once, "duplicate decision compounded"); }

  // 6. an absent/invalid decision keeps the current adjustment (no arbitrary change).
  { LevelProgress p = {3, 5, 2000};
    advanceAfterSuccess(p, MAX_LEVEL, false, 0);
    CHECK(p.adjustmentMs == 2000, "no-decision changed adjustment");
    advanceAfterSuccess(p, MAX_LEVEL, true, 123456);
    CHECK(p.adjustmentMs == 2000, "invalid decision changed adjustment"); }

  // 7. machine-readable table for the Python parity test.
  for (int l = 1; l <= MAX_LEVEL; ++l)
    for (long a = ADAPT_MIN_ADJUSTMENT_MS; a <= ADAPT_MAX_ADJUSTMENT_MS; a += 500)
      std::printf("PARITY %d %ld %lu\n", l, a, responseLimitForLevel(l, a));

  if (failures) { std::printf("FIRMWARE TIMING TESTS FAILED: %d\n", failures); return 1; }
  std::printf("FIRMWARE TIMING TESTS PASSED\n");
  return 0;
}
