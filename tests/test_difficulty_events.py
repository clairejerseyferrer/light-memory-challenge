import unittest

from backend.difficulty_events import explain_event, validate_events
from backend.telemetry import normalize_difficulty_events_input

LEVEL5 = [{"level": 5, "mistakes": 2}]


def event(**kw):
    base = {"round": 7, "from_level": 5, "to_level": 4, "mistakes_at_level": 2}
    base.update(kw); return base


class DifficultyEventTests(unittest.TestCase):
    def test_two_mistakes_on_level_5_reduce_to_level_4_with_the_agreed_wording(self):
        (ev,) = validate_events([event()], LEVEL5)
        self.assertEqual(ev["message"],
            "Difficulty adjusted: You made two mistakes at Level 5. The game has reduced the difficulty "
            "to Level 4 to help you regain consistency.")
        self.assertEqual((ev["from_level"], ev["to_level"], ev["trigger"]),
                         (5, 4, "two_mistakes_same_level"))

    def test_no_events_means_no_message(self):
        for empty in (None, [], "x"):
            self.assertEqual(validate_events(empty, LEVEL5), [])

    def test_level_1_is_the_floor_so_no_adjustment_is_reported(self):
        self.assertEqual(validate_events([event(from_level=1, to_level=0)], [{"level": 1, "mistakes": 2}]), [])

    def test_drop_must_be_exactly_one_level(self):
        self.assertEqual(validate_events([event(to_level=3)], LEVEL5), [])
        self.assertEqual(validate_events([event(to_level=5)], LEVEL5), [])

    def test_trigger_must_be_two_mistakes(self):
        self.assertEqual(validate_events([event(mistakes_at_level=1)], LEVEL5), [])

    def test_event_contradicted_by_level_telemetry_is_dropped(self):
        self.assertEqual(validate_events([event()], [{"level": 5, "mistakes": 1}]), [])

    def test_malformed_events_are_ignored(self):
        self.assertEqual(validate_events([None, {}, {"from_level": "x", "to_level": 1}], LEVEL5), [])

    def test_compact_firmware_keys_are_expanded(self):
        (raw,) = normalize_difficulty_events_input([{"r": 7, "f": 5, "t": 4, "m": 2}])
        self.assertEqual(raw, {"round": 7, "from_level": 5, "to_level": 4, "mistakes_at_level": 2})
        self.assertEqual(len(validate_events([raw], LEVEL5)), 1)

    def test_missing_field_in_payload_means_empty(self):
        self.assertEqual(normalize_difficulty_events_input(None), [])


if __name__ == "__main__":
    unittest.main()
