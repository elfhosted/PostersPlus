"""The IMDb weight can be sourced from a local, MDBList-free dataset instead
of MDBList's aggregated response.

These tests cover: the dataset module's own enable/lookup/refresh behaviour
in isolation, and the /poster-side wiring that merges a dataset value into
whatever MDBList did or didn't return.
"""
import asyncio
import gzip
import io
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cache as cache_mod
import imdb_dataset
import main


class _FakeResponse:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, content: bytes):
        self._content = content
        self.calls = 0

    async def get(self, url, **kwargs):
        self.calls += 1
        return _FakeResponse(self._content)


def _make_gz_tsv(rows: list[tuple[str, str, str]]) -> bytes:
    lines = ["tconst\taverageRating\tnumVotes"] + [
        "\t".join(r) for r in rows
    ]
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    return gzip.compress(raw)


class ImdbDatasetDisabledByDefaultTests(unittest.TestCase):
    def test_disabled_lookup_is_always_none(self):
        with patch.object(imdb_dataset, "IMDB_DATASET_ENABLED", False):
            self.assertIsNone(imdb_dataset.get_rating("tt0903747"))

    def test_disabled_status_reports_disabled(self):
        with patch.object(imdb_dataset, "IMDB_DATASET_ENABLED", False):
            self.assertFalse(imdb_dataset.is_enabled())
            self.assertEqual(imdb_dataset.row_count(), 0)


class ImdbDatasetLookupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "imdb_ratings.db")
        self._patches = [
            patch.object(imdb_dataset, "IMDB_DATASET_ENABLED", True),
            patch.object(imdb_dataset, "IMDB_DATASET_PATH", self.db_path),
            patch.object(imdb_dataset, "IMDB_DATASET_MIN_VOTES", 10),
        ]
        for p in self._patches:
            p.start()
        imdb_dataset._local.conn = None

    def tearDown(self):
        for p in self._patches:
            p.stop()
        imdb_dataset._local.conn = None

    def _seed(self, rows: list[tuple[str, float, int]]):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "CREATE TABLE imdb_ratings (tconst TEXT PRIMARY KEY, rating REAL NOT NULL, votes INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO imdb_ratings (tconst, rating, votes) VALUES (?, ?, ?)", rows
        )
        conn.commit()
        conn.close()

    def test_known_id_above_min_votes_returns_rating(self):
        self._seed([("tt0903747", 9.0, 2_000_000)])
        self.assertEqual(imdb_dataset.get_rating("tt0903747"), 9.0)

    def test_unknown_id_returns_none(self):
        self._seed([("tt0903747", 9.0, 2_000_000)])
        self.assertIsNone(imdb_dataset.get_rating("tt9999999"))

    def test_below_min_votes_is_filtered_out(self):
        self._seed([("tt0000001", 10.0, 3)])
        self.assertIsNone(imdb_dataset.get_rating("tt0000001"))

    def test_blank_id_returns_none_without_querying(self):
        self._seed([("tt0903747", 9.0, 2_000_000)])
        self.assertIsNone(imdb_dataset.get_rating(""))
        self.assertIsNone(imdb_dataset.get_rating(None))


class ImdbDatasetRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "imdb_ratings.db")
        self._patches = [
            patch.object(imdb_dataset, "IMDB_DATASET_ENABLED", True),
            patch.object(imdb_dataset, "IMDB_DATASET_PATH", self.db_path),
            patch.object(imdb_dataset, "IMDB_DATASET_MIN_VOTES", 10),
        ]
        for p in self._patches:
            p.start()
        imdb_dataset._local.conn = None
        # refresh_dataset writes this module global; zero it for the fresh
        # temp database and restore it in tearDown, so neither a leftover
        # count leaks in nor this test's count leaks out.
        self._saved_row_count = imdb_dataset._row_count
        imdb_dataset._row_count = 0

    def tearDown(self):
        for p in self._patches:
            p.stop()
        imdb_dataset._local.conn = None
        imdb_dataset._row_count = self._saved_row_count

    def test_refresh_replaces_rather_than_accumulates(self):
        # The load builds imdb_ratings_new and swaps it in, so a title that
        # drops out of IMDb's file must disappear locally too rather than
        # linger from the previous run.
        client = _FakeClient(_make_gz_tsv([
            ("tt0903747", "9.0", "2000000"),
            ("tt0000001", "5.5", "42"),
        ]))
        asyncio.run(imdb_dataset.refresh_dataset(client))
        self.assertEqual(imdb_dataset.row_count(), 2)

        client = _FakeClient(_make_gz_tsv([("tt0903747", "9.3", "2100000")]))
        count = asyncio.run(imdb_dataset.refresh_dataset(client))
        self.assertEqual(count, 1)
        self.assertEqual(imdb_dataset.row_count(), 1)
        self.assertEqual(imdb_dataset.get_rating("tt0903747"), 9.3)
        self.assertIsNone(imdb_dataset.get_rating("tt0000001"))

    def test_refresh_marks_the_dataset_ready(self):
        self.assertFalse(imdb_dataset.is_ready())
        client = _FakeClient(_make_gz_tsv([("tt0903747", "9.0", "2000000")]))
        asyncio.run(imdb_dataset.refresh_dataset(client))
        self.assertTrue(imdb_dataset.is_ready())

    def test_refresh_parses_and_loads_rows(self):
        gz = _make_gz_tsv([
            ("tt0903747", "9.0", "2000000"),
            ("tt0000001", "5.5", "42"),
        ])
        client = _FakeClient(gz)
        count = asyncio.run(imdb_dataset.refresh_dataset(client))
        self.assertEqual(count, 2)
        self.assertEqual(imdb_dataset.get_rating("tt0903747"), 9.0)
        self.assertEqual(imdb_dataset.get_rating("tt0000001"), 5.5)
        self.assertEqual(imdb_dataset.row_count(), 2)

    def test_malformed_rows_are_skipped_not_fatal(self):
        raw = b"tconst\taverageRating\tnumVotes\ntt0903747\t9.0\t2000000\nbad-row-only-one-field\n"
        gz = gzip.compress(raw)
        client = _FakeClient(gz)
        count = asyncio.run(imdb_dataset.refresh_dataset(client))
        self.assertEqual(count, 1)

    def test_refresh_is_a_noop_when_disabled(self):
        with patch.object(imdb_dataset, "IMDB_DATASET_ENABLED", False):
            client = _FakeClient(_make_gz_tsv([("tt0903747", "9.0", "2000000")]))
            count = asyncio.run(imdb_dataset.refresh_dataset(client))
            self.assertEqual(count, 0)
            self.assertEqual(client.calls, 0)

    def test_download_failure_returns_zero_without_raising(self):
        class _FailingClient:
            async def get(self, url, **kwargs):
                raise ConnectionError("boom")

        count = asyncio.run(imdb_dataset.refresh_dataset(_FailingClient()))
        self.assertEqual(count, 0)
        self.assertIsNotNone(imdb_dataset._last_refresh_error)


class RequestConfigImdbSourceTests(unittest.TestCase):
    def test_default_source_is_mdblist(self):
        rcfg = main.build_request_config({})
        self.assertEqual(rcfg.imdb_rating_source, "mdblist")

    def test_dataset_can_be_selected_via_query_param(self):
        rcfg = main.build_request_config({"imdb_rating_source": "dataset"})
        self.assertEqual(rcfg.imdb_rating_source, "dataset")

    def test_invalid_value_falls_back_to_default(self):
        rcfg = main.build_request_config({"imdb_rating_source": "nonsense"})
        self.assertEqual(rcfg.imdb_rating_source, "mdblist")

    def test_value_is_case_insensitive(self):
        rcfg = main.build_request_config({"imdb_rating_source": "DATASET"})
        self.assertEqual(rcfg.imdb_rating_source, "dataset")


class MergeImdbDatasetRatingTests(unittest.TestCase):
    def _rcfg(self, source="dataset"):
        rcfg = main.build_request_config({})
        rcfg.imdb_rating_source = source
        return rcfg

    def test_noop_when_source_is_mdblist(self):
        with patch.object(imdb_dataset, "get_rating", return_value=9.0):
            out = main._merge_imdb_dataset_rating({"tomatoes": 80}, "tt0903747", self._rcfg("mdblist"))
        self.assertEqual(out, {"tomatoes": 80})

    def test_noop_when_no_imdb_id(self):
        with patch.object(imdb_dataset, "get_rating", return_value=9.0) as mock_get:
            out = main._merge_imdb_dataset_rating({"tomatoes": 80}, None, self._rcfg("dataset"))
        mock_get.assert_not_called()
        self.assertEqual(out, {"tomatoes": 80})

    def test_noop_when_dataset_has_no_value(self):
        with patch.object(imdb_dataset, "get_rating", return_value=None):
            out = main._merge_imdb_dataset_rating({"tomatoes": 80}, "tt0903747", self._rcfg("dataset"))
        self.assertEqual(out, {"tomatoes": 80})

    def test_dataset_value_is_added_when_absent_from_mdblist(self):
        with patch.object(imdb_dataset, "get_rating", return_value=9.0):
            out = main._merge_imdb_dataset_rating({"tomatoes": 80}, "tt0903747", self._rcfg("dataset"))
        self.assertEqual(out, {"tomatoes": 80, "imdb": 9.0})

    def test_dataset_value_overrides_an_mdblist_imdb_value(self):
        with patch.object(imdb_dataset, "get_rating", return_value=9.0):
            out = main._merge_imdb_dataset_rating({"imdb": 7.0}, "tt0903747", self._rcfg("dataset"))
        self.assertEqual(out, {"imdb": 9.0})

    def test_non_dict_ratings_passed_through_untouched(self):
        # The rating-fetch-failed / string-sentinel path (FETCH_FAILED etc).
        out = main._merge_imdb_dataset_rating("N/A", "tt0903747", self._rcfg("dataset"))
        self.assertEqual(out, "N/A")

    def test_works_end_to_end_with_calculate_weighted_score(self):
        from ratings import calculate_weighted_score
        with patch.object(imdb_dataset, "get_rating", return_value=9.0):
            merged = main._merge_imdb_dataset_rating({}, "tt0903747", self._rcfg("dataset"))
        score = calculate_weighted_score(merged, {"imdb": 1.0})
        self.assertEqual(score, 90)  # (9.0 / 10) * 100


class MergeDirectTmdbRatingTests(unittest.TestCase):
    def _rcfg(self, source="direct"):
        rcfg = main.build_request_config({})
        rcfg.tmdb_rating_source = source
        return rcfg

    def test_noop_when_source_is_mdblist(self):
        out = main._merge_direct_tmdb_rating(
            {"tomatoes": 80}, {"vote_average": 8.2}, self._rcfg("mdblist")
        )
        self.assertEqual(out, {"tomatoes": 80})

    def test_noop_when_tmdb_data_missing_vote_average(self):
        out = main._merge_direct_tmdb_rating({"tomatoes": 80}, {}, self._rcfg("direct"))
        self.assertEqual(out, {"tomatoes": 80})

    def test_noop_when_tmdb_data_is_none(self):
        out = main._merge_direct_tmdb_rating({"tomatoes": 80}, None, self._rcfg("direct"))
        self.assertEqual(out, {"tomatoes": 80})

    def test_vote_average_is_added_rescaled_to_0_100(self):
        out = main._merge_direct_tmdb_rating({}, {"vote_average": 8.2}, self._rcfg("direct"))
        self.assertEqual(out, {"tmdb": 82.0})

    def test_direct_value_overrides_an_mdblist_tmdb_value(self):
        out = main._merge_direct_tmdb_rating(
            {"tmdb": 55}, {"vote_average": 8.2}, self._rcfg("direct")
        )
        self.assertEqual(out, {"tmdb": 82.0})

    def test_zero_vote_average_is_treated_as_unrated_not_zero_score(self):
        # An unreleased or brand-new title reports vote_average: 0 with no
        # votes yet — showing a 0/100 score would be actively misleading.
        out = main._merge_direct_tmdb_rating({}, {"vote_average": 0}, self._rcfg("direct"))
        self.assertEqual(out, {})

    def test_non_dict_ratings_passed_through_untouched(self):
        out = main._merge_direct_tmdb_rating("N/A", {"vote_average": 8.2}, self._rcfg("direct"))
        self.assertEqual(out, "N/A")

    def test_works_end_to_end_with_calculate_weighted_score(self):
        from ratings import calculate_weighted_score
        merged = main._merge_direct_tmdb_rating({}, {"vote_average": 8.2}, self._rcfg("direct"))
        score = calculate_weighted_score(merged, {"tmdb": 1.0})
        self.assertEqual(score, 82)


class DirectTmdbVoteFloorTests(unittest.TestCase):
    """RATING_MIN_VOTES applies to the direct-from-TMDB source too.

    Every MDBList-sourced rating is filtered on vote count in
    ratings.fetch_mdblist_data, including MDBList's own "tmdb". Without the
    same floor here, switching the source would quietly remove it and a
    single 10/10 vote would render a score of 100.
    """

    def _rcfg(self):
        rcfg = main.build_request_config({})
        rcfg.tmdb_rating_source = "direct"
        return rcfg

    def test_below_min_votes_is_skipped(self):
        with patch.object(main._cfg, "RATING_MIN_VOTES", 10):
            out = main._merge_direct_tmdb_rating(
                {}, {"vote_average": 10.0, "vote_count": 1}, self._rcfg()
            )
        self.assertEqual(out, {})

    def test_at_min_votes_is_kept(self):
        with patch.object(main._cfg, "RATING_MIN_VOTES", 10):
            out = main._merge_direct_tmdb_rating(
                {}, {"vote_average": 8.2, "vote_count": 10}, self._rcfg()
            )
        self.assertEqual(out, {"tmdb": 82.0})

    def test_missing_vote_count_is_allowed(self):
        # Matches ratings.fetch_mdblist_data: the floor only bites when a
        # count is actually reported, so an absent field is not a rejection.
        with patch.object(main._cfg, "RATING_MIN_VOTES", 10):
            out = main._merge_direct_tmdb_rating(
                {}, {"vote_average": 8.2}, self._rcfg()
            )
        self.assertEqual(out, {"tmdb": 82.0})

    def test_unparseable_vote_count_is_allowed(self):
        with patch.object(main._cfg, "RATING_MIN_VOTES", 10):
            out = main._merge_direct_tmdb_rating(
                {}, {"vote_average": 8.2, "vote_count": "many"}, self._rcfg()
            )
        self.assertEqual(out, {"tmdb": 82.0})

    def test_floor_of_zero_keeps_everything(self):
        with patch.object(main._cfg, "RATING_MIN_VOTES", 0):
            out = main._merge_direct_tmdb_rating(
                {}, {"vote_average": 10.0, "vote_count": 1}, self._rcfg()
            )
        self.assertEqual(out, {"tmdb": 100.0})


class ImdbFallbackModeTests(unittest.TestCase):
    """imdb_rating_source=fallback: MDBList wins, the dataset fills the gaps.

    The point of this mode is that operators who do use MDBList get something
    out of the dataset too. It covers a hard gap (fetch failed, key exhausted,
    every key cooling down — the failure path arrives with an empty dict) and
    a soft gap (MDBList answered but carried no IMDb score, or one that
    RATING_MIN_VOTES filtered out) with the same absence test.
    """

    def _rcfg(self, source="fallback"):
        rcfg = main.build_request_config({})
        rcfg.imdb_rating_source = source
        return rcfg

    def test_mdblist_value_is_not_overridden(self):
        with patch.object(imdb_dataset, "get_rating", return_value=9.3):
            out = main._merge_imdb_dataset_rating(
                {"imdb": 8.1, "tomatoes": 80}, "tt0111161", self._rcfg()
            )
        self.assertEqual(out, {"imdb": 8.1, "tomatoes": 80})

    def test_dataset_fills_a_soft_gap(self):
        # MDBList answered, but with no IMDb score for this title.
        with patch.object(imdb_dataset, "get_rating", return_value=9.3):
            out = main._merge_imdb_dataset_rating(
                {"tomatoes": 80}, "tt0111161", self._rcfg()
            )
        self.assertEqual(out, {"tomatoes": 80, "imdb": 9.3})

    def test_dataset_fills_a_hard_gap(self):
        # The MDBList failure path arrives here with an empty dict.
        with patch.object(imdb_dataset, "get_rating", return_value=9.3):
            out = main._merge_imdb_dataset_rating({}, "tt0111161", self._rcfg())
        self.assertEqual(out, {"imdb": 9.3})

    def test_an_explicit_none_is_treated_as_absent(self):
        with patch.object(imdb_dataset, "get_rating", return_value=9.3):
            out = main._merge_imdb_dataset_rating(
                {"imdb": None}, "tt0111161", self._rcfg()
            )
        self.assertEqual(out, {"imdb": 9.3})

    def test_still_a_noop_when_the_dataset_has_nothing_either(self):
        with patch.object(imdb_dataset, "get_rating", return_value=None):
            out = main._merge_imdb_dataset_rating(
                {"tomatoes": 80}, "tt0111161", self._rcfg()
            )
        self.assertEqual(out, {"tomatoes": 80})

    def test_dataset_mode_still_overrides(self):
        # The distinction between the two modes: "dataset" replaces MDBList's
        # answer, "fallback" defers to it.
        with patch.object(imdb_dataset, "get_rating", return_value=9.3):
            out = main._merge_imdb_dataset_rating(
                {"imdb": 8.1}, "tt0111161", self._rcfg("dataset")
            )
        self.assertEqual(out, {"imdb": 9.3})

    def test_mdblist_mode_never_consults_the_dataset(self):
        with patch.object(
            imdb_dataset, "get_rating", side_effect=AssertionError("looked it up")
        ):
            out = main._merge_imdb_dataset_rating({}, "tt0111161", self._rcfg("mdblist"))
        self.assertEqual(out, {})

    def test_fallback_is_accepted_as_a_query_param(self):
        rcfg = main.build_request_config({"imdb_rating_source": "fallback"})
        self.assertEqual(rcfg.imdb_rating_source, "fallback")

    def test_composes_with_fallback_to_imdb(self):
        # The two are different layers: this one puts a value in the ratings
        # dict, fallback_to_imdb reaches for it when the weights score nothing.
        from ratings import calculate_weighted_score

        rcfg = self._rcfg()
        with patch.object(imdb_dataset, "get_rating", return_value=9.3):
            ratings = main._merge_imdb_dataset_rating({}, "tt0111161", rcfg)
        # imdb carries weight 0 by default, so the weights alone score nothing.
        self.assertEqual(calculate_weighted_score(ratings, main._cfg.MOVIE_WEIGHTS), "N/A")
        self.assertEqual(
            calculate_weighted_score(
                ratings, main._cfg.MOVIE_WEIGHTS, fallback_to_imdb=True
            ),
            93,
        )


class TmdbFallbackModeTests(unittest.TestCase):
    """tmdb_rating_source=fallback — same rule, and free: no download, no
    table, no readiness window, so it covers an MDBList outage on an instance
    that has opted into nothing else."""

    def _rcfg(self, source="fallback"):
        rcfg = main.build_request_config({})
        rcfg.tmdb_rating_source = source
        return rcfg

    def test_mdblist_value_is_not_overridden(self):
        out = main._merge_direct_tmdb_rating(
            {"tmdb": 55}, {"vote_average": 8.7, "vote_count": 28000}, self._rcfg()
        )
        self.assertEqual(out, {"tmdb": 55})

    def test_fills_a_soft_gap(self):
        out = main._merge_direct_tmdb_rating(
            {"tomatoes": 80}, {"vote_average": 8.7, "vote_count": 28000}, self._rcfg()
        )
        self.assertEqual(out, {"tomatoes": 80, "tmdb": 87.0})

    def test_fills_a_hard_gap(self):
        out = main._merge_direct_tmdb_rating(
            {}, {"vote_average": 8.7, "vote_count": 28000}, self._rcfg()
        )
        self.assertEqual(out, {"tmdb": 87.0})

    def test_vote_floor_still_applies_in_fallback_mode(self):
        with patch.object(main._cfg, "RATING_MIN_VOTES", 10):
            out = main._merge_direct_tmdb_rating(
                {}, {"vote_average": 10.0, "vote_count": 1}, self._rcfg()
            )
        self.assertEqual(out, {})

    def test_direct_mode_still_overrides(self):
        out = main._merge_direct_tmdb_rating(
            {"tmdb": 55}, {"vote_average": 8.7, "vote_count": 28000}, self._rcfg("direct")
        )
        self.assertEqual(out, {"tmdb": 87.0})

    def test_fallback_is_accepted_as_a_query_param(self):
        rcfg = main.build_request_config({"tmdb_rating_source": "fallback"})
        self.assertEqual(rcfg.tmdb_rating_source, "fallback")

    def test_invalid_value_still_falls_back_to_the_default(self):
        rcfg = main.build_request_config({"tmdb_rating_source": "fallbck"})
        self.assertEqual(rcfg.tmdb_rating_source, "mdblist")


class NoMdblistKeyScoringTests(unittest.TestCase):
    """The whole point of these sources is running without an MDBList key —
    and that was the one configuration where they did nothing.

    With no key and no cached rating row, the rating tuple carries
    cached_ratings_dict, which is None rather than {}. Both merge helpers are
    guarded on isinstance(..., dict), so they were skipped exactly when they
    were supposed to be doing the work, and score fell through as None — a
    500 from the debug endpoint and no score on the poster.
    """

    def _rcfg(self):
        rcfg = main.build_request_config({})
        rcfg.imdb_rating_source = "dataset"
        rcfg.tmdb_rating_source = "direct"
        return rcfg

    def test_none_becomes_an_empty_dict(self):
        self.assertEqual(main._ratings_base(None), {})

    def test_an_existing_dict_is_untouched(self):
        ratings = {"letterboxd": 4.1}
        self.assertIs(main._ratings_base(ratings), ratings)

    def test_a_real_score_sentinel_is_not_swallowed(self):
        # "N/A" is an answer, not an absence — normalising it to {} would let
        # the merge helpers overwrite a deliberate result.
        self.assertEqual(main._ratings_base("N/A"), "N/A")

    def test_dataset_rating_survives_the_no_key_path(self):
        from ratings import calculate_weighted_score

        ratings = main._ratings_base(None)
        with patch.object(imdb_dataset, "get_rating", return_value=9.3):
            ratings = main._merge_imdb_dataset_rating(ratings, "tt0111161", self._rcfg())
        self.assertEqual(ratings, {"imdb": 9.3})
        self.assertEqual(calculate_weighted_score(ratings, {"imdb": 1.0}), 93)

    def test_direct_tmdb_rating_survives_the_no_key_path(self):
        from ratings import calculate_weighted_score

        ratings = main._ratings_base(None)
        ratings = main._merge_direct_tmdb_rating(
            ratings, {"vote_average": 8.7, "vote_count": 28000}, self._rcfg()
        )
        self.assertEqual(ratings, {"tmdb": 87.0})
        self.assertEqual(calculate_weighted_score(ratings, {"tmdb": 1.0}), 87)

    def test_no_key_and_no_usable_source_scores_na_not_none(self):
        from ratings import calculate_weighted_score

        ratings = main._ratings_base(None)
        with patch.object(imdb_dataset, "get_rating", return_value=None):
            ratings = main._merge_imdb_dataset_rating(ratings, "tt0111161", self._rcfg())
        ratings = main._merge_direct_tmdb_rating(ratings, {}, self._rcfg())
        score = calculate_weighted_score(ratings, main._cfg.MOVIE_WEIGHTS)
        self.assertEqual(score, "N/A")
        self.assertIsNotNone(score)


class RefreshClaimTests(unittest.TestCase):
    """Every uvicorn worker runs its own refresh loop against one shared
    database, so the download has to be claimed rather than just performed.

    Without this, WORKERS=4 means four downloads of the same file and four
    concurrent DROP/RENAME swaps of the same table, where the losers take a
    SQLITE_BUSY and log a failure for a refresh that actually succeeded.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._prev_db_path = cache_mod.DB_PATH
        cache_mod.DB_PATH = os.path.join(self.tmpdir, "cache.db")
        cache_mod._local = threading.local()
        cache_mod.init_db()
        self.key = imdb_dataset._REFRESH_CLAIM_KEY
        self.interval = 86400 * 0.9

    def tearDown(self):
        cache_mod.DB_PATH = self._prev_db_path
        cache_mod._local = threading.local()

    def test_first_claim_on_empty_state_wins(self):
        self.assertTrue(
            cache_mod.claim_app_state_slot(self.key, time.time(), self.interval)
        )

    def test_second_claim_inside_the_interval_loses(self):
        now = time.time()
        cache_mod.claim_app_state_slot(self.key, now, self.interval)
        self.assertFalse(
            cache_mod.claim_app_state_slot(self.key, now + 1, self.interval)
        )

    def test_claim_succeeds_once_the_interval_has_passed(self):
        now = time.time()
        cache_mod.claim_app_state_slot(self.key, now, self.interval)
        self.assertTrue(
            cache_mod.claim_app_state_slot(self.key, now + self.interval + 1, self.interval)
        )

    def test_a_released_claim_is_immediately_reclaimable(self):
        # The loop releases on a failed download so one transient error can't
        # lock every worker out until tomorrow.
        now = time.time()
        cache_mod.claim_app_state_slot(self.key, now, self.interval)
        cache_mod.set_app_state(self.key, "0")
        self.assertTrue(
            cache_mod.claim_app_state_slot(self.key, now + 1, self.interval)
        )

    def test_exactly_one_of_many_concurrent_claimants_wins(self):
        # Threads rather than processes so this stays fast, but they contend
        # on the same connection and lock the real implementation uses.
        results = []
        lock = threading.Lock()
        start = threading.Barrier(8)
        now = time.time()

        def claim():
            start.wait()
            won = cache_mod.claim_app_state_slot(self.key, now, self.interval)
            with lock:
                results.append(won)

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(results), 1, f"expected one winner, got {sum(results)}")

    def test_a_bookkeeping_failure_lets_the_job_run(self):
        # Degrading to "every worker refreshes" is wasteful; degrading to
        # "nobody refreshes" would silently freeze the dataset.
        with patch.object(cache_mod, "get_db", side_effect=RuntimeError("db gone")):
            self.assertTrue(
                cache_mod.claim_app_state_slot(self.key, time.time(), self.interval)
            )


class ImdbDatasetReadinessTests(unittest.TestCase):
    """is_ready() distinguishes "switched on" from "actually has data".

    The composite cache signature keys on it so that posters rendered in the
    window between first startup and the first completed download aren't
    served from cache at N/A once the data lands.
    """

    def test_not_ready_when_disabled(self):
        with patch.object(imdb_dataset, "IMDB_DATASET_ENABLED", False):
            with patch.object(imdb_dataset, "_row_count", 1_700_000):
                self.assertFalse(imdb_dataset.is_ready())

    def test_not_ready_when_enabled_but_empty(self):
        with patch.object(imdb_dataset, "IMDB_DATASET_ENABLED", True):
            with patch.object(imdb_dataset, "_row_count", 0):
                self.assertTrue(imdb_dataset.is_enabled())
                self.assertFalse(imdb_dataset.is_ready())

    def test_ready_once_rows_are_loaded(self):
        with patch.object(imdb_dataset, "IMDB_DATASET_ENABLED", True):
            with patch.object(imdb_dataset, "_row_count", 1_700_000):
                self.assertTrue(imdb_dataset.is_ready())

    def test_row_count_is_served_from_cache_not_a_table_scan(self):
        # /server-caps is polled by the configurator and the table runs to
        # ~1.7 M rows, so row_count() must never reach SQLite.
        with patch.object(imdb_dataset, "IMDB_DATASET_ENABLED", True):
            with patch.object(imdb_dataset, "_row_count", 42):
                with patch.object(
                    imdb_dataset, "_get_db", side_effect=AssertionError("queried the db")
                ):
                    self.assertEqual(imdb_dataset.row_count(), 42)

    def test_status_reports_readiness_and_last_error(self):
        with patch.object(imdb_dataset, "IMDB_DATASET_ENABLED", True):
            with patch.object(imdb_dataset, "_row_count", 5):
                with patch.object(imdb_dataset, "_last_refresh_error", "download failed: boom"):
                    st = imdb_dataset.status()
        self.assertTrue(st["enabled"])
        self.assertTrue(st["ready"])
        self.assertEqual(st["row_count"], 5)
        self.assertEqual(st["last_refresh_error"], "download failed: boom")


class TmdbRatingSourceRequestConfigTests(unittest.TestCase):
    def test_default_source_is_mdblist(self):
        rcfg = main.build_request_config({})
        self.assertEqual(rcfg.tmdb_rating_source, "mdblist")

    def test_direct_can_be_selected_via_query_param(self):
        rcfg = main.build_request_config({"tmdb_rating_source": "direct"})
        self.assertEqual(rcfg.tmdb_rating_source, "direct")

    def test_invalid_value_falls_back_to_default(self):
        rcfg = main.build_request_config({"tmdb_rating_source": "nonsense"})
        self.assertEqual(rcfg.tmdb_rating_source, "mdblist")


if __name__ == "__main__":
    unittest.main()
