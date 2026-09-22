"""Composite blob lifecycle invariants (ElfHosted fork).

Composite BYTES live in the blobstore under a VERSIONED key the metadata row
records; upstream deletes composites from synchronous code, so those call
sites queue the old version and the periodic drain deletes it.

The hazard that design exists to remove: _run_trending_fetch_cycle
invalidates a composite and immediately re-renders it under the same cache
key, often on a different worker or pod than the one that queued the delete.
With the blob stored under the cache key itself, the queued delete landed on
the replacement — a fresh row pointing at nothing, which a CDN redirect turns
into a 404 for every client until the row expires.
"""
import asyncio
import os
import tempfile
import unittest

import config

_TMP = tempfile.mkdtemp()
config.DB_PATH = os.path.join(_TMP, "c.db")
config.TMDB_POSTER_CACHE_DIR = os.path.join(_TMP, "p")
config.TMDB_LOGO_CACHE_DIR = os.path.join(_TMP, "l")
config.COMPOSITE_BLOB_DIR = os.path.join(_TMP, "comp")

import storage.sqlite_backend as sb
sb.DB_PATH = config.DB_PATH
sb.TMDB_POSTER_CACHE_DIR = config.TMDB_POSTER_CACHE_DIR
sb.TMDB_LOGO_CACHE_DIR = config.TMDB_LOGO_CACHE_DIR

import blobstore
from blobstore import local as _bl
_bl._BUCKETS["composites"] = config.COMPOSITE_BLOB_DIR

import cache

KEY = "tt1:99:movie:hash"


class CompositeBlobLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cache.init_db()
        await blobstore.init()
        with blobstore._deferred_lock:
            blobstore._deferred_deletes.clear()
        self._drop_l1()

    async def asyncTearDown(self):
        await blobstore.close()
        cache.close()

    @staticmethod
    def _drop_l1():
        """Empty the composite L1. Every write populates it, so a read straight
        after one is answered from RAM and never touches the blobstore — which
        is the thing under test. Without this, assertions pass whether or not
        the blob still exists."""
        with sb._composite_l1_lock:
            sb._composite_l1.clear()

    @staticmethod
    def _blob_key(cache_key=KEY):
        row = sb.get_db().execute(
            "SELECT blob_key FROM final_poster_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        return None if row is None else row[0]

    async def _blob(self, blob_key):
        return await blobstore.get(
            blobstore.BUCKET_COMPOSITES, blob_key, max_age_seconds=10_000
        )

    async def _read(self):
        self._drop_l1()
        entry = await cache.get_cached_final_poster_entry(KEY)
        return None if entry is None else entry[0]

    async def test_every_write_gets_a_new_blob_version(self):
        await cache.set_cached_final_poster(KEY, b"FIRST")
        first = self._blob_key()
        await cache.set_cached_final_poster(KEY, b"SECOND")
        second = self._blob_key()
        self.assertTrue(first and second)
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, KEY, "blob stored under the reusable cache key")

    async def test_a_regenerated_composite_survives_the_queued_delete(self):
        await cache.set_cached_final_poster(KEY, b"FIRST")
        # Trending refresh: invalidate, then re-render under the same key.
        cache.invalidate_final_posters("99", "movie")
        await cache.set_cached_final_poster(KEY, b"SECOND")

        await blobstore.drain_deferred_deletes()

        self.assertEqual(await self._blob(self._blob_key()), b"SECOND")
        self.assertEqual(await self._read(), b"SECOND")

    async def test_a_delete_queued_on_another_worker_spares_the_replacement(self):
        """Worker A invalidates and queues; worker B regenerates. Nothing is
        shared between their processes except the row, and A's drain runs
        after B's write. The replacement must survive."""
        await cache.set_cached_final_poster(KEY, b"FIRST")
        old_version = self._blob_key()
        cache.invalidate_final_posters("99", "movie")
        queued = dict(blobstore._deferred_deletes)          # worker A's queue

        with blobstore._deferred_lock:
            blobstore._deferred_deletes.clear()
        await cache.set_cached_final_poster(KEY, b"SECOND")  # worker B

        with blobstore._deferred_lock:                       # A drains later
            blobstore._deferred_deletes.clear()
            blobstore._deferred_deletes.update(queued)
        await blobstore.drain_deferred_deletes()

        self.assertIsNone(await self._blob(old_version), "old version not collected")
        self.assertEqual(await self._blob(self._blob_key()), b"SECOND")
        self.assertEqual(await self._read(), b"SECOND")

    async def test_an_invalidated_composite_that_is_not_rewritten_is_deleted(self):
        await cache.set_cached_final_poster(KEY, b"FIRST")
        version = self._blob_key()
        cache.invalidate_final_posters("99", "movie")
        await blobstore.drain_deferred_deletes()
        self.assertIsNone(await self._blob(version))
        self.assertIsNone(await self._read())

    async def test_a_rewrite_queues_the_version_it_replaces(self):
        """No orphans: overwriting a composite must not leave the old version
        in object storage forever."""
        await cache.set_cached_final_poster(KEY, b"FIRST")
        first = self._blob_key()
        await cache.set_cached_final_poster(KEY, b"SECOND")
        await blobstore.drain_deferred_deletes()
        self.assertIsNone(await self._blob(first))
        self.assertEqual(await self._read(), b"SECOND")

    async def test_the_redirect_resolves_the_current_version_not_l1(self):
        """A CDN redirect must follow the shared row. Simulate another pod
        replacing the composite while this process still holds the old bytes
        in L1: the redirect must name the new version."""
        prev = _bl.url_for
        _bl.url_for = lambda bucket, key: f"https://cdn.test/{bucket}/{key}"
        blobstore.url_for = _bl.url_for
        try:
            await cache.set_cached_final_poster(KEY, b"FIRST")
            # Another pod's write: new row, new version; our L1 is untouched.
            with sb._composite_l1_lock:
                l1_before = dict(sb._composite_l1)
            await cache.set_cached_final_poster(KEY, b"SECOND")
            current = self._blob_key()
            with sb._composite_l1_lock:
                sb._composite_l1.clear()
                sb._composite_l1.update(l1_before)
            url, _exp = await cache.get_cached_final_poster_redirect(KEY)
            self.assertTrue(url.endswith(current), url)
        finally:
            _bl.url_for = prev
            blobstore.url_for = prev

    async def test_the_queue_is_bounded_and_says_so(self):
        original = blobstore.DEFERRED_DELETE_MAX
        blobstore.DEFERRED_DELETE_MAX = 3
        try:
            for i in range(10):
                blobstore.delete_later(blobstore.BUCKET_COMPOSITES, f"k{i}")
            stats = blobstore.deferred_delete_stats()
            self.assertEqual(stats["queued"], 3)
            self.assertEqual(stats["dropped"], 7)
        finally:
            blobstore.DEFERRED_DELETE_MAX = original


if __name__ == "__main__":
    unittest.main()
