"""Anime gets its own rating weights, and only when a URL asks for them.

A title is anime when it carries a MyAnimeList, AniList or Kitsu rating —
nothing else is consulted. `anime_movie_weights` / `anime_tv_weights` are the
opt-in; a URL naming neither scores anime with its movie/TV weights exactly as
it did before the parameters existed.
"""

import re
import unittest
from pathlib import Path

import config as _cfg
import main
from main import _select_rating_weights, build_request_config
from ratings import calculate_weighted_score, is_anime_rated

MOVIE = {"letterboxd": 1.0}
TV = {"trakt": 1.0}
ANIME_MOVIE = {"myanimelist": 1.0}
ANIME_TV = {"myanimelist": 0.5, "trakt": 0.5}


def _select(ratings, media_type, *, anime_native=False, anime_movie=None, anime_tv=None):
    return _select_rating_weights(
        ratings, media_type, anime_native=anime_native,
        movie_weights=MOVIE, tv_weights=TV,
        # Mirrors get_poster: the anime set falls back to the movie/TV set.
        anime_movie_weights=anime_movie or MOVIE,
        anime_tv_weights=anime_tv or TV,
    )


class AnimeDetectionTests(unittest.TestCase):
    def test_an_anime_source_is_the_whole_test(self):
        for source in _cfg.ANIME_RATING_SOURCES:
            with self.subTest(source=source):
                self.assertTrue(is_anime_rated({"imdb": 8.0, source: 80}))

    def test_anime_sources_are_exactly_mal_anilist_kitsu(self):
        self.assertEqual(set(_cfg.ANIME_RATING_SOURCES), {"myanimelist", "anilist", "kitsu"})

    def test_western_sources_alone_are_not_anime(self):
        # Every non-anime source at once, still not anime — no genre or
        # keyword guesswork sneaks in through the back door.
        ratings = {s: 80 for s in _cfg.SCORE_NORMALISERS if s not in _cfg.ANIME_RATING_SOURCES}
        self.assertFalse(is_anime_rated(ratings))
        self.assertFalse(is_anime_rated({}))


class WeightSelectionTests(unittest.TestCase):
    def test_anime_movie_takes_the_anime_movie_set(self):
        got = _select({"letterboxd": 4.2, "myanimelist": 8.5}, "movie",
                      anime_movie=ANIME_MOVIE, anime_tv=ANIME_TV)
        self.assertIs(got, ANIME_MOVIE)

    def test_anime_show_takes_the_anime_tv_set(self):
        for media_type in ("tv", "series"):
            with self.subTest(media_type=media_type):
                got = _select({"trakt": 85, "myanimelist": 8.5}, media_type,
                              anime_movie=ANIME_MOVIE, anime_tv=ANIME_TV)
                self.assertIs(got, ANIME_TV)

    def test_live_action_is_untouched_by_anime_weights(self):
        self.assertIs(_select({"letterboxd": 4.2}, "movie",
                              anime_movie=ANIME_MOVIE, anime_tv=ANIME_TV), MOVIE)
        self.assertIs(_select({"trakt": 85}, "tv",
                              anime_movie=ANIME_MOVIE, anime_tv=ANIME_TV), TV)

    def test_anime_native_request_is_anime_even_with_no_score(self):
        # Requested by anilist_id/kitsu_id but the provider returned no score
        # and MDBList had no MAL entry: still anime, the id said so.
        got = _select({"imdb": 8.0}, "tv", anime_native=True,
                      anime_movie=ANIME_MOVIE, anime_tv=ANIME_TV)
        self.assertIs(got, ANIME_TV)

    def test_without_anime_weights_anime_scores_as_before(self):
        """The fallback for existing URLs: no anime params, no change."""
        ratings = {"letterboxd": 4.0, "myanimelist": 9.0}
        self.assertIs(_select(ratings, "movie"), MOVIE)
        self.assertEqual(
            calculate_weighted_score(ratings, _select(ratings, "movie")),
            calculate_weighted_score(ratings, MOVIE),
        )
        self.assertIs(_select({"trakt": 80, "myanimelist": 9.0}, "tv"), TV)

    def test_anime_weights_change_the_score(self):
        ratings = {"letterboxd": 4.0, "myanimelist": 9.0}   # 80 vs 90
        self.assertEqual(calculate_weighted_score(ratings, MOVIE), 80)
        self.assertEqual(
            calculate_weighted_score(ratings, _select(ratings, "movie", anime_movie=ANIME_MOVIE)),
            90,
        )


class RequestConfigTests(unittest.TestCase):
    def test_absent_means_none(self):
        cfg = build_request_config({"movie_weights": "letterboxd:1.00"})
        self.assertIsNone(cfg.anime_movie_weights)
        self.assertIsNone(cfg.anime_tv_weights)

    def test_parses_both_parameters(self):
        cfg = build_request_config({
            "anime_movie_weights": "myanimelist:0.60,letterboxd:0.40",
            "anime_tv_weights":    "myanimelist:0.60,trakt:0.40",
        })
        self.assertEqual(cfg.anime_movie_weights, {"myanimelist": 0.6, "letterboxd": 0.4})
        self.assertEqual(cfg.anime_tv_weights,    {"myanimelist": 0.6, "trakt": 0.4})

    def test_sources_mdblist_never_returns_for_anime_tv_are_dropped(self):
        # Metacritic critic and Roger Ebert don't exist for anime series, so
        # naming them is filtered like any unknown source rather than kept as
        # a weight that can never fire.
        cfg = build_request_config({
            "anime_tv_weights": "metacritic:0.50,rogerebert:0.30,myanimelist:0.20",
        })
        self.assertEqual(cfg.anime_tv_weights, {"myanimelist": 0.2})

    def test_letterboxd_is_a_valid_anime_tv_source(self):
        # Unlike TV_WEIGHTS: MDBList does return it for anime series.
        cfg = build_request_config({"anime_tv_weights": "letterboxd:1.00"})
        self.assertEqual(cfg.anime_tv_weights, {"letterboxd": 1.0})

    def test_a_lone_zero_is_still_an_opt_in(self):
        # Same contract as movie_weights: the configurator sends one zero entry
        # for "every anime source off", and that must not read as absent.
        cfg = build_request_config({"anime_movie_weights": "myanimelist:0.00"})
        self.assertEqual(cfg.anime_movie_weights, {"myanimelist": 0.0})

    def test_defaults_are_published_as_none(self):
        # So the configurator can omit the params when the toggle is off.
        defaults = main._render_param_defaults()
        self.assertIsNone(defaults["anime_movie_weights"])
        self.assertIsNone(defaults["anime_tv_weights"])


class SourceListTests(unittest.TestCase):
    def test_every_anime_source_has_a_normaliser(self):
        for src in (*_cfg.ANIME_MOVIE_SOURCES, *_cfg.ANIME_TV_SOURCES):
            with self.subTest(source=src):
                self.assertIn(src, _cfg.SCORE_NORMALISERS)

    def test_both_lists_carry_the_anime_sources(self):
        for src in _cfg.ANIME_RATING_SOURCES:
            self.assertIn(src, _cfg.ANIME_MOVIE_SOURCES)
            self.assertIn(src, _cfg.ANIME_TV_SOURCES)

    def test_anime_movies_offer_every_movie_source(self):
        self.assertEqual(set(_cfg.ANIME_MOVIE_SOURCES), set(_cfg.MOVIE_WEIGHTS))

    def test_anime_tv_excludes_sources_mdblist_never_returns(self):
        self.assertNotIn("metacritic", _cfg.ANIME_TV_SOURCES)
        self.assertNotIn("rogerebert", _cfg.ANIME_TV_SOURCES)
        self.assertIn("letterboxd", _cfg.ANIME_TV_SOURCES)


class ConfiguratorWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def _js_list(self, name):
        m = re.search(rf"const {name}\s*=\s*\[([^\]]*)\]", self.html)
        self.assertIsNotNone(m, name)
        return re.findall(r"'([a-z]+)'", m.group(1))

    def test_source_lists_match_config(self):
        self.assertEqual(self._js_list("ANIME_MOVIE_SOURCES"), list(_cfg.ANIME_MOVIE_SOURCES))
        self.assertEqual(self._js_list("ANIME_TV_SOURCES"),    list(_cfg.ANIME_TV_SOURCES))

    def test_toggle_gates_the_parameters(self):
        # Emitted only inside the toggle check, so an unchecked toggle leaves
        # both parameters out of the URL — which is the server's "as before".
        self.assertIn('id="tog-anime-weights"', self.html)
        m = re.search(
            r"if \(c\('tog-anime-weights'\)\) \{(.*?)\}", self.html, re.S
        )
        self.assertIsNotNone(m)
        self.assertIn("params.set('anime_movie_weights'", m.group(1))
        self.assertIn("params.set('anime_tv_weights'",    m.group(1))
        self.assertEqual(self.html.count("params.set('anime_movie_weights'"), 1)
        self.assertEqual(self.html.count("params.set('anime_tv_weights'"),    1)

    def test_import_reads_the_parameters_back(self):
        self.assertIn("applyWeights('anime_movie_weights', 'anime-movie-weights', ANIME_MOVIE_SOURCES)", self.html)
        self.assertIn("applyWeights('anime_tv_weights',    'anime-tv-weights',    ANIME_TV_SOURCES)", self.html)
        self.assertIn("_setEl('tog-anime-weights', true)", self.html)

    def test_sum_elements_exist_for_every_grid(self):
        for grid in ("movie", "tv", "anime-movie", "anime-tv"):
            with self.subTest(grid=grid):
                self.assertIn(f'id="{grid}-weights"', self.html)
                self.assertIn(f'id="{grid}-sum"', self.html)


if __name__ == "__main__":
    unittest.main()
