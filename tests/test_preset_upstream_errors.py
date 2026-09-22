"""Upstream-error handling on /p (ElfHosted fork).

/p resolves an IMDb id to a TMDB id once and caches the mapping with no TTL,
on the reasoning that a TMDB id never changes. TMDB does, however, delete and
merge duplicate entries, and the id it retires then 404s forever — which is
exactly what happened in production to tt13207736 (cached as tv/329491, a TV
entry TMDB has since merged into 335840). With no except ladder on the route
the httpx error escaped to ASGI as a 500, and because nothing invalidated the
mapping the title 500'd on every subsequent request.

Covered here:
  * upstream 404      → 404, and the stale imdb->tmdb mapping is dropped
  * upstream 5xx      → 502, and the mapping is KEPT (a transient outage must
                        not evict good mappings fleet-wide)
  * upstream timeout  → 504
  * direct tmdb: id   → 404 without touching the mapping table
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

import httpx
from PIL import Image

import config

_TMP = tempfile.mkdtemp()
config.DB_PATH = os.path.join(_TMP, "c.db")
config.TMDB_POSTER_CACHE_DIR = os.path.join(_TMP, "p")
config.TMDB_LOGO_CACHE_DIR = os.path.join(_TMP, "l")
config.COMPOSITE_BLOB_DIR = os.path.join(_TMP, "comp")
config.SERVER_TMDB_KEY = "test-server-key"

import storage.sqlite_backend as sb
sb.DB_PATH = config.DB_PATH
sb.TMDB_POSTER_CACHE_DIR = config.TMDB_POSTER_CACHE_DIR
sb.TMDB_LOGO_CACHE_DIR = config.TMDB_LOGO_CACHE_DIR

import blobstore
import cache
import main
from fastapi import HTTPException

_IMDB = "tt13207736"
_STALE_TMDB = "329491"


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"http://emdb/tmdb/3/tv/{_STALE_TMDB}")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


class PresetUpstreamErrorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from blobstore import local as _bl
        _bl._BUCKETS["composites"] = config.COMPOSITE_BLOB_DIR
        self._prev_preset_enabled = config.PRESET_ENABLED
        config.PRESET_ENABLED = True
        cache.init_db()
        await blobstore.init()
        main._HTTP_CLIENT = object()  # sentinel; the fetch is stubbed
        db = sb.get_db()
        db.execute("DELETE FROM imdb_to_tmdb_cache")
        db.commit()
        # The mapping production held: right when it was written, retired since.
        # Seeded under "tv" because /p folds the route's "series" to "tv"
        # before resolving — an invalidation keyed on the raw segment would
        # miss this row.
        cache.set_cached_imdb_to_tmdb(_IMDB, "tv", _STALE_TMDB)

    async def asyncTearDown(self):
        config.PRESET_ENABLED = self._prev_preset_enabled
        await blobstore.close()
        cache.close()

    async def _call(self, exc, imdb=_IMDB, type="series"):
        """Drive /p with fetch_poster_metadata raising `exc`."""
        with mock.patch.object(
            main, "fetch_poster_metadata", mock.AsyncMock(side_effect=exc)
        ), mock.patch.object(
            main, "fetch_poster_image",
            mock.AsyncMock(return_value=Image.new("RGB", (10, 15))),
        ), mock.patch.object(
            main, "fetch_logo", mock.AsyncMock(return_value=None)
        ):
            with self.assertRaises(HTTPException) as ctx:
                await main.get_preset_poster("clean_notch", type, imdb)
        return ctx.exception

    async def test_upstream_404_becomes_404_and_drops_stale_mapping(self):
        exc = await self._call(_status_error(404))
        self.assertEqual(exc.status_code, 404)
        # The substantive half: without this the title 404s upstream forever.
        self.assertIsNone(
            cache.get_cached_imdb_to_tmdb(_IMDB, "tv"),
            "stale imdb->tmdb mapping must be dropped so the next request "
            "re-resolves via TMDB /find",
        )

    async def test_upstream_5xx_becomes_502_and_keeps_mapping(self):
        exc = await self._call(_status_error(503))
        self.assertEqual(exc.status_code, 502)
        self.assertEqual(
            cache.get_cached_imdb_to_tmdb(_IMDB, "tv"), _STALE_TMDB,
            "a transient upstream outage must not evict resolved mappings",
        )

    async def test_upstream_timeout_becomes_504(self):
        exc = await self._call(httpx.ReadTimeout("timed out"))
        self.assertEqual(exc.status_code, 504)
        self.assertEqual(cache.get_cached_imdb_to_tmdb(_IMDB, "tv"), _STALE_TMDB)

    async def _artwork_404(self):
        """Drive /p to a 404 from the IMAGE fetch, the title itself being fine."""
        meta = ([28], False, [], "2021", "Monster", "/gone.jpg", None,
                {"vote_count": 10, "original_language": "en"})
        with mock.patch.object(
            main, "fetch_poster_metadata", mock.AsyncMock(return_value=meta)
        ), mock.patch.object(
            main, "fetch_poster_image", mock.AsyncMock(side_effect=_status_error(404))
        ), mock.patch.object(
            main, "fetch_logo", mock.AsyncMock(return_value=None)
        ), mock.patch.object(
            main, "fetch_trending_rank", mock.AsyncMock(return_value=None)
        ):
            with self.assertRaises(HTTPException) as ctx:
                await main.get_preset_poster("clean_notch", "series", _IMDB)
        return ctx.exception

    async def test_artwork_404_keeps_the_mapping(self):
        # The title resolved fine; it is the image path that is stale. The id
        # is still good, so evicting the mapping would buy a /find call per
        # bad image and churn a row that was never wrong.
        exc = await self._artwork_404()
        self.assertEqual(exc.status_code, 404)
        self.assertEqual(
            cache.get_cached_imdb_to_tmdb(_IMDB, "tv"), _STALE_TMDB,
            "a stale image path must not evict the id mapping",
        )

    async def test_artwork_404_invalidates_the_metadata_cache(self):
        # The cached metadata is what named the dead image path, so it has to
        # go or every retry rebuilds the same broken render from cache.
        key = main.tmdb_metadata_cache_key("tv", _STALE_TMDB, "en")
        cache.set_cached_tmdb_metadata(
            key, "Monster", "2021", [28], False, "/gone.jpg", [],
            # vote_count and original_title are not decoration: the reader
            # treats a row missing them as a pre-migration row and deletes it,
            # which would make this fixture a no-op and the test vacuous.
            vote_count=1234, original_title="Monster", original_language="en",
        )
        self.assertIsNotNone(cache.get_cached_tmdb_metadata(key))  # fixture is real
        await self._artwork_404()
        self.assertIsNone(
            cache.get_cached_tmdb_metadata(key),
            "the metadata row naming the dead image path must be dropped",
        )

    async def test_waiters_on_a_published_render_get_the_real_status(self):
        # The tests above all run the non-persisting path, where no coalescing
        # future is ever published — so nothing there notices if the ladder
        # stops routing through _fail_render_future and the finally block's
        # blanket 500 reaches the waiters instead. Warm the title so the render
        # IS published, then fail the artwork and read the future a waiter
        # would have been handed.
        cache.set_cached_imdb_to_tmdb(_IMDB, "movie", _STALE_TMDB)
        cache.set_cached_rating(
            _IMDB, {"letterboxd": 80}, "Action", "1994-01-01",
            [], [], 1, None, None, False, False, False,
        )
        cache.set_cached_release_status(f"movie_{_STALE_TMDB}", "Streaming")
        cache.set_cached_movie_release_info(f"movie_{_STALE_TMDB}", {"status": "Streaming"})

        published = []

        class _Capturing(dict):
            def __setitem__(self, key, value):
                published.append(value)
                super().__setitem__(key, value)

        meta = ([28], False, [], "1994", "Monster", "/gone.jpg", None,
                {"vote_count": 1234, "original_language": "en"})
        with mock.patch.object(main, "_render_inflight", _Capturing()), \
             mock.patch.object(
                 main, "fetch_poster_metadata", mock.AsyncMock(return_value=meta)
             ), mock.patch.object(
                 main, "fetch_poster_image",
                 mock.AsyncMock(side_effect=_status_error(404))
             ), mock.patch.object(
                 main, "fetch_logo", mock.AsyncMock(return_value=None)
             ), mock.patch.object(
                 main, "fetch_trending_rank", mock.AsyncMock(return_value=None)
             ):
            with self.assertRaises(HTTPException) as ctx:
                await main.get_preset_poster("clean_notch", "movie", _IMDB)

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertTrue(
            published,
            "no future was published — the render never took the persisting "
            "path, so this test proves nothing about waiters",
        )
        fut = published[0]
        self.assertTrue(fut.done())
        self.assertEqual(
            fut.exception().status_code, 404,
            "a waiter must see the 404 the originator raised, not a blanket 500",
        )


    async def test_late_404_cannot_undo_a_repaired_mapping(self):
        # Two requests race on the retired id. The first 404s, drops the
        # mapping and re-resolves to the replacement; the straggler is still
        # carrying the id it resolved BEFORE that repair, and its 404 lands
        # afterwards. Its delete must name the id that actually failed, or the
        # two ping-pong and the title never stays fixed under real traffic.
        #
        # The straggler's pinned id is modelled by stubbing the resolver: the
        # route re-reads the cache otherwise, which is the one thing an
        # already-in-flight request does not do.
        cache.set_cached_imdb_to_tmdb(_IMDB, "tv", "335840")   # the repair
        with mock.patch.object(
            main, "resolve_imdb_to_tmdb", mock.AsyncMock(return_value=_STALE_TMDB)
        ):
            await self._call(_status_error(404))               # the straggler
        self.assertEqual(
            cache.get_cached_imdb_to_tmdb(_IMDB, "tv"), "335840",
            "a delete naming the retired id must not remove the replacement",
        )

    async def test_direct_tmdb_id_404s_without_mapping_lookup(self):
        # A "tmdb:<id>" request never consulted the mapping table, so there is
        # nothing of its own to invalidate — and it must not delete someone
        # else's row on the way out.
        exc = await self._call(_status_error(404), imdb=f"tmdb:{_STALE_TMDB}")
        self.assertEqual(exc.status_code, 404)
        self.assertEqual(cache.get_cached_imdb_to_tmdb(_IMDB, "tv"), _STALE_TMDB)


class FailRenderFutureTest(unittest.IsolatedAsyncioTestCase):
    """The helper the ladder uses to hand waiters the originator's status."""

    async def test_waiter_receives_the_originators_status(self):
        fut = asyncio.get_running_loop().create_future()
        exc = HTTPException(status_code=404, detail="Title not found on TMDB")
        self.assertIs(main._fail_render_future(fut, exc), exc)
        # Bounded: a helper that fails to settle the future leaves the waiter
        # hanging rather than failing, and a hung test reports nothing.
        with self.assertRaises(HTTPException) as ctx:
            await asyncio.wait_for(fut, timeout=2)
        # Not 500: before this helper, the finally block failed every waiter
        # with a blanket server error while the originator returned 404.
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_settled_future_is_left_alone(self):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(("bytes", False, None))
        main._fail_render_future(fut, HTTPException(status_code=502))
        self.assertEqual(await fut, ("bytes", False, None))

    async def test_absent_future_is_tolerated(self):
        exc = HTTPException(status_code=504)
        self.assertIs(main._fail_render_future(None, exc), exc)


class PosterRouteRetirementTest(unittest.TestCase):
    """The same retirement case on /poster, via POSTER_RESOLVE_IMDB.

    /poster already shaped the upstream 404 into a clean 404, so this never
    showed up as a 500 — it just meant the poisoned mapping was never dropped
    and the title stayed broken. Tenants run with POSTER_RESOLVE_IMDB on.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        cache.init_db()
        self._saved = (config.POSTER_RESOLVE_IMDB, config.PRESET_ENABLED,
                       config.ACCESS_KEY, main._HTTP_CLIENT)
        config.POSTER_RESOLVE_IMDB = True
        config.PRESET_ENABLED, config.ACCESS_KEY = False, ""
        main._HTTP_CLIENT = object()
        sb.get_db().execute("DELETE FROM imdb_to_tmdb_cache")
        sb.get_db().commit()
        cache.set_cached_imdb_to_tmdb(_IMDB, "movie", _STALE_TMDB)
        self.client = TestClient(main.app)

    def tearDown(self):
        (config.POSTER_RESOLVE_IMDB, config.PRESET_ENABLED, config.ACCESS_KEY,
         main._HTTP_CLIENT) = self._saved

    def _get(self, params):
        with mock.patch.object(main, "resolve_imdb_to_tmdb",
                               mock.AsyncMock(return_value=_STALE_TMDB)), \
             mock.patch.object(main, "get_cached_imdb_to_tmdb",
                               return_value=_STALE_TMDB), \
             mock.patch.object(main, "_coalesced_fetch_poster_metadata",
                               mock.AsyncMock(side_effect=_status_error(404))):
            return self.client.get("/poster", params=params)

    def test_resolved_id_that_tmdb_retired_drops_the_mapping(self):
        resp = self._get({"imdb_id": _IMDB, "type": "movie"})
        self.assertEqual(resp.status_code, 404)
        self.assertIsNone(cache.get_cached_imdb_to_tmdb(_IMDB, "movie"))

    def test_caller_supplied_tmdb_id_leaves_the_mapping_alone(self):
        # Nothing was resolved, so there is no mapping of this request's making
        # to invalidate — and the row it happens to collide with is someone
        # else's working resolution.
        resp = self._get({"imdb_id": _IMDB, "tmdb_id": _STALE_TMDB, "type": "movie"})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(cache.get_cached_imdb_to_tmdb(_IMDB, "movie"), _STALE_TMDB)


if __name__ == "__main__":
    unittest.main()
