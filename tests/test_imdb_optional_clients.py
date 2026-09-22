"""The clients that generate /poster URLs must not require an IMDb id either.

A required "{imdb_id}" placeholder is worse than a missing rating: AIOMetadata
drops the entire URL when a required placeholder resolves to null, so a title
with no IMDb link lost its poster altogether.
"""

from pathlib import Path
from urllib.parse import parse_qs, urlparse
import unittest

import jellyfin_sync
import plex_sync


class ConfiguratorTemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_no_required_imdb_placeholder_is_generated(self):
        # Both the preset catalogue's hand-written URLs and the JS literal the
        # URL builder used to fall back to.
        self.assertNotIn("imdb_id={imdb_id}", self.html)
        self.assertNotIn("imdb_id=%7Bimdb_id%7D", self.html)
        self.assertNotIn("'{imdb_id}'", self.html)

    def test_the_optional_form_is_not_used_either(self):
        # Bingecat rejects "{name?}" at config time and some AIOMetadata builds
        # pass it through verbatim, so it is not a safe substitute.
        self.assertNotIn("{imdb_id?}", self.html)

    def test_tmdb_id_remains_the_required_identity(self):
        self.assertIn("params.set('tmdb_id', tmdbId);", self.html)

    def test_imdb_id_is_only_sent_when_resolved(self):
        self.assertIn("if (imdbId) params.set('imdb_id', imdbId);", self.html)

    def test_preview_does_not_wait_on_an_imdb_id(self):
        self.assertIn("if (resolvedTmdbId) loadPreview();", self.html)
        self.assertNotIn("if (resolvedImdbId && resolvedTmdbId) loadPreview();", self.html)
        self.assertNotIn("if (!resolvedImdbId || !resolvedTmdbId)", self.html)

    def test_missing_imdb_id_is_reported_as_nonfatal(self):
        self.assertIn("None — using TMDB", self.html)
        self.assertNotIn("Could not resolve IMDB ID — preview may fail", self.html)

    def test_negative_resolution_is_cached_but_expires(self):
        self.assertIn("IMDB_NEGATIVE_TTL_MS", self.html)
        self.assertIn("data.imdbCheckedAt = Date.now();", self.html)


class _FakeItem:
    """Minimal stand-in for the attributes build_poster_request touches."""

    TYPE = "movie"

    def __init__(self, title="Some Film"):
        self.title = title


class SyncRequestTests(unittest.TestCase):
    modules = (plex_sync, jellyfin_sync)

    def _params(self, module, **kwargs):
        request = module.build_poster_request(
            media_type="movie", quality_tokens=["1080p"], **kwargs
        )
        return parse_qs(urlparse(str(request.url)).query)

    def test_imdb_id_is_sent_when_the_item_has_one(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                params = self._params(module, imdb_id="tt0903747", tmdb_id="1396")
                self.assertEqual(params["imdb_id"], ["tt0903747"])
                self.assertEqual(params["tmdb_id"], ["1396"])

    def test_imdb_id_is_omitted_rather_than_sent_empty(self):
        for module in self.modules:
            with self.subTest(module=module.__name__):
                params = self._params(module, imdb_id=None, tmdb_id="1698026")
                self.assertNotIn("imdb_id", params)
                self.assertEqual(params["tmdb_id"], ["1698026"])

    def test_a_recipe_imdb_id_never_leaks_onto_a_tmdb_only_item(self):
        # A recipe URL copied out of the configurator can carry a concrete
        # imdb_id. Left in place it would label every TMDB-only item in the
        # library with one unrelated title's IMDb id.
        for module in self.modules:
            with self.subTest(module=module.__name__):
                original = module.POSTERSPLUS_RECIPE_DEFAULTS
                module.POSTERSPLUS_RECIPE_DEFAULTS = {"imdb_id": "tt99999999"}
                try:
                    params = self._params(module, imdb_id=None, tmdb_id="1698026")
                finally:
                    module.POSTERSPLUS_RECIPE_DEFAULTS = original
                self.assertNotIn("imdb_id", params)

    def test_explicit_quality_is_still_sent_for_a_tmdb_only_item(self):
        # The whole point of the sync scripts: badges from the real local file,
        # which needs no upstream lookup and so needs no IMDb id.
        for module in self.modules:
            with self.subTest(module=module.__name__):
                params = self._params(module, imdb_id=None, tmdb_id="1698026")
                self.assertEqual(params["quality"], ["1080p"])


class SyncFingerprintTests(unittest.TestCase):
    def test_fingerprint_is_unchanged_for_items_that_have_an_imdb_id(self):
        # Switching to the optional form must not invalidate cached state and
        # trigger a full re-push of every poster in the library.
        for module in (plex_sync, jellyfin_sync):
            with self.subTest(module=module.__name__):
                imdb_id, tmdb_id, tokens = "tt0903747", "1396", ["1080p"]
                legacy = f"{imdb_id}:{tmdb_id}:{','.join(tokens)}:{module.RECIPE_FINGERPRINT}"
                current = (
                    f"{imdb_id or f'tmdb:{tmdb_id}'}:{tmdb_id}:"
                    f"{','.join(tokens)}:{module.RECIPE_FINGERPRINT}"
                )
                self.assertEqual(current, legacy)

    def test_tmdb_only_items_get_a_distinct_namespaced_fingerprint(self):
        first = f"{None or 'tmdb:1698026'}"
        second = f"{None or 'tmdb:1647910'}"
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("tmdb:"))


if __name__ == "__main__":
    unittest.main()
