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
        position = [r for r in result["personalized_recommendations"] if r["id"] == "position_pattern"]
        self.assertEqual(len(position), 1)
        self.assertIn("Try dividing sequences into smaller groups of 2–3 buttons", position[0]["action"])

    def test_level_floor_and_game_cap(self):
        low = level(1, 40, 2.9, .9)
        result_low = analyze_performance(40, low["mistakes"], low["avg_response_time_ms"], [low])
        self.assertEqual(result_low["recommended_level"], 1)

        high = level(20, 96, .8, .05)
        result_high = analyze_performance(96, high["mistakes"], high["avg_response_time_ms"], [high])
        self.assertEqual(result_high["recommended_level"], 20)



def detail(level_no, pos, pressed, expected, length, rnd=1):
    return [rnd, pos, pressed, expected, length]


def row(level_no, attempts, correct, details=(), seconds_per_press=1.2, length=None, **kw):
    length = length or level_no + 2
    mistakes = attempts - correct
    data = {
        "level": level_no, "sequences_attempted": attempts, "correct_sequences": correct,
        "accuracy": 100.0 * correct / attempts, "mistakes": mistakes,
        "incorrect_press_count": len(details), "avg_response_time_ms": seconds_per_press * length * 1000,
        "sequence_length": length, "incorrect_press_details": list(details),
    }
    data.update(kw)
    return data


def analyze(rows, history=None, accuracy=None, avg_press_ms=1200):
    attempts = sum(r["sequences_attempted"] for r in rows)
    correct = sum(r["correct_sequences"] for r in rows)
    acc = accuracy if accuracy is not None else 100.0 * correct / attempts
    return analyze_performance(acc, attempts - correct, avg_press_ms, rows, history=history)


def recs(result):
    return {r["id"]: r for r in result["personalized_recommendations"]}


class ButtonRecommendationTests(unittest.TestCase):
    def rows(self):
        d = [detail(3, 5, 4, 2, 5, 1), detail(3, 4, 4, 1, 5, 2), detail(4, 6, 4, 3, 6, 3),
             detail(4, 2, 2, 1, 6, 3), detail(4, 6, 5, 1, 6, 4)]
        return [row(3, 3, 1, d[:2]), row(4, 3, 1, d[2:])]

    def test_names_the_button_with_count_percentage_and_levels(self):
        r = recs(analyze(self.rows()))["repeated_button"]
        self.assertIn("Button 4", r["detected"])
        self.assertIn("60% (3 of 5)", r["evidence"])
        self.assertIn("Levels 3 and 4", r["evidence"])
        self.assertIn("Button 4", r["action"])

    def test_flat_recommendation_text_is_still_provided(self):
        result = analyze(self.rows())
        self.assertTrue(any("Button 4" in text for text in result["recommendations"]))

    def test_insufficient_button_data_gives_no_confident_recommendation(self):
        result = analyze([row(3, 3, 2, [detail(3, 3, 4, 2, 5), detail(3, 4, 4, 1, 5)])])
        self.assertNotIn("repeated_button", recs(result))
        self.assertTrue(any("not enough" in n for n in result["data_notes"]))

    def test_mistakes_spread_across_buttons_do_not_single_one_out(self):
        d = [detail(3, 1, b, 7, 5) for b in (1, 2, 3, 4, 5)]
        result = analyze([row(3, 5, 2, d)])
        self.assertNotIn("repeated_button", recs(result))
        self.assertTrue(any("spread" in n for n in result["data_notes"]))

    def test_original_minimums_are_unchanged(self):
        # 2 of 4 = 50% share but only 2 presses of that button: below the 3-press minimum.
        d = [detail(3, 1, 4, 2, 5), detail(3, 2, 4, 2, 5), detail(3, 3, 1, 2, 5), detail(3, 4, 2, 2, 5)]
        self.assertNotIn("repeated_button", recs(analyze([row(3, 4, 1, d)])))


class PositionRecommendationTests(unittest.TestCase):
    def test_end_of_sequence_pattern(self):
        d = [detail(4, p, 2, 1, 6) for p in (5, 6, 5, 6, 4)]
        r = recs(analyze([row(4, 4, 1, d)]))["position_pattern"]
        self.assertIn("toward the end", r["detected"])
        self.assertIn("2–3 buttons", r["action"])

    def test_beginning_pattern(self):
        d = [detail(4, p, 2 + p, 1, 6) for p in (1, 1, 2, 1, 1)]
        r = recs(analyze([row(4, 4, 1, d)]))["position_pattern"]
        self.assertIn("beginning", r["detected"])

    def test_too_few_positions_gives_no_recommendation(self):
        d = [detail(4, p, 2, 1, 6) for p in (5, 6, 6)]
        result = analyze([row(4, 3, 1, d)])
        self.assertNotIn("position_pattern", recs(result))
        self.assertTrue(any("incorrect-press position" in n for n in result["data_notes"]))

    def test_evenly_spread_positions_give_no_pattern(self):
        d = [detail(4, p, 2, 1, 6) for p in (1, 2, 3, 4, 5, 6)]
        self.assertNotIn("position_pattern", recs(analyze([row(4, 4, 1, d)])))


class DifficultyTrendTests(unittest.TestCase):
    def test_accuracy_deterioration_at_higher_levels_is_explained(self):
        rows = [row(1, 3, 3), row(2, 3, 3), row(3, 3, 1), row(4, 3, 0)]
        r = recs(analyze(rows))["higher_level_decline"]
        self.assertIn("0%", r["evidence"]); self.assertIn("83%", r["evidence"])
        self.assertIn("Levels 1–2", r["evidence"]); self.assertIn("Levels 3–4", r["evidence"])
        self.assertIn("shorter sequences", r["action"])

    def test_consistent_lower_levels_is_reported_as_a_strength(self):
        result = analyze([row(1, 3, 3), row(2, 3, 3), row(3, 3, 1), row(4, 3, 0)])
        self.assertIn("consistent_lower_levels", [s["id"] for s in result["strengths"]])

    def test_steady_performance_across_levels_is_a_strength_not_a_weakness(self):
        result = analyze([row(1, 3, 3), row(2, 3, 3), row(3, 3, 3)])
        self.assertIn("consistent_levels", [s["id"] for s in result["strengths"]])
        self.assertNotIn("higher_level_decline", recs(result))

    def test_not_enough_levels_means_no_trend_claim(self):
        result = analyze([row(1, 3, 3), row(2, 3, 0)])
        self.assertNotIn("higher_level_decline", recs(result))
        self.assertTrue(any("three levels" in n for n in result["data_notes"]))


class ResponseTimeTests(unittest.TestCase):
    def test_slow_pace_is_described_as_input_pace_not_reaction_speed(self):
        r = recs(analyze([row(2, 3, 3, seconds_per_press=3.0)]))["slow_pace"]
        self.assertIn("not pure reaction speed", r["evidence"])
        self.assertNotIn("reaction speed to", r["action"])

    def test_whole_game_per_button_average_is_cited_when_available(self):
        r = recs(analyze([row(2, 3, 3, seconds_per_press=3.0)], avg_press_ms=1800))["slow_pace"]
        self.assertIn("1.8 s", r["evidence"])

    def test_fast_but_inaccurate_is_a_slow_down_message_not_a_memorise_message(self):
        result = analyze([row(3, 5, 1, seconds_per_press=0.8)])
        self.assertIn("rushing", recs(result)); self.assertNotIn("low_accuracy", recs(result))

    def test_close_to_time_limit_is_detected_from_the_level_deadline(self):
        # Level 2: limit = 16 s + 5 s grace = 21 s. 4-button sequence at 4.5 s/press = 18 s (86%).
        r = recs(analyze([row(2, 3, 3, seconds_per_press=4.5, length=4)]))
        self.assertTrue("time_pressure" in r or "slow_pace" in r)

    def test_no_pace_claims_without_timing_data(self):
        data = row(3, 3, 3); data["avg_response_time_ms"] = None
        result = analyze([data])
        self.assertNotIn("slow_pace", recs(result)); self.assertNotIn("time_pressure", recs(result))


class ConfidenceTests(unittest.TestCase):
    def test_single_sequence_insight_is_marked_provisional(self):
        r = recs(analyze([row(3, 1, 0)]))["low_accuracy"]
        self.assertTrue(r["provisional"]); self.assertEqual(r["confidence"], "low")
        self.assertTrue(r["detected"].startswith("Early signal"))

    def test_more_evidence_is_not_provisional(self):
        r = recs(analyze([row(3, 4, 1)]))["low_accuracy"]
        self.assertFalse(r["provisional"]); self.assertEqual(r["confidence"], "moderate")

    def test_overall_assessment_and_suggested_difficulty_flag_low_confidence(self):
        result = analyze([row(3, 1, 1)])
        self.assertTrue(result["suggested_difficulty"]["provisional"])
        self.assertIn("provisional", result["overall_assessment"]["summary"].lower())

    def test_insufficient_per_level_data_is_explicit(self):
        result = analyze_performance(90, 0, 900, [])
        self.assertEqual(result["overall_assessment"]["confidence"], "low")
        self.assertEqual(result["personalized_recommendations"], [])


class HistoryTests(unittest.TestCase):
    def previous(self):
        d = [detail(3, 5, 4, 2, 5, r) for r in range(1, 6)]
        return {"session_id": "prev", "accuracy": 50.0, "mistakes": 6, "highest_level": 3, "total_rounds": 6,
                "round_data": [row(3, 6, 3, d)]}

    def test_improvements_are_reported_against_the_previous_attempt(self):
        d = [detail(3, 5, 4, 2, 5)]
        result = analyze([row(3, 5, 4, d)], history=[self.previous()])
        text = " ".join(i["evidence"] for i in result["strengths"] if i["id"].startswith("improvement"))
        self.assertIn("Accuracy improved from 50% to 80%", text)
        self.assertIn("Button 4 fell from 5 to 1", text)
        self.assertTrue(result["history_comparison"]["available"])

    def test_no_history_means_no_comparison(self):
        result = analyze([row(3, 5, 4)])
        self.assertFalse(result["history_comparison"]["available"])
        self.assertFalse(any(s["id"].startswith("improvement") for s in result["strengths"]))

    def test_thin_previous_game_is_not_compared(self):
        prev = self.previous(); prev["total_rounds"] = 1; prev["round_data"] = [row(1, 1, 1)]
        result = analyze([row(3, 5, 4)], history=[prev])
        self.assertEqual(result["history_comparison"]["improvements"], [])

    def test_regression_is_reported_honestly(self):
        prev = self.previous(); prev["accuracy"] = 95.0
        result = analyze([row(3, 5, 2)], history=[prev])
        self.assertIn("regression_accuracy", [w["id"] for w in result["weaknesses"]])

    def test_comparison_states_that_codenames_are_not_identities(self):
        self.assertIn("not verified identities", analyze([row(3, 5, 4)], history=[self.previous()])["history_comparison"]["basis"])


class PresentationTests(unittest.TestCase):
    def test_all_sections_are_present(self):
        result = analyze([row(3, 5, 4)])
        for key in ("overall_assessment", "strengths", "weaknesses", "personalized_recommendations",
                    "suggested_difficulty", "recommendations", "data_notes", "history_comparison"):
            self.assertIn(key, result)

    def test_each_recommendation_has_detection_evidence_and_action(self):
        for rec in analyze([row(3, 4, 1)])["personalized_recommendations"]:
            for key in ("detected", "evidence", "action"):
                self.assertTrue(rec[key], (rec["id"], key))

    def test_no_repeated_recommendations(self):
        result = analyze([row(1, 3, 3), row(2, 3, 3), row(3, 3, 1, [detail(3, 5, 4, 2, 5)] * 4), row(4, 3, 0)])
        actions = [r["action"] for r in result["personalized_recommendations"]]
        self.assertEqual(len(actions), len(set(actions)))

    def test_positive_reinforcement_when_nothing_is_wrong(self):
        result = analyze([row(3, 5, 5, seconds_per_press=0.8)])
        self.assertIn("ready_for_harder", recs(result))
        self.assertEqual(result["weaknesses"], [])

    def test_suggested_difficulty_matches_unchanged_classification_rules(self):
        result = analyze([row(3, 5, 5, seconds_per_press=0.8)])
        self.assertEqual(result["suggested_difficulty"]["recommended_level"], result["recommended_level"])
        self.assertEqual(result["suggested_difficulty"]["adjustment"], result["proposed_level_adjustment"])
        self.assertIn("next game", result["suggested_difficulty"]["scope"])


if __name__ == "__main__":
    unittest.main()
