import asyncio
import base64
import unittest
from unittest.mock import patch


from i18n import load_languages, translate_genre, translate_sash
from tmdb import (
    _image_language_keys,
    _image_matches_language,
    _tmdb_include_image_languages,
    fetch_logo,
    image_language_order,
)


class _FakeImageResponse:
    content = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
        "/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )

    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self):
        self.urls = []

    async def get(self, url):
        self.urls.append(url)
        return _FakeImageResponse()


class ImageLanguageOrderTests(unittest.TestCase):
    def test_native_content_keeps_native_language_first(self):
        self.assertEqual(
            image_language_order("fr", "fr", "native_if_original_english"),
            ["fr", "en"],
        )

    def test_foreign_content_prefers_english_then_original(self):
        for original_language in ("ko", "ja", "ru", "zh"):
            with self.subTest(original_language=original_language):
                self.assertEqual(
                    image_language_order(
                        "fr", original_language, "native_if_original_english"
                    ),
                    ["en", original_language],
                )

    def test_existing_priorities_are_unchanged(self):
        self.assertEqual(
            image_language_order("fr", "ja", "native_original"),
            ["fr", "ja"],
        )
        self.assertEqual(
            image_language_order("fr", "ja", "original_native"),
            ["ja", "fr"],
        )
        self.assertEqual(
            image_language_order("fr", "ja", "native_text"),
            ["fr"],
        )

    def test_duplicate_languages_are_only_tried_once(self):
        self.assertEqual(
            image_language_order("en", "en", "native_if_original_english"),
            ["en"],
        )

    def test_region_qualified_french_does_not_fall_back_to_bare_french_art(self):
        self.assertEqual(
            image_language_order("fr-fr", "en", "native_original"),
            ["fr-fr", "en"],
        )
        self.assertNotIn(
            "fr",
            image_language_order("fr-fr", "en", "native_original"),
        )

    def test_tmdb_language_region_images_match_locale_requests(self):
        france = {"iso_639_1": "fr", "iso_3166_1": "FR"}
        canada = {"iso_639_1": "fr", "iso_3166_1": "CA"}
        generic = {"iso_639_1": "fr", "iso_3166_1": None}

        self.assertEqual(_image_language_keys(france), ["fr-fr", "fr"])
        self.assertTrue(_image_matches_language(france, "fr-fr"))
        self.assertFalse(_image_matches_language(canada, "fr-fr"))
        self.assertFalse(_image_matches_language(generic, "fr-fr"))
        self.assertTrue(_image_matches_language(canada, "fr"))

    def test_region_qualified_fetch_includes_base_language_for_tmdb(self):
        self.assertEqual(
            _tmdb_include_image_languages("fr-fr"),
            ["fr-fr", "fr", "en", "null"],
        )
        self.assertEqual(
            _tmdb_include_image_languages("fr"),
            ["fr", "en", "null"],
        )
        self.assertEqual(
            _tmdb_include_image_languages("en"),
            ["en", "null"],
        )

    def test_native_text_uses_english_before_neutral_logo(self):
        async def run_case():
            client = _FakeClient()
            logos = [
                {
                    "file_path": "/neutral-native-text-test.png",
                    "iso_639_1": None,
                    "vote_average": 99,
                },
                {
                    "file_path": "/english-native-text-test.png",
                    "iso_639_1": "en",
                    "iso_3166_1": "US",
                    "vote_average": 1,
                },
            ]
            with patch("tmdb.get_cached_tmdb_logo", return_value=None), patch(
                "tmdb.set_cached_tmdb_logo"
            ):
                await fetch_logo(
                    client,
                    logos,
                    logo_language="fr",
                    original_language="ja",
                    logo_priority="native_text",
                    use_metahub=False,
                )
            return client.urls[0]

        self.assertIn("/english-native-text-test.png", asyncio.run(run_case()))

    def test_other_priorities_keep_neutral_before_english_fallback(self):
        async def run_case():
            client = _FakeClient()
            logos = [
                {
                    "file_path": "/neutral-default-test.png",
                    "iso_639_1": None,
                    "vote_average": 1,
                },
                {
                    "file_path": "/english-default-test.png",
                    "iso_639_1": "en",
                    "iso_3166_1": "US",
                    "vote_average": 99,
                },
            ]
            with patch("tmdb.get_cached_tmdb_logo", return_value=None), patch(
                "tmdb.set_cached_tmdb_logo"
            ):
                await fetch_logo(
                    client,
                    logos,
                    logo_language="fr",
                    original_language="ja",
                    logo_priority="native_original",
                    use_metahub=False,
                )
            return client.urls[0]

        self.assertIn("/neutral-default-test.png", asyncio.run(run_case()))

    def test_native_text_uses_metahub_before_neutral_logo(self):
        async def run_case():
            logos = [
                {
                    "file_path": "/neutral-native-text-test.png",
                    "iso_639_1": None,
                    "vote_average": 99,
                },
            ]
            with patch("tmdb._fetch_metahub_logo", return_value="metahub") as metahub:
                result = await fetch_logo(
                    _FakeClient(),
                    logos,
                    logo_language="fr",
                    imdb_id="tt1234567",
                    original_language="ja",
                    logo_priority="native_text",
                    use_metahub=True,
                )
            return result, metahub.called

        result, metahub_called = asyncio.run(run_case())
        self.assertEqual(result, "metahub")
        self.assertTrue(metahub_called)

    def test_native_text_uses_neutral_logo_after_metahub_miss(self):
        async def run_case():
            client = _FakeClient()
            logos = [
                {
                    "file_path": "/neutral-after-metahub-test.png",
                    "iso_639_1": None,
                    "vote_average": 99,
                },
            ]
            with patch("tmdb._fetch_metahub_logo", return_value=None), patch(
                "tmdb.get_cached_tmdb_logo", return_value=None
            ), patch("tmdb.set_cached_tmdb_logo"):
                await fetch_logo(
                    client,
                    logos,
                    logo_language="fr",
                    imdb_id="tt1234567",
                    original_language="ja",
                    logo_priority="native_text",
                    use_metahub=True,
                )
            return client.urls[0]

        self.assertIn("/neutral-after-metahub-test.png", asyncio.run(run_case()))

    def test_region_qualified_spanish_does_not_cross_between_spain_and_mexico(self):
        spain = {"iso_639_1": "es", "iso_3166_1": "ES"}
        mexico = {"iso_639_1": "es", "iso_3166_1": "MX"}
        generic = {"iso_639_1": "es", "iso_3166_1": None}

        self.assertEqual(_image_language_keys(spain), ["es-es", "es"])
        self.assertEqual(_image_language_keys(mexico), ["es-mx", "es"])

        self.assertTrue(_image_matches_language(spain, "es-es"))
        self.assertFalse(_image_matches_language(mexico, "es-es"))
        self.assertTrue(_image_matches_language(mexico, "es-mx"))
        self.assertFalse(_image_matches_language(spain, "es-mx"))

        # Untagged Spanish art is not claimed by either region, but both are
        # still Spanish for a bare "es" request.
        self.assertFalse(_image_matches_language(generic, "es-es"))
        self.assertFalse(_image_matches_language(generic, "es-mx"))
        self.assertTrue(_image_matches_language(spain, "es"))
        self.assertTrue(_image_matches_language(mexico, "es"))

    def test_region_qualified_spanish_does_not_fall_back_to_bare_spanish_art(self):
        for locale in ("es-es", "es-mx"):
            with self.subTest(locale=locale):
                order = image_language_order(locale, "en", "native_original")
                self.assertEqual(order, [locale, "en"])
                self.assertNotIn("es", order)

    def test_region_qualified_spanish_fetch_includes_base_language_for_tmdb(self):
        self.assertEqual(
            _tmdb_include_image_languages("es-es"),
            ["es-es", "es", "en", "null"],
        )
        self.assertEqual(
            _tmdb_include_image_languages("es-mx"),
            ["es-mx", "es", "en", "null"],
        )

    def test_brazilian_portuguese_does_not_cross_with_european_portuguese(self):
        brazil = {"iso_639_1": "pt", "iso_3166_1": "BR"}
        portugal = {"iso_639_1": "pt", "iso_3166_1": "PT"}
        generic = {"iso_639_1": "pt", "iso_3166_1": None}

        self.assertEqual(_image_language_keys(brazil), ["pt-br", "pt"])

        self.assertTrue(_image_matches_language(brazil, "pt-br"))
        self.assertFalse(_image_matches_language(portugal, "pt-br"))
        self.assertFalse(_image_matches_language(generic, "pt-br"))
        self.assertTrue(_image_matches_language(brazil, "pt"))

    def test_brazilian_portuguese_does_not_fall_back_to_bare_portuguese_art(self):
        order = image_language_order("pt-br", "en", "native_original")
        self.assertEqual(order, ["pt-br", "en"])
        self.assertNotIn("pt", order)

    def test_brazilian_portuguese_fetch_includes_base_language_for_tmdb(self):
        self.assertEqual(
            _tmdb_include_image_languages("pt-br"),
            ["pt-br", "pt", "en", "null"],
        )

    def test_brazilian_portuguese_renders_translated_poster_text(self):
        # Resolves against languages/pt-br.json when present, otherwise the
        # bare pt.json — either way the poster must not fall back to English.
        load_languages()
        self.assertEqual(translate_genre("Horror", "pt-BR"), "Terror")
        self.assertEqual(translate_genre("Comedy", "pt-BR"), "Comédia")
        self.assertNotEqual(translate_sash("Season Finale", "pt-BR"), "Season Finale")

    def test_region_qualified_language_uses_base_translation_table(self):
        load_languages()
        self.assertEqual(translate_genre("Drama", "fr-FR"), "Drame")
        self.assertEqual(translate_sash("Season Finale", "fr-FR"), "Finale saison")

    def test_region_qualified_spanish_uses_base_translation_table(self):
        load_languages()
        for locale in ("es-ES", "es-MX"):
            with self.subTest(locale=locale):
                self.assertEqual(
                    translate_genre("Drama", locale),
                    translate_genre("Drama", "es"),
                )
                self.assertEqual(
                    translate_sash("Season Finale", locale),
                    translate_sash("Season Finale", "es"),
                )


if __name__ == "__main__":
    unittest.main()
