#!/usr/bin/env python3
"""Audit the top-prize id sets against each festival's own winners table.

    python3 tools/audit_festival_winners.py

Wikidata's P166 is the generator (tools/refresh_festival_winners.py); this is
the check on it, and the two disagree more than you would expect.  P166 had no
record of Joker's 2019 Golden Lion or four recent Locarno Leopards, and it
credited the Golden Bear to three Best-Short-Film winners, two sidebar prizes,
and — via a mismatched id — the 2009 science-fiction film *Push* rather than
*Precious*, which premiered at Sundance under the title "Push: Based on the
Novel by Sapphire".

Every winner is resolved wikilink -> Wikidata item -> TMDB id, never by title
text, so nothing here rests on a fuzzy match.  Rows are dropped when the target
is not a film (Cannes 1939 links the outbreak of the war), when the edition gave
no award (Berlin 1970 links *o.k.*, the film the jury collapsed over, which is
the one film that certainly did not win), and when the cell carries a qualifier
such as "(Best Musical Comedy)" — Cannes 1947 awarded by genre, and without that
filter *Dumbo* becomes a Palme d'Or winner.

Output has two halves.  MISSING is a winner the table lists that we do not have;
add it to the prize's *_EXTRA_TMDB_IDS.  UNCONFIRMED is an id we have that the
table does not list; check it by hand before acting — some are genuine table
rows this scraper cannot read (a shared prize links only one film, Sundance 2002
links only its director), and those belong where they are.

Responses are cached under tools/.audit-cache, so a re-run costs nothing and a
rate-limited run resumes instead of restarting.  Delete that directory to
refetch.
"""

import hashlib, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
from html.parser import HTMLParser

UA = {"User-Agent": "PostersPlus/1.0 (https://github.com/UmbraProjects/PostersPlus)"}
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".audit-cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def api(host, params, attempts=9):
    query = urllib.parse.urlencode({**params, "format": "json"})
    key = hashlib.sha256(f"{host}?{query}".encode()).hexdigest()[:32]
    path = os.path.join(CACHE_DIR, f"{key}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    delay = 5.0
    for n in range(attempts):
        try:
            req = urllib.request.Request(f"https://{host}/w/api.php?{query}", headers=UA)
            with urllib.request.urlopen(req, timeout=90) as resp:
                doc = json.load(resp)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            time.sleep(1.0)          # be a good citizen between live fetches
            return doc
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 503) or n == attempts - 1:
                raise
            print(f"    [{exc.code}] backing off {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 1.8, 120)


class TableParser(HTMLParser):
    """Collect (year, [wiki titles]) per row of every winners table."""
    def __init__(self):
        super().__init__()
        self.tables, self.tbl = [], None
        self.row, self.cell = None, None
        self.depth = 0
        self.pending_year = None      # carried across a rowspan'd year cell
        self.rowspan_left = 0
        self.in_cell = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.tbl = {"headers": [], "rows": []}
        elif self.depth == 1 and tag == "tr":
            self.row = {"year": None, "links": [], "text": []}
        elif self.depth == 1 and tag in ("td", "th"):
            self.in_cell = True
            self.cell = {"tag": tag, "links": [], "text": "",
                         "rowspan": int(a.get("rowspan", 1) or 1)}
        elif self.in_cell and tag == "a":
            href = a.get("href", "")
            if href.startswith("/wiki/") and ":" not in href[6:]:
                self.cell["links"].append(urllib.parse.unquote(href[6:]))

    def handle_data(self, data):
        if self.in_cell:
            self.cell["text"] += data

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            self.in_cell = False
            if self.row is not None:
                self.row.setdefault("cells", []).append(self.cell)
        elif tag == "tr" and self.depth == 1 and self.row is not None:
            self.tbl["rows"].append(self.row.get("cells", []))
            self.row = None
        elif tag == "table":
            if self.depth == 1 and self.tbl is not None:
                self.tables.append(self.tbl)
                self.tbl = None
            self.depth -= 1

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import festivals as F

PRIZES = [
    ("PALME_DOR_TMDB_IDS",      "Palme d'Or",                "Palme d'Or",     F.PALME_DOR_TMDB_IDS),
    ("GOLDEN_LION_TMDB_IDS",    "Golden Lion",               "Golden Lion",    F.GOLDEN_LION_TMDB_IDS),
    ("GOLDEN_BEAR_TMDB_IDS",    "Golden Bear",               "Golden Bear",    F.GOLDEN_BEAR_TMDB_IDS),
    ("GOLDEN_LEOPARD_TMDB_IDS", "Golden Leopard",            "Golden Leopard", F.GOLDEN_LEOPARD_TMDB_IDS),
    ("SUNDANCE_GJ_TMDB_IDS",    "Grand Jury Prize Dramatic", "Sundance GJ",    F.SUNDANCE_GJ_TMDB_IDS),
]

SKIP_SECTION = re.compile(r"special|honorary|lifetime|international", re.I)
DISQUALIFY   = re.compile(r"\((best|second|shared|ex[- ]aequo|special|honou?rary)[^)]*\)", re.I)
# A cancelled or prize-less edition still gets a row, and its prose often links a
# film -- Berlin 1970 links o.k., the film the jury collapsed over, which is the
# one film that certainly did not win.  Cannes 1939/2020 link an event instead,
# which the film-type check already catches; these do not.
NO_AWARD     = re.compile(r"no award|not award|no prize|cancell?ed|was to have|"
                          r"did not take place|interrupted", re.I)
FILM_TYPES = {"Q11424", "Q24869", "Q506240", "Q226730", "Q202866", "Q93204",
              "Q29168811", "Q130232", "Q17517379", "Q20650540", "Q1054574",
              "Q24856", "Q2431196", "Q1361932", "Q319221", "Q157394"}

def leaf_sections(page):
    secs = api("en.wikipedia.org", {"action": "parse", "page": page, "prop": "sections"})["parse"]["sections"]
    chosen, inside, level = [], False, None
    for s in secs:
        if s["line"].strip().lower() == "winners":
            inside, level = True, int(s["level"]); chosen.append(s); continue
        if inside:
            if int(s["level"]) <= level:
                break
            chosen.append(s)
    if len(chosen) > 1:
        chosen = chosen[1:]
    return [s for s in chosen if not SKIP_SECTION.search(s["line"])]

def rows_for(page):
    """[(year, [candidate links])] — every link in the winner cell, in order."""
    seen, found = set(), []
    for sec in leaf_sections(page):
        doc = api("en.wikipedia.org", {"action": "parse", "page": page,
                                       "prop": "text", "section": sec["index"]})
        p = TableParser(); p.feed(doc["parse"]["text"]["*"])
        for tbl in p.tables:
            if not tbl["rows"]:
                continue
            header = [c["text"].strip().lower() for c in tbl["rows"][0]]
            if not any(h.startswith("year") for h in header):
                continue
            if not any(("title" in h or "film" in h or h.startswith("winner"))
                       for h in header):
                continue
            carry_year, carry_left = None, 0
            for cells in tbl["rows"][1:]:
                if not cells:
                    continue
                year, rest, first = None, cells, cells[0]
                m = YEAR_RE.search(first["text"])
                if m and len(first["text"].strip()) <= 8:
                    year = int(m.group(0))
                    carry_year, carry_left = year, first["rowspan"] - 1
                    rest = cells[1:]
                elif carry_left > 0:
                    year, carry_left = carry_year, carry_left - 1
                if year is None:
                    continue
                if NO_AWARD.search(" ".join(c["text"] for c in rest)):
                    continue
                links, blocked = [], False
                for cell in rest:
                    if not cell["links"]:
                        continue
                    if DISQUALIFY.search(cell["text"]):
                        blocked = True
                        break
                    links.extend(cell["links"])
                    if len(links) >= 4:
                        break
                if blocked or not links:
                    continue
                key = (year, tuple(links))
                if key not in seen:
                    seen.add(key); found.append((year, links))
    return found

_CANON = os.path.join(CACHE_DIR, "redirects.json")
_canon = json.load(open(_CANON)) if os.path.exists(_CANON) else {}
def canonical(titles):
    """Follow enwiki redirects so a wikilink matches Wikidata's sitelink."""
    todo = [t for t in dict.fromkeys(titles) if t not in _canon]
    for i in range(0, len(todo), 40):
        chunk = todo[i:i + 40]
        doc = api("en.wikipedia.org", {"action": "query", "redirects": "1",
                                       "titles": "|".join(t.replace("_", " ") for t in chunk)})
        q = doc.get("query", {})
        remap = {r["from"]: r["to"] for r in q.get("redirects", [])}
        remap.update({n["from"]: n["to"] for n in q.get("normalized", [])})
        for t in chunk:
            step = t.replace("_", " ")
            for _ in range(3):
                step = remap.get(step, step)
            _canon[t] = step.replace(" ", "_")
        json.dump(_canon, open(_CANON, "w"))
    return _canon

_WD = os.path.join(CACHE_DIR, "wikidata.json")
_wd = json.load(open(_WD)) if os.path.exists(_WD) else {}
def resolve(titles):
    todo = [t for t in dict.fromkeys(titles) if t not in _wd]
    for i in range(0, len(todo), 25):
        chunk = todo[i:i + 25]
        doc = api("www.wikidata.org", {
            "action": "wbgetentities", "sites": "enwiki",
            "titles": "|".join(t.replace("_", " ") for t in chunk),
            "props": "claims|sitelinks",
        })
        got = {}
        for qid, ent in (doc.get("entities") or {}).items():
            if qid.startswith("-"):
                continue
            link = (ent.get("sitelinks", {}).get("enwiki", {}) or {}).get("title")
            if not link:
                continue
            claims = ent.get("claims", {})
            tmdb = next((int(c["mainsnak"]["datavalue"]["value"])
                         for c in claims.get("P4947", [])
                         if c.get("mainsnak", {}).get("datavalue")), None)
            inst = sorted({c["mainsnak"]["datavalue"]["value"]["id"]
                           for c in claims.get("P31", [])
                           if c.get("mainsnak", {}).get("datavalue")})
            got[link.replace(" ", "_")] = {"qid": qid, "tmdb": tmdb, "inst": inst}
        for t in chunk:
            _wd[t] = got.get(t)
        json.dump(_wd, open(_WD, "w"))
    return _wd

PAGE_FOR = {prize: page for _v, page, prize, _s in PRIZES}


def winners(page):
    """[(year, title, tmdb)] — one film per winning row, redirects followed."""
    rows = rows_for(page)
    canonical([l for _y, ls in rows for l in ls])
    resolve([_canon[l] for _y, ls in rows for l in ls])
    out = []
    for year, links in rows:
        for link in links:
            meta = _wd.get(_canon[link])
            if meta and meta["tmdb"] and set(meta["inst"]) & FILM_TYPES:
                out.append((year, _canon[link].replace("_", " "), meta["tmdb"]))
                break
    return out


def main() -> int:
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "festivals.py"), encoding="utf-8").read()
    names = {int(m.group(1)): (m.group(2), m.group(3).strip())
             for m in re.finditer(r"^    (\d+),\s+# (\S+)\s+(.*)$", src, re.M)}

    print(f"{'prize':16} {'ids':>5} {'table':>6} {'confirmed':>10} {'unconfirmed':>12}")
    print("-" * 55)
    total = confirmed_total = 0
    notes = []
    for fest in F.FESTIVALS:
        page = PAGE_FOR[fest.top_prize]
        listed = winners(page)
        listed_ids = {tmdb for _y, _n, tmdb in listed}
        ours = fest.top_prize_ids
        confirmed = ours & listed_ids
        print(f"{fest.top_prize:16} {len(ours):5} {len(listed):6} {len(confirmed):10} "
              f"{len(ours - listed_ids):12}")
        total += len(ours)
        confirmed_total += len(confirmed)
        for year, title, tmdb in sorted(listed):
            if tmdb not in ours:
                notes.append(("MISSING", fest.top_prize, tmdb, str(year), title))
        for tmdb in sorted(ours - listed_ids):
            year, title = names.get(tmdb, ("?", "?"))
            notes.append(("UNCONFIRMED", fest.top_prize, tmdb, year, title))
    print("-" * 55)
    pct = 100 * confirmed_total / total if total else 0
    print(f"{'TOTAL':16} {total:5} {'':6} {confirmed_total:10} {total - confirmed_total:12}"
          f"   ({pct:.1f}% confirmed)")

    for kind in ("MISSING", "UNCONFIRMED"):
        rows = [n for n in notes if n[0] == kind]
        if not rows:
            continue
        print(f"\n{kind}:")
        for _k, prize, tmdb, year, title in rows:
            print(f"  {prize:16} {tmdb:<9} {year}  {title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
