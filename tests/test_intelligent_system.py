import unittest

from backend.intelligent_system import (
    MAX_ADJUSTMENT_MS, MAX_RESPONSE_ALLOWANCE_MS, MIN_ADJUSTMENT_MS, MIN_RESPONSE_ALLOWANCE_MS,
    adaptive_difficulty_decision, analyze_performance, clamp_adjustment, response_allowance_ms,
)


def level(level_no, accuracy, seconds_per_press, mistake_rate, attempts=100):
    mistakes = round(mistake_rate * attempts)
    return {
        "level": level_no,
        "sequences_attempted": attempts,
        "correct_sequences": round(accuracy / 100 * attempts),
        "accuracy": accuracy,
        "mistakes": mistakes,
        "avg_response_time_ms": seconds_per_press * 4 * 1000,
        "sequence_length": 4,
    }


class IntelligentSystemTests(unittest.TestCase):
    def classify(self, accuracy, speed, mistake_rate, attempts=100):
        row = level(3, accuracy, speed, mistake_rate, attempts)
        return analyze_performance(
            accuracy, row["mistakes"], row["avg_response_time_ms"], [row]
        )

    def test_pdf_classification_boundaries(self):
        cases = [
            (96, .8, .05, "Excellent", 2),
            (95, .9, .09, "Excellent", 1),
            (85, 1.3, .15, "Good", 1),
            (80, 1.5, .25, "Good", 1),
            (72, 2.0, .35, "Average", 0),
            (79, 1.4, .20, "Average", 0),
            (93, 3.0, .07, "Good", 0),
            (45, .7, .80, "Struggling", -1),
            (85, 2.8, .40, "Average", 0),
            (59, 1.4, .45, "Struggling", 0),
            (25, 3.2, 1.20, "Struggling", -2),
            (40, 2.9, .90, "Struggling", -1),
            (97, .8, .03, "Excellent", 2),
            (68, 1.8, .45, "Average", 0),
            (82, 1.3, .20, "Good", 1),
        ]
        for accuracy, speed, mistake_rate, expected_class, expected_adjustment in cases:
            with self.subTest(accuracy=accuracy, speed=speed, mistake_rate=mistake_rate):
                result = self.classify(accuracy, speed, mistake_rate)
                self.assertEqual(result["classification"], expected_class)
                self.assertEqual(result["proposed_level_adjustment"], expected_adjustment)

    def test_one_sequence_is_provisional_and_two_improve_confidence(self):
        one = self.classify(100, .8, 0, attempts=1)
        self.assertEqual(one["classification"], "Excellent")
        self.assertEqual(one["confidence"], "low")
        self.assertEqual(one["minimum_sequences_required"], 1)

        two = self.classify(100, .8, 0, attempts=2)
        self.assertEqual(two["classification"], "Excellent")
        self.assertEqual(two["confidence"], "moderate")

    def test_missing_per_level_data_is_not_guessed(self):
        result = analyze_performance(95, 0, 500, [])
        self.assertEqual(result["data_status"], "insufficient_per_level_data")
        self.assertEqual(result["proposed_level_adjustment"], 0)

    def test_per_level_summaries_and_incorrect_press_positions(self):
        row = level(4, 80, 1.2, .20, attempts=5)
        row["incorrect_press_count"] = 5
        row["incorrect_press_details"] = [
            [1, 3, 2, 4, 5],
            [2, 4, 3, 1, 5],
            [3, 5, 1, 2, 5],
            [4, 4, 2, 3, 5],
            [5, 5, 4, 1, 5],
        ]
        result = analyze_performance(
            row["accuracy"], row["mistakes"], row["avg_response_time_ms"], [row]
        )
        self.assertEqual(result["metrics"]["sequences_attempted"], 5)
        self.assertEqual(result["metrics"]["mistakes"], row["mistakes"])
        self.assertEqual(result["per_level_analysis"][0]["level"], 4)
        self.assertEqual(len(result["metrics"]["incorrect_press_details"]), 5)
        self.assertEqual(result["metrics"]["incorrect_press_count"], 5)
        self.assertIn(
            "Try grouping the sequence into small chunks of 2 to 3 buttons.",
            result["recommendations"],
        )

    def test_analysis_reports_per_level_progression_without_fabricating_missing_trends(self):
        first = level(1, 90, 1.5, .10, attempts=5)
        second = level(2, 70, 2.0, .30, attempts=5)
        result = analyze_performance(80, 2, 8000, [first, second])
        trend = result["performance_progression"]
        self.assertEqual(trend["levels_observed"], [1, 2])
        self.assertEqual(trend["accuracy_change_percentage_points"], -20.0)
        self.assertEqual(trend["response_time_trend"], "deteriorated")

        single = analyze_performance(90, 0, 6000, [first])
        self.assertEqual(single["performance_progression"]["response_time_trend"], "insufficient_data")

    def test_level_floor_and_game_cap(self):
        low = level(1, 40, 2.9, .9)
        result_low = analyze_performance(40, low["mistakes"], low["avg_response_time_ms"], [low])
        self.assertEqual(result_low["recommended_level"], 1)

        high = level(20, 96, .8, .05)
        result_high = analyze_performance(96, high["mistakes"], high["avg_response_time_ms"], [high])
        self.assertEqual(result_high["recommended_level"], 20)


class AdaptiveDifficultyTests(unittest.TestCase):
    """Adaptive difficulty = response-time adjustment only; never sequence length."""
    BASELINE = 24000          # original limit for the next level (base 19 s + 5 s grace)

    def make_row(self, accuracy, speed, mistake_rate, attempts=100):
        return {
            "level": 4,
            "sequences_attempted": attempts,
            "correct_sequences": round(accuracy * attempts / 100),
            "accuracy": accuracy,
            "mistakes": round(mistake_rate * attempts),
            "sequence_length": 4,
            "avg_response_time_ms": speed * 4 * 1000,
        }

    def decide(self, accuracy, speed, mistake_rate, attempts=100, adjustment=0, previous_action=None):
        return adaptive_difficulty_decision(
            self.make_row(accuracy, speed, mistake_rate, attempts),
            adjustment, self.BASELINE, self.BASELINE + adjustment, previous_action)

    def test_excellent_tightens_slightly(self):
        r = self.decide(100, .8, 0)
        self.assertEqual(r["classification"], "Excellent")
        self.assertEqual(r["difficulty_action"], "tighten")
        self.assertEqual(r["new_response_time_adjustment_ms"], -1000)
        self.assertEqual(r["new_response_time_allowance_ms"], self.BASELINE - 1000)

    def test_good_follows_configured_policy(self):
        r = self.decide(85, 1.3, .15)                      # Good, PDF adjustment +1 -> small tighten
        self.assertEqual((r["classification"], r["new_response_time_adjustment_ms"]), ("Good", -500))
        r = self.decide(93, 3.0, .07)                      # Good but a Struggling metric -> no tighten
        self.assertEqual((r["classification"], r["new_response_time_adjustment_ms"]), ("Good", 0))

    def test_average_maintains_current_allowance(self):
        r = self.decide(72, 2.0, .35)
        self.assertEqual(r["classification"], "Average")
        self.assertEqual(r["new_response_time_allowance_ms"], self.BASELINE)
        held = self.decide(72, 2.0, .35, adjustment=2000)
        self.assertEqual(held["new_response_time_adjustment_ms"], 2000)

    def test_struggling_increases_allowance(self):
        r = self.decide(45, .7, .80)
        self.assertEqual(r["classification"], "Struggling")
        self.assertEqual(r["difficulty_action"], "ease")
        self.assertGreater(r["new_response_time_allowance_ms"], self.BASELINE)
        severe = self.decide(25, 3.2, 1.2)
        self.assertGreater(severe["recommended_step_ms"], r["recommended_step_ms"])

    def test_single_sequence_never_tightens_but_can_ease(self):
        self.assertEqual(self.decide(100, .8, 0, attempts=1)["new_response_time_adjustment_ms"], 0)
        self.assertTrue(self.decide(100, .8, 0, attempts=1)["provisional"])
        self.assertGreater(self.decide(0, 3.0, 1.0, attempts=1)["new_response_time_adjustment_ms"], 0)

    def test_adjustment_and_allowance_bounds(self):
        tight = self.decide(100, .8, 0, adjustment=MIN_ADJUSTMENT_MS)
        self.assertEqual(tight["new_response_time_adjustment_ms"], MIN_ADJUSTMENT_MS)
        loose = self.decide(25, 3.2, 1.2, adjustment=MAX_ADJUSTMENT_MS)
        self.assertEqual(loose["new_response_time_adjustment_ms"], MAX_ADJUSTMENT_MS)
        for baseline in range(15000, 35001, 1000):         # every original level limit
            for adj in range(MIN_ADJUSTMENT_MS, MAX_ADJUSTMENT_MS + 1, 500):
                v = response_allowance_ms(baseline, adj)
                self.assertTrue(MIN_RESPONSE_ALLOWANCE_MS <= v <= MAX_RESPONSE_ALLOWANCE_MS)
        self.assertEqual(clamp_adjustment("garbage"), 0)

    def test_zero_adjustment_is_exactly_the_original_timing(self):
        # Original firmware: base = MIN(30000, 15000 + 1000*(L-1)); limit = base + 5000.
        for lvl in range(1, 21):
            original = min(30000, 15000 + 1000 * (lvl - 1)) + 5000
            self.assertEqual(response_allowance_ms(original, 0), original)

    def test_no_oscillation_tighten_deferred_after_ease(self):
        r = self.decide(100, .8, 0, adjustment=2000, previous_action="ease")
        self.assertEqual(r["difficulty_action"], "maintain")
        self.assertEqual(r["new_response_time_adjustment_ms"], 2000)
        self.assertEqual(self.decide(100, .8, 0, adjustment=2000, previous_action="tighten")["difficulty_action"], "tighten")

    def test_decision_never_contains_sequence_length_or_led_settings(self):
        for args in ((100, .8, 0), (85, 1.3, .15), (72, 2.0, .35), (25, 3.2, 1.2)):
            r = self.decide(*args)
            self.assertEqual(r["sequence_length_policy"], "fixed_original_progression")
            for key in r:
                self.assertNotIn("new_sequence_length", key)
                self.assertNotIn("led", key.lower())

    def test_each_recommendation_has_a_reason(self):
        row = level(4, 40, 3.0, .60, attempts=5)
        result = analyze_performance(40, row["mistakes"], row["avg_response_time_ms"], [row])
        details = result["recommendation_details"]
        self.assertEqual([d["recommendation"] for d in details], result["recommendations"])
        self.assertTrue(all(d["reason"] for d in details))
        self.assertGreaterEqual(len(details), 2)


if __name__ == "__main__":
    unittest.main()
