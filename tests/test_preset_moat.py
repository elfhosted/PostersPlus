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
import os
import tempfile
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

import storage.sqlite_backend as sb
sb.DB_PATH = config.DB_PATH
sb.TMDB_POSTER_CACHE_DIR = config.TMDB_POSTER_CACHE_DIR
sb.TMDB_LOGO_CACHE_DIR = config.TMDB_LOGO_CACHE_DIR

import blobstore
import cache
import main
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
            mock.patch.object(main, "extract_discovery_meta", lambda **k: {}),
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
        # Rendered (200 inline JPEG), short Cache-Control, NOT persisted.
        self.assertEqual(resp.media_type, "image/jpeg")
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
            self.assertEqual(resp.media_type, "image/jpeg")
            self.assertIn("max-age=60", resp.headers.get("Cache-Control", ""))
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
