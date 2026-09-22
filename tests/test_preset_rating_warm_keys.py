"""Key selection in the /p background rating warm (ElfHosted fork).

Each queued warm must pick its MDBList key only once admitted, and must skip
a key that is cooling locally or fleet-wide. Otherwise a burst of queued warms
all carry the same key into the semaphore, and when the first is 429'd the
rest still spend it.
"""
import asyncio
import unittest
from unittest import mock

import config
import main
from awards import _RateLimited


class PresetRatingWarmKeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = (config.PRESET_MDBLIST_FETCH, config.SERVER_MDBLIST_KEYS,
                       main._HTTP_CLIENT, main._mdblist_semaphore,
                       main._mdblist_active_key_idx)
        config.PRESET_MDBLIST_FETCH = True
        config.SERVER_MDBLIST_KEYS = ["key-a", "key-b"]
        main._HTTP_CLIENT = object()
        main._mdblist_semaphore = asyncio.Semaphore(1)   # force queueing
        main._mdblist_active_key_idx = 0
        main._mdblist_key_cooldown.clear()
        main._rating_warm_pending.clear()
        main._rating_warm_failed_until.clear()

    async def asyncTearDown(self):
        (config.PRESET_MDBLIST_FETCH, config.SERVER_MDBLIST_KEYS,
         main._HTTP_CLIENT, main._mdblist_semaphore,
         main._mdblist_active_key_idx) = self._saved
        main._mdblist_key_cooldown.clear()

    async def _run(self, fetch, n=4):
        with mock.patch.object(main, "fetch_rating", fetch), \
             mock.patch.object(main, "set_cached_rating", mock.Mock()):
            for i in range(n):
                main._queue_rating_warm(f"tt000000{i}", f"tt000000{i}", str(i), "movie", [])
            await asyncio.gather(*list(main._rating_warm_tasks))
        return [c.args[1] for c in fetch.await_args_list]

    async def test_a_429_is_honoured_by_warms_already_queued(self):
        async def _fetch(client, key, *a, **k):
            # Yield like a real network call. Without it each warm runs to
            # completion before the next starts, nothing ever queues, and the
            # pick-before-admission bug this test exists for can't happen.
            await asyncio.sleep(0.01)
            if key == "key-a":
                return _RateLimited(retry_after=3600)
            return ({"imdb": 7.0}, "Drama", None, [], None)
        keys = await self._run(mock.AsyncMock(side_effect=_fetch))
        self.assertEqual(keys[0], "key-a")
        self.assertNotIn("key-a", keys[1:], keys)

    async def test_a_key_cooling_fleet_wide_is_skipped(self):
        fetch = mock.AsyncMock(return_value=({"imdb": 7.0}, "Drama", None, [], None))
        cooling = {main._fleet_cooldown_id("key-a")}

        async def _is_backoff(ns, key):
            return key in cooling
        with mock.patch.object(main.coord, "is_backoff_active", _is_backoff):
            keys = await self._run(fetch, n=2)
        self.assertEqual(keys, ["key-b", "key-b"])

    async def test_nothing_is_spent_when_every_key_is_cooling(self):
        fetch = mock.AsyncMock()

        async def _all_cooling(ns, key):
            return True
        with mock.patch.object(main.coord, "is_backoff_active", _all_cooling):
            await self._run(fetch, n=2)
        fetch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
