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


if __name__ == "__main__":
    unittest.main()
