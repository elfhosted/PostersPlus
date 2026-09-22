import unittest

from tvdb import _logo_language_order, _select_by_language, _to_tvdb_lang


class ToTvdbLangTests(unittest.TestCase):
    def test_bare_codes_map_to_iso_639_2(self):
        self.assertEqual(_to_tvdb_lang("es"), "spa")
        self.assertEqual(_to_tvdb_lang("fr"), "fra")
        self.assertEqual(_to_tvdb_lang("en"), "eng")

    def test_region_qualified_locales_collapse_to_base_language(self):
        self.assertEqual(_to_tvdb_lang("es-es"), "spa")
        self.assertEqual(_to_tvdb_lang("es-mx"), "spa")
        self.assertEqual(_to_tvdb_lang("fr-fr"), "fra")

    def test_locale_separators_and_casing_are_normalised(self):
        self.assertEqual(_to_tvdb_lang("es_MX"), "spa")
        self.assertEqual(_to_tvdb_lang("  ES-mx "), "spa")

    def test_unmapped_codes_pass_through_without_region(self):
        self.assertEqual(_to_tvdb_lang("xx"), "xx")
        self.assertEqual(_to_tvdb_lang("xx-yy"), "xx")

    def test_empty_input_is_preserved(self):
        self.assertIsNone(_to_tvdb_lang(None))
        self.assertEqual(_to_tvdb_lang(""), "")


class LogoLanguageOrderTests(unittest.TestCase):
    def test_region_qualified_request_prefers_base_language_artwork(self):
        self.assertEqual(
            _logo_language_order("es-mx", "en", "native_original"),
            ["spa", "eng"],
        )

    def test_locale_and_base_collapsing_onto_one_code_is_deduplicated(self):
        # A Mexican-Spanish request for a Spain-original title: TMDB ordering
        # yields es-mx then es, both of which are just "spa" to TVDB. Neither
        # tier is English, so "spa" is the whole preference list.
        self.assertEqual(
            _logo_language_order("es-mx", "es", "native_original"),
            ["spa"],
        )


class SelectByLanguageTests(unittest.TestCase):
    def test_region_locale_now_matches_spanish_artwork(self):
        spanish = {"language": "spa", "url": "/spanish.png"}
        neutral = {"language": None, "url": "/neutral.png"}
        english = {"language": "eng", "url": "/english.png"}
        items = [spanish, neutral, english]

        for locale in ("es-es", "es-mx"):
            with self.subTest(locale=locale):
                chosen = _select_by_language(
                    items, [_to_tvdb_lang(locale)], strict=True
                )
                self.assertEqual(chosen, spanish)

    def test_strict_selection_still_declines_unrelated_languages(self):
        items = [{"language": "deu", "url": "/german.png"}]
        self.assertIsNone(
            _select_by_language(items, [_to_tvdb_lang("es-mx")], strict=True)
        )


if __name__ == "__main__":
    unittest.main()
