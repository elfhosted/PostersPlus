import unittest

import numpy as np
from PIL import Image

from main import RequestConfig, build_poster
from tmdb import LOGO_ABS_MAX_H


def _bright_text_bbox(image: Image.Image) -> tuple[int, int, int, int]:
    arr = np.array(image.convert("RGBA"))
    mask = (arr[:, :, 0] > 150) & (arr[:, :, 1] > 150) & (arr[:, :, 2] > 150)
    ys, xs = np.nonzero(mask)
    if len(xs) == 0 or len(ys) == 0:
        raise AssertionError("fallback title text was not rendered")
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


class FallbackLogoTextTests(unittest.TestCase):
    def _render(self, cfg: RequestConfig) -> Image.Image:
        poster = Image.new("RGBA", (500, 750), (10, 10, 10, 255))
        return build_poster(
            poster,
            "N/A",
            "Crime",
            cfg,
            fallback_title="FIFA WORLD CUP 2026 PREVIEW SERIES",
            quality_tokens=[],
        )

    def test_bottom_anchored_fallback_text_expands_up_from_logo_baseline(self):
        cfg = RequestConfig(
            rating_display_mode=0,
            top_gradient="off",
            bottom_gradient="off",
            logo_bottom_anchor=True,
            logo_bottom_ratio=0.16,
            logo_max_w_ratio=0.50,
            logo_max_h_ratio=0.12,
        )

        x1, y1, x2, y2 = _bright_text_bbox(self._render(cfg))
        baseline = 750 - int(750 * cfg.logo_bottom_ratio)
        max_w = int(500 * cfg.logo_max_w_ratio)
        max_h = min(int(750 * cfg.logo_max_h_ratio), LOGO_ABS_MAX_H)

        self.assertLessEqual(y2, baseline)
        self.assertGreaterEqual(y1, baseline - max_h)
        self.assertLessEqual(x2 - x1, max_w)
        self.assertLessEqual(y2 - y1, max_h)

    def test_centered_fallback_text_uses_logo_sized_envelope(self):
        cfg = RequestConfig(
            rating_display_mode=0,
            top_gradient="off",
            bottom_gradient="off",
            logo_bottom_anchor=False,
            logo_max_w_ratio=0.45,
            logo_max_h_ratio=0.10,
        )

        x1, y1, x2, y2 = _bright_text_bbox(self._render(cfg))
        max_w = int(500 * cfg.logo_max_w_ratio)
        max_h = min(int(750 * cfg.logo_max_h_ratio), LOGO_ABS_MAX_H)

        self.assertLessEqual(x2 - x1, max_w)
        self.assertLessEqual(y2 - y1, max_h)


if __name__ == "__main__":
    unittest.main()
