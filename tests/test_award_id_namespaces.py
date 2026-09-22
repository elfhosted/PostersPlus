import unittest

from awards import (
    EMMY_WINNER_TMDB_IDS,
    GOLDEN_GLOBE_DRAMA_WINNER_TMDB_IDS,
    GOLDEN_GLOBE_TV_DRAMA_WINNER_TMDB_IDS,
    parse_mdblist_awards,
    reconcile_cached_awards,
    tmdb_id_awards,
)


# TMDB movie and TV ids are separate namespaces.  movie/105 is Back to the
# Future; tv/105 is Sex and the City, an Emmy and Globe winner.
BTTF = 105
SATC = 105
CHEERS = 141          # tv/141 — Emmy winner; movie/141 is Donnie Darko


class AwardNamespaceTests(unittest.TestCase):
    def test_the_report_is_real(self):
        self.assertIn(SATC, EMMY_WINNER_TMDB_IDS)
        self.assertIn(CHEERS, EMMY_WINNER_TMDB_IDS)

    def test_movies_never_get_emmys(self):
        for movie_id in (BTTF, CHEERS):
            with self.subTest(tmdb_id=movie_id):
                wins, noms = parse_mdblist_awards([], tmdb_id=movie_id, media_type="movie")
                self.assertNotIn("Emmy Winner", wins)
                self.assertNotIn("Emmy Nominee", noms)

    def test_the_same_id_as_a_series_still_wins(self):
        wins, _ = parse_mdblist_awards([], tmdb_id=SATC, media_type="tv")
        self.assertIn("Emmy Winner", wins)
        wins, _ = parse_mdblist_awards([], tmdb_id=CHEERS, media_type="series")
        self.assertIn("Emmy Winner", wins)

    def test_globe_sets_are_split_by_namespace(self):
        film_id = next(iter(GOLDEN_GLOBE_DRAMA_WINNER_TMDB_IDS - GOLDEN_GLOBE_TV_DRAMA_WINNER_TMDB_IDS))
        tv_id = next(iter(GOLDEN_GLOBE_TV_DRAMA_WINNER_TMDB_IDS - GOLDEN_GLOBE_DRAMA_WINNER_TMDB_IDS))
        self.assertEqual(tmdb_id_awards(film_id, "movie")[0], ["Globe Winner"])
        self.assertEqual(tmdb_id_awards(film_id, "tv"), ([], []))
        self.assertIn("Globe Winner", tmdb_id_awards(tv_id, "tv")[0])
        self.assertNotIn("Globe Winner", tmdb_id_awards(tv_id, "movie")[0])

    def test_unknown_media_type_is_a_movie(self):
        # /poster defaults type to "movie"; a None must not widen the search.
        self.assertEqual(tmdb_id_awards(SATC, None), ([], []))

    def test_oscar_keywords_are_unaffected(self):
        wins, _ = parse_mdblist_awards(
            [{"name": "best-picture-winner"}], tmdb_id=BTTF, media_type="movie")
        self.assertEqual(wins, ["Oscar Winner"])


class CachedAwardReconciliationTests(unittest.TestCase):
    def test_a_stale_emmy_on_a_movie_row_is_dropped(self):
        # Row written before the namespaces were separated.
        wins, noms = reconcile_cached_awards(["Emmy Winner"], [], BTTF, "movie")
        self.assertEqual((wins, noms), ([], []))

    def test_keyword_labels_survive_and_id_labels_are_rebuilt(self):
        wins, noms = reconcile_cached_awards(
            ["Oscar Winner", "Emmy Winner"], ["Globe Nominee"], BTTF, "movie")
        self.assertEqual(wins, ["Oscar Winner"])
        self.assertEqual(noms, [])
        wins, noms = reconcile_cached_awards(["Oscar Winner"], [], SATC, "tv")
        self.assertEqual(wins[0], "Oscar Winner")
        self.assertIn("Emmy Winner", wins)

    def test_a_correct_row_round_trips_unchanged(self):
        wins, noms = parse_mdblist_awards([], tmdb_id=SATC, media_type="tv")
        self.assertEqual(reconcile_cached_awards(wins, noms, SATC, "tv"), (wins, noms))


if __name__ == "__main__":
    unittest.main()
