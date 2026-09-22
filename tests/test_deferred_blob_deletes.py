"""Deferred blob-delete queue invariants (ElfHosted fork).

Composite BYTES live in the blobstore, but upstream deletes composites from
synchronous code. Those call sites queue the blob instead of awaiting an
object-store round trip, and the periodic prune drains the queue.

The hazard that buys is ordering: _run_trending_fetch_cycle invalidates a
composite and immediately re-renders it under the SAME key, so a naive queue
deletes the replacement. The inline read path survives that (it drops the
orphaned row), but the CDN redirect path does not — it 302s on the strength of
the row alone, sending clients to an object that is not there.
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


class DeferredBlobDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        cache.init_db()
        await blobstore.init()
        with blobstore._deferred_lock:
            blobstore._deferred_deletes.clear()
            blobstore._blob_generation.clear()
        self._drop_l1()

    @staticmethod
    def _drop_l1():
        """Empty the composite L1.

        Every write populates it, so a read straight after one is answered from
        RAM and never touches the blobstore — which is the thing these tests are
        about. Without this the assertions below pass whether or not the blob
        still exists, which is how the first version of this file managed to
        stay green with the fix removed.
        """
        with sb._composite_l1_lock:
            sb._composite_l1.clear()

    async def _blob(self):
        return await blobstore.get(
            blobstore.BUCKET_COMPOSITES, KEY, max_age_seconds=10_000
        )

    async def asyncTearDown(self):
        await blobstore.close()
        cache.close()

    async def test_a_regenerated_blob_survives_the_queued_delete(self):
        await cache.set_cached_final_poster(KEY, b"FIRST")

        # Trending refresh: invalidate, then re-render under the same key.
        cache.invalidate_final_posters("99", "movie")
        self.assertEqual(blobstore.deferred_delete_stats()["queued"], 1)
        await cache.set_cached_final_poster(KEY, b"SECOND")

        await blobstore.drain_deferred_deletes()

        # Assert on the BLOB, not on a read that L1 could answer from RAM.
        self.assertEqual(
            await self._blob(), b"SECOND",
            "the drain deleted the regenerated blob",
        )
        self._drop_l1()
        entry = await cache.get_cached_final_poster_entry(KEY)
        self.assertIsNotNone(entry, "regenerated composite was deleted by the drain")
        self.assertEqual(entry[0], b"SECOND")
        self._drop_l1()
        self.assertIsNotNone(
            await cache.is_cached_final_poster_fresh(KEY),
            "row survived but the blob behind it did not — a CDN redirect "
            "would 302 to a missing object",
        )

    async def test_an_invalidated_blob_that_is_not_rewritten_is_deleted(self):
        await cache.set_cached_final_poster(KEY, b"FIRST")
        cache.invalidate_final_posters("99", "movie")
        await blobstore.drain_deferred_deletes()

        self.assertIsNone(
            await self._blob(),
            "the whole point of the queue is that this blob does get removed",
        )

    async def test_a_write_concurrent_with_a_drain_is_not_clobbered(self):
        """The same race, with the write landing while a drain is running.

        The drain now holds a per-key lock across its generation check and its
        delete, so the write either completes first (and cancels the queued
        delete) or waits for the delete and rewrites afterwards. Either
        ordering has to end with the blob present — what must not happen is a
        delete landing on top of a completed write.

        The delay goes on an unrelated key so the drain is genuinely mid-loop
        when the write is issued. Blocking inside the delete of THIS key would
        just deadlock against the lock that makes the fix work, which is a
        statement about the test, not about the code.
        """
        await cache.set_cached_final_poster(KEY, b"FIRST")
        await cache.set_cached_final_poster("tt2:98:movie:other", b"OTHER")
        cache.invalidate_final_posters("99", "movie")
        cache.invalidate_final_posters("98", "movie")
        self.assertEqual(blobstore.deferred_delete_stats()["queued"], 2)

        real_delete = blobstore.delete

        async def _slow_delete(bucket, key):
            if key != KEY:
                await asyncio.sleep(0.05)
            return await real_delete(bucket, key)

        blobstore.delete = _slow_delete
        try:
            drain = asyncio.create_task(blobstore.drain_deferred_deletes())
            await asyncio.sleep(0)
            await cache.set_cached_final_poster(KEY, b"SECOND")
            await drain
        finally:
            blobstore.delete = real_delete

        self.assertEqual(
            await self._blob(), b"SECOND", "write during drain lost its blob"
        )
        self._drop_l1()
        entry = await cache.get_cached_final_poster_entry(KEY)
        self.assertIsNotNone(entry, "write during drain lost its blob")
        self.assertEqual(entry[0], b"SECOND")

    async def test_a_delete_queued_by_another_worker_spares_a_live_row(self):
        """The cross-process case the generation map cannot see.

        Worker A invalidates a composite and queues its blob. Worker B — a
        different process, so a different generation map and a different key
        lock — regenerates it under the same key. A's drain must still not
        delete B's blob.

        Simulated by clearing this process's generation bookkeeping between the
        queue and the write, which is exactly what A would observe: a queued
        delete and no local record of the write. What saves the blob is the
        metadata row, which both workers share.
        """
        await cache.set_cached_final_poster(KEY, b"FIRST")
        cache.invalidate_final_posters("99", "movie")
        with blobstore._deferred_lock:
            queued_at = blobstore._deferred_deletes[
                (blobstore.BUCKET_COMPOSITES, KEY)
            ]

        # Worker B's write. In one process this cancels the queued delete; in
        # another process it cannot, so put the entry back exactly as worker A
        # still holds it — queued, with no local record of B's write.
        await cache.set_cached_final_poster(KEY, b"SECOND")
        with blobstore._deferred_lock:
            blobstore._blob_generation.clear()
            blobstore._deferred_deletes[
                (blobstore.BUCKET_COMPOSITES, KEY)
            ] = queued_at

        await blobstore.drain_deferred_deletes(is_live=sb._composite_row_is_live)

        self.assertEqual(
            await self._blob(), b"SECOND",
            "a delete queued in another process deleted a live composite",
        )
        self._drop_l1()
        self.assertIsNotNone(
            await cache.is_cached_final_poster_fresh(KEY),
            "row survived but its blob did not — a CDN redirect would 302 to "
            "a missing object",
        )

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
