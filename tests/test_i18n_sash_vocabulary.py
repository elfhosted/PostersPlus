import json
import unittest
from pathlib import Path

from festivals import FESTIVAL_SASH_LABELS
from i18n import load_languages, translate_sash


LANGUAGE_DIR = Path(__file__).resolve().parents[1] / "languages"

RELEASE_STATUS_LABELS = {
    "Physical",
    "Streaming",
    "Cinema",
    "Production",
    "Airing",
    "Ended",
    "Cancelled",
}

FIXED_SASH_LABELS = RELEASE_STATUS_LABELS | FESTIVAL_SASH_LABELS


def _load_language(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


class FixedSashVocabularyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_languages()

    def test_canonical_locale_documents_all_fixed_sash_labels(self):
        english = _load_language(LANGUAGE_DIR / "en.json")
        self.assertTrue(FIXED_SASH_LABELS <= english["sashLabels"].keys())

    def test_every_shipped_translation_includes_all_fixed_sash_labels(self):
        for path in LANGUAGE_DIR.glob("*.json"):
            if path.name == "en.json":
                continue
            with self.subTest(language=path.stem):
                language = _load_language(path)
                self.assertTrue(
                    FIXED_SASH_LABELS <= language["sashLabels"].keys(),
                    f"{path.name} is missing fixed sash translations",
                )

    def test_release_status_translation_uses_the_new_locale_entries(self):
        self.assertEqual(translate_sash("Cinema", "fr-FR"), "Au cinéma")
        self.assertEqual(translate_sash("Airing", "es-MX"), "En emisión")
        self.assertEqual(translate_sash("Physical", "pt-BR"), "Mídia Física")

    def test_festival_translation_uses_the_new_locale_entries(self):
        self.assertEqual(translate_sash("Golden Lion", "it-IT"), "Leone d'oro")
        self.assertEqual(translate_sash("Golden Bear", "es-ES"), "Oso de Oro")

    def test_the_weaker_festival_claim_is_translated_too(self):
        # Tier two carries as much of the poster as the top prize does, so a
        # missing translation here would render an English sash on a French one.
        self.assertEqual(translate_sash("Cannes Winner", "fr-FR"), "Primé à Cannes")
        self.assertEqual(translate_sash("Venice Winner", "it-IT"), "Premio Venezia")
        self.assertEqual(translate_sash("Sundance Winner", "pt-BR"), "Prêmio Sundance")

    def test_dropped_festivals_are_gone_from_every_locale(self):
        # Toronto, Busan, Rotterdam, SXSW and Tribeca named prizes we had no
        # way to verify.  A leftover entry is a sash waiting to come back.
        retired = {"People's Choice", "New Currents", "Tiger Award",
                   "SXSW Jury", "Tribeca AA"}
        for path in LANGUAGE_DIR.glob("*.json"):
            with self.subTest(language=path.stem):
                labels = _load_language(path)["sashLabels"].keys()
                self.assertFalse(retired & labels, f"{path.name} still lists a retired prize")


if __name__ == "__main__":
    unittest.main()
