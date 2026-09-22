import inspect
from pathlib import Path
import unittest

import awards
import main


class NotchInsetDefaultTests(unittest.TestCase):
    def test_backend_request_default(self):
        self.assertEqual(main.RequestConfig().sash_badge_inset, 0.0)
        self.assertEqual(main.build_request_config({}).sash_badge_inset, 0.0)

    def test_drawing_helper_defaults(self):
        self.assertEqual(
            inspect.signature(awards.draw_award_badge)
            .parameters["notch_inset"].default,
            0.004,
        )

    def test_removed_text_offset_is_ignored(self):
        cfg = main.build_request_config({"sash_badge_notch_offset": "0.5"})
        self.assertFalse(hasattr(cfg, "sash_badge_notch_offset"))
        self.assertNotIn("notch_text_offset", inspect.signature(awards.draw_award_badge).parameters)

    def test_configurator_default_and_removed_text_offset(self):
        html = Path("configurator.html").read_text(encoding="utf-8")
        self.assertIn('id="cfg-sash-badge-inset" min="-0.020" max="0.020" step="0.001" value="0.000"', html)
        self.assertNotIn("sash_badge_notch_offset", html)


if __name__ == "__main__":
    unittest.main()
