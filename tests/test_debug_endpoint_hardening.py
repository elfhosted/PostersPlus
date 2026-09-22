"""Hardening of upstream's debug endpoints (ElfHosted fork; CodeQL on #49).

Both are reachable unauthenticated on an instance with no ACCESS_KEY and no
PRESET_ENABLED — upstream's default posture.
"""
import os
import unittest

from fastapi.testclient import TestClient

import config
import main


class DebugEndpointHardeningTests(unittest.TestCase):
    def setUp(self):
        self._saved = (config.PRESET_ENABLED, config.ACCESS_KEY)
        config.PRESET_ENABLED, config.ACCESS_KEY = False, ""
        self.client = TestClient(main.app)

    def tearDown(self):
        config.PRESET_ENABLED, config.ACCESS_KEY = self._saved

    def test_gallery_does_not_reflect_markup_from_access_key(self):
        payload = '"><script>alert(1)</script>'
        body = self.client.get(
            "/debug/fallback-gallery", params={"access_key": payload}
        ).text
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertNotIn('"><script', body)

    def test_genre_background_paths_cannot_escape_the_asset_dir(self):
        # The old code only returned a path that EXISTS, so the target has to
        # be a real .png outside the asset dir — a nonexistent one would read
        # as None either way and prove nothing.
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png") as outside:
            style_dir = os.path.join(main._GENRE_BG_DIR, "minimal")
            rel = os.path.relpath(outside.name, style_dir)[:-len(".png")]
            self.assertTrue(rel.startswith(".."))
            for genre in (rel, outside.name[:-len(".png")]):
                with self.subTest(genre=genre):
                    self.assertIsNone(main._genre_bg_path("minimal", genre))

    def test_every_shipped_genre_background_still_resolves(self):
        base = main._GENRE_BG_DIR
        for style in main._GENRE_BG_STYLES:
            for f in os.listdir(os.path.join(base, style)):
                if f.endswith(".png"):
                    with self.subTest(path=f"{style}/{f}"):
                        self.assertIsNotNone(main._genre_bg_path(style, f[:-4]))


if __name__ == "__main__":
    unittest.main()
