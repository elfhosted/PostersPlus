from pathlib import Path
import unittest

import main
from ratings import parse_custom_score_palette, score_color_for_mode


class CustomScorePaletteTests(unittest.TestCase):
    def test_parses_threshold_hex_entries(self):
        palette = parse_custom_score_palette(
            "0:#111111,50:DD4444\n70:#DDBB33;85:55CC88,bad:FFFFFF"
        )

        self.assertEqual(
            palette,
            [
                (0, (17, 17, 17)),
                (50, (221, 68, 68)),
                (70, (221, 187, 51)),
                (85, (85, 204, 136)),
            ],
        )
        self.assertEqual(score_color_for_mode(49, 3, palette)[0], (17, 17, 17))
        self.assertEqual(score_color_for_mode(50, 3, palette)[0], (221, 68, 68))
        self.assertEqual(score_color_for_mode(90, 3, palette)[0], (85, 204, 136))

    def test_request_config_accepts_custom_mode(self):
        cfg = main.build_request_config({
            "score_color_mode": "3",
            "score_custom_palette": "0:111111,80:ABCDEF",
            "bar_accent": "palette_custom",
        })

        self.assertEqual(cfg.score_color_mode, 3)
        self.assertEqual(cfg.score_custom_palette, [(0, (17, 17, 17)), (80, (171, 205, 239))])
        self.assertEqual(cfg.bar_accent, "palette_custom")

    def test_configurator_round_trips_custom_palette(self):
        html = Path("configurator.html").read_text(encoding="utf-8")

        self.assertIn('<option value="3">Custom</option>', html)
        self.assertIn('<option value="palette_custom">Custom</option>', html)
        self.assertIn("params.set('score_custom_palette', _customPalette)", html)
        self.assertIn("p.has('score_custom_palette')", html)


if __name__ == "__main__":
    unittest.main()
