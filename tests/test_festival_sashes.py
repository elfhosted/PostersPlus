"""A festival keyword says a film won *something*, never that it won the top prize.

MDblist tags every prizewinner at a festival with the same
``festival-<name>-winner`` keyword.  Reading that as "Palme d'Or" was wrong for
about nine titles in ten — the entire Cannes 2023 slate carries it, from
*Anatomy of a Fall* (which did win the Palme) down to *How to Have Sex* (Un
Certain Regard).  The top prize now comes from a TMDB id set in festivals.py and
the keyword only backs the weaker claim.

The films named below are real, and their prizes are real: these tests fail if
the id sets are regenerated into something that no longer knows a Palme winner
from a Grand Prix one.
"""

import sqlite3
import unittest

import cache
import festivals
from discovery import extract_discovery_meta, pick_sash
from festivals import festival_label, match_festival_keyword

CANNES = "festival-cannes-winner"
VENICE = "festival-venice-winner"

# Cannes 2023 — every one of these carries festival-cannes-winner on MDblist.
ANATOMY_OF_A_FALL = 915935    # Palme d'Or
ZONE_OF_INTEREST = 467244     # Grand Prix
PERFECT_DAYS = 976893         # Best Actor
HOW_TO_HAVE_SEX = 1075175     # Un Certain Regard

PARASITE = 496243             # Palme d'Or 2019
DRIVE = 64690                 # Cannes Best Director 2011 — the original report
NOMADLAND = 581734            # Golden Lion 2020
JOKER = 475557                # Golden Lion 2019 — absent from P166, added by hand
THE_MATRIX = 603              # no festival of ours


class TopPrizeTests(unittest.TestCase):
    def test_the_palme_winner_gets_the_palme(self):
        self.assertEqual(festival_label(CANNES, ANATOMY_OF_A_FALL), "Palme d'Or")
        self.assertEqual(festival_label(CANNES, PARASITE), "Palme d'Or")

    def test_the_runner_up_does_not_get_the_palme(self):
        # The Zone of Interest took the Grand Prix.  It has carried a "Palme
        # d'Or" sash for months on the strength of a keyword it shares with
        # eight other films.
        self.assertEqual(festival_label(CANNES, ZONE_OF_INTEREST), "Cannes Winner")

    def test_an_acting_or_sidebar_prize_does_not_get_the_palme(self):
        self.assertEqual(festival_label(CANNES, PERFECT_DAYS), "Cannes Winner")
        self.assertEqual(festival_label(CANNES, HOW_TO_HAVE_SEX), "Cannes Winner")
        self.assertEqual(festival_label(CANNES, DRIVE), "Cannes Winner")

    def test_each_festival_keeps_its_own_prize_name(self):
        self.assertEqual(festival_label(VENICE, NOMADLAND), "Golden Lion")
        self.assertEqual(festival_label(VENICE, THE_MATRIX), "Venice Winner")

    def test_a_winner_wikidata_forgot_is_patched_back_in(self):
        # Wikidata records no Golden Lion for Joker though it won Venice 2019,
        # so it came from the festival's own winners table instead.
        self.assertEqual(festival_label(VENICE, JOKER), "Golden Lion")

    def test_no_keyword_and_no_prize_means_no_sash(self):
        self.assertIsNone(festival_label(None, THE_MATRIX))

    def test_a_keyword_with_no_usable_id_still_earns_the_weaker_claim(self):
        # An unknown TMDB id says nothing about the prize, but the keyword
        # still establishes that the film won something at the festival.
        self.assertEqual(festival_label(CANNES, None), "Cannes Winner")

    def test_a_top_prize_shows_even_when_mdblist_never_answered(self):
        # The id set is local, so a rate-limited or failing MDblist costs the
        # weaker claim but never the prize itself.
        self.assertEqual(festival_label(None, ANATOMY_OF_A_FALL), "Palme d'Or")

    def test_an_unparseable_tmdb_id_falls_back_rather_than_raising(self):
        self.assertEqual(festival_label(CANNES, "not-a-number"), "Cannes Winner")
        self.assertEqual(festival_label(CANNES, str(ANATOMY_OF_A_FALL)), "Palme d'Or")


class DroppedFestivalTests(unittest.TestCase):
    """Toronto, Busan, Rotterdam, SXSW and Tribeca are gone, not guessed at.

    Their sashes named prizes — People's Choice, New Currents, the Tiger Award —
    that no source can confirm.  Wikidata lists 8 People's Choice winners where
    there should be ~48, 4 Tiger winners, and has no award item at all for Busan,
    SXSW or Tribeca.  A sash is better absent than invented.
    """

    RETIRED = [
        "festival-toronto-winner",
        "festival-busan-winner",
        "festival-rotterdam-winner",
        "festival-sxsw-winner",
        "festival-tribeca-winner",
    ]

    def test_a_retired_festival_keyword_produces_no_sash(self):
        for keyword in self.RETIRED:
            with self.subTest(keyword=keyword):
                self.assertIsNone(festival_label(keyword, THE_MATRIX))

    def test_a_retired_keyword_is_never_chosen_for_caching(self):
        self.assertIsNone(match_festival_keyword(set(self.RETIRED)))

    def test_a_live_festival_still_wins_over_retired_ones(self):
        self.assertEqual(
            match_festival_keyword({*self.RETIRED, CANNES}), CANNES
        )


class KeywordSelectionTests(unittest.TestCase):
    def test_the_first_festival_in_prestige_order_wins(self):
        # Titane carries both the Cannes and Toronto keywords; more usefully,
        # a film at two live festivals must resolve deterministically.
        self.assertEqual(match_festival_keyword({VENICE, CANNES}), CANNES)

    def test_no_festival_keyword_means_nothing_to_cache(self):
        self.assertIsNone(match_festival_keyword({"cult-classic", "f-word"}))


class DiscoveryIntegrationTests(unittest.TestCase):
    """The sash the renderer actually receives."""

    def _meta(self, tmdb_id, *, keywords=None, festival_keyword=None):
        return extract_discovery_meta(
            tmdb_data={"original_language": "en"},
            media_type="movie",
            award_wins=[],
            award_noms=[],
            trending_rank=None,
            tmdb_id=tmdb_id,
            keywords=keywords,
            festival_keyword=festival_keyword,
        )

    def test_a_cached_keyword_resolves_against_current_code(self):
        # The cache holds the keyword, so a title cached while the old code was
        # running renders the corrected label without an MDblist round trip.
        meta = self._meta(ZONE_OF_INTEREST, festival_keyword=CANNES)
        self.assertEqual(pick_sash(meta, ["festival"]), ("Cannes Winner", "win"))

    def test_a_fresh_fetch_resolves_from_the_raw_keywords(self):
        meta = self._meta(ANATOMY_OF_A_FALL, keywords=[{"name": "Festival-Cannes-Winner"}])
        self.assertEqual(pick_sash(meta, ["festival"]), ("Palme d'Or", "win"))

    def test_the_tmdb_id_falls_back_to_the_metadata_payload(self):
        meta = extract_discovery_meta(
            tmdb_data={"id": PARASITE, "original_language": "ko"},
            media_type="movie",
            award_wins=[],
            award_noms=[],
            trending_rank=None,
            festival_keyword=CANNES,
        )
        self.assertEqual(meta.festival_label, "Palme d'Or")

    def test_a_title_with_no_festival_gets_no_festival_sash(self):
        meta = self._meta(THE_MATRIX, keywords=[{"name": "cult-classic"}])
        self.assertIsNone(pick_sash(meta, ["festival"]))


class CacheMigrationTests(unittest.TestCase):
    """Rows written when the cache stored a resolved label convert in place.

    Re-fetching would cost one MDblist request per cached title, and the old
    labels map back to their keyword exactly — including the retired ones, which
    map to nothing and so lose a sash they never earned.
    """

    def setUp(self):
        self.previous_initialised = cache._initialised
        self.previous_conn = getattr(cache._local, "conn", None)
        self.conn = sqlite3.connect(":memory:")
        cache._initialised = True
        cache._local.conn = self.conn

    def tearDown(self):
        self.conn.close()
        cache._initialised = self.previous_initialised
        if self.previous_conn is None:
            cache._local.__dict__.pop("conn", None)
        else:
            cache._local.conn = self.previous_conn

    def _legacy_table(self, rows):
        """A rating_cache as it looked before festival_keyword existed."""
        self.conn.execute(
            """
            CREATE TABLE rating_cache (
                imdb_id TEXT PRIMARY KEY,
                festival_label TEXT
            )
            """
        )
        self.conn.executemany("INSERT INTO rating_cache VALUES (?, ?)", rows)
        self.conn.commit()

    def _migrate(self):
        """The festival_keyword half of init_db, against the open connection."""
        added = cache._add_column_if_missing(
            self.conn, "rating_cache", "festival_keyword", "TEXT"
        )
        self.assertTrue(added, "migration should report that it added the column")
        self.conn.executemany(
            "UPDATE rating_cache SET festival_keyword = ? WHERE festival_label = ?",
            [(k, label) for label, k in festivals.LEGACY_LABEL_KEYWORDS.items()],
        )
        self.conn.commit()
        return dict(
            self.conn.execute("SELECT imdb_id, festival_keyword FROM rating_cache")
        )

    def test_a_stored_label_becomes_its_keyword(self):
        self._legacy_table([
            ("tt17009710", "Palme d'Or"),
            ("tt7286456", "Golden Lion"),
            ("tt2278388", "Golden Bear"),
        ])
        self.assertEqual(
            self._migrate(),
            {"tt17009710": CANNES, "tt7286456": VENICE, "tt2278388": "festival-berlin-winner"},
        )

    def test_a_retired_prize_migrates_to_nothing(self):
        self._legacy_table([("tt6966692", "People's Choice"), ("tt0000001", "Tiger Award")])
        self.assertEqual(self._migrate(), {"tt6966692": None, "tt0000001": None})

    def test_a_row_with_no_festival_stays_empty(self):
        self._legacy_table([("tt0133093", None)])
        self.assertEqual(self._migrate(), {"tt0133093": None})

    def test_running_the_migration_twice_is_not_attempted(self):
        self._legacy_table([("tt17009710", "Palme d'Or")])
        self._migrate()
        self.assertFalse(
            cache._add_column_if_missing(
                self.conn, "rating_cache", "festival_keyword", "TEXT"
            ),
            "a second startup must not re-run the backfill",
        )


class DataIntegrityTests(unittest.TestCase):
    """Guards on the generated id sets — tools/refresh_festival_winners.py."""

    def test_every_festival_has_a_distinct_pair_of_labels(self):
        labels = [f.top_prize for f in festivals.FESTIVALS]
        labels += [f.winner_label for f in festivals.FESTIVALS]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(set(labels), set(festivals.FESTIVAL_SASH_LABELS))

    def test_no_prize_list_is_empty(self):
        # A regeneration that silently returns nothing would turn every top
        # prize into the weaker claim without failing anything else.
        for festival in festivals.FESTIVALS:
            with self.subTest(prize=festival.top_prize):
                self.assertGreater(len(festival.top_prize_ids), 40)

    def test_films_holding_two_top_prizes_stay_a_known_short_list(self):
        """Overlap is possible, so it is enumerated rather than forbidden.

        A modern festival demands a premiere, but the early post-war ones did
        not.  Both known cases are genuine doubles from that era, and prestige
        order decides which prize they get.  An overlap outside this list is
        worth seeing: it usually means a regeneration went wrong.
        """
        expected = {
            204,    # The Wages of Fear — Cannes 1953, then Berlin 1953
            43349,  # Gate of Hell — Cannes 1954, then Locarno 1954
            67731,  # Justice Is Done — Venice 1950, then Berlin 1951
        }

        seen: dict[int, str] = {}
        overlaps: set[int] = set()
        for festival in festivals.FESTIVALS:
            for tmdb_id in festival.top_prize_ids:
                if tmdb_id in seen:
                    overlaps.add(tmdb_id)
                seen[tmdb_id] = festival.top_prize

        self.assertEqual(overlaps, expected)
        # Whatever overlaps, the answer has to be stable and the higher prize.
        self.assertEqual(festival_label("festival-berlin-winner", 204), "Palme d'Or")
        self.assertEqual(festival_label("festival-locarno-winner", 43349), "Palme d'Or")
        self.assertEqual(festival_label("festival-berlin-winner", 67731), "Golden Lion")

    def test_every_legacy_label_still_names_a_live_festival(self):
        keywords = {f.keyword for f in festivals.FESTIVALS}
        for label, keyword in festivals.LEGACY_LABEL_KEYWORDS.items():
            with self.subTest(label=label):
                self.assertIn(keyword, keywords)


class PatchedWinnerTests(unittest.TestCase):
    """Winners Wikidata's P166 does not record, recovered from the winners tables.

    Each is a real gap that was showing the weaker sash: *Joker* read "Venice
    Winner", four recent Locarno Leopards read "Locarno Winner", and Cannes had
    no record of its own 1946 edition beyond a single film.
    """

    CASES = [
        (VENICE, 475557, "Golden Lion", "Joker, Venice 2019"),
        (VENICE, 408542, "Golden Lion", "The Woman Who Left, Venice 2016"),
        ("festival-locarno-winner", 468592, "Golden Leopard", "Vitalina Varela, 2019"),
        ("festival-locarno-winner", 1147359, "Golden Leopard", "Critical Zone, 2023"),
        ("festival-locarno-winner", 467256, "Golden Leopard", "Mrs. Fang, 2017"),
        ("festival-sundance-winner", 25793, "Sundance GJ", "Precious, Sundance 2009"),
        (CANNES, 307, "Palme d'Or", "Rome, Open City — 1946's eleven-way tie"),
        (CANNES, 1092, "Palme d'Or", "The Third Man, Cannes 1949"),
        ("festival-berlin-winner", 1315657, "Golden Bear", "Yellow Letters, Berlin 2026"),
    ]

    def test_each_patched_winner_reads_as_its_prize(self):
        for keyword, tmdb_id, prize, who in self.CASES:
            with self.subTest(film=who):
                self.assertEqual(festival_label(keyword, tmdb_id), prize)


class RefutedWinnerTests(unittest.TestCase):
    """Entries P166 claims that the festival's own winners table contradicts.

    All are the mistake this module exists to stop — a parent award standing in
    for one of its sub-awards — just committed by Wikidata rather than MDblist.
    """

    def test_a_short_film_bear_is_not_the_golden_bear(self):
        # Ascensor is 11 minutes; it won the Golden Bear for Best Short Film.
        for tmdb_id, who in [(162443, "Ascensor"), (211453, "Bolero"),
                             (179599, "A Good Day for a Swim")]:
            with self.subTest(film=who):
                self.assertEqual(
                    festival_label("festival-berlin-winner", tmdb_id), "Berlin Winner")

    def test_a_sidebar_prize_is_not_the_top_prize(self):
        # The Works and Days took Encounters in 2022; Alcarras took the Bear.
        self.assertEqual(
            festival_label("festival-berlin-winner", 664591), "Berlin Winner")
        # Nightsiren took Filmmakers of the Present; Rule 34 took the Leopard.
        self.assertEqual(
            festival_label("festival-locarno-winner", 997660), "Locarno Winner")

    def test_the_sundance_prize_follows_the_right_film(self):
        """Precious premiered as "Push: Based on the Novel by Sapphire".

        P166 put its Grand Jury Prize on TMDB 13455, which is the unrelated 2009
        science-fiction film *Push* — so a thriller was wearing a Sundance sash
        while the film that won it wore none.
        """
        self.assertEqual(festival_label("festival-sundance-winner", 25793), "Sundance GJ")
        self.assertEqual(festival_label("festival-sundance-winner", 13455), "Sundance Winner")

    def test_nothing_refuted_survives_into_a_prize_set(self):
        for fest in festivals.FESTIVALS:
            with self.subTest(prize=fest.top_prize):
                self.assertFalse(fest.top_prize_ids & festivals.REFUTED_TMDB_IDS)

    def test_each_refutation_belongs_to_exactly_one_prize(self):
        """REFUTED_TMDB_IDS is subtracted from every prize, not just its own.

        That is safe only while no refuted id is a legitimate winner elsewhere.
        Films do win at two festivals — Gate of Hell took Cannes and Locarno in
        1954 — so a regeneration that put a refuted id into a second prize would
        silently delete a real winner from it.  Fail loudly instead.
        """
        sets = {
            "Palme d'Or": festivals.PALME_DOR_TMDB_IDS | festivals.PALME_DOR_EXTRA_TMDB_IDS,
            "Golden Lion": festivals.GOLDEN_LION_TMDB_IDS | festivals.GOLDEN_LION_EXTRA_TMDB_IDS,
            "Golden Bear": festivals.GOLDEN_BEAR_TMDB_IDS | festivals.GOLDEN_BEAR_EXTRA_TMDB_IDS,
            "Golden Leopard": festivals.GOLDEN_LEOPARD_TMDB_IDS | festivals.GOLDEN_LEOPARD_EXTRA_TMDB_IDS,
            "Sundance GJ": festivals.SUNDANCE_GJ_TMDB_IDS | festivals.SUNDANCE_GJ_EXTRA_TMDB_IDS,
        }
        for tmdb_id in festivals.REFUTED_TMDB_IDS:
            claimants = [prize for prize, ids in sets.items() if tmdb_id in ids]
            with self.subTest(tmdb_id=tmdb_id):
                self.assertEqual(
                    len(claimants), 1,
                    f"{tmdb_id} is claimed by {claimants}; refuting it would "
                    f"remove it from all of them",
                )

    def test_a_refutation_only_ever_removes_something_real(self):
        # A stale entry here would quietly do nothing; the sets are regenerated,
        # so an id that has already gone from Wikidata needs deleting from here.
        everything = set().union(
            festivals.PALME_DOR_TMDB_IDS, festivals.GOLDEN_LION_TMDB_IDS,
            festivals.GOLDEN_BEAR_TMDB_IDS, festivals.GOLDEN_LEOPARD_TMDB_IDS,
            festivals.SUNDANCE_GJ_TMDB_IDS,
        )
        self.assertTrue(festivals.REFUTED_TMDB_IDS <= everything)


if __name__ == "__main__":
    unittest.main()
