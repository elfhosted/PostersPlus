"""Festival sashes — which festival, and whether it was the *top* prize.

The problem this module exists to solve
---------------------------------------
MDblist (via IMDb) tags a title ``festival-<name>-winner`` if it won *anything*
at that festival.  Cannes alone hands out nine or ten prizes a year — Grand
Prix, Jury Prize, Best Director, Best Screenplay, the acting awards, Un Certain
Regard, the Caméra d'Or — and every one of those films carries the identical
keyword the Palme d'Or winner does.  Reading that keyword as "won the Palme
d'Or" was wrong for roughly nine titles in ten: the whole 2023 slate, from
*Anatomy of a Fall* down to *How to Have Sex*, is tagged the same way.

MDblist has no finer keyword to offer.  Its only category-level award terms are
Oscar ones (``oscar-best-director-winner`` and friends); nothing distinguishes a
Palme from a Caméra d'Or.  So the top prize has to come from somewhere else.

Where the top-prize lists come from
-----------------------------------
Wikidata, property P166 (*award received*) on items that are films, one query
per prize.  Every film it returns carries a TMDB id, which is what makes a plain
id-set lookup possible.  Regenerate with ``tools/refresh_festival_winners.py``
after each festival.

P166 alone was patchy, so every prize was then cross-checked against the
festival's own winners table on Wikipedia, edition by edition.  That found 34
winners P166 does not record — Joker's 2019 Golden Lion, four recent Locarno
Leopards, Precious at Sundance 2009 — and refuted 9 entries P166 does record,
each in ``*_EXTRA_TMDB_IDS`` and ``REFUTED_TMDB_IDS`` respectively.  Where the
sets stand now, "confirmed" meaning the winners table lists that exact film:

    prize             ids   confirmed   unconfirmed
    Palme d'Or         97          97             0
    Golden Lion        69          66             3
    Golden Bear        86          86             0
    Golden Leopard     93          92             1
    Sundance GJ        50          48             2
                                        98.5% overall

Nothing a winners table lists is missing.  The six unconfirmed are rows the
table does list but the scraper could not read — Venice 1993 tied between
*Short Cuts* and *Three Colours: Blue* and only one link per row survives, and
Sundance 2002 links its director instead of *Personal Velocity* — so each was
checked against the festival's own history by hand and kept.

A future gap costs only the prize name, not the sash: that is the point of
resolving in two tiers, and it is the floor the old behaviour never had.

Only five festivals are represented, because only five have a top-prize list
worth having at all.  Toronto's People's Choice returns 8 films where it should
return ~48; Rotterdam's Tiger 4; Busan's New Currents, SXSW and Tribeca have no
usable award item. Those five used to carry sashes naming prizes nothing could
verify, so they are gone rather than guessed at.

How a label is chosen
---------------------
``festival_label`` answers in two tiers:

1. The TMDB id is in a top-prize set → the prize itself ("Palme d'Or").  This
   is a local lookup, so it stands even when MDblist is down, rate-limited, or
   backing off — the keyword is not consulted.
2. Otherwise the MDblist keyword says the film won *something* there →
   "Cannes Winner".  Weaker, but true, and it never overstates.

Both tiers translate through ``languages/*.json`` like every other sash label.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Festival:
    """One festival, its top prize, and the films that actually won it."""

    keyword: str                    # MDblist/IMDb keyword — "won something here"
    top_prize: str                  # sash label for a top-prize winner
    winner_label: str               # sash label for any other prize at the festival
    top_prize_ids: frozenset[int]   # TMDB ids of the top-prize winners


# Palme d'Or — every Cannes top-prize winner Wikidata knows (84 films).
PALME_DOR_TMDB_IDS: frozenset[int] = frozenset({
    154879,    # 1946  Neecha Nagar
    18650,     # 1951  Miss Julie
    117478,    # 1952  Two Cents Worth of Hope
    47697,     # 1955  Othello
    204,       # 1955  The Wages of Fear
    43258,     # 1956  Friendly Persuasion
    60234,     # 1956  The Silent World
    40423,     # 1959  Black Orpheus
    15919,     # 1959  Marty
    38360,     # 1960  The Cranes Are Flying
    439,       # 1961  La Dolce Vita
    131836,    # 1961  The Long Absence
    4497,      # 1962  Viridiana
    59990,     # 1963  O Pagador de Promessas
    1040,      # 1963  The Leopard
    42744,     # 1965  Q1198726
    42726,     # 1966  A Man and a Woman
    17974,     # 1966  The Birds, the Bees and the Italians
    5967,      # 1966  The Umbrellas of Cherbourg
    1052,      # 1967  Blowup
    14794,     # 1969  If....
    651,       # 1970  MASH
    36194,     # 1971  The Go-Between
    56231,     # 1971  The Working Class Goes to Heaven
    62382,     # 1972  The Mattei Affair
    80382,     # 1973  The Hireling
    52555,     # 1975  Chronicle of the Years of Fire
    31587,     # 1975  Scarecrow
    592,       # 1975  The Conversation
    103,       # 1977  Taxi Driver
    42225,     # 1978  Padre Padrone
    659,       # 1979  The Tin Drum
    31542,     # 1979  The Tree of Wooden Clogs
    16858,     # 1980  All That Jazz
    11953,     # 1980  Kagemusha
    225,       # 1981  Man of Iron
    52556,     # 1982  Yol
    15600,     # 1984  Missing
    655,       # 1985  Paris, Texas
    11416,     # 1987  The Mission
    31300,     # 1987  Under the Sun of Satan
    21042,     # 1988  When Father Was Away on Business
    11174,     # 1989  Pelle the Conqueror
    1412,      # 1989  Sex, Lies, and Videotape
    483,       # 1990  Wild at Heart
    290,       # 1991  Barton Fink
    41764,     # 1992  The Best Intentions
    10997,     # 1993  Farewell My Concubine
    713,       # 1993  The Piano
    680,       # 1994  Pulp Fiction
    11159,     # 1996  Secrets & Lies
    20506,     # 1997  The Eel
    11902,     # 1997  Underground
    24858,     # 1999  Eternity and a Day
    16,        # 2000  Dancer in the Dark
    11489,     # 2001  Rosetta
    423,       # 2002  The Pianist
    11447,     # 2002  The Son's Room
    1807,      # 2004  Elephant
    1777,      # 2004  Fahrenheit 9/11
    11490,     # 2005  The Child
    2009,      # 2007  4 Months, 3 Weeks and 2 Days
    1116,      # 2007  The Wind That Shakes the Barley
    8841,      # 2009  The Class
    37903,     # 2010  The White Ribbon
    38368,     # 2010  Uncle Boonmee Who Can Recall His Past Lives
    8967,      # 2011  The Tree of Life
    86837,     # 2012  Amour
    152584,    # 2013  Blue Is the Warmest Colour
    314402,    # 2015  Dheepan
    265169,    # 2015  Winter Sleep
    30020,     # 2016  Taste of Cherry
    374473,    # 2017  I, Daniel Blake
    401246,    # 2017  The Square
    42113,     # 2018  The Ballad of Narayama
    28,        # 2019  Apocalypse Now
    496243,    # 2019  Parasite
    505192,    # 2019  Shoplifters
    630240,    # 2021  Titane
    497828,    # 2022  Triangle of Sadness
    915935,    # 2024  Anatomy of a Fall
    1064213,   # 2024  Anora
    1456349,   # 2026  It Was Just an Accident
    1401459,   # 2027  Fjord
})

# Golden Lion — every Venice top-prize winner Wikidata knows (67 films).
GOLDEN_LION_TMDB_IDS: frozenset[int] = frozenset({
    67731,     # 1950  Justice Is Done
    132332,    # 1950  Manon
    5000,      # 1952  Forbidden Games
    548,       # 1952  Rashomon
    92784,     # 1954  Romeo and Juliet
    897,       # 1956  Aparajito
    48035,     # 1957  Ordet
    43100,     # 1959  Q862713
    55823,     # 1959  The Great War
    154578,    # 1960  Tomorrow Is My Turn
    4024,      # 1961  Last Year at Marienbad
    80557,     # 1962  Family Diary
    31442,     # 1962  Ivan's Childhood
    58383,     # 1963  Hands Over the City
    26638,     # 1965  Red Desert
    92432,     # 1965  Sandra
    649,       # 1967  Belle de Jour
    17295,     # 1967  The Battle of Algiers
    154582,    # 1968  Artists Under the Big Top: Perplexed
    43121,     # 1969  Rickshaw Man
    10889,     # 1980  Gloria
    23954,     # 1981  Atlantic City
    42142,     # 1981  Marianne and Juliane
    30363,     # 1982  The State of Things
    42096,     # 1984  A Year of the Quiet Sun
    32689,     # 1984  First Name: Carmen
    44018,     # 1986  Vagabond
    1786,      # 1987  Au revoir les enfants
    54898,     # 1987  The Green Ray
    54990,     # 1988  The Legend of the Holy Drinker
    49982,     # 1991  A City of Sadness
    36346,     # 1991  Close to Eden
    18971,     # 1991  Rosencrantz & Guildenstern Are Dead
    108,       # 1993  Three Colours: Blue
    695,       # 1994  Short Cuts
    38143,     # 1994  The Story of Qiu Ju
    19155,     # 1995  Before the Rain
    36266,     # 1995  Cyclo
    11985,     # 1996  Vive L'Amour
    1770,      # 1997  Michael Collins
    5910,      # 1998  Hana-bi
    58098,     # 1999  The Way We Laughed
    36210,     # 2001  Not One Less
    13898,     # 2001  The Circle
    480,       # 2002  Monsoon Wedding
    8094,      # 2003  The Magdalene Sisters
    11190,     # 2003  The Return
    11109,     # 2005  Vera Drake
    142,       # 2006  Brokeback Mountain
    4588,      # 2007  Lust, Caution
    2346,      # 2007  Still Life
    12163,     # 2009  The Wrestler
    32084,     # 2010  Lebanon
    39210,     # 2010  Somewhere
    77560,     # 2012  Faust (2011 film)
    123377,    # 2012  Pietà
    110390,    # 2015  A Pigeon Sat on a Branch Reflecting on Existence
    216790,    # 2015  Sacro GRA
    352162,    # 2016  From Afar
    426426,    # 2018  Roma
    399055,    # 2018  The Shape of Water
    581734,    # 2021  Nomadland
    793998,    # 2022  Happening
    1004663,   # 2023  All the Beauty and the Bloodshed
    792307,    # 2024  Poor Things
    1088514,   # 2025  The Room Next Door
    1159206,   # 2026  Father Mother Sister Brother
})

# Golden Bear — every Berlin top-prize winner Wikidata knows (90 films).
GOLDEN_BEAR_TMDB_IDS: frozenset[int] = frozenset({
    11224,     # 1950  Cinderella
    67731,     # 1950  Justice Is Done
    162339,    # 1951  Four in a Jeep
    162344,    # 1951  Without Leaving an Address
    56719,     # 1952  One Summer of Happiness
    16410,     # 1954  Hobson's Choice
    148836,    # 1955  Die Ratten
    204,       # 1955  The Wages of Fear
    47310,     # 1956  Invitation to the Dance
    389,       # 1958  12 Angry Men
    162368,    # 1959  El Lazarillo de Tormes
    2363,      # 1959  The Cousins
    614,       # 1959  Wild Strawberries
    33765,     # 1962  A Kind of Loving
    41050,     # 1962  La Notte
    90299,     # 1963  Bushido, Samurai Saga
    162382,    # 1963  To Bed or Not to Bed
    58897,     # 1964  Dry Summer
    8072,      # 1966  Alphaville
    4772,      # 1966  Cul-de-sac
    137726,    # 1967  The Departure
    122671,    # 1968  Who Saw Him Die?
    162421,    # 1969  Early Works
    4789,      # 1970  The Garden of the Finzi-Continis
    113012,    # 1973  Distant Thunder
    42451,     # 1974  The Apprenticeship of Duddy Kravitz
    61703,     # 1975  Adoption
    50183,     # 1977  The Ascent
    5691,      # 1977  The Canterbury Tales
    162443,    # 1978  Ascensor
    162438,    # 1978  Las truchas
    162439,    # 1978  What Max Said
    163133,    # 1979  David
    49427,     # 1979  Heartland
    131349,    # 1979  The Theme
    42233,     # 1980  Buffalo Bill and the Indians, or Sitting Bull's History Lesson
    130544,    # 1980  Palermo or Wolfsburg
    47211,     # 1981  Deprisa, Deprisa
    2262,      # 1982  Veronika Voss
    163143,    # 1983  Ascendancy
    104435,    # 1985  La colmena
    52109,     # 1985  Love Streams
    147747,    # 1985  Wetherby
    52327,     # 1986  Stammheim
    380,       # 1989  Rain Man
    2263,      # 1990  Music Box
    12078,     # 1991  Larks on a String
    211453,    # 1992  Bolero
    13697,     # 1992  Grand Canyon
    42006,     # 1992  Red Sorghum
    148757,    # 1992  The House of Smiles
    9261,      # 1993  The Wedding Banquet
    162841,    # 1993  Woman Sesame Oil Maker
    7984,      # 1994  In the Name of the Father
    4584,      # 1996  Sense and Sensibility
    38414,     # 1996  The Bait
    1630,      # 1997  The People vs. Larry Flynt
    8741,      # 1999  The Thin Red Line
    334,       # 2000  Magnolia
    11845,     # 2001  Intimacy
    4107,      # 2002  Bloody Sunday
    36791,     # 2003  In This World
    363,       # 2004  Head-On
    1901,      # 2005  In Good Company
    79706,     # 2005  U-Carmen eKhayelitsha
    317,       # 2006  Grbavica
    2694,      # 2007  Tuya's Marriage
    179599,    # 2008  A Good Day for a Swim
    163149,    # 2008  The Woman and the Stranger
    7347,      # 2009  Elite Squad
    28644,     # 2009  The Milk of Sorrow
    44160,     # 2010  Honey
    60243,     # 2012  A Separation
    96821,     # 2013  Caesar Must Die
    160118,    # 2013  Child's Pose
    255756,    # 2014  Black Coal, Thin Ice
    320006,    # 2015  Taxi
    377151,    # 2016  Fire at Sea
    436343,    # 2017  On Body and Soul
    666,       # 2018  Central Station
    501590,    # 2019  Synonyms
    499155,    # 2019  Touch Me Not
    790496,    # 2021  Bad Luck Banging or Loony Porn
    667935,    # 2021  There Is No Evil
    804251,    # 2022  Alcarràs
    664591,    # 2022  The Works and Days
    1070449,   # 2023  On the Adamant
    1101256,   # 2024  Dahomey
    129,       # 2024  Spirited Away
    1228682,   # 2025  Dreams
})

# Golden Leopard — every Locarno top-prize winner Wikidata knows (85 films).
GOLDEN_LEOPARD_TMDB_IDS: frozenset[int] = frozenset({
    146828,    # 1947  Man About Town
    79596,     # 1948  The Emperor's Nightingale
    241483,    # 1950  Julius Caesar
    257095,    # 1950  Prince Bayaya
    212434,    # 1950  Rotation
    275090,    # 1950  The Farm of Seven Sins
    54615,     # 1950  When Willie Comes Marching Home
    89903,     # 1952  Composer Glinka
    8016,      # 1952  Germany, Year Zero
    131074,    # 1952  Hunted
    4886,      # 1953  And Then There Were None
    34752,     # 1953  The Glass Wall
    43349,     # 1954  Gate of Hell
    74810,     # 1954  The Sheep Has Five Legs
    368775,    # 1954  Wild Fruit
    10056,     # 1955  Killer's Kiss
    101288,    # 1958  Ten North Frederick
    31427,     # 1959  Fires on the Plain
    76157,     # 1960  Il bell'Antonio
    275092,    # 1961  The Winner
    57913,     # 1963  Transport z ráje
    46982,     # 1964  Black Peter
    173873,    # 1964  Courage for Every Day
    129134,    # 1965  Four in the Morning
    67062,     # 1967  Entranced Earth
    195841,    # 1967  Soleil Ô
    83444,     # 1968  No Path Through Fire
    122097,    # 1968  Three Sad Tigers
    108543,    # 1969  Charles, Dead or Alive
    343466,    # 1969  Those Who Wear Glasses
    231754,    # 1970  Lilika
    104452,    # 1970  Mujo
    577836,    # 1970  Znaki na drodze
    122271,    # 1971  Bleak Moments
    607189,    # 1971  On the Point of Death
    77368,     # 1971  Private Road
    219823,    # 1971  The Friends
    100468,    # 1971  They Have Changed Their Face
    154575,    # 1973  The Illumination
    42467,     # 1973  Tűzoltó utca 25.
    458983,    # 1975  Le Fils d'Amr est mort
    549380,    # 1976  The Big Night
    246457,    # 1977  Antonio Gramsci: The Days of Prison
    258026,    # 1978  The Idlers of the Fertile Valley
    67068,     # 1980  To Love the Damned
    388687,    # 1981  Chakra
    273602,    # 1983  The Princess
    469,       # 1984  Stranger Than Paradise
    67263,     # 1986  Alpine Fire
    158326,    # 1986  Jezioro Bodenskie
    84014,     # 1986  The Herd
    41799,     # 1988  Distant Voices, Still Lives
    354176,    # 1988  Schmetterlinge
    62637,     # 1989  Why Has Bodhi-Dharma Left for the East?
    389825,    # 1990  Accidental Waltz
    45145,     # 1991  Johnny Suede
    26855,     # 1992  Autumn Moon
    287173,    # 1993  The Place on the Tricorne
    275096,    # 1994  Khomreh
    40910,     # 1995  Raï
    40882,     # 1997  The Mirror
    287171,    # 1998  Mr. Zhao
    99002,     # 1998  Nenette and Boni
    104743,    # 1999  Skin of Man, Heart of Beast
    255526,    # 2000  Father
    73929,     # 2001  Off to the Revolution by a 2CV
    100086,    # 2004  Khamosh Pani
    6179,      # 2004  The Longing
    16022,     # 2005  Nine Lives
    44104,     # 2006  Private
    10420,     # 2007  Das Fräulein
    12474,     # 2008  Parque via
    73970,     # 2010  She, a Chinese
    95361,     # 2010  Winter Vacation
    153820,    # 2012  The Girl from Nowhere
    214251,    # 2013  Story of My Death
    85544,     # 2014  Back to Stay
    280492,    # 2014  From What Is Before
    408267,    # 2016  Godless
    354275,    # 2016  Right Now, Wrong Then
    537526,    # 2018  A Land Imagined
    997660,    # 2022  Nightsiren
    997265,    # 2022  Rule 34
    1315091,   # 2024  Toxic
    1462735,   # 2025  Two Seasons, Two Strangers
})

# Sundance GJ — every Sundance top-prize winner Wikidata knows (44 films).
SUNDANCE_GJ_TMDB_IDS: frozenset[int] = frozenset({
    11368,     # 1985  Blood Simple
    33766,     # 1985  Smooth Talk
    123382,    # 1987  Heat and Sunlight
    118500,    # 1987  The Trouble with Dick
    223658,    # 1987  Waiting for the Moon
    111605,    # 1989  True Love
    102831,    # 1991  Chameleon Street
    47620,     # 1991  Poison
    52936,     # 1992  In the Soup
    77064,     # 1993  Public Access
    47889,     # 1993  Ruby in Paradise
    83718,     # 1994  What Happened Was
    16934,     # 1995  The Young Poisoner's Handbook
    16388,     # 1996  The Brothers McMullen
    11446,     # 1996  Welcome to the Dollhouse
    70805,     # 1998  Sunday
    37532,     # 2000  Slam
    19348,     # 2001  Girlfight
    4154,      # 2001  Three Seasons
    32625,     # 2002  Personal Velocity: Three Portraits
    4012,      # 2002  The Believer
    14295,     # 2002  You Can Count on Me
    2771,      # 2004  American Splendor
    14337,     # 2004  Primer
    30082,     # 2005  Forty Shades of Blue
    64499,     # 2006  Quinceañera
    79983,     # 2007  Padre Nuestro
    10183,     # 2008  Frozen River
    13455,     # 2009  Push
    60420,     # 2011  Like Crazy
    39013,     # 2011  Winter's Bone
    84175,     # 2013  Beasts of the Southern Wild
    157354,    # 2014  Fruitvale Station
    308369,    # 2015  Me & Earl & the Dying Girl
    244786,    # 2015  Whiplash
    425591,    # 2017  I Don't Feel at Home in This World Anymore
    339408,    # 2017  The Birth of a Nation
    426613,    # 2018  The Miseducation of Cameron Post
    565307,    # 2019  Clemency
    776503,    # 2021  CODA
    615643,    # 2021  Minari
    843932,    # 2022  Nanny
    855263,    # 2023  A Thousand and One
    1151082,   # 2024  In the Summers
})


# Palme d'Or winners missing from P166, taken from the Cannes winners table
# on Wikipedia (13 films).
PALME_DOR_EXTRA_TMDB_IDS: frozenset[int] = frozenset({
    851,       # 1946  Brief Encounter
    60225,     # 1946  María Candelaria
    154623,    # 1946  Men Without Wings
    117500,    # 1946  Pastoral Symphony
    307,       # 1946  Rome, Open City
    101772,    # 1946  The Last Chance
    28580,     # 1946  The Lost Weekend
    99272,     # 1946  The Red Meadows
    154620,    # 1946  The Turning Point
    51144,     # 1946  Torment
    1092,      # 1949  The Third Man
    43379,     # 1951  Miracle in Milan
    43349,     # 1954  Gate of Hell
})

# Golden Lion winners missing from P166, taken from the Venice winners table
# on Wikipedia (2 films).
GOLDEN_LION_EXTRA_TMDB_IDS: frozenset[int] = frozenset({
    408542,    # 2016  The Woman Who Left
    475557,    # 2019  Joker
})

# Golden Bear winners missing from P166, taken from the Berlin winners table
# on Wikipedia (1 film).
GOLDEN_BEAR_EXTRA_TMDB_IDS: frozenset[int] = frozenset({
    1315657,   # 2026  Yellow Letters
})

# Golden Leopard winners missing from P166, taken from the Locarno winners table
# on Wikipedia (11 films).
GOLDEN_LEOPARD_EXTRA_TMDB_IDS: frozenset[int] = frozenset({
    51044,     # 1955  Carmen Jones
    41054,     # 1957  Il Grido
    275093,    # 1968  The Visionaries
    151679,    # 1970  End of the Road
    128966,    # 1987  O Bobo
    102336,    # 2007  The Rebirth
    467256,    # 2017  Mrs. Fang
    468592,    # 2019  Vitalina Varela
    674255,    # 2021  Vengeance Is Mine, All Others Pay Cash
    1147359,   # 2023  Critical Zone
    1728593,   # 2026  You Don't Belong Here
})

# Sundance GJ winners missing from P166, taken from the Sundance winners table
# on Wikipedia (7 films).
SUNDANCE_GJ_EXTRA_TMDB_IDS: frozenset[int] = frozenset({
    111469,    # 1978  Girlfriends
    84605,     # 1982  Circle of Power
    117511,    # 1983  Purple Haze
    104043,    # 1984  Old Enough
    25793,     # 2009  Precious
    1239288,   # 2025  Atropia
    1299329,   # 2026  Josephine
})

# ---------------------------------------------------------------------------
# Refuted by the winners tables
# ---------------------------------------------------------------------------
# Wikidata attaches P166 to these films, and the festival's own winners table
# does not list them.  Each was checked by hand; each is the same mistake the
# MDblist keyword makes, which is to let a parent award stand for one of its
# sub-awards.  Left in place they would hand out a top prize nobody won.
#
# Subtracted from the generated sets rather than deleted out of them, because
# tools/refresh_festival_winners.py rewrites those wholesale and would restore
# every one of these on the next run.
REFUTED_TMDB_IDS: frozenset[int] = frozenset({
    # Golden Bear for Best Short Film — a different award, and these are shorts.
    162443,    # 1978  Ascensor (11 min)
    211453,    # 1992  Bolero (6 min)
    179599,    # 2008  A Good Day for a Swim (10 min)
    # Berlinale sidebars, not the Competition.
    664591,    # 2022  The Works and Days — Encounters; Alcarras took the Bear
    1901,      # 2005  In Good Company — no Berlinale prize; U-Carmen took the Bear
    # Locarno sidebar, not the Concorso internazionale.
    997660,    # 2022  Nightsiren — Filmmakers of the Present; Rule 34 took the Leopard
    # Locarno entries no winners table confirms.  Kept out on the same principle
    # the two tiers exist for: unproven means the weaker claim, not the prize.
    257095,    # 1950  Prince Bayaya
    368775,    # 1954  Wild Fruit
    # The prize is real but the id is not: Precious premiered at Sundance as
    # "Push: Based on the Novel by Sapphire", and the award landed on TMDB 13455,
    # which is the unrelated 2009 science-fiction film Push.  The genuine winner
    # is 25793, added above.
    13455,     # 2009  Push (not Precious)
})


# Checked in this order, which is roughly prestige order.  It settles two ties:
# which prize wins when a film somehow sits in two top-prize sets, and which
# festival names the sash when a film carries several festival keywords (Titane
# has both Cannes and Toronto).
def _top_prize(generated: frozenset[int], confirmed: frozenset[int]) -> frozenset[int]:
    """What Wikidata found, plus what the winners tables add, minus what they refute."""
    return (generated | confirmed) - REFUTED_TMDB_IDS


FESTIVALS: tuple[Festival, ...] = (
    Festival("festival-cannes-winner",   "Palme d'Or",     "Cannes Winner",
             _top_prize(PALME_DOR_TMDB_IDS, PALME_DOR_EXTRA_TMDB_IDS)),
    Festival("festival-venice-winner",   "Golden Lion",    "Venice Winner",
             _top_prize(GOLDEN_LION_TMDB_IDS, GOLDEN_LION_EXTRA_TMDB_IDS)),
    Festival("festival-berlin-winner",   "Golden Bear",    "Berlin Winner",
             _top_prize(GOLDEN_BEAR_TMDB_IDS, GOLDEN_BEAR_EXTRA_TMDB_IDS)),
    Festival("festival-locarno-winner",  "Golden Leopard", "Locarno Winner",
             _top_prize(GOLDEN_LEOPARD_TMDB_IDS, GOLDEN_LEOPARD_EXTRA_TMDB_IDS)),
    Festival("festival-sundance-winner", "Sundance GJ",    "Sundance Winner",
             _top_prize(SUNDANCE_GJ_TMDB_IDS, SUNDANCE_GJ_EXTRA_TMDB_IDS)),
)

_BY_KEYWORD: dict[str, Festival] = {f.keyword: f for f in FESTIVALS}

# Every label this module can put on a sash — the vocabulary languages/*.json
# has to cover.  Both tiers, because either can reach the renderer.
FESTIVAL_SASH_LABELS: frozenset[str] = frozenset(
    [f.top_prize for f in FESTIVALS] + [f.winner_label for f in FESTIVALS]
)

# Labels the rating cache stored back when it cached a *resolved* label instead
# of the keyword, mapped to the keyword that produced them.  Read once, by the
# schema migration in cache.py, to convert existing rows in place rather than
# spend an MDblist request per cached title re-learning what we already knew.
#
# The five festivals dropped above are deliberately absent: their rows migrate
# to NULL, which is the honest answer for a prize we can no longer stand behind.
LEGACY_LABEL_KEYWORDS: dict[str, str] = {
    "Palme d'Or":     "festival-cannes-winner",
    "Golden Lion":    "festival-venice-winner",
    "Golden Bear":    "festival-berlin-winner",
    "Golden Leopard": "festival-locarno-winner",
    "Sundance GJ":    "festival-sundance-winner",
}


def match_festival_keyword(keyword_names: set[str]) -> str | None:
    """The festival keyword to remember for a title, or None if it has none.

    This is what gets cached — the keyword, never the label it resolves to.  A
    label baked into the cache is a label that survives the code being fixed,
    which is exactly how the Palme d'Or claim outlived its own correction for
    months.  Resolution happens at render time, every time, in festival_label().
    """
    for festival in FESTIVALS:
        if festival.keyword in keyword_names:
            return festival.keyword
    return None


def festival_label(keyword: str | None, tmdb_id: int | str | None) -> str | None:
    """The sash label for a title, or None if no festival applies.

    A top-prize winner is recognised from its TMDB id alone, so the prize still
    shows when MDblist never answered.  Everything else needs the keyword to
    know a festival is involved at all.
    """
    numeric_id: int | None = None
    if tmdb_id is not None:
        try:
            numeric_id = int(tmdb_id)
        except (TypeError, ValueError):
            numeric_id = None

    if numeric_id is not None:
        for festival in FESTIVALS:
            if numeric_id in festival.top_prize_ids:
                return festival.top_prize

    festival = _BY_KEYWORD.get(keyword) if keyword else None
    return festival.winner_label if festival else None
