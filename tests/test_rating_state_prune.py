"""Periodic sweep of the in-process rating back-off state.

Both dicts are keyed by (imdb_id, API key).  The request path keeps them in
lockstep — a failure writes both, an expiry on access deletes both, a success
clears both — so the only thing that could ever separate them is the sweep
itself, and for a long time it swept one and not the other.  A counter left
behind is never revisited unless that exact title is requested again, so on a
busy instance they accumulate for the life of the process.
"""

import unittest

import main


class PruneRatingStateTests(unittest.TestCase):
    def setUp(self):
        self._backoff = dict(main._rating_backoff)
        self._fails = dict(main._rating_fail_count)
        main._rating_backoff.clear()
        main._rating_fail_count.clear()

    def tearDown(self):
        main._rating_backoff.clear()
        main._rating_backoff.update(self._backoff)
        main._rating_fail_count.clear()
        main._rating_fail_count.update(self._fails)

    def test_expired_backoff_takes_its_counter_with_it(self):
        key = ("tt0000001", "apikey")
        main._rating_backoff[key] = 50.0
        main._rating_fail_count[key] = 3

        expired, orphans = main.prune_rating_state(100.0)

        self.assertEqual((expired, orphans), (1, 1))
        self.assertNotIn(key, main._rating_backoff)
        self.assertNotIn(key, main._rating_fail_count)

    def test_live_backoff_keeps_its_counter(self):
        # Escalation has to survive while the back-off is still running, or a
        # title that keeps failing restarts at the shortest retry interval.
        key = ("tt0000002", "apikey")
        main._rating_backoff[key] = 500.0
        main._rating_fail_count[key] = 2

        expired, orphans = main.prune_rating_state(100.0)

        self.assertEqual((expired, orphans), (0, 0))
        self.assertEqual(main._rating_fail_count[key], 2)

    def test_counter_stranded_by_an_earlier_sweep_is_collected(self):
        # What the old sweep left behind: the back-off went, the counter stayed,
        # and nothing requests that title again.
        stranded = ("tt0000003", "apikey")
        main._rating_fail_count[stranded] = 4

        expired, orphans = main.prune_rating_state(100.0)

        self.assertEqual((expired, orphans), (0, 1))
        self.assertEqual(main._rating_fail_count, {})

    def test_sweeping_repeatedly_leaves_nothing_behind(self):
        # The property that matters: however many titles fail, once their
        # back-offs expire the process holds no state for them at all.
        for i in range(50):
            key = (f"tt{i:07d}", "apikey")
            main._rating_backoff[key] = 10.0
            main._rating_fail_count[key] = 1

        main.prune_rating_state(100.0)
        main.prune_rating_state(200.0)

        self.assertEqual(main._rating_backoff, {})
        self.assertEqual(main._rating_fail_count, {})

    def test_keys_of_other_api_keys_are_independent(self):
        # The same title under a rotated MDBList key is a separate entry; the
        # sweep must not take a live one out with an expired sibling.
        live = ("tt0000004", "key-b")
        main._rating_backoff[("tt0000004", "key-a")] = 50.0
        main._rating_fail_count[("tt0000004", "key-a")] = 1
        main._rating_backoff[live] = 500.0
        main._rating_fail_count[live] = 1

        main.prune_rating_state(100.0)

        self.assertEqual(list(main._rating_backoff), [live])
        self.assertEqual(list(main._rating_fail_count), [live])


if __name__ == "__main__":
    unittest.main()
