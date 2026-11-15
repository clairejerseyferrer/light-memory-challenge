import unittest

from backend.data_access import (build_leaderboard_entries, fetch_all, get_result, leaderboard_standing,
                                 search_sessions)
from tests.fakes import FakeDB


def seed():
    db = FakeDB()
    plays = [  # sid, codename, score, level, saved_at
        ("s1", "Ann", 500, 5, "2026-11-01T10:00:00+00:00"),
        ("s2", "ann", 900, 5, "2026-11-02T10:00:00+00:00"),   # same codename (case-insensitive), better score
        ("s3", "Bob", 900, 5, "2026-11-03T10:00:00+00:00"),   # ties Ann on level+score, saved later
        ("s4", "Cy", 100, 7, "2026-11-04T10:00:00+00:00"),    # highest level wins despite low score
        ("s5", "Ann", 9999, 3, "2026-11-05T10:00:00+00:00"),  # lower level: must not replace Ann's best
    ]
    for sid, name, score, level, saved in plays:
        db.add_session(sid, name); db.add_result(sid, score, level, saved)
    db.add_session("ab", "Zed", status="abandoned")
    db.add_result("ab", 5000, 9, "2026-11-06T10:00:00+00:00")  # abandoned: never ranked
    return db


class LeaderboardTests(unittest.TestCase):
    def setUp(self):
        self.db = seed(); self.client = self.db.client()
        self.entries = build_leaderboard_entries(self.client)

    def test_order_is_level_then_score_then_earliest_saved(self):
        self.assertEqual([e["codename"] for e in self.entries], ["Cy", "ann", "Bob"])
        self.assertEqual([e["rank"] for e in self.entries], [1, 2, 3])

    def test_one_entry_per_codename_ignoring_case(self):
        names = [e["codename"].casefold() for e in self.entries]
        self.assertEqual(len(names), len(set(names)))

    def test_abandoned_sessions_are_not_ranked(self):
        self.assertNotIn("Zed", [e["codename"] for e in self.entries])

    def test_tie_break_parses_timestamps_instead_of_comparing_strings(self):
        db = FakeDB()
        # Postgres trims trailing zeros: ".12345" vs ".123456" mis-sorts as plain strings.
        db.add_session("a", "P1"); db.add_result("a", 100, 4, "2026-11-01T10:00:00.12345+00:00")
        db.add_session("b", "P2"); db.add_result("b", 100, 4, "2026-11-01T10:00:00.123456+00:00")
        order = [e["codename"] for e in build_leaderboard_entries(db.client())]
        self.assertEqual(order, ["P1", "P2"])  # .12345 is earlier than .123456

    def test_empty_database(self):
        self.assertEqual(build_leaderboard_entries(FakeDB().client()), [])

    def test_pagination_reads_past_the_page_size(self):
        db = FakeDB()
        for i in range(25):
            db.add_session(f"s{i:02d}", f"P{i:02d}"); db.add_result(f"s{i:02d}", i, 3, "2026-11-01T00:00:00+00:00")
        import backend.data_access as da
        old, da.PAGE_SIZE = da.PAGE_SIZE, 10
        try:
            entries = build_leaderboard_entries(db.client())
        finally:
            da.PAGE_SIZE = old
        self.assertEqual(len(entries), 25)

    def test_fetch_all_pages_until_a_short_page(self):
        db = FakeDB()
        for i in range(7): db.add_session(f"s{i}", f"P{i}")
        rows = fetch_all(lambda: db.client().table("game_sessions").select("id").order("id"), page_size=3)
        self.assertEqual(len(rows), 7)


class ResultVersusLeaderboardTests(unittest.TestCase):
    def setUp(self):
        self.db = seed(); self.client = self.db.client()
        self.entries = build_leaderboard_entries(self.client)

    def test_rank_matches_the_leaderboard_for_the_codename(self):
        ranks = {e["codename"].casefold(): e["rank"] for e in self.entries}
        for sid, name in (("s1", "ann"), ("s2", "ann"), ("s3", "bob"), ("s4", "cy"), ("s5", "ann")):
            result = get_result(self.client, sid)
            self.assertEqual(result["ranking"], ranks[name], sid)
            self.assertEqual(result["leaderboard"]["rank"], ranks[name], sid)

    def test_game_that_is_not_the_leaderboard_entry_says_so(self):
        weak = get_result(self.client, "s5")  # Ann's level-3 game; her entry is s2
        self.assertFalse(weak["leaderboard"]["is_this_session"])
        self.assertEqual(weak["leaderboard"]["entry_session_id"], "s2")
        self.assertEqual(weak["score"], 9999)  # the game's own numbers are untouched
        best = get_result(self.client, "s2")
        self.assertTrue(best["leaderboard"]["is_this_session"])

    def test_standing_is_labelled_as_leaderboard_standing(self):
        label = get_result(self.client, "s1")["leaderboard"]["label"].lower()
        self.assertIn("leaderboard standing", label)

    def test_missing_result_returns_none(self):
        self.assertIsNone(get_result(self.client, "nope"))

    def test_standing_for_unranked_codename_is_none(self):
        self.assertIsNone(leaderboard_standing(self.entries, "Nobody", "x"))

    def test_precomputed_entries_avoid_rebuilding_the_leaderboard(self):
        self.db.calls.clear()
        get_result(self.client, "s1", codename="Ann", entries=self.entries)
        self.assertEqual(self.db.calls, [("game_results", "select")])

    def test_difficulty_events_are_exposed_from_the_analysis(self):
        self.db.tables["game_results"][0]["analysis"] = {"gameplay_difficulty_events": [{"from_level": 5}]}
        self.assertEqual(get_result(self.client, "s1")["difficulty_events"], [{"from_level": 5}])


class SearchTests(unittest.TestCase):
    def test_search_uses_two_queries_regardless_of_session_count(self):
        db = FakeDB()
        for i in range(10):
            db.add_session(f"s{i}", "Ann"); db.add_result(f"s{i}", i * 10, 3, "2026-11-01T00:00:00+00:00")
        db.calls.clear()
        sessions = search_sessions(db.client(), "ann")
        self.assertEqual(len(sessions), 10)
        self.assertEqual(db.calls, [("game_sessions", "select"), ("game_results", "select")])
        self.assertEqual({s["score"] for s in sessions}, {i * 10 for i in range(10)})

    def test_search_without_completed_sessions_skips_the_result_query(self):
        db = FakeDB(); db.add_session("w", "Ann", status="waiting"); db.calls.clear()
        sessions = search_sessions(db.client(), "Ann")
        self.assertEqual(db.calls, [("game_sessions", "select")])
        self.assertIsNone(sessions[0]["score"])

    def test_empty_search(self):
        self.assertEqual(search_sessions(FakeDB().client(), "Nobody"), [])

    def test_underscore_is_literal_not_a_wildcard(self):
        db = FakeDB(); db.add_session("a", "Star_1"); db.add_session("b", "StarX1")
        self.assertEqual([s["codename"] for s in search_sessions(db.client(), "Star_1")], ["Star_1"])


if __name__ == "__main__":
    unittest.main()
