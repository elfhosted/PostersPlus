"""Static-preset overload-moat tests (ElfHosted fork).

Exercises /p/{preset}/{type}/{imdb_id}.jpg's safety invariants without any
network or keys, by pointing storage at a temp DB and stubbing the TMDB
helpers in main's namespace:

  * disabled (PRESET_ENABLED=False) → 404
  * unknown preset → 404
  * uncached title → renders, NEVER triggers a foreground OCR scan, is NOT
    persisted, and is served with a short Cache-Control
  * fully-warmed title (rating + quality + text-detection cached) → persisted
    under the SAME composite key /poster would use, with the long preset TTL
"""
import asyncio
import io
import os
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image

import config

_TMP = tempfile.mkdtemp()
config.DB_PATH = os.path.join(_TMP, "c.db")
config.TMDB_POSTER_CACHE_DIR = os.path.join(_TMP, "p")
config.TMDB_LOGO_CACHE_DIR = os.path.join(_TMP, "l")
config.COMPOSITE_BLOB_DIR = os.path.join(_TMP, "comp")
config.SERVER_TMDB_KEY = "test-server-key"
config.PRESET_ENABLED = True
config.PRESET_CDN_CACHE_TTL = 86400
config.TEXTLESS_TEXT_DETECTION = True
# /p and /poster share composite keys, so they must agree on the encoding.
# Pinned to the non-default here so a regression back to hardcoded JPEG shows
# up as a failure rather than passing by coincidence.
config.IMAGE_FORMAT = "webp"

import storage.sqlite_backend as sb
sb.DB_PATH = config.DB_PATH
sb.TMDB_POSTER_CACHE_DIR = config.TMDB_POSTER_CACHE_DIR
sb.TMDB_LOGO_CACHE_DIR = config.TMDB_LOGO_CACHE_DIR

import blobstore
import cache
import main
from discovery import DiscoveryMeta
from fastapi import HTTPException

# A non-textless poster so the OCR branch is skipped on the happy path; a
# separate test forces is_textless=True to assert no foreground scan.
_META_NON_TEXTLESS = (
    [28], False, [], "1994", "Test Title", "/poster.jpg", None,
    {"vote_count": 1234, "original_language": "en"},
)
_META_TEXTLESS = (
    [28], True, [], "1994", "Test Title", "/poster.jpg", None,
    {"vote_count": 1234, "original_language": "en"},
)


def _img():
    return Image.new("RGB", (10, 15), (20, 20, 20))


class PresetMoatTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Redirect the local blobstore's composites dir into the temp dir too.
        # (blobstore.local binds COMPOSITE_BLOB_DIR at import, which may have
        # happened — with the real /app path — before this module set config.)
        from blobstore import local as _bl
        _bl._BUCKETS["composites"] = config.COMPOSITE_BLOB_DIR
        cache.init_db()
        await blobstore.init()
        main._HTTP_CLIENT = object()  # sentinel; network helpers are stubbed
        # Every test resolves to tmdb 278, so warmed facts would leak between
        # them and make "unwarmed" cases pass or fail by run order.
        db = sb.get_db()
        for table in ("release_status_cache", "movie_release_info_cache",
                      "rating_cache", "final_poster_cache", "imdb_to_tmdb_cache"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        with sb._composite_l1_lock:
            sb._composite_l1.clear()

    async def asyncTearDown(self):
        await blobstore.close()
        cache.close()

    def _patches(self, metadata):
        """Stub every network/render helper /p calls so nothing hits TMDB."""
        return [
            mock.patch.object(main, "resolve_imdb_to_tmdb",
                              mock.AsyncMock(return_value="278")),
            mock.patch.object(main, "fetch_poster_metadata",
                              mock.AsyncMock(return_value=metadata)),
            mock.patch.object(main, "fetch_poster_image",
                              mock.AsyncMock(return_value=_img())),
            mock.patch.object(main, "fetch_logo", mock.AsyncMock(return_value=None)),
            mock.patch.object(main, "fetch_trending_rank",
                              mock.AsyncMock(return_value=None)),
            mock.patch.object(main, "build_poster", lambda *a, **k: _img()),
            # A real (empty) DiscoveryMeta, not {}: the persist path runs
            # pick_sash on it to cap the composite's lifetime.
            mock.patch.object(
                main, "extract_discovery_meta",
                lambda **k: DiscoveryMeta(
                    award_wins=[], award_noms=[], trending_rank=None,
                    original_language="en",
                ),
            ),
            mock.patch.object(main, "is_digital_release", lambda _i: False),
            # If the foreground OCR scanner is ever invoked from /p, fail loudly.
            mock.patch.object(main, "_start_text_detection",
                              mock.Mock(side_effect=AssertionError(
                                  "/p must never foreground-scan"))),
        ]

    async def _call(self, preset="clean_notch", type="movie", imdb="tt0111161"):
        ctxs = self._patches(_META_NON_TEXTLESS)
        for c in ctxs:
            c.start()
        try:
            return await main.get_preset_poster(preset, type, imdb)
        finally:
            for c in ctxs:
                c.stop()

    async def test_disabled_returns_404(self):
        config.PRESET_ENABLED = False
        try:
            with self.assertRaises(HTTPException) as ctx:
                await main.get_preset_poster("clean_notch", "movie", "tt0111161")
            self.assertEqual(ctx.exception.status_code, 404)
        finally:
            config.PRESET_ENABLED = True

    async def test_unknown_preset_404(self):
        with self.assertRaises(HTTPException) as ctx:
            await main.get_preset_poster("does_not_exist", "movie", "tt0111161")
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_uncached_not_persisted_short_ttl(self):
        resp = await self._call(imdb="tt1111111")
        # Rendered inline in the CONFIGURED format, short Cache-Control, NOT
        # persisted. The format matters beyond the header: /p writes into the
        # same composite key /poster reads, so a JPEG written here would later
        # be served as image/webp by /poster.
        self.assertEqual(resp.media_type, f"image/{config.IMAGE_FORMAT}")
        self.assertEqual(
            Image.open(io.BytesIO(resp.body)).format,
            config.IMAGE_FORMAT.upper(),
        )
        self.assertIn("max-age=60", resp.headers.get("Cache-Control", ""))
        key = main._composite_cache_key(
            "tt1111111", "278", "movie",
            dict(main.get_preset("clean_notch")),
            main.build_request_config(dict(main.get_preset("clean_notch"))).fallback_to_imdb,
        )
        self.assertIsNone(await cache.get_cached_final_poster(key))

    async def test_no_foreground_scan_on_textless_uncached(self):
        # is_textless + uncached detection must NOT foreground-scan; the
        # _start_text_detection stub raises if it does.
        ctxs = self._patches(_META_TEXTLESS)
        for c in ctxs:
            c.start()
        try:
            resp = await main.get_preset_poster("clean_notch", "movie", "tt2222222")
            self.assertEqual(resp.media_type, f"image/{config.IMAGE_FORMAT}")
            self.assertIn("max-age=60", resp.headers.get("Cache-Control", ""))
        finally:
            for c in ctxs:
                c.stop()

    async def test_coalescing_does_not_hold_a_render_slot(self):
        """Joining an in-flight render must not occupy an admission slot.

        With the cap at one, a /poster request can publish its future and then
        queue for a slot. If /p waits for that future while holding the only
        slot, neither can proceed: RENDER_QUEUE_TIMEOUT turns it into a 503 and
        with the timeout disabled it hangs forever. So the join has to happen
        before admission — with the cap at one and a slot already taken, this
        request must still return the coalesced bytes promptly.
        """
        imdb = "tt5555555"
        preset = "clean_notch"
        key = main._composite_cache_key(
            imdb, "278", "movie",
            dict(main.get_preset(preset)),
            main.build_request_config(dict(main.get_preset(preset))).fallback_to_imdb,
            imdb_id=imdb,
        )

        # Someone is already rendering this title, so its imdb->tmdb mapping is
        # warm; a cold lookup would (correctly) queue for a slot of its own.
        cache.set_cached_imdb_to_tmdb(imdb, "movie", "278")

        prev_cap = config.POSTER_RENDER_CONCURRENCY
        prev_sem = main._render_semaphore
        config.POSTER_RENDER_CONCURRENCY = 1
        main._render_semaphore = None
        sem = main._get_render_semaphore()
        await sem.acquire()          # the only slot, held by someone else
        try:
            fut = asyncio.get_running_loop().create_future()
            fut.set_result((b"COALESCED", False, int(time.time()) + 3600))
            main._render_inflight[key] = fut
            try:
                ctxs = self._patches(_META_NON_TEXTLESS)
                for c in ctxs:
                    c.start()
                try:
                    resp = await asyncio.wait_for(
                        main.get_preset_poster(preset, "movie", imdb), timeout=5
                    )
                finally:
                    for c in ctxs:
                        c.stop()
            finally:
                main._render_inflight.pop(key, None)
        finally:
            sem.release()
            config.POSTER_RENDER_CONCURRENCY = prev_cap
            main._render_semaphore = prev_sem

        self.assertEqual(resp.body, b"COALESCED")

    async def test_a_titled_poster_is_swapped_for_the_backdrop_like_poster(self):
        """/poster replaces a poster with its title burned in by a backdrop
        crop. /p must pick the same art: the two share a composite key, and a
        preset that rendered the titled poster would store a different image
        under the key /poster reads."""
        meta = (
            [28], False, [], "1994", "Test Title", "/titled.jpg", "/backdrop.jpg",
            {"vote_count": 1234, "original_language": "en"},
        )
        backdrop = mock.AsyncMock(return_value=_img())
        poster = mock.AsyncMock(return_value=_img())
        ctxs = self._patches(meta) + [
            mock.patch.object(main, "fetch_backdrop_image", backdrop),
            mock.patch.object(main, "fetch_poster_image", poster),
        ]
        for c in ctxs:
            c.start()
        try:
            await main.get_preset_poster("clean_notch", "movie", "tt6666666")
        finally:
            for c in ctxs:
                c.stop()
        backdrop.assert_awaited()
        poster.assert_not_awaited()

    async def test_disable_composite_cache_is_honoured(self):
        """The operator's dev switch must reach /p too: no persistence and a
        non-cacheable response, even for a fully warmed title."""
        imdb = "tt7777777"
        cache.set_cached_rating(
            imdb, {"letterboxd": 80}, "Action", "1994-01-01",
            [], [], 1, None, None, False, False, False,
        )
        cache.set_cached_release_status("movie_278", "Streaming")
        cache.set_cached_movie_release_info("movie_278", {"status": "Streaming"})
        prev = config.DISABLE_COMPOSITE_CACHE
        config.DISABLE_COMPOSITE_CACHE = True
        try:
            resp = await self._call(imdb=imdb)
        finally:
            config.DISABLE_COMPOSITE_CACHE = prev
        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))
        key = main._composite_cache_key(
            imdb, "278", "movie",
            dict(main.get_preset("clean_notch")),
            main.build_request_config(dict(main.get_preset("clean_notch"))).fallback_to_imdb,
            imdb_id=imdb,
        )
        self.assertIsNone(await cache.get_cached_final_poster(key))

    async def test_a_cold_id_lookup_waits_for_a_render_slot(self):
        """An uncached imdb->tmdb lookup is a TMDB request, so it is admitted
        like a render: with the only slot taken it gives up with a 503 instead
        of hitting TMDB anyway."""
        prev_cap, prev_sem = config.POSTER_RENDER_CONCURRENCY, main._render_semaphore
        prev_to = config.RENDER_QUEUE_TIMEOUT
        config.POSTER_RENDER_CONCURRENCY, main._render_semaphore = 1, None
        config.RENDER_QUEUE_TIMEOUT = 0.05
        sem = main._get_render_semaphore()
        await sem.acquire()
        resolver = mock.AsyncMock(return_value="278")
        try:
            with mock.patch.object(main, "resolve_imdb_to_tmdb", resolver):
                with self.assertRaises(HTTPException) as ctx:
                    await main.get_preset_poster("clean_notch", "movie", "tt8888888")
        finally:
            sem.release()
            config.POSTER_RENDER_CONCURRENCY, main._render_semaphore = prev_cap, prev_sem
            config.RENDER_QUEUE_TIMEOUT = prev_to
        self.assertEqual(ctx.exception.status_code, 503)
        resolver.assert_not_awaited()

    async def test_an_unwarmed_film_warms_itself_in_the_background(self):
        """Nothing else fetches film release facts on a /p-only instance, so
        without background warming a film would never become persistable and
        would re-render on every hit. The first hit queues the fetch; once it
        lands, the next hit is persisted under the long preset TTL."""
        imdb = "tt9999999"
        cache.set_cached_rating(
            imdb, {"letterboxd": 80}, "Action", "1994-01-01",
            [], [], 1, None, None, False, False, False,
        )

        async def _fake_status(client, tmdb_id, key, media_type, status):
            cache.set_cached_release_status(f"movie_{tmdb_id}", "Streaming")
            cache.set_cached_movie_release_info(f"movie_{tmdb_id}", {"status": "Streaming"})
            return "Streaming"

        prev_key = config.SERVER_TMDB_KEY
        with mock.patch.object(main, "fetch_release_status", _fake_status):
            first = await self._call(imdb=imdb)
            self.assertIn("max-age=60", first.headers.get("Cache-Control", ""))
            # Let the queued background warm run.
            await asyncio.gather(*list(main._release_warm_tasks))
            second = await self._call(imdb=imdb)
        config.SERVER_TMDB_KEY = prev_key
        self.assertIn("max-age=86400", second.headers.get("Cache-Control", ""))

    async def test_unwarmed_release_status_is_not_persisted(self):
        """A film with a cached rating but no cached release status is still
        incomplete.

        /poster would draw a status sash here (and grey a Cinema title); /p
        cannot resolve the status without a TMDB request the moat forbids. The
        render is therefore not the one /poster would produce, so it must not
        go into the composite both routes read, nor be advertised for a day.
        """
        imdb = "tt4444444"
        cache.set_cached_rating(
            imdb, {"letterboxd": 80}, "Action", "1994-01-01",
            [], [], 1, None, None, False, False, False,
        )
        ctxs = self._patches(_META_NON_TEXTLESS)
        for c in ctxs:
            c.start()
        try:
            resp = await main.get_preset_poster("clean_notch", "movie", imdb)
            self.assertIn("max-age=60", resp.headers.get("Cache-Control", ""))
            key = main._composite_cache_key(
                imdb, "278", "movie",
                dict(main.get_preset("clean_notch")),
                main.build_request_config(dict(main.get_preset("clean_notch"))).fallback_to_imdb,
                imdb_id=imdb,
            )
            self.assertIsNone(await cache.get_cached_final_poster(key))
        finally:
            for c in ctxs:
                c.stop()

    async def test_warmed_persists_long_ttl_shared_key(self):
        imdb = "tt3333333"
        preset = "clean_notch"   # badge_display_mode=0 → no quality needed
        # Warm the rating cache (11-tuple shape).
        cache.set_cached_rating(
            imdb, {"letterboxd": 80}, "Action", "1994-01-01",
            [], [], 1, None, None, False, False, False,
        )
        # ...and the release status. Every gallery preset asks for a release
        # slot, and /p may not call TMDB's /release_dates itself, so a film
        # whose status has never been resolved is not fully warmed — see
        # test_unwarmed_release_status_is_not_persisted.
        cache.set_cached_release_status("movie_278", "Streaming")
        # "Just added" reads the cached release dates the same way.
        cache.set_cached_movie_release_info("movie_278", {"status": "Streaming"})
        ctxs = self._patches(_META_NON_TEXTLESS)
        for c in ctxs:
            c.start()
        try:
            resp = await main.get_preset_poster(preset, "movie", imdb)
            self.assertIn("max-age=86400", resp.headers.get("Cache-Control", ""))
            key = main._composite_cache_key(
                imdb, "278", "movie",
                dict(main.get_preset(preset)),
                main.build_request_config(dict(main.get_preset(preset))).fallback_to_imdb,
            )
            # Persisted under the shared composite key.
            self.assertIsNotNone(await cache.get_cached_final_poster(key))
        finally:
            for c in ctxs:
                c.stop()


if __name__ == "__main__":
    unittest.main()
