"""Cross-component checks: firmware <-> backend <-> dashboard."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from backend import intelligent_system as IS
from backend.difficulty_events import validate_events
from backend.telemetry import ROUND_DATA_ALIASES, normalize_difficulty_events_input, normalize_round_data
from tests import firmware_harness as fw

ROOT = Path(__file__).resolve().parent.parent
INO = (ROOT / "firmware/src/LightMemoryChallenge.ino").read_text(encoding="utf-8")

TWO_MISTAKES_MAIN = r"""
int main() {
  resetGame();
  level = 5; sequenceLength = 7; highestLevel = 5; totalRounds = 9; correctRounds = 4;
  uint8_t pr[MAX_SEQUENCE] = {0}, ex[MAX_SEQUENCE] = {0};
  pr[6] = 4; ex[6] = 2;
  totalRounds = 10; addRoundLog(false, false, 1u << 6, pr, ex); updateOnMistake(false);
  totalRounds = 11; addRoundLog(false, true, 0, pr, ex);        updateOnMistake(true);
  static char buf[4096]; buildPayload(buf, sizeof(buf)); puts(buf);
  printf("LEVEL=%d SEQ=%d\n", level, sequenceLength);
}
"""
CLEAN_MAIN = r"""
int main() {
  resetGame(); static char buf[4096]; buildPayload(buf, sizeof(buf)); puts(buf);
}
"""
LEVEL1_MAIN = r"""
int main() {
  resetGame(); uint8_t pr[MAX_SEQUENCE] = {0}, ex[MAX_SEQUENCE] = {0};
  totalRounds = 1; addRoundLog(false, true, 0, pr, ex); updateOnMistake(true);
  totalRounds = 2; addRoundLog(false, true, 0, pr, ex); updateOnMistake(true);
  static char buf[4096]; buildPayload(buf, sizeof(buf)); puts(buf);
  printf("LEVEL=%d\n", level);
}
"""


def firmware_output(main):
    binary = fw.build_harness(main)
    return None if binary is None else fw.run(binary)


class FirmwareConstantsTests(unittest.TestCase):
    def const(self, name):
        return int(re.search(rf"{name}\s*=\s*(\d+)", INO).group(1))

    def test_is_time_limits_match_the_firmware(self):
        self.assertEqual(IS.FW_BASE_TIME_MS, self.const("BASE_TIME_MS"))
        self.assertEqual(IS.FW_TIME_PER_LEVEL_MS, self.const("TIME_PER_LEVEL_MS"))
        self.assertEqual(IS.FW_MAX_BASE_TIME_MS, self.const("MAX_BASE_TIME_MS"))
        self.assertEqual(IS.FW_GRACE_MS, self.const("GRACE_MS"))
        self.assertEqual(IS.MAX_GAME_LEVEL, self.const("MAX_LEVEL"))

    def test_firmware_documents_the_two_mistake_rule_the_backend_validates(self):
        self.assertIn("mistakesAtCurrentLevel >= 2", INO)


@unittest.skipUnless(shutil.which("g++"), "g++ not available")
class FirmwarePayloadTests(unittest.TestCase):
    def payload(self, main):
        text = firmware_output(main)
        return json.loads(text.splitlines()[0]), text

    def test_firmware_payload_is_valid_json_the_backend_can_expand(self):
        data, _ = self.payload(TWO_MISTAKES_MAIN)
        rows = normalize_round_data(data["round_data"])
        self.assertEqual(rows[0]["level"], 5)
        self.assertEqual(rows[0]["mistakes"], 2)
        self.assertTrue(set(data["round_data"][0]) <= set(ROUND_DATA_ALIASES))
        analysis = IS.analyze_performance(data["accuracy"], data["mistakes"], data["avg_response_time_ms"], rows)
        self.assertIn("overall_assessment", analysis)

    def test_two_mistakes_on_a_level_produce_a_matching_event_and_level_drop(self):
        data, text = self.payload(TWO_MISTAKES_MAIN)
        self.assertIn("LEVEL=4 SEQ=6", text)  # firmware really is at Level 4
        events = validate_events(normalize_difficulty_events_input(data["difficulty_events"]),
                                 normalize_round_data(data["round_data"]))
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["from_level"], events[0]["to_level"]), (5, 4))
        self.assertIn("two mistakes at Level 5", events[0]["message"])
        self.assertIn("Level 4", events[0]["message"])

    def test_no_adjustment_means_no_events(self):
        data, _ = self.payload(CLEAN_MAIN)
        self.assertEqual(data["difficulty_events"], [])

    def test_level_1_floor_reports_no_adjustment(self):
        data, text = self.payload(LEVEL1_MAIN)
        self.assertIn("LEVEL=1", text)
        self.assertEqual(data["difficulty_events"], [])

    def test_top_level_fields_match_database_columns(self):
        data, _ = self.payload(CLEAN_MAIN)
        schema = (ROOT / "database/schema.sql").read_text()
        for key in data:
            if key == "difficulty_events":
                continue  # not a column: stored inside analysis JSONB
            self.assertIn(key, schema, key)


class DashboardTests(unittest.TestCase):
    HTML = (ROOT / "web/index.html").read_text(encoding="utf-8")

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_dashboard_script_parses(self):
        script = re.search(r"<script>(.*)</script>", self.HTML, re.S).group(1)
        tmp = Path(__import__("tempfile").mkdtemp()) / "dash.js"
        tmp.write_text(script)
        subprocess.run(["node", "--check", str(tmp)], check=True, capture_output=True)

    def test_dashboard_reads_fields_the_backend_provides(self):
        for field in ("overall_assessment", "personalized_recommendations", "suggested_difficulty",
                      "difficulty_events", "leaderboard", "database"):
            self.assertIn(field, self.HTML)


if __name__ == "__main__":
    unittest.main()
