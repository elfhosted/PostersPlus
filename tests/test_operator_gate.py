"""Fail-closed operator gate on a preset-enabled instance (ElfHosted fork).

The public instance runs PRESET_ENABLED with NO ACCESS_KEY and relies on that
closing /poster and every other endpoint that spends operator keys or exposes
operator data. Upstream's gate only fires when ACCESS_KEY is set, so under it
all of these were anonymous. The v1.1.0 rebuild dropped the fork's gate
without anyone noticing; these tests are what should have noticed.
"""
import unittest

from fastapi import HTTPException

import config
import main

# Endpoints that spend operator keys or expose operator data.
_GATED = ("/poster", "/stats", "/logo", "/debug/canvas", "/debug/fallback-gallery")


class OperatorGateTests(unittest.TestCase):
    def setUp(self):
        self._saved = (config.PRESET_ENABLED, config.ACCESS_KEY)

    def tearDown(self):
        config.PRESET_ENABLED, config.ACCESS_KEY = self._saved

    def _gate(self, access_key=""):
        main._require_operator(access_key)

    def test_public_tier_without_access_key_is_closed(self):
        config.PRESET_ENABLED, config.ACCESS_KEY = True, ""
        for supplied in ("", "anything"):
            with self.assertRaises(HTTPException) as ctx:
                self._gate(supplied)
            self.assertEqual(ctx.exception.status_code, 403)

    def test_public_tier_with_access_key_admits_the_key(self):
        config.PRESET_ENABLED, config.ACCESS_KEY = True, "sekrit"
        self._gate("sekrit")
        with self.assertRaises(HTTPException):
            self._gate("")
        with self.assertRaises(HTTPException):
            self._gate("wrong")

    def test_private_instance_behaves_like_upstream(self):
        config.PRESET_ENABLED, config.ACCESS_KEY = False, ""
        self._gate("")                       # open, as upstream
        config.ACCESS_KEY = "sekrit"
        self._gate("sekrit")
        with self.assertRaises(HTTPException):
            self._gate("")

    def test_non_ascii_key_is_a_403_not_a_500(self):
        config.PRESET_ENABLED, config.ACCESS_KEY = True, "sekrit"
        with self.assertRaises(HTTPException) as ctx:
            self._gate("ключ")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_every_gated_endpoint_is_closed_on_the_public_tier(self):
        """Through the real app, so a route that forgets the gate fails here
        even if _require_operator itself is correct."""
        from fastapi.testclient import TestClient
        config.PRESET_ENABLED, config.ACCESS_KEY = True, ""
        client = TestClient(main.app)            # no lifespan: gate runs first
        params = {"tmdb_id": "278", "type": "movie"}
        for path in _GATED:
            with self.subTest(path=path):
                self.assertEqual(client.get(path, params=params).status_code, 403, path)


if __name__ == "__main__":
    unittest.main()
