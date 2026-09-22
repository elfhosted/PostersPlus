"""IMDb-only /poster requests (ElfHosted fork, POSTER_RESOLVE_IMDB).

Nuvio only fills {tmdb_id} when the catalogue carries one, and drops the
whole URL when a required placeholder is empty — so on Cinemeta-backed
catalogues, which mostly carry only IMDb ids, a tmdb_id pattern silently keeps
the original posters. With POSTER_RESOLVE_IMDB the server resolves it.
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import config
import main


class PosterImdbResolutionTests(unittest.TestCase):
    def setUp(self):
        self._saved = (config.POSTER_RESOLVE_IMDB, config.PRESET_ENABLED,
                       config.ACCESS_KEY, config.SERVER_TMDB_KEY, main._HTTP_CLIENT)
        config.PRESET_ENABLED, config.ACCESS_KEY = False, ""
        config.SERVER_TMDB_KEY = "server-key"
        main._HTTP_CLIENT = object()      # the resolver is stubbed
        self.client = TestClient(main.app)  # no lifespan

    def tearDown(self):
        (config.POSTER_RESOLVE_IMDB, config.PRESET_ENABLED, config.ACCESS_KEY,
         config.SERVER_TMDB_KEY, main._HTTP_CLIENT) = self._saved

    def _get(self, resolver):
        with mock.patch.object(main, "resolve_imdb_to_tmdb", resolver), \
             mock.patch.object(main, "_check_tmdb_id",
                               side_effect=main.HTTPException(418, "resolved")):
            # _check_tmdb_id runs straight after resolution; making it raise a
            # sentinel proves resolution happened without running the render.
            return self.client.get(
                "/poster", params={"imdb_id": "tt0903747", "type": "movie"}
            )

    def test_off_by_default_keeps_upstreams_400(self):
        config.POSTER_RESOLVE_IMDB = False
        resolver = mock.AsyncMock(return_value="1396")
        resp = self._get(resolver)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("tmdb_id", resp.json()["detail"])
        resolver.assert_not_awaited()

    def test_on_resolves_the_tmdb_id_server_side(self):
        config.POSTER_RESOLVE_IMDB = True
        resolver = mock.AsyncMock(return_value="1396")
        resp = self._get(resolver)
        self.assertEqual(resp.status_code, 418)        # got past the 400
        resolver.assert_awaited_once()
        self.assertEqual(resolver.await_args.args[1], "tt0903747")

    def test_on_with_no_tmdb_match_is_a_404(self):
        config.POSTER_RESOLVE_IMDB = True
        resp = self._get(mock.AsyncMock(return_value=None))
        self.assertEqual(resp.status_code, 404)


class ColdResolutionAdmissionTests(PosterImdbResolutionTests):
    """A cold IMDb->TMDB lookup is a TMDB request made before render
    admission, so it takes a render slot of its own; a cached one doesn't."""

    def _with_slot_held(self, cached):
        import asyncio
        config.POSTER_RESOLVE_IMDB = True
        saved = (config.POSTER_RENDER_CONCURRENCY, main._render_semaphore,
                 config.RENDER_QUEUE_TIMEOUT)
        config.POSTER_RENDER_CONCURRENCY, config.RENDER_QUEUE_TIMEOUT = 1, 0.05
        sem = asyncio.Semaphore(0)          # the only slot is taken
        main._render_semaphore = sem
        resolver = mock.AsyncMock(return_value="1396")
        try:
            with mock.patch.object(main, "get_cached_imdb_to_tmdb",
                                   return_value="1396" if cached else None):
                resp = self._get(resolver)
        finally:
            (config.POSTER_RENDER_CONCURRENCY, main._render_semaphore,
             config.RENDER_QUEUE_TIMEOUT) = saved
        return resp, resolver

    def test_cold_lookup_waits_for_a_slot(self):
        resp, resolver = self._with_slot_held(cached=False)
        self.assertEqual(resp.status_code, 503)
        resolver.assert_not_awaited()

    def test_cached_lookup_needs_no_slot(self):
        resp, resolver = self._with_slot_held(cached=True)
        self.assertEqual(resp.status_code, 418)       # resolved, reached the id check
        resolver.assert_awaited_once()


class NuvioIdPatternTests(PosterImdbResolutionTests):
    """The configurator's Nuvio pattern sends only stremio_id={id}, which Nuvio
    always fills with the namespaced meta id. Each form must reach a tmdb_id."""

    def _get_id(self, stremio_id, resolver):
        seen = {}

        def _capture(tmdb_id):
            seen["tmdb_id"] = tmdb_id
            raise main.HTTPException(418, "resolved")

        with mock.patch.object(main, "resolve_imdb_to_tmdb", resolver), \
             mock.patch.object(main, "_check_tmdb_id", side_effect=_capture):
            resp = self.client.get(
                "/poster",
                params={"stremio_id": stremio_id, "type": "series", "shape": "landscape"},
            )
        return resp, seen.get("tmdb_id")

    def test_imdb_form_is_resolved(self):
        config.POSTER_RESOLVE_IMDB = True
        resolver = mock.AsyncMock(return_value="1396")
        resp, tmdb_id = self._get_id("tt0903747", resolver)
        self.assertEqual((resp.status_code, tmdb_id), (418, "1396"))
        resolver.assert_awaited_once()

    def test_tmdb_form_needs_no_lookup(self):
        config.POSTER_RESOLVE_IMDB = True
        resolver = mock.AsyncMock()
        for sid in ("tmdb:1396", "tmdb:1396:1:2"):
            with self.subTest(stremio_id=sid):
                resp, tmdb_id = self._get_id(sid, resolver)
                self.assertEqual((resp.status_code, tmdb_id), (418, "1396"))
        resolver.assert_not_awaited()

    def test_tmdb_form_is_upstreams_400_when_off(self):
        config.POSTER_RESOLVE_IMDB = False
        resp, _ = self._get_id("tmdb:1396", mock.AsyncMock())
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
