#!/usr/bin/env python3
"""Regenerate the top-prize TMDB id sets in festivals.py from Wikidata.

Run after a festival hands out its top prize — Cannes in May, Berlin in
February, Sundance in January, Locarno in August, Venice in September:

    python3 tools/refresh_festival_winners.py            # print a diff
    python3 tools/refresh_festival_winners.py --write    # rewrite festivals.py

Each prize is one SPARQL query: films (P31/P279* → film) that received the award
(P166), with their TMDB id (P4947).  Only films carrying a TMDB id can be used —
the sash is looked up by TMDB id — but in practice Wikidata has one for every
winner of all five prizes.  Wikidata's public endpoint is rate-limited, so the
queries are spaced out; the whole run takes under a minute.

No API key is needed, and nothing here runs in production: this writes a source
file that is reviewed and committed like any other change.
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FESTIVALS_PY = REPO_ROOT / "festivals.py"

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "PostersPlus/1.0 (https://github.com/UmbraProjects/PostersPlus)"

# Wikidata item for each top prize, and the id set it populates in festivals.py.
PRIZES: list[tuple[str, str, str]] = [
    # (variable in festivals.py, Wikidata award item, human name for comments)
    ("PALME_DOR_TMDB_IDS",      "Q179808",   "Palme d'Or"),
    ("GOLDEN_LION_TMDB_IDS",    "Q209459",   "Golden Lion"),
    ("GOLDEN_BEAR_TMDB_IDS",    "Q154590",   "Golden Bear"),
    ("GOLDEN_LEOPARD_TMDB_IDS", "Q1700510",  "Golden Leopard"),
    ("SUNDANCE_GJ_TMDB_IDS",    "Q3774974",  "Sundance GJ"),
]

# The P31/P279* clause matters: without it the query also returns the directors
# the prize is credited to on their own items, which have no TMDB id.
QUERY = """
SELECT ?film ?filmLabel ?year ?tmdb WHERE {
  ?film wdt:P166 wd:%s .
  ?film wdt:P31/wdt:P279* wd:Q11424 .
  ?film wdt:P4947 ?tmdb .
  OPTIONAL { ?film wdt:P577 ?date BIND(YEAR(?date) AS ?year) }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

# Seconds between queries.  The public endpoint answers 429 to a tight loop.
QUERY_SPACING = 6.0


def fetch(award_item: str) -> list[tuple[int, int | None, str]]:
    """Return (tmdb_id, release_year, title) for every winner of one prize."""
    url = f"{ENDPOINT}?" + urllib.parse.urlencode(
        {"query": QUERY % award_item, "format": "json"}
    )
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        rows = json.load(response)["results"]["bindings"]

    # A film with several release dates comes back once per date, so collapse by
    # entity and keep the latest year — it is only a comment on the id anyway.
    films: dict[str, dict] = {}
    for row in rows:
        entity = row["film"]["value"]
        entry = films.setdefault(
            entity,
            {"tmdb": int(row["tmdb"]["value"]),
             "title": row.get("filmLabel", {}).get("value", "?"),
             "year": None},
        )
        if "year" in row:
            year = int(row["year"]["value"])
            entry["year"] = year if entry["year"] is None else max(entry["year"], year)

    return sorted(
        ((f["tmdb"], f["year"], f["title"]) for f in films.values()),
        key=lambda f: (f[1] or 0, f[2]),
    )


def render_block(variable: str, prize: str, festival: str,
                 winners: list[tuple[int, int | None, str]]) -> str:
    lines = [
        f"# {prize} — every {festival} top-prize winner Wikidata knows "
        f"({len(winners)} films).",
        f"{variable}: frozenset[int] = frozenset({{",
    ]
    for tmdb_id, year, title in winners:
        lines.append(f"    {str(tmdb_id) + ',':<10} # {year or '????'}  {title}")
    lines.append("})")
    return "\n".join(lines)


def festival_name(variable: str, source: str) -> str:
    """Recover the festival name from the comment already in festivals.py."""
    match = re.search(
        rf"^# .*? — every (\S+) top-prize winner .*?\n{re.escape(variable)}:",
        source, re.MULTILINE,
    )
    return match.group(1) if match else "?"


def block_pattern(variable: str) -> re.Pattern:
    """Match one generated block and nothing else.

    The anchors matter.  ``PALME_DOR_TMDB_IDS`` is also a substring of
    ``PALME_DOR_EXTRA_TMDB_IDS`` and appears again inside the ``_top_prize``
    call, so a loose pattern would rewrite the hand-curated additions or the
    FESTIVALS table instead of the block it was aiming at.
    """
    return re.compile(
        rf"^# [^\n]*\n(?:# [^\n]*\n)*{re.escape(variable)}: frozenset\[int\] = "
        rf"frozenset\(\{{\n.*?^\}}\)",
        re.MULTILINE | re.DOTALL,
    )


def current_ids(source: str, variable: str) -> set[str]:
    match = block_pattern(variable).search(source)
    return set(re.findall(r"^    (\d+),", match.group(0), re.MULTILINE)) if match else set()


def replace_block(source: str, variable: str, block: str) -> str:
    updated, count = block_pattern(variable).subn(lambda _m: block, source, count=1)
    if count != 1:
        sys.exit(f"could not locate the {variable} block in festivals.py")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="rewrite festivals.py instead of only reporting")
    args = parser.parse_args()

    source = FESTIVALS_PY.read_text(encoding="utf-8")
    updated = source
    changed = False

    for index, (variable, award_item, prize) in enumerate(PRIZES):
        if index:
            time.sleep(QUERY_SPACING)
        winners = fetch(award_item)
        if not winners:
            sys.exit(f"{prize}: Wikidata returned nothing — refusing to empty the set")

        festival = festival_name(variable, source)
        block = render_block(variable, prize, festival, winners)
        candidate = replace_block(updated, variable, block)

        before = current_ids(updated, variable)
        after = {str(w[0]) for w in winners}
        added, removed = sorted(after - before), sorted(before - after)

        titles = {str(t): (y, n) for t, y, n in winners}
        status = "unchanged" if not (added or removed) else "CHANGED"
        print(f"{prize:16} {len(winners):4} films  {status}")
        for tmdb_id in added:
            year, title = titles[tmdb_id]
            print(f"    + {tmdb_id:<9} {year or '????'}  {title}")
        for tmdb_id in removed:
            print(f"    - {tmdb_id}")

        # A hand-added winner that P166 now records is a duplicate, not a bug —
        # say so, because nothing else would ever prompt tidying the extras.
        extra_var = variable.replace("_TMDB_IDS", "_EXTRA_TMDB_IDS")
        caught_up = sorted(current_ids(updated, extra_var) & after)
        for tmdb_id in caught_up:
            year, title = titles[tmdb_id]
            print(f"    ~ {tmdb_id:<9} {year or '????'}  {title}"
                  f"  — now in Wikidata, drop it from {extra_var}")

        updated = candidate
        changed = changed or bool(added or removed)

    if not changed:
        print("\nfestivals.py is already up to date.")
        return 0

    if not args.write:
        print("\nRe-run with --write to apply.")
        return 0

    FESTIVALS_PY.write_text(updated, encoding="utf-8")
    print(f"\nWrote {FESTIVALS_PY.relative_to(REPO_ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
