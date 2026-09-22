import time
import unittest

import httpx

import main
import ratings


class MDBListBackoffTests(unittest.TestCase):
    def setUp(self):
        self.server_keys = main._cfg.SERVER_MDBLIST_KEYS
        self.active_key_idx = main._mdblist_active_key_idx
        main._rating_backoff.clear()
        main._rating_fail_count.clear()
        main._mdblist_key_cooldown.clear()

    def tearDown(self):
        main._cfg.SERVER_MDBLIST_KEYS = self.server_keys
        main._mdblist_active_key_idx = self.active_key_idx
        main._rating_backoff.clear()
        main._rating_fail_count.clear()
        main._mdblist_key_cooldown.clear()

    def test_replacement_key_is_not_blocked_by_title_backoff(self):
        title = "tt11347692"
        first_key = main._rating_retry_key(title, "exhausted-key")
        replacement_key = main._rating_retry_key(title, "healthy-key")

        main._rating_backoff[first_key] = 3600.0

        self.assertIn(first_key, main._rating_backoff)
        self.assertNotIn(replacement_key, main._rating_backoff)

    def test_failure_escalation_is_independent_per_key(self):
        title = "tt11347692"
        first_key = main._rating_retry_key(title, "key-1")
        second_key = main._rating_retry_key(title, "key-2")

        main._rating_fail_count[first_key] = 3

        self.assertEqual(main._rating_fail_count[first_key], 3)
        self.assertEqual(main._rating_fail_count.get(second_key, 0), 0)

    def test_rotation_selects_next_healthy_server_key(self):
        main._cfg.SERVER_MDBLIST_KEYS = ["key-1", "key-2"]
        main._mdblist_key_cooldown["key-1"] = 100.0

        selected = main._next_mdblist_server_key("key-1", now=10.0)

        self.assertEqual(selected, "key-2")
        self.assertEqual(main._mdblist_active_key_idx, 1)

    def test_rotation_can_fall_back_to_primary_after_secondary_limit(self):
        main._cfg.SERVER_MDBLIST_KEYS = ["key-1", "key-2"]
        main._mdblist_key_cooldown["key-2"] = 100.0

        selected = main._next_mdblist_server_key("key-2", now=10.0)

        self.assertEqual(selected, "key-1")
        self.assertEqual(main._mdblist_active_key_idx, 0)
        self.assertEqual(main._mdblist_server_key_label(selected), "configured key #1")

    def test_rotation_does_not_replace_query_supplied_key(self):
        main._cfg.SERVER_MDBLIST_KEYS = ["key-1", "key-2"]
        self.assertIsNone(main._next_mdblist_server_key("user-key", now=10.0))

    def test_same_key_and_title_share_retry_state(self):
        self.assertEqual(
            main._rating_retry_key("tt11347692", "key-2"),
            main._rating_retry_key("tt11347692", "key-2"),
        )


class MDBListRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server_keys = main._cfg.SERVER_MDBLIST_KEYS
        main._cfg.SERVER_MDBLIST_KEYS = ["server-key-1", "server-key-2"]
        main._rating_backoff.clear()
        main._mdblist_key_cooldown.clear()

    def tearDown(self):
        main._cfg.SERVER_MDBLIST_KEYS = self.server_keys
        main._rating_backoff.clear()
        main._mdblist_key_cooldown.clear()

    async def test_query_key_rate_limit_does_not_select_server_fallback(self):
        result = main._RateLimited(retry_after=60)

        delay, fallback = main._mark_mdblist_rate_limit(
            "tt11347692", "request-key", result
        )

        self.assertEqual(delay, 60)
        self.assertIsNone(fallback)
        self.assertIn("request-key", main._mdblist_key_cooldown)

    async def test_quota_429_without_retry_after_sleeps_until_reset(self):
        reset_at = time.time() + 5 * 3600
        result = main._RateLimited(retry_after=None, reset_at=reset_at)

        delay, fallback = main._mark_mdblist_rate_limit(
            "tt11347692", "server-key-1", result
        )

        self.assertAlmostEqual(delay, 5 * 3600, delta=5)
        self.assertEqual(fallback, "server-key-2")

    async def test_retry_after_still_wins_over_reset(self):
        result = main._RateLimited(retry_after=60, reset_at=time.time() + 5 * 3600)

        delay, _ = main._mark_mdblist_rate_limit("tt11347692", "server-key-1", result)

        self.assertEqual(delay, 60)

    async def test_reset_in_the_past_still_cools_briefly(self):
        result = main._RateLimited(retry_after=None, reset_at=time.time() - 10)

        delay, _ = main._mark_mdblist_rate_limit("tt11347692", "server-key-1", result)

        self.assertEqual(delay, 60.0)


class MDBListQuotaTrackingTests(unittest.IsolatedAsyncioTestCase):
    """fetch_rating records X-RateLimit-* headers and the warmer honours them."""

    def setUp(self):
        ratings.MDBLIST_QUOTA.clear()

    def tearDown(self):
        ratings.MDBLIST_QUOTA.clear()

    @staticmethod
    def _client(status: int, headers: dict, body=None):
        async def handler(request):
            return httpx.Response(status, headers=headers, json=body if body is not None else {})
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_success_response_records_quota(self):
        reset = int(time.time()) + 3600
        async with self._client(200, {
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "647",
            "x-ratelimit-reset": str(reset),
        }, {"ratings": [], "keywords": []}) as client:
            result = await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertNotIsInstance(result, main._RateLimited)
        self.assertEqual(ratings.mdblist_quota_remaining("key-a"), 647)
        self.assertEqual(ratings.MDBLIST_QUOTA["key-a"].limit, 1000)
        self.assertEqual(ratings.MDBLIST_QUOTA["key-a"].reset_at, float(reset))

    async def test_quota_429_carries_reset_and_zero_remaining(self):
        reset = int(time.time()) + 3600
        async with self._client(429, {
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(reset),
        }) as client:
            result = await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertIsInstance(result, main._RateLimited)
        self.assertIsNone(result.retry_after)
        self.assertEqual(result.reset_at, float(reset))
        self.assertEqual(ratings.mdblist_quota_remaining("key-a"), 0)

    async def test_429_with_quota_left_does_not_carry_reset(self):
        async with self._client(429, {
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "412",
            "x-ratelimit-reset": str(int(time.time()) + 3600),
        }) as client:
            result = await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertIsInstance(result, main._RateLimited)
        self.assertIsNone(result.reset_at)
        self.assertEqual(ratings.mdblist_quota_remaining("key-a"), 412)

    async def test_stale_snapshot_is_unknown_after_reset(self):
        async with self._client(200, {
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "3",
            "x-ratelimit-reset": str(int(time.time()) - 1),
        }, {"ratings": [], "keywords": []}) as client:
            await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertIsNone(ratings.mdblist_quota_remaining("key-a"))

    async def test_missing_headers_leave_quota_unknown(self):
        async with self._client(200, {}, {"ratings": [], "keywords": []}) as client:
            await ratings.fetch_rating(client, "key-a", [], "movie", media_id="tt0111161")

        self.assertNotIn("key-a", ratings.MDBLIST_QUOTA)
        self.assertIsNone(ratings.mdblist_quota_remaining("key-a"))


class CacheWarmKeyFallbackTests(unittest.TestCase):
    """The warmer moves to key #2 when key #1 is limited or at its reserve."""

    def setUp(self):
        self.server_keys = main._cfg.SERVER_MDBLIST_KEYS
        self.active_key_idx = main._mdblist_active_key_idx
        main._cfg.SERVER_MDBLIST_KEYS = ["key-1", "key-2"]
        main._mdblist_key_cooldown.clear()
        ratings.MDBLIST_QUOTA.clear()

    def tearDown(self):
        main._cfg.SERVER_MDBLIST_KEYS = self.server_keys
        main._mdblist_active_key_idx = self.active_key_idx
        main._mdblist_key_cooldown.clear()
        ratings.MDBLIST_QUOTA.clear()

    @staticmethod
    def _quota(key, remaining):
        ratings.MDBLIST_QUOTA[key] = ratings.MDBListQuota(1000, remaining, time.time() + 3600, time.time())

    def test_keeps_current_key_when_quota_unknown(self):
        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 300), "key-1")

    def test_keeps_current_key_above_reserve(self):
        self._quota("key-1", 301)
        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 300), "key-1")

    def test_moves_to_sibling_at_reserve_without_changing_live_key(self):
        self._quota("key-1", 300)
        main._mdblist_active_key_idx = 0

        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 300), "key-2")
        self.assertEqual(main._mdblist_active_key_idx, 0)

    def test_moves_to_sibling_when_current_is_cooling_down(self):
        main._mdblist_key_cooldown["key-1"] = 100.0
        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 300), "key-2")

    def test_stops_when_every_key_is_spent_or_cooling(self):
        self._quota("key-1", 0)
        main._mdblist_key_cooldown["key-2"] = 100.0
        self.assertIsNone(main._warm_mdblist_key_with_quota("key-1", 10.0, 300))

    def test_reserve_zero_spends_down_to_the_last_request(self):
        self._quota("key-1", 1)
        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 0), "key-1")
        self._quota("key-1", 0)
        self.assertEqual(main._warm_mdblist_key_with_quota("key-1", 10.0, 0), "key-2")

    def test_rate_limit_then_reserve_chains_to_second_key(self):
        """Key #1 429s (rotates), later reaches reserve on key #2 -> stop."""
        async def run():
            result = main._RateLimited(retry_after=None, reset_at=time.time() + 3600)
            _, replacement = main._mark_mdblist_rate_limit("tt0111161", "key-1", result)
            self.assertEqual(replacement, "key-2")
            now = main.asyncio.get_running_loop().time()
            self.assertEqual(main._warm_mdblist_key_with_quota("key-2", now, 300), "key-2")
            self._quota("key-2", 250)
            self.assertIsNone(main._warm_mdblist_key_with_quota("key-2", now, 300))
        main.asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
