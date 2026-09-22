import json
import sqlite3
import time
import unittest

import cache


class RatingCachePolicyTests(unittest.TestCase):
    def setUp(self):
        self.previous_initialised = cache._initialised
        self.previous_conn = getattr(cache._local, "conn", None)
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            """
            CREATE TABLE rating_cache (
                imdb_id TEXT PRIMARY KEY,
                ratings_json TEXT,
                genre TEXT,
                cached_at INTEGER,
                release_date TEXT,
                award_wins TEXT,
                award_noms TEXT,
                awards_fetched INTEGER,
                festival_label TEXT,
                age_rating INTEGER,
                is_cult INTEGER,
                is_true_story INTEGER,
                is_metacritic INTEGER,
                rating_min_votes INTEGER,
                festival_keyword TEXT
            )
            """
        )
        cache._initialised = True
        cache._local.conn = self.conn

    def tearDown(self):
        self.conn.close()
        cache._initialised = self.previous_initialised
        if self.previous_conn is None:
            try:
                del cache._local.conn
            except AttributeError:
                pass
        else:
            cache._local.conn = self.previous_conn

    def _insert(self, imdb_id, policy):
        self.conn.execute(
            """
            INSERT INTO rating_cache VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                imdb_id,
                json.dumps({"imdb": 75}),
                "Drama",
                int(time.time()),
                "2020-01-01",
                "",
                "",
                1,
                None,
                15,
                0,
                0,
                0,
                policy,
                None,
            ),
        )
        self.conn.commit()

    def test_legacy_policy_row_is_reused_and_backfilled(self):
        self._insert("tt0000001", None)

        result = cache.get_cached_rating("tt0000001")

        self.assertIsNotNone(result)
        stored_policy = self.conn.execute(
            "SELECT rating_min_votes FROM rating_cache WHERE imdb_id = ?",
            ("tt0000001",),
        ).fetchone()[0]
        self.assertEqual(stored_policy, cache.RATING_MIN_VOTES)

    def test_explicit_policy_change_still_invalidates(self):
        self._insert("tt0000002", cache.RATING_MIN_VOTES + 1)

        result = cache.get_cached_rating("tt0000002")

        self.assertIsNone(result)
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM rating_cache WHERE imdb_id = ?",
            ("tt0000002",),
        ).fetchone()[0]
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
