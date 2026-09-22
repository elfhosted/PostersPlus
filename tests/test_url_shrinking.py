"""A generated poster URL has to fit in 2000 characters.

Some metadata clients truncate or reject URLs past that, and a configurator URL
was running ~1500 of which roughly two thirds restated defaults the server would
have picked anyway. Three things shrink it: the configurator omits any parameter
already at its default, sends sash_priority as a diff against the default order,
and drops zero-weighted rating sources.

All three are shorter *spellings*, never different requests, so the tests here
are mostly one invariant stated three ways: the short form and the long form
must build the same RequestConfig.
"""

import dataclasses
import unittest

import config as _cfg
import main
from main import RequestConfig, _parse_sash_priority, build_request_config
from ratings import calculate_weighted_score

DEFAULT_ORDER = list(_cfg.SASH_PRIORITY)
SEED = main._SASH_DIFF_SEED


class SashPriorityDiffTests(unittest.TestCase):
    """`default,-cult,festival@0` instead of all thirty slots."""

    def test_the_bare_seed_is_the_default_order(self):
        self.assertEqual(_parse_sash_priority(SEED), DEFAULT_ORDER)

    def test_a_removal_drops_exactly_one_slot(self):
        got = _parse_sash_priority(f"{SEED},-cult")
        self.assertEqual(got, [s for s in DEFAULT_ORDER if s != "cult"])

    def test_removals_compose(self):
        got = _parse_sash_priority(f"{SEED},-cult,-foreign,-true_story")
        dropped = {"cult", "foreign", "true_story"}
        self.assertEqual(got, [s for s in DEFAULT_ORDER if s not in dropped])

    def test_a_move_promotes_without_losing_anything(self):
        got = _parse_sash_priority(f"{SEED},festival@0")
        self.assertEqual(got[0], "festival")
        self.assertEqual(sorted(got), sorted(DEFAULT_ORDER))

    def test_moves_apply_left_to_right(self):
        got = _parse_sash_priority(f"{SEED},cult@0,festival@0")
        self.assertEqual(got[:2], ["festival", "cult"])

    def test_a_position_past_the_end_lands_at_the_end(self):
        got = _parse_sash_priority(f"{SEED},wins@999")
        self.assertEqual(got[-1], "wins")
        self.assertEqual(len(got), len(DEFAULT_ORDER))

    def test_removals_and_moves_compose(self):
        got = _parse_sash_priority(f"{SEED},-cult,festival@0")
        self.assertEqual(got[0], "festival")
        self.assertNotIn("cult", got)
        self.assertEqual(len(got), len(DEFAULT_ORDER) - 1)

    def test_a_slot_the_default_omits_can_be_added_back(self):
        shortened = _parse_sash_priority(f"{SEED},-cult")
        self.assertNotIn("cult", shortened)
        restored = _parse_sash_priority(f"{SEED},-cult,cult")
        self.assertIn("cult", restored)

    def test_nonsense_is_skipped_rather_than_fatal(self):
        # A URL that half-parses still renders a poster; a 400 renders nothing.
        self.assertEqual(_parse_sash_priority(f"{SEED},notaslot@2"), DEFAULT_ORDER)
        self.assertEqual(_parse_sash_priority(f"{SEED},festival@notanumber"), DEFAULT_ORDER)
        self.assertEqual(_parse_sash_priority(f"{SEED},-notaslot"), DEFAULT_ORDER)


class LegacySashPriorityTests(unittest.TestCase):
    """The long form has to keep meaning exactly what it meant.

    Every URL anyone has generated carries it, and one of these cases is load
    bearing in a way that is easy to miss: an all-exclusions value is how the
    configurator says "every sash off". Reading that as "the default order minus
    these" would switch sashes back on for everyone who had turned them all off,
    which is why the diff form needs its own explicit seed token.
    """

    def test_an_explicit_list_is_still_authoritative(self):
        self.assertEqual(_parse_sash_priority("wins,gg_wins"), ["wins", "gg_wins"])

    def test_an_explicit_list_still_honours_exclusions(self):
        self.assertEqual(_parse_sash_priority("wins,gg_wins,-cult"), ["wins", "gg_wins"])

    def test_all_exclusions_still_means_no_sashes(self):
        every = ",".join("-" + slot for slot in DEFAULT_ORDER)
        self.assertEqual(_parse_sash_priority(every), [])

    def test_empty_and_missing_still_mean_the_default(self):
        self.assertEqual(_parse_sash_priority(None), DEFAULT_ORDER)
        self.assertEqual(_parse_sash_priority(""), DEFAULT_ORDER)

    def test_the_legacy_combined_tokens_still_expand(self):
        got = _parse_sash_priority("wins,structural")
        self.assertEqual(got, ["wins", "short_film", "mini_series", "binge_ready"])

    def test_a_slot_named_default_would_break_the_seed(self):
        # The seed is only unambiguous while nothing is called "default".
        self.assertNotIn(SEED, main.ALL_PRIORITY_SLOTS)


class PublishedDefaultTests(unittest.TestCase):
    """The invariant the whole shrink rests on.

    The configurator drops a parameter when its value equals the default
    published by /server-caps. That is only sound if sending the published
    value and sending nothing produce the same RequestConfig — otherwise every
    generated URL renders a subtly different poster, and nothing would say so.
    """

    def _as_param(self, value):
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def test_sending_a_published_default_is_the_same_as_omitting_it(self):
        baseline = build_request_config({})
        for name, value in main._render_param_defaults().items():
            if value is None:
                continue
            with self.subTest(param=name):
                explicit = build_request_config({name: self._as_param(value)})
                self.assertEqual(
                    dataclasses.asdict(explicit), dataclasses.asdict(baseline),
                    f"sending {name}={value!r} differs from omitting it",
                )

    def test_client_dependent_parameters_are_never_published(self):
        # The client profile picks these two, so there is no single default to
        # publish and the configurator must always send them.
        published = main._render_param_defaults()
        for name in ("bar_bottom_inset", "sash_badge_inset"):
            with self.subTest(param=name):
                self.assertNotIn(name, published)

    def test_identity_and_credentials_are_never_published(self):
        published = main._render_param_defaults()
        for name in ("tmdb_id", "imdb_id", "type", "access_key", "tmdb_key",
                     "mdblist_key", "primary_client"):
            with self.subTest(param=name):
                self.assertNotIn(name, published)

    def test_the_published_defaults_cover_the_render_config(self):
        # A field missing here is a parameter the configurator can never drop,
        # so the URL keeps carrying it. Not a correctness problem, but the
        # reason this shrink exists, so notice if the coverage collapses.
        published = main._render_param_defaults()
        fields = {f.name for f in dataclasses.fields(RequestConfig())}
        self.assertGreater(len(published), len(fields) * 0.8)

    def test_every_published_value_survives_a_json_round_trip(self):
        # It is served as JSON and compared in JavaScript; a value that does not
        # round-trip would never match and would silently stop being dropped.
        import json
        published = main._render_param_defaults()
        self.assertEqual(json.loads(json.dumps(published)), published)


class ZeroWeightTests(unittest.TestCase):
    """Naming a source at 0.00 costs ~14 characters and changes nothing.

    The default weights leave 14 of 18 sources at zero, which was ~200
    characters of every URL saying "ignore this".
    """

    RATINGS = {"imdb": 7.5, "trakt": 80, "tomatoes": 90, "metacritic": 70}

    def test_a_zero_weighted_source_scores_the_same_as_an_absent_one(self):
        spelled_out = {"trakt": 0.8, "tomatoes": 0.2, "imdb": 0.0, "metacritic": 0.0}
        trimmed = {"trakt": 0.8, "tomatoes": 0.2}
        self.assertEqual(
            calculate_weighted_score(self.RATINGS, spelled_out),
            calculate_weighted_score(self.RATINGS, trimmed),
        )

    def test_dropping_zeros_does_not_disturb_the_imdb_fallback(self):
        # imdb:0.00 looks like it might arm the fallback. It does not — the
        # fallback fires on total_weight == 0, whoever is named.
        spelled_out = {"letterboxd": 0.0, "imdb": 0.0}
        self.assertEqual(
            calculate_weighted_score(self.RATINGS, spelled_out, fallback_to_imdb=True),
            calculate_weighted_score(self.RATINGS, {}, fallback_to_imdb=True),
        )

    def test_an_all_zero_weight_string_still_reaches_the_fallback(self):
        all_zero = {src: 0.0 for src in ("trakt", "tomatoes", "imdb")}
        self.assertEqual(
            calculate_weighted_score(self.RATINGS, all_zero, fallback_to_imdb=True),
            calculate_weighted_score(self.RATINGS, {}, fallback_to_imdb=True),
        )

    def test_zeroing_every_source_is_not_the_same_as_sending_nothing(self):
        """The one case where stripping zeros must not strip everything.

        Zeroing every source is a deliberate "show no weighted score". Strip it
        down to an empty string and the parameter reads as absent, and an absent
        weights parameter falls back to the *server's* weights — so a user who
        switched the score off would get one back. The configurator keeps a
        single zero entry for exactly this case; these are the two sides it has
        to stay between.
        """
        one_zero = build_request_config({"movie_weights": "imdb:0.00"})
        self.assertEqual(one_zero.movie_weights, {"imdb": 0.0})

        for empty in ("", None):
            with self.subTest(value=empty):
                omitted = build_request_config(
                    {} if empty is None else {"movie_weights": empty}
                )
                self.assertIsNone(omitted.movie_weights)

        # ...and the surviving zero entry still means "no weighted score",
        # exactly as the fully spelled-out version did.
        spelled_out = build_request_config({
            "movie_weights": ",".join(f"{s}:0.00" for s in ("imdb", "trakt", "tomatoes"))
        })
        self.assertEqual(
            calculate_weighted_score(self.RATINGS, one_zero.movie_weights),
            calculate_weighted_score(self.RATINGS, spelled_out.movie_weights),
        )


if __name__ == "__main__":
    unittest.main()
