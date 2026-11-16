import unittest

from backend.difficulty_events import check_event as _check_event
from backend.difficulty_events import explain_event
from backend.difficulty_events import validate_events as _validate_events

TOTAL = 20  # a game long enough to contain the sample events (round 7)


def validate_events(events, round_data=None, total_rounds=TOTAL):
    return _validate_events(events, round_data, total_rounds)


def check_event(raw, level_mistakes, total_rounds=TOTAL):
    return _check_event(raw, level_mistakes, total_rounds)
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


class EventSupportingDataTests(unittest.TestCase):
    """validate_events must not accept an event without supporting round data."""

    def test_valid_event_with_sufficient_supporting_data(self):
        (ev,) = validate_events([event()], [{"level": 4, "mistakes": 0}, {"level": 5, "mistakes": 2}])
        self.assertEqual({k: ev[k] for k in ("round", "from_level", "to_level", "mistakes_at_level")},
                         {"round": 7, "from_level": 5, "to_level": 4, "mistakes_at_level": 2})
        self.assertEqual(ev["trigger"], "two_mistakes_same_level")

    def test_empty_round_data_is_rejected(self):
        self.assertEqual(validate_events([event()], []), [])
        self.assertEqual(check_event(event(), {})[1], "no supporting round data")

    def test_missing_round_data_is_rejected(self):
        self.assertEqual(validate_events([event()], None), [])
        self.assertEqual(validate_events([event()]), [])

    def test_round_data_without_the_event_level_is_rejected(self):
        self.assertEqual(validate_events([event()], [{"level": 3, "mistakes": 5}]), [])

    def test_missing_mistakes_at_level_is_rejected_not_defaulted_to_two(self):
        ev = event(); del ev["mistakes_at_level"]
        self.assertEqual(validate_events([ev], LEVEL5), [])
        self.assertIn("mistakes_at_level", check_event(ev, {5: 2})[1])
        self.assertEqual(validate_events([event(mistakes_at_level=None)], LEVEL5), [])

    def test_other_required_fields_must_be_present(self):
        for field in ("round", "from_level", "to_level"):
            ev = event(); del ev[field]
            self.assertEqual(validate_events([ev], LEVEL5), [], field)

    def test_invalid_round_numbers_are_rejected(self):
        for bad in (0, -3, None, "x", 2.5, True, []):
            self.assertEqual(validate_events([event(round=bad)], LEVEL5), [], repr(bad))

    def test_insufficient_mistakes_at_the_level_are_rejected(self):
        self.assertEqual(validate_events([event()], [{"level": 5, "mistakes": 1}]), [])
        self.assertEqual(validate_events([event()], [{"level": 5}]), [])
        self.assertEqual(validate_events([event()], [{"level": 5, "mistakes": "many"}]), [])

    def test_mistakes_must_be_at_least_two_in_aggregate_data(self):
        self.assertEqual(len(validate_events([event()], [{"level": 5, "mistakes": 3}])), 1)

    def test_invalid_level_transitions_are_rejected(self):
        rows = [{"level": l, "mistakes": 2} for l in range(1, 8)]
        for frm, to in ((5, 5), (4, 5), (5, 3), (5, 6), (6, 4), (1, 0), (2, 0), (21, 20)):
            self.assertEqual(validate_events([event(from_level=frm, to_level=to)], rows), [], (frm, to))

    def test_mistakes_at_level_other_than_two_is_rejected(self):
        for bad in (0, 1, 3, "x"):
            self.assertEqual(validate_events([event(mistakes_at_level=bad)], LEVEL5), [], bad)

    def test_malformed_events_are_rejected_without_raising(self):
        junk = [None, 5, "event", [], {}, {"from_level": "five", "to_level": "four", "round": 1, "mistakes_at_level": 2},
                event(from_level=5.5, to_level=4.5)]
        self.assertEqual(validate_events(junk, LEVEL5), [])

    def test_malformed_round_data_is_rejected_without_raising(self):
        for bad in ("rows", 7, [None, "x", {"level": None}], [{"mistakes": 5}]):
            self.assertEqual(validate_events([event()], bad), [], repr(bad))

    def test_only_the_valid_events_in_a_mixed_list_survive(self):
        out = validate_events([event(round=0), event(), None], LEVEL5)
        self.assertEqual(len(out), 1)

    def test_numeric_strings_from_json_are_still_accepted(self):
        self.assertEqual(len(validate_events([event(round="7", from_level="5", to_level="4")], LEVEL5)), 1)

    def test_rejection_reason_is_specific(self):
        self.assertEqual(check_event(event(round=0), {5: 2})[1], "round must be a positive number")
        self.assertEqual(check_event(event(to_level=5), {5: 2})[1], "from_level must be greater than to_level")
        self.assertEqual(check_event(event(to_level=3), {5: 2})[1], "the drop must be exactly one level")
        self.assertIn("fewer than 2 mistakes", check_event(event(), {5: 1})[1])


class EventRoundWithinGameTests(unittest.TestCase):
    """The event's round must satisfy 1 <= round <= total_rounds (real functions, explicit totals)."""
    ROWS = [{"level": 5, "mistakes": 2}]

    def run_event(self, round_no, total):
        return _validate_events([event(round=round_no)], self.ROWS, total)

    def test_event_on_round_1_is_accepted(self):
        (ev,) = self.run_event(1, 10)
        self.assertEqual(ev["round"], 1)

    def test_event_on_the_final_valid_round_is_accepted(self):
        (ev,) = self.run_event(10, 10)
        self.assertEqual(ev["round"], 10)

    def test_event_beyond_total_rounds_is_rejected(self):
        self.assertEqual(self.run_event(11, 10), [])
        self.assertEqual(self.run_event(999, 10), [])
        self.assertEqual(_check_event(event(round=999), {5: 2}, 10)[1], "round 999 is beyond the game's 10 round(s)")

    def test_total_rounds_of_zero_rejects_every_event(self):
        for round_no in (1, 7, 999):
            self.assertEqual(self.run_event(round_no, 0), [], round_no)
        self.assertEqual(_check_event(event(round=1), {5: 2}, 0)[1], "the game has no completed rounds")

    def test_missing_or_invalid_total_rounds_is_rejected_not_assumed(self):
        for bad in (None, "x", 2.5, True):
            self.assertEqual(self.run_event(7, bad), [], repr(bad))
        self.assertEqual(_validate_events([event()], self.ROWS), [])  # parameter omitted

    def test_round_zero_and_negative_are_still_rejected(self):
        self.assertEqual(self.run_event(0, 10), [])
        self.assertEqual(self.run_event(-1, 10), [])

    def test_only_events_within_the_game_survive_a_mixed_list(self):
        out = _validate_events([event(round=3), event(round=50)], self.ROWS, 10)
        self.assertEqual([e["round"] for e in out], [3])


if __name__ == "__main__":
    unittest.main()
