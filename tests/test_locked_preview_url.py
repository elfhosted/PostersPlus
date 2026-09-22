"""The locked (public-tier) configurator must never preview via /poster.

A preset-only instance closes /poster, so the old fallback — used until a
preset was picked — rendered the preview as a 403. Reported from the live
site, with Referer https://postersplus.elfhosted.com/.

The functions are executed with node rather than pattern-matched, so this
tests behaviour rather than wording.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

_ROOT = os.path.dirname(os.path.dirname(__file__))
_HTML = open(os.path.join(_ROOT, "configurator.html")).read()


def _fn(name):
    i = _HTML.index(f"function {name}(")
    depth, k = 0, _HTML.index("{", i)
    while True:
        if _HTML[k] == "{":
            depth += 1
        elif _HTML[k] == "}":
            depth -= 1
            if depth == 0:
                return _HTML[i:k + 1]
        k += 1


@unittest.skipUnless(shutil.which("node"), "node not available")
class LockedPreviewUrlTests(unittest.TestCase):
    def _run(self, *, lock, preset, resolved="tt0111161", presets=("clean_notch", "mini")):
        harness = f"""
        let lockModeActive = {json.dumps(lock)};
        let lockedPresetId = {json.dumps(preset)};
        let resolvedImdbId = {json.dumps(resolved)};
        let mediaType = 'movie';
        const PRESETS = {json.dumps([{"id": p} for p in presets])};
        let serverCaps = {{ presets: PRESETS }};
        const _REWRITE_DOMAIN = false;
        const window = {{ location: {{ origin: 'https://pp.example', port: '' }} }};
        function vEl() {{ return ''; }}
        function buildBaseParams() {{ return 'https://pp.example/poster?tmdb_id=278'; }}
        {_fn('defaultLockedPresetId')}
        {_fn('buildLockedPreviewUrl')}
        {_fn('buildPreviewUrl')}
        console.log(JSON.stringify(buildPreviewUrl()));
        """
        out = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip())

    def test_locked_without_a_chosen_preset_still_previews_via_p(self):
        url = self._run(lock=True, preset="")
        self.assertIsNotNone(url)
        self.assertNotIn("/poster", url)
        self.assertEqual(url, "https://pp.example/p/clean_notch/movie/tt0111161.jpg")

    def test_locked_uses_the_chosen_preset(self):
        url = self._run(lock=True, preset="mini")
        self.assertEqual(url, "https://pp.example/p/mini/movie/tt0111161.jpg")

    def test_locked_with_no_catalogue_renders_nothing_rather_than_poster(self):
        self.assertIsNone(self._run(lock=True, preset="", presets=()))

    def test_unlocked_still_uses_poster(self):
        self.assertIn("/poster", self._run(lock=False, preset=""))

    def test_locked_mode_selects_a_default_preset_on_entry(self):
        self.assertIn("defaultLockedPresetId()", _fn("applyLockMode"))


if __name__ == "__main__":
    unittest.main()
