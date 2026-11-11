"""Firmware verification that can run WITHOUT hardware.

  * Compiles and runs tests/firmware/test_adaptive_timing.cpp against the real
    firmware/src/AdaptiveTiming.h (needs g++; skipped otherwise).
  * Static contract checks on LightMemoryChallenge.ino: original gameplay code
    is byte-for-byte unchanged (compared with fixtures taken from the original
    working version) and the adaptive code cannot touch sequence length / LED timing.

NOT covered (needs the physical ESP32 + TFT + buttons): real button timing, TFT
countdown rendering, Wi-Fi/HTTP behaviour. Do not read a pass here as hardware verification.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from backend.intelligent_system import response_allowance_ms

ROOT = Path(__file__).resolve().parents[1]
INO = (ROOT / "firmware/src/LightMemoryChallenge.ino").read_text()
REF = json.loads((ROOT / "tests/firmware/original_reference.json").read_text())


def function_body(name: str, text: str = INO) -> str:
    m = re.search(r"^[A-Za-z_][\w\s\*&:<>,]*\b" + name + r"\s*\([^;{]*\)\s*\{", text, re.M)
    assert m, f"{name} not found"
    i, depth = m.end(), 1
    while depth:
        depth += (text[i] == "{") - (text[i] == "}")
        i += 1
    return text[m.start():i]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


@unittest.skipUnless(shutil.which("g++"), "g++ not available")
class FirmwareTimingHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        exe = "/tmp/lmc_timing_test"
        subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-I", str(ROOT / "firmware/src"),
                        str(ROOT / "tests/firmware/test_adaptive_timing.cpp"), "-o", exe], check=True)
        cls.proc = subprocess.run([exe], capture_output=True, text=True)

    def test_cpp_suite_passes(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout)
        self.assertIn("FIRMWARE TIMING TESTS PASSED", self.proc.stdout)

    def test_firmware_and_backend_compute_identical_limits(self):
        rows = [l.split() for l in self.proc.stdout.splitlines() if l.startswith("PARITY")]
        self.assertEqual(len(rows), 20 * 29)      # 20 levels x 29 adjustment steps
        for _, lvl, adj, limit in rows:
            original = min(30000, 15000 + 1000 * (int(lvl) - 1)) + 5000
            self.assertEqual(response_allowance_ms(original, int(adj)), int(limit), (lvl, adj))


class FirmwareContractTests(unittest.TestCase):
    def test_original_gameplay_functions_are_unchanged(self):
        for name, original in REF["functions"].items():
            with self.subTest(function=name):
                self.assertEqual(norm(function_body(name)), original)

    def test_led_timing_is_fixed(self):
        for key, value in REF["constants"].items():
            self.assertRegex(INO, rf"{key}\s*=\s*{value}\s*;", key)
        for name in ("displaySequenceUsingLEDs",):
            body = function_body(name)
            self.assertNotIn("adaptive", body.lower())
            self.assertIn("LED_ON_MS", body)
            self.assertIn("LED_GAP_MS", body)

    def test_sequence_length_is_only_changed_by_the_original_rule(self):
        writes = [m.group(0) for m in re.finditer(r"(?<![\w.])sequenceLength\s*(=[^=]|\+\+|--|\+=|-=)[^;]*;", INO)]
        allowed = {"sequenceLength = 3;", "sequenceLength = MAX_SEQUENCE;",
                   "sequenceLength = progress.sequenceLength;"}
        self.assertEqual({norm(w) for w in writes} - allowed, set(), writes)
        self.assertNotIn("new_sequence_length", INO)
        self.assertNotIn("adaptiveResponseAllowanceMs", INO)

    def test_progression_goes_through_advance_after_success_with_no_decision_input_for_length(self):
        body = norm(function_body("updateOnCorrect"))
        self.assertIn("advanceAfterSuccess(progress, MAX_LEVEL, ar == ADAPT_OK, decisionAdjustment)", body)
        header = (ROOT / "firmware/src/AdaptiveTiming.h").read_text()
        fn = norm(function_body("advanceAfterSuccess", header))
        self.assertIn("p.level++;", fn)
        self.assertIn("p.sequenceLength++;", fn)
        self.assertEqual(len(re.findall(r"sequenceLength", fn)), 1)      # only that single ++ touches it

    def test_decision_requested_before_and_ack_sent_after_progression(self):
        body = function_body("updateOnCorrect")
        self.assertLess(body.index("requestAdaptiveDecision"), body.index("advanceAfterSuccess"))
        self.assertLess(body.index("advanceAfterSuccess"), body.index("reportAdaptiveApplied"))
        self.assertNotIn("sequenceLength++", body)

    def test_one_timing_source_for_countdown_timeout_and_scoring(self):
        self.assertIn("responseLimitForLevel(level, adaptiveAdjustmentMs)", function_body("getPlayerInput"))
        self.assertIn("scoreForResponseTime(lvl, respMs, adaptiveAdjustmentMs)", function_body("calcPoints"))
        self.assertIn("adaptedBaseTimeForLevel(level, adaptiveAdjustmentMs)", function_body("refreshTurnTimer"))
        self.assertNotRegex(INO, r"baseTimeForLevel\(level\)\s*\+\s*GRACE_MS")

    def test_scoring_uses_quick_full_score_window_then_linear_decay(self):
        calc = norm(function_body("calcPoints"))
        self.assertIn("return scoreForResponseTime(lvl, respMs, adaptiveAdjustmentMs);", calc)
        header = (ROOT / "firmware/src/AdaptiveTiming.h").read_text()
        self.assertIn("FULL_SCORE_WINDOW_BASE_MS = 5000", header)
        self.assertIn("FULL_SCORE_WINDOW_PER_LEVEL_MS = 1000", header)
        self.assertIn("responseMs >= deadline", header)
        self.assertIn("remaining * (unsigned long)BASE_POINTS", header)

    def test_mistake_logic_is_unchanged(self):
        self.assertEqual(norm(function_body("updateOnMistake")), REF["functions"]["updateOnMistake"])
        self.assertNotIn("advanceAfterSuccess", function_body("updateOnMistake"))

    def test_failure_paths_never_reset_progression(self):
        body = function_body("requestAdaptiveDecision")
        self.assertIsNone(re.search(r"(?<![\w.])level\s*(=(?!=)|\+\+|--)", body))
        self.assertIsNone(re.search(r"(?<![\w.])sequenceLength\s*(=(?!=)|\+\+|--)", body))
        self.assertNotIn("sequenceLength", function_body("reportAdaptiveApplied").split("snprintf")[0])

    def test_game_start_resets_adaptation(self):
        self.assertIn("adaptiveAdjustmentMs = 0;", INO)


if __name__ == "__main__":
    unittest.main()
