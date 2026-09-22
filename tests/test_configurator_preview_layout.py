from pathlib import Path
import re
import unittest


class ConfiguratorPreviewLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = Path("configurator.html").read_text(encoding="utf-8")

    def test_preview_tail_is_an_error_only_notice(self):
        # The console strip that used to print progress and success is gone —
        # the action buttons carry their own tooltips — so the only thing left
        # under the poster is a failure notice. These assertions are the guard
        # against the strip, its heading or the recipe row coming back.
        self.assertIn('id="preview-error"', self.html)
        self.assertNotIn('class="preview-console"', self.html)
        self.assertNotIn('id="preview-log"', self.html)
        self.assertNotIn('preview-console-head', self.html)
        self.assertNotIn('System Output', self.html)
        self.assertNotIn('class="preview-recipe"', self.html)
        self.assertNotIn("updatePreviewRecipe", self.html)

    def test_form_controls_share_one_radius_across_browsers(self):
        # The redesign rounded the controls, so the value is no longer 0 — but
        # the point of the rule is unchanged: every control takes the same
        # radius from one token, and the UA's own styling is suppressed so
        # Safari and Firefox can't substitute their defaults.
        self.assertRegex(
            self.html,
            r"input\[type=\"text\"\],\s*input\[type=\"number\"\],\s*(?:textarea,\s*)?select\s*\{"
            r"[^}]*border-radius:\s*var\(--radius-sm\);",
        )
        self.assertRegex(
            self.html,
            r"input\[type=\"text\"\],\s*input\[type=\"number\"\],\s*(?:textarea,\s*)?select\s*\{"
            r"[^}]*appearance:\s*none;\s*-webkit-appearance:\s*none;",
        )

    def test_select_focus_preserves_dropdown_arrow(self):
        self.assertIn("transition: border-color 0.15s, background-color 0.15s;", self.html)
        self.assertRegex(
            self.html,
            r"select:focus\s*\{[^}]*background-color:\s*var\(--black3\)",
        )
        self.assertNotRegex(
            self.html,
            r"select:focus\s*\{[^}]*\bbackground:\s*var\(--black3\)",
        )

    def test_error_notice_sits_on_the_preview_background(self):
        # The console strip painted its own background so it read as a separate
        # panel. Its replacement must not: the failure text belongs on the
        # preview surface, directly under where the poster renders.
        match = re.search(r"\.preview-error\s*\{([^}]*)\}", self.html)
        self.assertIsNotNone(match, ".preview-error rule not found")
        self.assertNotRegex(match.group(1), r"\bbackground(-color)?\s*:")

    def test_error_notice_takes_no_space_when_empty(self):
        # It used to reserve two lines so the layout didn't jump when a message
        # arrived. The notice now collapses entirely instead, which is why it
        # ships with the hidden attribute set.
        self.assertRegex(
            self.html, r'<div class="preview-error" id="preview-error" hidden>'
        )
        self.assertRegex(
            self.html, r"\.preview-error\[hidden\]\s*\{\s*display:\s*none;\s*\}"
        )

    def test_preview_actions_sit_between_the_notice_and_the_url_row(self):
        # The three actions moved out of the old metadata row into their own
        # equal-thirds row. Order still matters: the buttons come after the
        # failure notice and before the URL field.
        for handler in ("copyUrl()", "openPresetModal()", "resetDefaults()"):
            with self.subTest(handler=handler):
                self.assertIn(handler, self.html)
        for label in ("Copy config", "Load preset", "Reset config"):
            with self.subTest(label=label):
                self.assertIn(f">{label}</button>", self.html)

        frame = self.html.index('<div class="preview-frame"')
        error = self.html.index('<div class="preview-error"', frame)
        actions = self.html.index('<div class="preview-actions">', frame)
        controls = self.html.index('<div class="preview-controls">', frame)
        self.assertLess(error, actions)
        self.assertLess(actions, controls)

    def test_panels_use_the_available_desktop_height(self):
        self.assertIn("height: max(680px, calc(100vh - 144px));", self.html)
        self.assertIn("max-height: max(680px, calc(100vh - 144px));", self.html)
        self.assertIn("grid-template-rows: minmax(0, 1fr);", self.html)
        self.assertRegex(self.html, r"\.left-col\s*\{[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;")
        self.assertRegex(self.html, r"\.right-col\s*\{[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;")
        self.assertIn("#tab-host {\n    flex: 1;\n    min-height: 0;", self.html)
        self.assertNotIn("max-height: calc(100vh - 180px)", self.html)
        self.assertNotIn("_lockTabHeight", self.html)
        self.assertIn("if (host) host.scrollTop = 0;", self.html)

    def test_live_preview_corner_notch_is_restored(self):
        right = self.html.index("<!-- RIGHT: PREVIEW -->")
        header = self.html.index('<div class="panel-header">', right)
        self.assertIn('class="notch"', self.html[right:header])

    def test_desktop_trailing_padding_is_reduced(self):
        self.assertIn("padding: 0 28px 28px;", self.html)
        self.assertNotIn("padding: 0 28px 80px;", self.html)


if __name__ == "__main__":
    unittest.main()
