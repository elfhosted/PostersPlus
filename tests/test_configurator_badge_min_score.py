from pathlib import Path
import re
import unittest


# Switching quality display mode used to overwrite "Minimum Quality to Display"
# with that mode's default, throwing away a choice the user had already made.
# Badge *height* is still re-seeded on purpose — a bar and a badge want
# different heights — but a minimum quality means the same thing in every mode.
#
# These are source-shape assertions, not behavioural ones: the repo has no JS
# runtime, so they pin the four parts that have to agree rather than driving the
# widget.  Anything that reworks this logic should expect to rewrite them.
class BadgeMinScoreStickinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def _fn(self, name: str) -> str:
        match = re.search(rf"function {name}\(\)\s*\{{(.*?)\n\}}", self.html, re.S)
        self.assertIsNotNone(match, f"{name}() not found")
        return match.group(1)

    def test_mode_default_only_seeds_an_untouched_field(self):
        body = self._fn("updateBadgeModeHint")
        # The assignment must be guarded, not unconditional.
        self.assertNotRegex(
            body,
            r"getElementById\('cfg-badge-min-score'\)\.value\s*=",
            "the minimum is still assigned unconditionally on mode change",
        )
        self.assertRegex(body, r"if\s*\(!\s*_?\w*[Mm]inScore\.dataset\.userSet\)\s*_?\w*[Mm]inScore\.value\s*=")

    def test_badge_height_is_still_reseeded_per_mode(self):
        # Guard against "fixing" this one too — the per-mode heights are wanted.
        self.assertRegex(self._fn("updateBadgeModeHint"),
                         r"getElementById\('cfg-badge-h'\)\.value\s*=")

    def test_choosing_a_minimum_marks_it_user_set(self):
        select = re.search(
            r"<select id=\"cfg-badge-min-score\"[^>]*onchange=\"([^\"]*)\"", self.html
        )
        self.assertIsNotNone(select, "cfg-badge-min-score select not found")
        self.assertIn("dataset.userSet", select.group(1))

    def test_imported_minimum_is_marked_user_set(self):
        # An imported URL (and so the localStorage settings restore) is as
        # deliberate as a click, and must survive a later mode change.
        self.assertRegex(
            self.html,
            r"badge_min_score'\)\)\s*\{[^}]*cfg-badge-min-score'\)\.dataset\.userSet\s*=",
        )

    def test_both_reset_paths_clear_the_mark(self):
        # Reset to Defaults must re-arm the per-mode seeding; there are two
        # separate reset loops in this file and both have to forget the mark.
        self.assertEqual(
            len(re.findall(r"delete el\.dataset\.userSet;", self.html)), 2,
            "both form-reset loops must clear dataset.userSet",
        )


if __name__ == "__main__":
    unittest.main()
