"""Background refreshes that must survive a restart, or a pod that sleeps
between requests, without re-running every time or leaving stale data cached.
"""
import asyncio
import unittest
from unittest import mock

import digital_release


class InitialWaitTests(unittest.TestCase):
    def test_never_synced_keeps_the_startup_grace(self):
        self.assertEqual(digital_release._initial_wait(1_000_000.0, None), (60.0, False))

    def test_recent_sync_is_current_and_waits_for_the_next_one(self):
        now = 1_000_000.0
        wait, current = digital_release._initial_wait(now, now - 3600)
        self.assertTrue(current)
        self.assertAlmostEqual(wait, 86400 - 3600)

    def test_stale_sync_catches_up_straight_away(self):
        now = 1_000_000.0
        self.assertEqual(digital_release._initial_wait(now, now - 86400 - 1), (0.0, False))
        self.assertEqual(digital_release._initial_wait(now, now - 86400), (0.0, False))


def _post(imdb_id, ts):
    return {"created_utc": ts, "title": f"https://www.imdb.com/title/{imdb_id}/"}


class SyncFailureTests(unittest.TestCase):
    def _run(self, pages):
        pages = list(pages)

        async def fake_fetch(client, after_ts, before_ts):
            return pages.pop(0)

        added = []
        with mock.patch.object(digital_release, "_fetch_page", fake_fetch), \
             mock.patch.object(digital_release, "_PAGE_PAUSE", 0), \
             mock.patch.object(digital_release, "add_digital_releases",
                               side_effect=lambda e: added.extend(e) or len(e)):
            return added, asyncio.run(digital_release.sync_digital_releases(None))

    def test_complete_sync_returns_the_count(self):
        added, result = self._run([[_post("tt0000001", 100)]])
        self.assertEqual(result, 1)
        self.assertEqual(added, [("tt0000001", 100)])

    def test_empty_feed_is_a_successful_sync(self):
        added, result = self._run([[]])
        self.assertEqual((added, result), ([], 0))

    def test_a_failed_write_is_a_failed_sync(self):
        async def one_page(client, after_ts, before_ts, _pages=[[_post("tt0000001", 100)]]):
            return _pages.pop(0)

        with mock.patch.object(digital_release, "_fetch_page", one_page), \
             mock.patch.object(digital_release, "add_digital_releases",
                               side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                asyncio.run(digital_release.sync_digital_releases(None))

    def test_failed_first_page_raises(self):
        with self.assertRaises(RuntimeError):
            self._run([None])

    def test_failure_after_a_full_page_keeps_what_was_fetched_and_raises(self):
        full = [_post(f"tt{i:07d}", 1000 - i) for i in range(digital_release._LIMIT)]
        kept = []

        async def fake_fetch(client, after_ts, before_ts, _pages=[full, None]):
            return _pages.pop(0)

        with mock.patch.object(digital_release, "_fetch_page", fake_fetch), \
             mock.patch.object(digital_release, "_PAGE_PAUSE", 0), \
             mock.patch.object(digital_release, "add_digital_releases",
                               side_effect=lambda e: kept.extend(e) or len(e)):
            with self.assertRaises(RuntimeError):
                asyncio.run(digital_release.sync_digital_releases(None))
        self.assertEqual(len(kept), digital_release._LIMIT)


class PollLoopTests(unittest.TestCase):
    def _loop_until_first_sleep(self, last_sync, sync_side_effect=None, leader=True):
        """Run the poll loop until it first sleeps after a sync attempt (or
        before the first one), and report what it did."""
        events = {"sleeps": [], "stamped": [], "synced": 0}
        ready = None
        current = None

        async def scenario():
            nonlocal ready, current
            ready = asyncio.Event()
            current = asyncio.Event()

            async def fake_sleep(seconds):
                # Stop at the first sleep: either the initial wait (current
                # set) or the one after the first sync attempt.
                events["sleeps"].append(seconds)
                events["ready_at_sleep"] = ready.is_set()
                raise asyncio.CancelledError

            async def fake_sync(client):
                events["synced"] += 1
                if sync_side_effect:
                    raise sync_side_effect
                return 0

            async def lease(*a, **k):
                return "token" if leader else None

            async def refresh(*a, **k):
                return True

            async def release(*a, **k):
                return None

            with mock.patch.object(digital_release.asyncio, "sleep", fake_sleep), \
                 mock.patch.object(digital_release, "sync_digital_releases", fake_sync), \
                 mock.patch.object(digital_release, "get_app_state",
                                   return_value=None if last_sync is None else str(last_sync)), \
                 mock.patch.object(digital_release, "set_app_state",
                                   side_effect=lambda k, v: events["stamped"].append(k)), \
                 mock.patch.object(digital_release.coord, "try_acquire_lease", lease), \
                 mock.patch.object(digital_release.coord, "refresh_lease", refresh), \
                 mock.patch.object(digital_release.coord, "release_lease", release):
                with self.assertRaises(asyncio.CancelledError):
                    await digital_release.digital_release_poll_loop(None, ready, current)

        asyncio.run(scenario())
        events["ready"] = ready.is_set()
        events["current"] = current.is_set()
        return events

    def test_current_set_marks_ready_before_sleeping_and_does_not_sync(self):
        import time
        events = self._loop_until_first_sleep(time.time() - 600)
        self.assertTrue(events["ready_at_sleep"])
        self.assertTrue(events["current"])
        self.assertEqual(events["synced"], 0)
        self.assertGreater(events["sleeps"][0], 80000)

    def test_stale_set_syncs_without_waiting_and_records_it(self):
        events = self._loop_until_first_sleep(1.0)
        self.assertEqual(events["synced"], 1)
        self.assertEqual(events["stamped"], [digital_release._LAST_SYNC_KEY])
        self.assertTrue(events["ready"])
        self.assertTrue(events["current"])
        self.assertEqual(events["sleeps"], [digital_release._POLL_INTERVAL])

    def test_failed_sync_is_not_recorded_and_retries_within_the_hour(self):
        events = self._loop_until_first_sleep(1.0, sync_side_effect=RuntimeError("down"))
        self.assertEqual(events["synced"], 1)
        self.assertEqual(events["stamped"], [])
        self.assertTrue(events["ready"])
        # A feed outage means no newer set exists: renders are cached again
        # rather than held uncached for the whole outage.
        self.assertTrue(events["current"])
        self.assertEqual(events["sleeps"], [digital_release._RETRY_INTERVAL])

    def test_follower_with_a_stale_set_holds_renders_and_rechecks_soon(self):
        events = self._loop_until_first_sleep(1.0, leader=False)
        self.assertEqual(events["synced"], 0)
        self.assertTrue(events["ready"])          # the warmer is not held up
        self.assertFalse(events["current"])       # renders are, for now
        self.assertEqual(events["sleeps"], [digital_release._FOLLOWER_POLL])


class TrendingReplayTests(unittest.TestCase):
    """The trending cycle deletes each cached poster before replaying it, so it
    must not start while the digital-release set is catching up: those replays
    are deliberately not cached, and the posters would be lost."""

    def setUp(self):
        import main
        self.main = main
        self.saved = (main._cfg.SERVER_TMDB_KEY, main._digital_release_current,
                      main._TRENDING_RELEASE_WAIT_SECS)
        main._cfg.SERVER_TMDB_KEY = "key"

    def tearDown(self):
        (self.main._cfg.SERVER_TMDB_KEY, self.main._digital_release_current,
         self.main._TRENDING_RELEASE_WAIT_SECS) = self.saved

    def test_cycle_waits_for_the_release_set_before_deleting_anything(self):
        calls = []

        async def candidates(*a, **k):
            calls.append("fetch")
            return []

        async def scenario():
            ev = asyncio.Event()
            self.main._digital_release_current = ev
            task = asyncio.create_task(self.main._run_trending_fetch_cycle(None))
            await asyncio.sleep(0.05)
            self.assertEqual(calls, [])          # held while catching up
            ev.set()
            return await task

        with mock.patch.object(self.main, "fetch_trending_candidates", candidates):
            asyncio.run(scenario())
        self.assertEqual(calls, ["fetch"])

    def test_cycle_is_deferred_if_the_set_never_catches_up(self):
        calls = []

        async def candidates(*a, **k):
            calls.append("fetch")
            return []

        async def scenario():
            self.main._digital_release_current = asyncio.Event()
            return await self.main._run_trending_fetch_cycle(None)

        self.main._TRENDING_RELEASE_WAIT_SECS = 0.05
        with mock.patch.object(self.main, "fetch_trending_candidates", candidates):
            self.assertEqual(asyncio.run(scenario()), self.main._TRENDING_DEFERRED)
        self.assertEqual(calls, [])


class DigitalReleasePendingTests(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main
        self.saved = (main._digital_release_ready, main._digital_release_current)

    def tearDown(self):
        self.main._digital_release_ready, self.main._digital_release_current = self.saved

    def test_outside_the_lifespan_nothing_is_pending(self):
        self.main._digital_release_current = None
        self.assertFalse(self.main._digital_release_pending())

    def test_pending_until_the_set_is_current(self):
        ev = asyncio.Event()
        self.main._digital_release_current = ev
        self.assertTrue(self.main._digital_release_pending())
        ev.set()
        self.assertFalse(self.main._digital_release_pending())


class SqliteWriteFailureTests(unittest.TestCase):
    def test_add_digital_releases_raises_on_a_db_error(self):
        from storage import sqlite_backend

        class _Broken:
            def executemany(self, *a, **k):
                raise RuntimeError("database is locked")
            def execute(self, *a, **k):
                raise RuntimeError("database is locked")
            def commit(self):
                raise RuntimeError("database is locked")

        with mock.patch.object(sqlite_backend, "get_db", return_value=_Broken()):
            with self.assertRaises(RuntimeError):
                sqlite_backend.add_digital_releases([("tt0000001", 100)])


class LifespanTeardownTests(unittest.TestCase):
    def test_a_finished_lifespan_leaves_nothing_pending(self):
        """A TestClient that closes before the startup sync finishes used to
        leave an unset event behind, so later route calls without a lifespan
        treated every render as pending and stopped persisting them."""
        from fastapi.testclient import TestClient
        import main

        with TestClient(main.app):
            self.assertIsNotNone(main._digital_release_current)
        self.assertIsNone(main._digital_release_current)
        self.assertIsNone(main._digital_release_ready)
        self.assertFalse(main._digital_release_pending())


if __name__ == "__main__":
    unittest.main()
