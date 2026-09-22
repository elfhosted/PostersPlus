from pathlib import Path
import re
import unittest


# Almost every configurator switch is off by default on both sides, so build()
# can omit its param when unchecked.  These are the exception: the renderer
# defaults them to *on*, so an omitted param means "on" and a switch that only
# writes itself when checked can never turn anything off.
DEFAULT_ON_PARAMS = ("vignette_color_ramp", "vignette_color_local", "release_status_dates")


class DefaultOnToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")
        cls.main = Path("main.py").read_text(encoding="utf-8")

    def test_renderer_still_defaults_these_on(self):
        # If one of these ever flips to False in RequestConfig the pairing below
        # stops being required — this test is what says so out loud.
        for param in DEFAULT_ON_PARAMS:
            with self.subTest(param=param):
                self.assertRegex(
                    self.main,
                    re.compile(rf"^\s*{param}:\s*bool\s*=\s*True\b", re.M),
                )

    def test_configurator_writes_both_states(self):
        for param in DEFAULT_ON_PARAMS:
            with self.subTest(param=param):
                match = re.search(rf"params\.set\(\s*'{param}',([^\n]*)", self.html)
                self.assertIsNotNone(match, f"{param} is never written by build()")
                self.assertIn("'false'", match.group(1),
                              f"{param} must be written as false when its switch is off")

    def test_configurator_reads_both_states_back(self):
        # The importer is what makes an off switch survive a paste or a reload.
        for param in DEFAULT_ON_PARAMS:
            with self.subTest(param=param):
                self.assertRegex(self.html, rf"p\.has\('{param}'\)")


if __name__ == "__main__":
    unittest.main()
