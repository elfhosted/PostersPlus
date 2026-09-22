"""presets.py must match the configurator gallery (ElfHosted fork).

/p/<id> promises the same look as picking that gallery card, so every id the
catalogue advertises has to carry exactly the gallery's params. This caught a
real bug: presets.py once listed clean_notch/clean_sash twice (current and
retired), and a dict literal keeps the LAST duplicate — so both cards silently
rendered their old v1.1.0 looks.
"""
import ast
import os
import re
import unittest
from urllib.parse import parse_qsl

import presets

_ROOT = os.path.dirname(os.path.dirname(__file__))


def _gallery() -> dict[str, dict[str, str]]:
    html = open(os.path.join(_ROOT, "configurator.html")).read()
    block = html[html.index("const PRESETS = ["):]
    block = block[:block.index("\n];")]
    return {
        pid: dict(parse_qsl(params, keep_blank_values=True))
        for pid, params in re.findall(
            r"id:\s*'([^']+)'.*?params:\s*'([^']*)'", block, re.S
        )
    }


class PresetGalleryTests(unittest.TestCase):
    def test_every_catalogue_preset_matches_its_gallery_card(self):
        gallery = _gallery()
        self.assertTrue(gallery)
        for entry in presets.preset_catalog():
            with self.subTest(preset=entry["id"]):
                self.assertIn(entry["id"], gallery)
                self.assertEqual(presets.get_preset(entry["id"]), gallery[entry["id"]])

    def test_every_gallery_card_is_in_the_catalogue(self):
        catalogue = {p["id"] for p in presets.preset_catalog()}
        self.assertEqual(set(_gallery()), catalogue)

    def test_no_preset_id_is_defined_twice(self):
        tree = ast.parse(open(os.path.join(_ROOT, "presets.py")).read())
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "PRESETS":
                keys = [k.value for k in node.value.keys]
                self.assertEqual(len(keys), len(set(keys)), "duplicate preset id")
                return
        self.fail("PRESETS literal not found")


if __name__ == "__main__":
    unittest.main()
