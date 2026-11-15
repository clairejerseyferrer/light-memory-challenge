import unittest
from functools import partial

from backend.data_access import create_waiting_session, queue_state
from backend.reliability import (ApiError, ResilientClient, check_database, classify_db_error, with_retries)
from tests.fakes import (APIError, ConnectError, FakeDB, ReadTimeout, RemoteProtocolError,
                         fail_times, lose_response)

NO_SLEEP = partial(with_retries, sleep=lambda s: None)


def resilient(db):
    return ResilientClient(db.client(), retry=NO_SLEEP)


class ClassificationTests(unittest.TestCase):
    def test_transport_errors_are_503_and_retryable(self):
        for exc in (ConnectError(), ReadTimeout(), RemoteProtocolError()):
            info = classify_db_error(exc)
            self.assertEqual((info.status, info.retryable), (503, True), type(exc).__name__)
            self.assertNotIn("migration", info.message.lower())

    def test_connect_phase_is_distinguished_from_ambiguous_failures(self):
        self.assertTrue(classify_db_error(ConnectError()).connect_phase)
        self.assertFalse(classify_db_error(ReadTimeout()).connect_phase)

    def test_only_schema_errors_mention_a_migration(self):
        self.assertIn("migration", classify_db_error(APIError("42703")).message.lower())
        self.assertIn("migration", classify_db_error(APIError("PGRST204")).message.lower())
        for other in (APIError("XX000"), ValueError("boom"), ConnectError()):
            self.assertNotIn("migration", classify_db_error(other).message.lower())

    def test_permanent_and_unknown_errors_are_not_retryable(self):
        for exc in (APIError("42703"), APIError("23505"), APIError("PGRST301"), ValueError("x")):
            self.assertFalse(classify_db_error(exc).retryable, exc)

    def test_gateway_codes_are_transient(self):
        self.assertTrue(classify_db_error(APIError("503")).retryable)

    def test_messages_never_leak_exception_text(self):
        info = classify_db_error(APIError("XX000", "secret-key-abc123 failed"))
        self.assertNotIn("secret", info.message)


class RetryTests(unittest.TestCase):
    def run_with(self, outcomes, **kw):
        calls, sleeps = [], []
        it = iter(outcomes)
        def fn():
            calls.append(1)
            out = next(it)
            if isinstance(out, Exception): raise out
            return out
        try:
            return with_retries(fn, sleep=sleeps.append, rng=lambda: 1.0, **kw), len(calls), sleeps
        except Exception as exc:
            return exc, len(calls), sleeps

    def test_retries_then_succeeds_with_exponential_backoff(self):
        result, n, sleeps = self.run_with([ConnectError(), ConnectError(), "ok"], base_delay=0.25)
        self.assertEqual((result, n), ("ok", 3))
        self.assertEqual(sleeps, [0.25, 0.5])

    def test_gives_up_after_the_attempt_cap_and_raises_the_original_error(self):
        err = ConnectError()
        result, n, _ = self.run_with([err] * 5, attempts=3)
        self.assertIs(result, err)
        self.assertEqual(n, 3)

    def test_permanent_errors_are_not_retried(self):
        result, n, sleeps = self.run_with([APIError("42703"), "ok"])
        self.assertIsInstance(result, APIError)
        self.assertEqual((n, sleeps), (1, []))

    def test_ambiguous_write_failures_are_not_replayed(self):
        result, n, _ = self.run_with([ReadTimeout(), "ok"], idempotent=False)
        self.assertIsInstance(result, ReadTimeout)
        self.assertEqual(n, 1)

    def test_connect_phase_write_failures_are_retried(self):
        result, n, _ = self.run_with([ConnectError(), "ok"], idempotent=False)
        self.assertEqual((result, n), ("ok", 2))

    def test_total_time_budget_stops_retrying(self):
        ticks = iter([0, 0, 100, 100, 100])
        err = ConnectError()
        with self.assertRaises(ConnectError):
            with_retries(lambda: (_ for _ in ()).throw(err), sleep=lambda s: None, clock=lambda: next(ticks),
                         attempts=5, budget=5)


class ResilientClientTests(unittest.TestCase):
    def test_read_survives_two_transient_failures(self):
        db = FakeDB(); db.add_session("a", "Ann")
        db.hooks.append(fail_times("game_sessions", "select", ConnectError, 2))
        rows = resilient(db).table("game_sessions").select("id").execute().data
        self.assertEqual(rows, [{"id": "a"}])
        self.assertEqual(db.calls.count(("game_sessions", "select")), 3)

    def test_database_query_failure_surfaces_after_retries(self):
        db = FakeDB()
        db.hooks.append(fail_times("game_sessions", "select", ConnectError, 99))
        with self.assertRaises(ConnectError):
            resilient(db).table("game_sessions").select("id").execute()
        self.assertEqual(db.calls.count(("game_sessions", "select")), 3)

    def test_empty_response_is_not_an_error(self):
        self.assertEqual(resilient(FakeDB()).table("game_sessions").select("id").execute().data, [])

    def test_write_with_lost_response_is_not_blindly_replayed(self):
        db = FakeDB()
        db.hooks.append(lose_response("game_sessions", "insert"))
        with self.assertRaises(ReadTimeout):
            resilient(db).table("game_sessions").insert({"id": "x", "codename": "Ann"}).execute()
        self.assertEqual(db.calls.count(("game_sessions", "insert")), 1)


class SessionCreationTests(unittest.TestCase):
    def test_creates_one_waiting_session(self):
        db = FakeDB()
        row, replayed = create_waiting_session(resilient(db), "Ann", "ESP32-001")
        self.assertFalse(replayed)
        self.assertEqual((row["status"], row["codename"]), ("waiting", "Ann"))
        self.assertEqual(len(db.tables["game_sessions"]), 1)

    def test_lost_insert_response_does_not_create_a_duplicate(self):
        db = FakeDB()
        db.hooks.append(lose_response("game_sessions", "insert"))
        row, replayed = create_waiting_session(resilient(db), "Ann", "ESP32-001")
        self.assertTrue(replayed)
        self.assertEqual(len(db.tables["game_sessions"]), 1)
        self.assertEqual(row["id"], db.tables["game_sessions"][0]["id"])

    def test_retrying_with_the_same_key_returns_the_same_session(self):
        db = FakeDB(); client = resilient(db)
        first, _ = create_waiting_session(client, "Ann", "ESP32-001", "11111111-1111-4111-8111-111111111111")
        second, replayed = create_waiting_session(client, "Ann", "ESP32-001", "11111111-1111-4111-8111-111111111111")
        self.assertTrue(replayed)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(db.tables["game_sessions"]), 1)

    def test_key_reused_for_a_different_codename_is_rejected(self):
        db = FakeDB(); client = resilient(db)
        create_waiting_session(client, "Ann", "ESP32-001", "11111111-1111-4111-8111-111111111111")
        with self.assertRaises(ApiError) as ctx:
            create_waiting_session(client, "Bob", "ESP32-001", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_database_unavailable_raises_a_transient_error_and_creates_nothing(self):
        db = FakeDB()
        db.hooks.append(fail_times("game_sessions", "select", ConnectError, 99))
        with self.assertRaises(ConnectError):
            create_waiting_session(resilient(db), "Ann", "ESP32-001")
        self.assertEqual(db.tables["game_sessions"], [])

    def test_insert_failure_is_reported_not_hidden(self):
        db = FakeDB()
        db.hooks.append(fail_times("game_sessions", "insert", lambda: APIError("42703"), 1))
        with self.assertRaises(APIError):
            create_waiting_session(resilient(db), "Ann", "ESP32-001")
        self.assertEqual(db.tables["game_sessions"], [])

    def test_queue_full_is_a_409(self):
        db = FakeDB()
        for i in range(3): db.add_session(f"w{i}", f"P{i}", status="waiting")
        with self.assertRaises(ApiError) as ctx:
            create_waiting_session(resilient(db), "Ann", "ESP32-001")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(queue_state(db.client(), "ESP32-001")["waiting_count"], 3)


class HealthTests(unittest.TestCase):
    def test_ok_when_database_answers(self):
        self.assertEqual(check_database(FakeDB().client())["status"], "ok")

    def test_unreachable_database_is_reported_as_such(self):
        db = FakeDB(); db.hooks.append(fail_times("game_sessions", "select", ConnectError, 1))
        self.assertEqual(check_database(db.client())["status"], "unreachable")

    def test_schema_problem_is_distinguished_from_connectivity(self):
        db = FakeDB(); db.hooks.append(fail_times("game_sessions", "select", lambda: APIError("42P01"), 1))
        self.assertEqual(check_database(db.client())["status"], "schema_mismatch")

    def test_health_check_does_not_retry(self):
        db = FakeDB(); db.hooks.append(fail_times("game_sessions", "select", ConnectError, 99))
        check_database(db.client())
        self.assertEqual(len(db.calls), 1)


if __name__ == "__main__":
    unittest.main()
