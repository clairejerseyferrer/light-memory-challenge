import unittest

from backend import adaptive_service as svc
from backend.intelligent_system import POLICY_VERSION
from tests.fakes import FakeSupabase

SID = "11111111-1111-1111-1111-111111111111"


def telemetry(level=1, attempted=4, correct=4, mistakes=0, seq_len=None, avg_ms=4000.0,
              adj=0, allowance=None, baseline=None, **extra):
    seq_len = seq_len if seq_len is not None else level + 2          # original progression
    base_now = 15000 + 1000 * (level - 1) + 5000
    base_next = 15000 + 1000 * level + 5000
    p = {"device_id": "lmc-1", "session_id": SID, "completed_level": level,
         "sequences_attempted": attempted, "correct_sequences": correct, "mistakes": mistakes,
         "sequence_length": seq_len, "avg_response_time_ms": avg_ms, "incorrect_press_count": 0,
         "current_response_time_adjustment_ms": adj,
         "current_response_time_allowance_ms": allowance or min(45000, max(15000, base_now + adj)),
         "next_level_baseline_allowance_ms": baseline or base_next}
    p.update(extra)
    return p


def ack(level, decision, seq_len=None, adj=None, allowance=None, applied=True):
    return {"device_id": "lmc-1", "session_id": SID, "completed_level": level, "applied": applied,
            "actual_sequence_length": seq_len if seq_len is not None else level + 3,
            "actual_response_time_adjustment_ms": decision["new_response_time_adjustment_ms"] if adj is None else adj,
            "actual_response_time_allowance_ms": decision["new_response_time_allowance_ms"] if allowance is None else allowance}


class AdaptiveServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeSupabase()
        self.db.add_session()

    def events(self):
        return self.db.tables.get("game_adaptive_events", [])

    # ---- decisions reaching the device -------------------------------------------------
    def test_decision_has_no_sequence_length_and_is_stored(self):
        d = svc.process_adaptive_decision(self.db, telemetry())
        for forbidden in ("new_sequence_length", "previous_sequence_length", "led_on_ms", "led_gap_ms"):
            self.assertNotIn(forbidden, d)
        self.assertEqual(d["sequence_length_policy"], "fixed_original_progression")
        self.assertEqual(d["difficulty_action"], "tighten")
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]["policy_version"], POLICY_VERSION)
        self.assertIsNone(self.events()[0].get("applied"))   # unconfirmed until the board acknowledges

    def test_decision_never_depends_on_displayed_level_progression(self):
        # Identical performance at level 2 and 9 -> identical adjustment step.
        a = svc.process_adaptive_decision(self.db, telemetry(level=2))
        b = svc.process_adaptive_decision(self.db, telemetry(level=9))
        self.assertEqual(a["recommended_step_ms"], b["recommended_step_ms"])

    # ---- duplicates --------------------------------------------------------------------
    def test_duplicate_request_returns_original_decision_and_does_not_compound(self):
        first = svc.process_adaptive_decision(self.db, telemetry(level=3))
        # The board retries with its *old* state, and then (wrongly) with the already-adjusted state.
        again = svc.process_adaptive_decision(self.db, telemetry(level=3))
        compounded = svc.process_adaptive_decision(
            self.db, telemetry(level=3, adj=first["new_response_time_adjustment_ms"]))
        for r in (again, compounded):
            self.assertTrue(r["idempotent"])
            self.assertEqual(r["new_response_time_adjustment_ms"], first["new_response_time_adjustment_ms"])
        self.assertEqual(len(self.events()), 1)

    def test_concurrent_insert_race_returns_canonical_decision(self):
        real_event = svc._event_for_level
        calls = {"n": 0}
        winner = {}

        def racing(client, sid, level):
            calls["n"] += 1
            if calls["n"] == 1:                      # first look: nothing yet ...
                winner.update(svc.process_adaptive_decision(FakeAlias(client), telemetry(level=4)))
                return None                          # ... but another request wins before our insert
            return real_event(client, sid, level)

        class FakeAlias:                             # run the "other request" against the same db without recursion
            def __init__(self, c): self.c = c
            def table(self, n): return self.c.table(n)

        svc._event_for_level = lambda c, s, l: real_event(c, s, l)
        try:
            svc._event_for_level = racing
            result = svc.process_adaptive_decision(self.db, telemetry(level=4))
        finally:
            svc._event_for_level = real_event
        self.assertTrue(result["idempotent"])
        self.assertEqual(len(self.events()), 1)

    # ---- adaptive policy through the service --------------------------------------------
    def test_struggling_eases_and_average_maintains_via_service(self):
        hard = svc.process_adaptive_decision(self.db, telemetry(level=1, attempted=5, correct=1, mistakes=4, avg_ms=40000))
        self.assertEqual(hard["classification"], "Struggling")
        self.assertGreater(hard["new_response_time_adjustment_ms"], 0)
        self.assertGreater(hard["new_response_time_allowance_ms"], hard["baseline_response_time_allowance_ms"])
        mid = svc.process_adaptive_decision(self.db, telemetry(level=2, attempted=5, correct=3, mistakes=2, avg_ms=2.0 * 4 * 1000,
                                                              adj=hard["new_response_time_adjustment_ms"]))
        self.assertEqual(mid["classification"], "Average")
        self.assertEqual(mid["new_response_time_adjustment_ms"], hard["new_response_time_adjustment_ms"])

    def test_tighten_right_after_ease_is_deferred_one_level(self):
        e = svc.process_adaptive_decision(self.db, telemetry(level=1, attempted=5, correct=1, mistakes=4, avg_ms=40000))
        t = svc.process_adaptive_decision(self.db, telemetry(level=2, adj=e["new_response_time_adjustment_ms"]))
        self.assertEqual(t["difficulty_action"], "maintain")
        t2 = svc.process_adaptive_decision(self.db, telemetry(level=3, adj=t["new_response_time_adjustment_ms"]))
        self.assertEqual(t2["difficulty_action"], "tighten")

    # ---- safety -----------------------------------------------------------------------
    def test_authorization_and_state_checks(self):
        with self.assertRaises(svc.AdaptiveError) as c:
            svc.process_adaptive_decision(self.db, telemetry(device_id="other"))
        self.assertEqual(c.exception.status, 403)
        self.db.tables["game_sessions"][0]["status"] = "finished"
        with self.assertRaises(svc.AdaptiveError) as c:
            svc.process_adaptive_decision(self.db, telemetry())
        self.assertEqual(c.exception.status, 409)
        with self.assertRaises(svc.AdaptiveError) as c:
            svc.process_adaptive_decision(FakeSupabase(), telemetry())
        self.assertEqual(c.exception.status, 404)

    def test_invalid_telemetry_rejected(self):
        for bad in ({"current_response_time_adjustment_ms": 999999}, {"next_level_baseline_allowance_ms": 10},
                    {"completed_level": 20}, {"sequences_attempted": 0}, {"mistakes": "x"}):
            with self.subTest(bad=bad), self.assertRaises(svc.AdaptiveError) as c:
                svc.process_adaptive_decision(self.db, telemetry(**bad))
            self.assertEqual(c.exception.status, 422)

    def test_level_19_to_20_sequence_lengths_are_accepted(self):
        d = svc.process_adaptive_decision(self.db, telemetry(level=19, seq_len=21))   # level 19 = 21 buttons
        self.assertEqual(d["completed_sequence_length"], 21)

    def test_storage_failure_returns_503_so_board_falls_back(self):
        self.db.fail_table = "game_adaptive_events"
        with self.assertRaises(svc.AdaptiveError) as c:
            svc.process_adaptive_decision(self.db, telemetry())
        self.assertEqual(c.exception.status, 503)

    def test_legacy_record_is_never_replayed_to_the_board(self):
        self.db.tables["game_adaptive_events"] = [{
            "session_id": SID, "completed_level": 2, "applied": True,
            "decision": {"new_sequence_length": 9, "new_response_time_allowance_ms": 20000}}]
        with self.assertRaises(svc.AdaptiveError) as c:
            svc.process_adaptive_decision(self.db, telemetry(level=2))
        self.assertEqual(c.exception.status, 409)

    # ---- applied acknowledgement --------------------------------------------------------
    def test_applied_settings_recorded_next_to_recommendation(self):
        d = svc.process_adaptive_decision(self.db, telemetry(level=1))
        r = svc.record_adaptive_applied(self.db, ack(1, d))
        self.assertTrue(r["applied_matches_recommendation"])
        self.assertTrue(r["sequence_progression_verified"])            # level 1 (3) -> level 2 (4)
        row = self.events()[0]
        self.assertTrue(row["applied"])
        self.assertEqual(row["actual_response_time_adjustment_ms"], row["recommended_new_adjustment_ms"])
        self.assertEqual(row["actual_response_time_allowance_ms"], row["recommended_response_time_allowance_ms"])
        self.assertEqual(row["actual_sequence_length"], 4)

    def test_mismatch_and_wrong_progression_are_flagged_not_hidden(self):
        d = svc.process_adaptive_decision(self.db, telemetry(level=1))
        r = svc.record_adaptive_applied(self.db, ack(1, d, seq_len=3, adj=0, allowance=d["baseline_response_time_allowance_ms"]))
        self.assertFalse(r["applied_matches_recommendation"])
        self.assertFalse(r["sequence_progression_verified"])          # sequence did NOT grow: regression detector

    def test_rejected_decision_ack_records_applied_false(self):
        d = svc.process_adaptive_decision(self.db, telemetry(level=1))
        svc.record_adaptive_applied(self.db, ack(1, d, applied=False, adj=0, allowance=d["baseline_response_time_allowance_ms"]))
        self.assertIs(self.events()[0]["applied"], False)

    def test_duplicate_ack_does_not_overwrite(self):
        d = svc.process_adaptive_decision(self.db, telemetry(level=1))
        svc.record_adaptive_applied(self.db, ack(1, d))
        r = svc.record_adaptive_applied(self.db, ack(1, d, adj=0, allowance=d["baseline_response_time_allowance_ms"], applied=False))
        self.assertTrue(r["idempotent"])
        self.assertTrue(self.events()[0]["applied"])

    def test_ack_validation_and_missing_record(self):
        with self.assertRaises(svc.AdaptiveError) as c:
            svc.record_adaptive_applied(self.db, {"device_id": "lmc-1", "session_id": SID, "completed_level": 1,
                                                  "applied": True, "actual_sequence_length": 4,
                                                  "actual_response_time_adjustment_ms": 0,
                                                  "actual_response_time_allowance_ms": 20000})
        self.assertEqual(c.exception.status, 404)
        d = svc.process_adaptive_decision(self.db, telemetry(level=1))
        with self.assertRaises(svc.AdaptiveError) as c:
            svc.record_adaptive_applied(self.db, ack(1, d, allowance=1))
        self.assertEqual(c.exception.status, 422)

    def test_history_summary_counts_unconfirmed_and_legacy_honestly(self):
        d1 = svc.process_adaptive_decision(self.db, telemetry(level=1))
        svc.record_adaptive_applied(self.db, ack(1, d1))
        svc.process_adaptive_decision(self.db, telemetry(level=2))                     # never acknowledged
        events = self.events() + [{"completed_level": 9, "policy_version": None, "applied": True}]
        s = svc.summarize_adaptive_history(events)
        self.assertEqual((s["decisions"], s["applied"], s["unconfirmed"], s["legacy_records"]), (2, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
