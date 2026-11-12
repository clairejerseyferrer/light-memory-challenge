import unittest

from backend.intelligent_system import analyze_performance


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

    def test_level_floor_and_game_cap(self):
        low = level(1, 40, 2.9, .9)
        result_low = analyze_performance(40, low["mistakes"], low["avg_response_time_ms"], [low])
        self.assertEqual(result_low["recommended_level"], 1)

        high = level(20, 96, .8, .05)
        result_high = analyze_performance(96, high["mistakes"], high["avg_response_time_ms"], [high])
        self.assertEqual(result_high["recommended_level"], 20)


if __name__ == "__main__":
    unittest.main()
