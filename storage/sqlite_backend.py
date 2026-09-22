"""SQLite storage backend — the default, used when DATABASE_URL is unset.

Seeded from upstream's cache.py (v1.2.0) so it carries upstream's full
schema (rating/quality/metadata/trending/release-status/text-detection/
movie-release-info/tvdb/app-state caches) and its concurrency hardening
verbatim: thread-local connections, WAL, busy_timeout, BEGIN IMMEDIATE
schema creation, and the additive ``_add_column_if_missing`` migrations.

The ElfHosted fork grafts on top of that base, kept deliberately small so
upstream cache.py changes still map almost 1-to-1:

  * Composite (rendered) poster BYTES move to the ``blobstore`` package
    (local FS default, S3/CDN via OBJECT_STORE_URL). final_poster_cache
    keeps only the metadata row (cache_key + cached_at + request_params +
    expires_at) for TTL/freshness; the JPEG itself never bloats the
    relational backend. This makes the final-poster read/write/delete trio
    async. Upstream's L1 RAM LRU is kept as-is.
  * imdb_to_tmdb_cache table + get/set — lets the anonymous /p preset
    route skip a TMDB /find call on every public hit.
  * count_digital_releases / ping / close — lifecycle + /ready + /stats
    helpers the hosted layer expects.

The Postgres backend (storage/postgres_backend.py) mirrors this surface.
"""
import asyncio
import hashlib
import logging
import os
import sqlite3
import threading
import tempfile
import time
import json
from collections import OrderedDict
from datetime import datetime

from festivals import LEGACY_LABEL_KEYWORDS
import config as _cfg

logger = logging.getLogger(__name__)

import blobstore
from config import (
    DB_PATH,
    DAYS_CONSIDERED_NEW,
    NEW_CACHE_DURATION,
    OLD_CACHE_DURATION,
    TRENDING_CACHE_DURATION,
    TMDB_POSTER_CACHE_DIR,
    TMDB_POSTER_CACHE_DURATION,
    TMDB_LOGO_CACHE_DIR,
    TMDB_LOGO_CACHE_DURATION,
    TMDB_IMAGE_CACHE_JITTER_DAYS,
    TMDB_METADATA_CACHE_DURATION,
    COMPOSITE_CACHE_TTL,
    COMPOSITE_CACHE_TTL_JITTER,
    COMPOSITE_MAX_ENTRIES,
    COMPOSITE_MEM_ENTRIES,
    QUALITY_OLD_CACHE_DURATION,
    DIGITAL_RELEASE_MAX_AGE_DAYS,
    RATING_MIN_VOTES,
    IMAGE_FORMAT as _IMAGE_FORMAT,
)


def _ttl_jitter(cache_key: str, window: float) -> float:
    """Deterministic +/- window/2 offset derived from cache_key, so the same
    key always gets the same jitter (stable across reads and cache-warm
    cycles) while spreading expiry times across a batch of keys."""
    if window <= 0:
        return 0.0
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()[:8]
    return (int(digest, 16) / 0xFFFFFFFF) * window - window / 2

# One SQLite connection PER THREAD (thread-local).  A single shared connection
# serialises every statement — reads included — on its internal mutex, so under
# load reads queue behind one another and behind writes.  Per-thread connections
# let WAL's concurrent readers actually run in parallel; writes are still
# serialised within this process by _db_lock, and across worker processes by
# SQLite plus the busy timeout below.
_local = threading.local()
_db_lock = threading.Lock()     # serialises writes within this process
_initialised = False


def _apply_conn_pragmas(conn: sqlite3.Connection) -> None:
    """Connection-level PRAGMAs, applied to every connection.  (journal_mode=WAL
    and auto_vacuum are DB-level and persist in the file, so they're set once in
    init_db.)"""
    conn.execute("PRAGMA synchronous=NORMAL")       # safe with WAL; avoids unnecessary fsyncs
    # 4 MB in-process page cache.  This is PER CONNECTION and connections are
    # per-thread (see above), so the real cost is this figure times every thread
    # that touches the DB — the asyncio default executor alone is
    # min(32, cpu_count+4) threads, plus the loop thread and the OCR workers.
    # At the old 32 MB that ceiling was ~350 MB on a 4-core host, nearly all of
    # it redundant: the same pages are already in the host's OS page cache, so
    # copies two-through-eleven only save a memcpy, never a disk read.
    conn.execute("PRAGMA cache_size=-4000")
    conn.execute("PRAGMA temp_store=MEMORY")        # temp tables/indices stay in RAM
    conn.execute("PRAGMA busy_timeout=15000")       # wait up to 15s if another worker holds the write lock
    conn.execute("PRAGMA wal_autocheckpoint=1000")  # fold WAL back into main DB at 1000 pages (~4 MB)


def _enable_wal_with_retry(conn: sqlite3.Connection) -> None:
    """Enable WAL despite simultaneous worker startup on the same database."""
    for attempt in range(20):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 19:
                raise
            time.sleep(0.1)


def get_db() -> sqlite3.Connection:
    if not _initialised:
        raise RuntimeError("Database not initialized")
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _apply_conn_pragmas(conn)
        _local.conn = conn
    return conn

def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> bool:
    """Apply an additive migration safely when multiple workers start together.

    Returns True only for the caller that actually added the column, so a
    one-shot backfill can be hung off the same call and run exactly once.
    """
    columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column in columns:
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError as exc:
        # Another worker may have added the column after our PRAGMA snapshot.
        if "duplicate column name" not in str(exc).lower():
            raise
        return False
    return True


def init_db() -> None:
    global _initialised
    os.makedirs(TMDB_POSTER_CACHE_DIR, exist_ok=True)
    os.makedirs(TMDB_LOGO_CACHE_DIR, exist_ok=True)
    _initialised = True
    conn = get_db()   # this thread's connection, with the per-connection PRAGMAs

    # Enable incremental auto-vacuum so prune_caches' PRAGMA incremental_vacuum
    # can actually return freed pages to the OS.  auto_vacuum can only be set
    # before the first table is created; an existing DB is converted lazily by a
    # one-time VACUUM in prune_caches (off the event loop).  So we only enable it
    # here on a brand-new database.  Must run before any table is created.
    _is_new_db = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0] == 0
    if _is_new_db:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")

    _enable_wal_with_retry(conn)
    # Serialize all schema creation and additive migrations across workers.
    conn.execute("BEGIN IMMEDIATE")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS rating_cache (
        imdb_id        TEXT PRIMARY KEY,
        ratings_json   TEXT,
        genre          TEXT,
        cached_at      INTEGER,
        release_date   TEXT,
        award_wins     TEXT,
        award_noms     TEXT,
        awards_fetched INTEGER NOT NULL DEFAULT 0,
        festival_label TEXT,
        age_rating     INTEGER,
        is_cult        INTEGER NOT NULL DEFAULT 0,
        is_true_story  INTEGER NOT NULL DEFAULT 0,
        is_metacritic  INTEGER NOT NULL DEFAULT 0,
        rating_min_votes INTEGER,
        festival_keyword TEXT
    )
    """)

    for col, definition in (
        ("award_wins",     "TEXT NOT NULL DEFAULT ''"),
        ("award_noms",     "TEXT NOT NULL DEFAULT ''"),
        ("awards_fetched", "INTEGER NOT NULL DEFAULT 0"),
        ("festival_label", "TEXT"),
        ("age_rating",     "INTEGER"),
        ("is_cult",        "INTEGER NOT NULL DEFAULT 0"),
        ("is_true_story",  "INTEGER NOT NULL DEFAULT 0"),
        ("is_metacritic",  "INTEGER NOT NULL DEFAULT 0"),
        ("rating_min_votes", "INTEGER"),
    ):
        _add_column_if_missing(conn, "rating_cache", col, definition)

    # festival_keyword replaces festival_label: the cache now remembers *which
    # festival* a title won something at and lets festivals.py decide the wording
    # at render time.  Storing the wording was the bug — rows written while
    # "festival-cannes-winner" was read as "Palme d'Or" kept saying Palme d'Or
    # long after the code stopped believing it.
    #
    # The old labels map back to their keyword exactly, so existing rows convert
    # in place instead of costing one MDblist request each.  The five festivals
    # dropped for want of a trustworthy top-prize list have no entry in the map
    # and land on NULL, losing a sash that was never earned.  festival_label is
    # left in the table, unread, so a rollback still finds its data.
    if _add_column_if_missing(conn, "rating_cache", "festival_keyword", "TEXT"):
        conn.executemany(
            "UPDATE rating_cache SET festival_keyword = ? WHERE festival_label = ?",
            [(keyword, label) for label, keyword in LEGACY_LABEL_KEYWORDS.items()],
        )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_cache (
            imdb_id      TEXT PRIMARY KEY,
            tokens       TEXT,
            cached_at    INTEGER,
            release_date TEXT,
            cache_context TEXT NOT NULL DEFAULT ''
        )
    """)
    _add_column_if_missing(
        conn, "quality_cache", "cache_context", "TEXT NOT NULL DEFAULT ''"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trending_cache (
            media_type    TEXT PRIMARY KEY,
            rankings_json TEXT,
            cached_at     INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS tmdb_metadata_cache (
            cache_key           TEXT PRIMARY KEY,
            title               TEXT,
            release_year        TEXT,
            genre_ids           TEXT,
            is_textless         INTEGER,
            poster_path         TEXT,
            logos_json          TEXT,
            cached_at           INTEGER,
            credits_json        TEXT,
            production_cos_json TEXT,
            runtime             INTEGER,
            number_of_seasons   INTEGER,
            number_of_episodes  INTEGER,
            original_language   TEXT,
            backdrop_path       TEXT
        )
    """)

    # Final composite poster cache — METADATA ONLY.
    # ElfHosted fork: the composited JPEG bytes live in the blobstore
    # (local FS default, S3/CDN via OBJECT_STORE_URL). This table keeps
    # only the cache_key + cached_at so TTL/freshness checks stay a cheap
    # relational lookup and the bytes never bloat SQLite/Postgres.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS final_poster_cache (
            cache_key  TEXT PRIMARY KEY,
            cached_at  INTEGER NOT NULL,
            request_params TEXT,
            expires_at INTEGER
        )
    """)
    # Pre-v1.2.0 fork databases created final_poster_cache without these two
    # columns; the CREATE above only applies to a fresh file.
    try:
        conn.execute("ALTER TABLE final_poster_cache ADD COLUMN request_params TEXT")
    except Exception:
        pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_final_poster_cached_at "
        "ON final_poster_cache(cached_at)"
    )
    # Drop the legacy jpeg_bytes column if migrating an old DB that stored
    # bytes inline. SQLite can't DROP COLUMN before 3.35; if unsupported,
    # the column is simply left unused (writes target the named columns).
    try:
        _legacy_cols = {
            row[1] for row in conn.execute(
                "PRAGMA table_info(final_poster_cache)"
            ).fetchall()
        }
        if "jpeg_bytes" in _legacy_cols:
            conn.execute("ALTER TABLE final_poster_cache DROP COLUMN jpeg_bytes")
    except sqlite3.OperationalError:
        pass

    # imdb_id -> tmdb_id mapping (ElfHosted fork). Lets the anonymous /p
    # preset route skip a TMDB /find call on every public hit. The mapping
    # is effectively immutable, so there's no TTL.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS imdb_to_tmdb_cache (
            imdb_id    TEXT NOT NULL,
            media_type TEXT NOT NULL,
            tmdb_id    TEXT NOT NULL,
            PRIMARY KEY (imdb_id, media_type)
        )
    """)

    # Digital release cache.
    # Populated by the r/movieleaks poller; one row per IMDB ID.
    # posted_at is the Reddit post's created_utc (used for expiry).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS digital_release_cache (
            imdb_id   TEXT PRIMARY KEY,
            posted_at INTEGER NOT NULL
        )
    """)

    # Release status cache — populated on demand when the "release_status"
    # sash slot is enabled.  Stored separately from the main metadata cache
    # so users who don't enable the feature never pay the extra API call.
    # cache_key = "{media_type}_{tmdb_id}", status = "BluRay"|"Streaming"|"Cinema"|"Production"
    conn.execute("""
        CREATE TABLE IF NOT EXISTS release_status_cache (
            cache_key TEXT PRIMARY KEY,
            status    TEXT NOT NULL,
            cached_at INTEGER NOT NULL
        )
    """)

    # Movie release-info cache - richer sibling of release_status_cache for
    # TMDB /release_dates data. Stores JSON with status plus theatrical,
    # digital/TV, and physical dates so multiple sash slots can share one TMDB
    # lookup. cache_key = "{media_type}_{tmdb_id}".
    conn.execute("""
        CREATE TABLE IF NOT EXISTS movie_release_info_cache (
            cache_key TEXT PRIMARY KEY,
            info_json TEXT NOT NULL,
            cached_at INTEGER NOT NULL
        )
    """)

    # Burned-in-text detection results, keyed by source asset + detection params.
    # The PP-OCR scan depends only on the image bytes and confidence, never
    # on the user's URL config — so memoising it here stops the most expensive
    # feature from re-running on every config change (composite-cache miss).
    # TMDB image paths are content-addressed (immutable), so results never go
    # stale; cached_at exists only for housekeeping/pruning.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS text_detection_cache (
            cache_key TEXT PRIMARY KEY,
            has_text  INTEGER NOT NULL,
            cached_at INTEGER NOT NULL
        )
    """)

    # Small generic key/value store for app-level bookkeeping (e.g. the last
    # cache-warm cycle's timestamp) that doesn't warrant its own table.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Generic JSON cache for TVDB bookkeeping: resolved TVDB ids (incl. negative
    # "no match" results), per-title artwork indexes, the artwork-type catalogue,
    # and the auth token.  Each row carries its own TTL so different record kinds
    # (long-lived artwork vs. short negative cache) coexist in one table.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tvdb_cache (
            cache_key   TEXT PRIMARY KEY,
            value_json  TEXT NOT NULL,
            cached_at   INTEGER NOT NULL,
            ttl_seconds INTEGER NOT NULL
        )
    """)

    # Migrate existing tmdb_metadata_cache rows.
    for col, definition in (
        ("credits_json",        "TEXT"),
        ("production_cos_json", "TEXT"),
        ("runtime",             "INTEGER"),
        ("number_of_seasons",   "INTEGER"),
        ("number_of_episodes",  "INTEGER"),
        ("original_language",   "TEXT"),
        ("original_title",      "TEXT"),
        ("backdrop_path",       "TEXT"),
        ("tmdb_status",         "TEXT"),
        ("vote_count",          "INTEGER"),
        ("vote_average",        "REAL"),
        ("text_backdrop_path",  "TEXT"),
        ("original_poster_path","TEXT"),
        ("poster_langs_json",   "TEXT"),
        ("imdb_id",             "TEXT"),
        ("tmdb_release_date",   "TEXT"),
        ("last_air_date",       "TEXT"),
        ("next_episode_json",   "TEXT"),
        ("last_episode_json",   "TEXT"),
        ("seasons_json",        "TEXT"),
        ("metadata_version",    "INTEGER"),
    ):
        _add_column_if_missing(conn, "tmdb_metadata_cache", col, definition)

    # Per-row expiry for the release caches.  Their TTL is no longer a single
    # constant — it depends on the status and on when TMDB says the title next
    # moves — so the deadline is computed at write time and stored.  Rows written
    # before this column existed have expires_at NULL and fall back to the
    # status-tiered TTL measured from cached_at (see _release_row_expiry).
    for table in ("release_status_cache", "movie_release_info_cache"):
        _add_column_if_missing(conn, table, "expires_at", "INTEGER")

    # Same treatment for composites.  A rendered poster is derived from facts
    # that expire on their own schedules — a trending rank, a release status —
    # and the flat COMPOSITE_CACHE_TTL let it outlive them.  The deadline is now
    # computed at write time from whichever input expires soonest.  NULL rows
    # predate the column and fall back to cached_at + TTL + jitter.
    _add_column_if_missing(conn, "final_poster_cache", "expires_at", "INTEGER")

    # Which source a trending snapshot came from.  Without this, changing
    # TRENDING_SOURCE_* had no visible effect until the snapshot aged out on its
    # own — up to a day of an operator setting the variable, restarting, seeing
    # the old rankings and concluding the feature was broken.
    _add_column_if_missing(conn, "trending_cache", "source_sig", "TEXT")

    conn.commit()


# ---------------------------------------------------------------------------
# TTL helper
# ---------------------------------------------------------------------------

def _rating_ttl(release_date: str | None) -> int:
    if not release_date:
        return OLD_CACHE_DURATION
    try:
        days_since = (datetime.now() - datetime.strptime(release_date, "%Y-%m-%d")).days
        return NEW_CACHE_DURATION if days_since <= DAYS_CONSIDERED_NEW else OLD_CACHE_DURATION
    except ValueError:
        return OLD_CACHE_DURATION


def _quality_ttl(release_date: str | None) -> int:
    """Quality data is far more stable than ratings for older titles."""
    if not release_date:
        return QUALITY_OLD_CACHE_DURATION
    try:
        days_since = (datetime.now() - datetime.strptime(release_date, "%Y-%m-%d")).days
        return NEW_CACHE_DURATION if days_since <= DAYS_CONSIDERED_NEW else QUALITY_OLD_CACHE_DURATION
    except ValueError:
        return QUALITY_OLD_CACHE_DURATION


# ---------------------------------------------------------------------------
# Final poster cache  (L1 in-memory LRU + L2 metadata row + blobstore bytes)
# ---------------------------------------------------------------------------
#
# Fork graft over upstream's two-tier composite cache: upstream keeps the JPEG
# in the L2 SQLite row; we keep it in the ``blobstore`` package (local FS by
# default, S3/CDN when OBJECT_STORE_URL is set) and leave only the metadata
# row — cache_key, cached_at, request_params, expires_at — in the relational
# backend. Upstream's L1 RAM LRU is carried verbatim: it is pure process-local
# memory and is worth even more here, because an L1 hit now skips a network
# round trip to object storage rather than a local page-cache read.
#
# The consequence is that the read/write/delete trio is ASYNC in this fork.
# Everything else (jittered expiry, ttl_override capping, the tv/series
# invalidation variants, L1 eviction) matches upstream line for line.

# L1: bounded in-memory LRU — most-recently-used composites served without
# any L2 read, keeping the hot set off the OS page cache.  Each value is
# (expires_at, jpeg_bytes): L1 carries the same deadline as its L2 row, because
# an entry that never ages out in RAM would happily serve a Cinema sash for as
# long as the LRU kept it resident.
_composite_l1: OrderedDict[str, tuple[int, bytes]] = OrderedDict()
_composite_l1_lock = threading.Lock()


def composite_l1_stats() -> dict:
    with _composite_l1_lock:
        count = len(_composite_l1)
        total_bytes = sum(len(data) for _expires_at, data in _composite_l1.values())
    return {"entries": count, "bytes": total_bytes}


def _composite_expiry(cache_key: str, cached_at: float) -> float:
    """Deadline for a composite row written before the expires_at column."""
    return cached_at + COMPOSITE_CACHE_TTL + _ttl_jitter(cache_key, COMPOSITE_CACHE_TTL_JITTER)


def _l1_get(cache_key: str, now: float) -> "tuple[bytes, int] | None":
    """L1 lookup with the same aged-out-on-read drop upstream does."""
    if COMPOSITE_MEM_ENTRIES <= 0:
        return None
    with _composite_l1_lock:
        entry = _composite_l1.get(cache_key)
        if entry is None:
            return None
        expires_at, data = entry
        if now <= expires_at:
            _composite_l1.move_to_end(cache_key)
            return data, int(expires_at)
        # Nothing sweeps L1 on a timer, so an aged-out entry is dropped on the
        # read that finds it and the L2 check takes over.
        del _composite_l1[cache_key]
        return None


def _l1_put(cache_key: str, jpeg_bytes: bytes, expires_at: int) -> None:
    if COMPOSITE_MEM_ENTRIES <= 0:
        return
    with _composite_l1_lock:
        _composite_l1[cache_key] = (int(expires_at), jpeg_bytes)
        _composite_l1.move_to_end(cache_key)
        while len(_composite_l1) > COMPOSITE_MEM_ENTRIES:
            _composite_l1.popitem(last=False)


def _peek_final_poster(cache_key: str) -> int | None:
    """Internal: row-only TTL check. Returns the row's expires_at if a fresh
    metadata row exists, else None. Side-effect: deletes the row on expiry.
    Never touches the blobstore — the caller schedules any blob cleanup in
    its async context."""
    row = get_db().execute(
        "SELECT cached_at, expires_at FROM final_poster_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    if not row:
        return None
    cached_at, expires_at = row
    if expires_at is None:
        expires_at = _composite_expiry(cache_key, cached_at)
    now = time.time()
    if now > expires_at:
        logger.info(
            f"Final poster cache expired for {cache_key} "
            f"({(now - cached_at)/86400:.1f}d old)"
        )
        with _db_lock:
            get_db().execute(
                "DELETE FROM final_poster_cache WHERE cache_key = ?", (cache_key,)
            )
            get_db().commit()
        return None
    return int(expires_at)


def _composite_row_is_live(bucket: str, cache_key: str) -> bool:
    """True when a fresh metadata row still names this blob.

    The drain's authority: the row lives in the shared database, so this answer
    holds across workers and replicas, where the process-local generation map
    cannot. Expired rows are swept by the peek, so they correctly read as not
    live and their blobs are collected.
    """
    if bucket != blobstore.BUCKET_COMPOSITES:
        return False
    return _peek_final_poster(cache_key) is not None


async def is_cached_final_poster_fresh(cache_key: str) -> int | None:
    """Lightweight freshness probe — checks L1, then the metadata row + TTL,
    never pulls the bytes. Lets /poster and /p 302 straight to the CDN when a
    public URL is configured. Deletes the orphaned blob on expiry.

    Returns the composite's expires_at when fresh, else None. Callers may keep
    treating it as a boolean — a unix timestamp is always truthy — but the
    redirect path needs the deadline to set an honest max-age, because with
    CDN_CACHE_TTL=auto there is no flat TTL to fall back on.
    """
    try:
        hit = _l1_get(cache_key, time.time())
        if hit is not None:
            return hit[1]
        expires_at = _peek_final_poster(cache_key)
        if expires_at is not None:
            return expires_at
        blobstore.delete_later(blobstore.BUCKET_COMPOSITES, cache_key)
        return None
    except Exception as exc:
        logger.error(f"Final poster freshness probe error: {exc}")
        return None


async def get_cached_final_poster(cache_key: str) -> bytes | None:
    """Return cached JPEG bytes for a fully composited poster, or None on miss/expiry."""
    entry = await get_cached_final_poster_entry(cache_key)
    return None if entry is None else entry[0]


async def get_cached_final_poster_entry(cache_key: str) -> "tuple[bytes, int] | None":
    """Return (jpeg_bytes, expires_at) for a composited poster, or None on miss.

    Checks the in-memory LRU (L1) first; falls through to the metadata row plus
    a blobstore fetch on miss, and promotes the result to L1 so the next hit is
    served entirely from RAM.

    Prefer the freshness probe + url_for + 302 path when a CDN URL is
    configured; this is the inline-serve fallback (local blobstore, or no
    public URL).
    """
    try:
        hit = _l1_get(cache_key, time.time())
        if hit is not None:
            return hit

        expires_at = _peek_final_poster(cache_key)
        if expires_at is None:
            # Row gone or expired: queue the orphaned blob rather than deleting
            # it here. This read can interleave with another request that is
            # mid-write for the same key, and an inline delete would remove the
            # blob it had just published. The drain re-checks the row before
            # deleting anything, which is the check that makes it safe.
            blobstore.delete_later(blobstore.BUCKET_COMPOSITES, cache_key)
            return None

        data = await blobstore.get(
            blobstore.BUCKET_COMPOSITES, cache_key, max_age_seconds=COMPOSITE_CACHE_TTL,
        )
        if data is None:
            # Metadata row without a blob — the blob was evicted or the write
            # was interrupted. Drop the row so the next request re-renders
            # rather than looping on a permanent phantom hit.
            delete_cached_final_poster(cache_key)
            return None

        _l1_put(cache_key, data, expires_at)
        return data, expires_at
    except Exception as exc:
        logger.error(f"Final poster cache read error: {exc}")
        return None


def get_cached_final_poster_url(cache_key: str) -> str | None:
    """Return a public CDN URL for the composite if the blobstore backend
    provides one (OBJECT_STORE_PUBLIC_URL set). Sync — just constructs the
    URL from the configured prefix; doesn't touch S3. Caller must confirm
    the row exists + isn't expired (is_cached_final_poster_fresh) first."""
    return blobstore.url_for(blobstore.BUCKET_COMPOSITES, cache_key)


async def set_cached_final_poster(
    cache_key: str,
    jpeg_bytes: bytes,
    request_params: str = None,
    ttl_override: int = None,
) -> int:
    """Store a composited JPEG: bytes to the blobstore, metadata row to the
    relational backend, and the bytes into L1 as well.

    *ttl_override* caps the lifetime, in seconds, for a render that depends on
    something shorter-lived than COMPOSITE_CACHE_TTL — a trending rank, a
    release status.  It is a cap on the jittered TTL rather than a value added
    to it, so a one-day override really means one day and not one day plus up
    to COMPOSITE_CACHE_TTL_JITTER.

    Returns the unix time this composite expires.
    """
    now = int(time.time())
    ttl = COMPOSITE_CACHE_TTL + _ttl_jitter(cache_key, COMPOSITE_CACHE_TTL_JITTER)
    if ttl_override is not None:
        ttl = min(ttl, ttl_override)
    expires_at = now + int(ttl)

    try:
        # Write the bytes first so the metadata row is never present without
        # a corresponding blob (which would race a reader into a broken state
        # on the very first hit).
        # The composite is encoded in config.IMAGE_FORMAT (webp by default),
        # not necessarily JPEG. Hardcoding image/jpeg here put the wrong
        # Content-Type on every object, which the app never noticed because it
        # re-serves the bytes itself — but a CDN redirect hands the object
        # straight to the browser with whatever type S3 stored.
        # The key lock is held across the put and the note_write so a drain
        # cannot delete this blob between the two. note_write cancels any
        # delete an earlier invalidation queued for this key — the trending
        # refresh invalidates and immediately re-renders, and without it the
        # next drain would remove the replacement.
        async with blobstore.key_lock(blobstore.BUCKET_COMPOSITES, cache_key):
            await blobstore.put(
                blobstore.BUCKET_COMPOSITES, cache_key, jpeg_bytes,
                content_type=f"image/{_IMAGE_FORMAT}",
            )
            blobstore.note_write(blobstore.BUCKET_COMPOSITES, cache_key)

        # L1 only after the durable write succeeds: an L1 entry whose blob
        # never landed would serve this replica a poster the rest of the
        # fleet cannot see, and would mask the failure until it aged out.
        _l1_put(cache_key, jpeg_bytes, expires_at)

        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO final_poster_cache
                    (cache_key, cached_at, request_params, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, now, request_params, expires_at),
            )
            evict_keys: list[str] = []
            if COMPOSITE_MAX_ENTRIES > 0:
                (count,) = get_db().execute(
                    "SELECT COUNT(*) FROM final_poster_cache"
                ).fetchone()
                overflow = count - COMPOSITE_MAX_ENTRIES
                if overflow > 0:
                    evict_keys = [
                        r[0] for r in get_db().execute(
                            "SELECT cache_key FROM final_poster_cache "
                            "ORDER BY cached_at ASC LIMIT ?",
                            (overflow,),
                        ).fetchall()
                    ]
                    get_db().execute(
                        "DELETE FROM final_poster_cache WHERE cache_key IN "
                        f"({','.join('?' * len(evict_keys))})",
                        evict_keys,
                    )
                    logger.info(f"Composite cache cap: evicted {overflow} oldest entries")
            get_db().commit()

        # Best-effort blob + L1 cleanup for evicted keys, outside the write lock.
        for k in evict_keys:
            if COMPOSITE_MEM_ENTRIES > 0:
                with _composite_l1_lock:
                    _composite_l1.pop(k, None)
            # Queued, not deleted inline: the eviction and a competing
            # re-render of the same key can interleave, and the drain's row
            # check is what tells the two apart.
            blobstore.delete_later(blobstore.BUCKET_COMPOSITES, k)
    except Exception as exc:
        logger.error(f"Final poster cache write error: {exc}")

    return expires_at


def delete_cached_final_poster(cache_key: str) -> None:
    """Remove a composited poster from L1 (RAM) and the metadata row, and queue
    its blob for deletion.

    Sync, like upstream: the blob delete is an object-store round trip, so it
    goes on blobstore's deferred queue rather than forcing every caller of this
    onto the event loop. See blobstore.delete_later.
    """
    if COMPOSITE_MEM_ENTRIES > 0:
        with _composite_l1_lock:
            _composite_l1.pop(cache_key, None)
    try:
        with _db_lock:
            get_db().execute("DELETE FROM final_poster_cache WHERE cache_key = ?", (cache_key,))
            get_db().commit()
    except Exception as exc:
        logger.error(f"Final poster cache delete error: {exc}")
    blobstore.delete_later(blobstore.BUCKET_COMPOSITES, cache_key)


def invalidate_final_posters(tmdb_id: str, media_type: str | None = None) -> None:
    """Invalidate all composited posters for a specific TMDB ID.
    Used when underlying dynamic data (like trending rank or release status)
    changes so the next request renders a fresh poster with updated badges.
    """
    # TV posters are cached under either "tv" or "series" (Stremio requests use
    # "series"), so treat the two as equivalent when filtering by media type —
    # otherwise a trending/status change leaves half the cache stale.
    if media_type in ("tv", "series"):
        type_variants: tuple[str, ...] | None = ("tv", "series")
    elif media_type:
        type_variants = (media_type,)
    else:
        type_variants = None

    if COMPOSITE_MEM_ENTRIES > 0:
        with _composite_l1_lock:
            keys_to_delete = []
            for k in _composite_l1:
                parts = k.split(":")
                if len(parts) >= 3 and parts[1] == tmdb_id:
                    if type_variants is None or parts[2] in type_variants:
                        keys_to_delete.append(k)
            for k in keys_to_delete:
                _composite_l1.pop(k, None)

    # The blobs are keyed by cache_key, so the rows have to be READ before they
    # are deleted — once the LIKE delete has run there is nothing left to name
    # the blobs, and they would sit in object storage with no row to reap them.
    if type_variants is None:
        patterns = [f"%:{tmdb_id}:%"]
    else:
        patterns = [f"%:{tmdb_id}:{_tv}:%" for _tv in type_variants]

    blob_keys: list[str] = []
    try:
        db = get_db()
        for pattern in patterns:
            blob_keys.extend(
                r[0] for r in db.execute(
                    "SELECT cache_key FROM final_poster_cache WHERE cache_key LIKE ?",
                    (pattern,),
                ).fetchall()
            )
        with _db_lock:
            for pattern in patterns:
                get_db().execute(
                    "DELETE FROM final_poster_cache WHERE cache_key LIKE ?",
                    (pattern,),
                )
            get_db().commit()
        logger.info(f"Invalidated final poster cache for tmdb_id={tmdb_id}")
    except Exception as exc:
        logger.error(f"Final poster cache invalidate error: {exc}")

    for k in blob_keys:
        blobstore.delete_later(blobstore.BUCKET_COMPOSITES, k)


def get_cache_stats() -> dict:
    """
    Return row counts for every cache table plus the composite cache's total
    byte size and the DB file size on disk.  Used by the /stats endpoint so
    operators can see cache health at a glance.  Never raises.
    """
    stats: dict = {}
    try:
        db = get_db()
        for table in (
            "rating_cache", "quality_cache", "trending_cache",
            "tmdb_metadata_cache", "final_poster_cache",
            "digital_release_cache", "release_status_cache",
            "movie_release_info_cache", "text_detection_cache",
            "tvdb_cache", "imdb_to_tmdb_cache",
        ):
            try:
                (n,) = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                stats[table] = n
            except Exception:
                stats[table] = None

        # Composite bytes live in the blobstore now, not SQLite — the
        # relational backend only holds metadata rows, so there is no
        # inline byte total to report here.
        stats["composite_bytes"] = None

        try:
            stats["db_file_bytes"] = os.path.getsize(DB_PATH)
        except OSError:
            stats["db_file_bytes"] = None

        l1 = composite_l1_stats()
        stats["composite_l1_entries"] = l1["entries"]
        stats["composite_l1_bytes"]   = l1["bytes"]

        deferred = blobstore.deferred_delete_stats()
        stats["blob_deletes_queued"]  = deferred["queued"]
        stats["blob_deletes_dropped"] = deferred["dropped"]
    except Exception as exc:
        logger.error(f"Cache stats error: {exc}")
    return stats


async def prune_caches() -> None:
    """Prune every cache, without stalling the event loop.

    The relational deletes, the filesystem sweep and — on a legacy database —
    a full VACUUM under an exclusive lock are all synchronous, and they run on
    a cache that can be very large. Awaiting them directly would freeze every
    request and health probe in this worker for the duration; upstream ran the
    equivalent work in an executor for exactly that reason, and this restores
    it. Only the blob-store work, which is genuinely async, stays on the loop.
    """
    expired_composites = await asyncio.to_thread(_prune_sync)

    # Drop the blobs behind the expired composite rows, plus anything the
    # synchronous delete/invalidate paths queued since the last sweep. The
    # expired ones go through the same queue rather than being deleted here,
    # so they get the same row check: a title re-rendered between the prune's
    # DELETE and this line has a live row again, and its blob must survive.
    for k in expired_composites:
        blobstore.delete_later(blobstore.BUCKET_COMPOSITES, k)
    await blobstore.drain_deferred_deletes(is_live=_composite_row_is_live)
    blobstore._forget_generations_if_idle()


async def prune_local_caches() -> None:
    """Pod-local cleanup: the deferred blob queue and the TMDB artwork files.

    Split out of prune_caches because these are per-POD, not shared. Prune is
    leader-elected so exactly one replica rewrites the shared database, but a
    follower still accumulates its own queued blob deletes and its own TMDB
    poster/logo files — neither of which any other replica can clean up for it.
    """
    await blobstore.drain_deferred_deletes(is_live=_composite_row_is_live)
    blobstore._forget_generations_if_idle()
    # High end of the per-key jitter range, so this never deletes a file before
    # get_cached_tmdb_poster/_logo would (they apply the same jitter per key).
    await asyncio.to_thread(_prune_local_files)


def _prune_local_files() -> None:
    _prune_file_cache(
        TMDB_POSTER_CACHE_DIR,
        TMDB_POSTER_CACHE_DURATION + TMDB_IMAGE_CACHE_JITTER_DAYS / 2,
    )
    _prune_file_cache(
        TMDB_LOGO_CACHE_DIR,
        TMDB_LOGO_CACHE_DURATION + TMDB_IMAGE_CACHE_JITTER_DAYS / 2,
    )
    _close_thread_connection()


def _close_thread_connection() -> None:
    """Close only THIS thread's connection.

    Connections are thread-local, and prune now runs on a pooled executor
    thread, so without this each prune could strand a connection on whichever
    thread it landed on. Deliberately not close(): that also flips the module's
    _initialised flag, which would tell every other thread the database had
    been shut down.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None


def _prune_sync() -> list[str]:
    """
    Delete expired rows from every SQLite cache table. Returns the cache_keys
    of the composites it removed, so the caller can drop their blobs.

    Runs on an executor thread — see prune_caches.

    Called periodically by a leader-elected background task in main.py.  All
    tables use a simple age cutoff; pruning everything keeps the DB tidy.

    For rating/quality we use the maximum possible TTL as the cutoff so we
    never delete an entry that might still be considered fresh for a new
    release.  Any surviving-but-expired rows will be evicted lazily on the
    next read as before.

    ElfHosted fork: composite BYTES live in the blobstore, so expired
    composite rows are collected first and their blobs deleted alongside the
    metadata rows.
    """
    now = int(time.time())
    expired_composites: list[str] = []
    try:
        # Collect expired composite keys before deleting the rows so we can
        # also drop their blobs from the blobstore.
        # The predicate MUST stay identical to the DELETE below: a SELECT that
        # matched fewer rows would leave blobs orphaned in object storage with
        # no row left to name them, and one that matched more would delete the
        # bytes out from under a row the read path still considers fresh.
        _composite_prune_where = (
            "(expires_at IS NOT NULL AND expires_at < ?) OR "
            "(expires_at IS NULL AND cached_at < ?)"
        )
        _composite_prune_args = (
            now,
            now - COMPOSITE_CACHE_TTL - COMPOSITE_CACHE_TTL_JITTER // 2,
        )
        try:
            expired_composites = [
                r[0] for r in get_db().execute(
                    "SELECT cache_key FROM final_poster_cache WHERE "
                    + _composite_prune_where,
                    _composite_prune_args,
                ).fetchall()
            ]
        except Exception:
            pass

        with _db_lock:
            db = get_db()

            # Composites — per-row deadline (a render can be pinned to a
            # trending rank or a release status that expires well before
            # COMPOSITE_CACHE_TTL).  Rows predating the expires_at column fall
            # back to the flat TTL plus the largest jitter any key can draw, so
            # this never deletes one the read path would still call fresh.
            r = db.execute(
                "DELETE FROM final_poster_cache WHERE " + _composite_prune_where,
                _composite_prune_args,
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired composite cache entries")

            # Ratings / quality / metadata — use the most generous TTL so we
            # never evict something that could still be considered fresh.
            rating_cutoff   = now - OLD_CACHE_DURATION           * 86400
            quality_cutoff  = now - QUALITY_OLD_CACHE_DURATION   * 86400
            metadata_cutoff = now - TMDB_METADATA_CACHE_DURATION * 86400

            r = db.execute(
                "DELETE FROM rating_cache WHERE cached_at < ?", (rating_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired rating cache entries")

            r = db.execute(
                "DELETE FROM quality_cache WHERE cached_at < ?", (quality_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired quality cache entries")

            r = db.execute(
                "DELETE FROM tmdb_metadata_cache WHERE cached_at < ?", (metadata_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired TMDB metadata cache entries")

            digital_cutoff = now - DIGITAL_RELEASE_MAX_AGE_DAYS * 86400
            r = db.execute(
                "DELETE FROM digital_release_cache WHERE posted_at < ?", (digital_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired digital release cache entries")

            # Expiry is per-row now (see release_status_expiry), so prune on the
            # stored deadline.  Rows predating the expires_at column are only
            # dropped once they are past the LONGEST tier, since their real
            # deadline depends on a status this SQL cannot evaluate — the read
            # path tiers them correctly in the meantime and rewrites them with a
            # deadline as soon as they are refreshed.
            legacy_cutoff = now - max(_RELEASE_STATUS_TTL_DAYS.values()) * 86400
            for table, label in (
                ("release_status_cache",     "release status"),
                ("movie_release_info_cache", "movie release info"),
            ):
                r = db.execute(
                    f"DELETE FROM {table} WHERE "
                    "(expires_at IS NOT NULL AND expires_at < ?) OR "
                    "(expires_at IS NULL AND cached_at < ?)",
                    (now, legacy_cutoff),
                )
                if r.rowcount:
                    logger.info(f"Pruned {r.rowcount} expired {label} cache entries")

            detection_cutoff = now - 180 * 86400
            r = db.execute(
                "DELETE FROM text_detection_cache WHERE cached_at < ?", (detection_cutoff,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} old text-detection cache entries")

            # Each tvdb_cache row stores its own TTL, so expiry is per-row rather
            # than a single cutoff.
            r = db.execute(
                "DELETE FROM tvdb_cache WHERE (? - cached_at) > ttl_seconds", (now,)
            )
            if r.rowcount:
                logger.info(f"Pruned {r.rowcount} expired TVDB cache entries")

            db.commit()

        # Reclaim free pages left by the deletes.
        with _db_lock:
            db = get_db()
            auto_vac = db.execute("PRAGMA auto_vacuum").fetchone()[0]
            if auto_vac == 2:   # INCREMENTAL — cheap, moves a few pages, no long lock
                db.execute("PRAGMA incremental_vacuum(100)")
                db.commit()
            else:
                # Legacy DB created before incremental auto-vacuum (auto_vacuum=0):
                # the incremental pragma is a no-op there, so freed pages (e.g. from
                # evicted composite JPEGs) never return and the file bloats.  Do a
                # one-time conversion: enable INCREMENTAL then full VACUUM to rewrite
                # the DB compactly.  Gated on meaningful dead space so it only fires
                # when worthwhile, and it runs here in the background prune task
                # (off the event loop), so it never blocks request handling.
                page  = db.execute("PRAGMA page_size").fetchone()[0]
                free  = db.execute("PRAGMA freelist_count").fetchone()[0]
                total = db.execute("PRAGMA page_count").fetchone()[0]
                live_mb = page * (total - free) / 1e6
                if page * free > 20 * 1024 * 1024:   # >20 MB reclaimable
                    # VACUUM rewrites ALL live data while holding an exclusive lock.
                    # On a large live set that could exceed busy_timeout and lock out
                    # the other worker process, so cap it: skip (and tell the operator
                    # to VACUUM offline) when the live data is big.  Small DBs convert
                    # in well under a second.  (After the first worker converts,
                    # auto_vacuum becomes INCREMENTAL and every later prune takes the
                    # cheap incremental path above, so this runs at most once.)
                    if live_mb > 256:
                        logger.warning(
                            f"Cache DB has ~{page * free / 1e6:.0f} MB reclaimable but "
                            f"{live_mb:.0f} MB live — skipping automatic VACUUM to avoid "
                            f"a long exclusive lock. Reclaim offline with: "
                            f"sqlite3 {DB_PATH} 'PRAGMA auto_vacuum=INCREMENTAL; VACUUM;'"
                        )
                    else:
                        logger.info(
                            f"Cache DB: one-time conversion to incremental auto-vacuum, "
                            f"reclaiming ~{page * free / 1e6:.0f} MB of dead space "
                            f"({live_mb:.0f} MB live)…"
                        )
                        db.commit()                   # close any open transaction
                        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
                        db.execute("VACUUM")
                        logger.info("Cache DB vacuum complete")

    except Exception as exc:
        logger.error(f"Cache prune error: {exc}")
    finally:
        _close_thread_connection()
    return expired_composites


# ---------------------------------------------------------------------------
# Rating cache
# ---------------------------------------------------------------------------

def get_cached_rating(
    imdb_id: str,
) -> tuple[
    dict[str, float], str, str | None,
    list[str], list[str], bool,
    str | None, int | None,
    bool, bool, bool,
] | None:
    """
    Returns an 11-tuple:
        (ratings_dict, genre, release_date, award_wins, award_noms,
         awards_fetched, festival_keyword, age_rating,
         is_cult, is_true_story, is_metacritic)
    Returns None if the row is absent or expired.

    *festival_keyword* is the raw MDblist keyword ("festival-cannes-winner"),
    not a sash label — festivals.py turns it into wording at render time.
    """
    try:
        row = get_db().execute(
            """
            SELECT ratings_json, genre, cached_at, release_date,
                   award_wins, award_noms, awards_fetched, festival_keyword,
                   age_rating, is_cult, is_true_story, is_metacritic,
                   rating_min_votes
            FROM rating_cache
            WHERE imdb_id = ?
            """,
            (imdb_id,),
        ).fetchone()

        if not row:
            return None

        (ratings_json, genre, cached_at, release_date,
         wins_raw, noms_raw, awards_fetched_int, festival_keyword,
         age_rating, is_cult_int, is_true_story_int, is_metacritic_int,
         rating_min_votes) = row

        if rating_min_votes is not None and rating_min_votes != RATING_MIN_VOTES:
            logger.info(
                f"Rating cache policy changed for {imdb_id}: "
                f"stored={rating_min_votes!r}, current={RATING_MIN_VOTES}; refreshing"
            )
            with _db_lock:
                get_db().execute(
                    "DELETE FROM rating_cache WHERE imdb_id = ?",
                    (imdb_id,),
                )
                get_db().commit()
            return None

        age_days = (time.time() - cached_at) / 86400

        if age_days > _rating_ttl(release_date):
            logger.info(f"Rating cache expired for {imdb_id} ({age_days:.1f}d old)")
            with _db_lock:
                get_db().execute(
                    "DELETE FROM rating_cache WHERE imdb_id = ?",
                    (imdb_id,),
                )
                get_db().commit()
            return None

        if rating_min_votes is None:
            # Rows created before policy tracking are still valid until their
            # normal TTL expires. Backfill in place instead of consuming one
            # MDBList request per legacy cache entry after an upgrade.
            with _db_lock:
                get_db().execute(
                    "UPDATE rating_cache SET rating_min_votes = ? "
                    "WHERE imdb_id = ? AND rating_min_votes IS NULL",
                    (RATING_MIN_VOTES, imdb_id),
                )
                get_db().commit()
            logger.debug(f"Backfilled rating cache policy for {imdb_id}")

        ratings_dict = json.loads(ratings_json or "{}")
        wins = [w for w in (wins_raw or "").split("|") if w]
        noms = [n for n in (noms_raw or "").split("|") if n]
        awards_fetched = bool(awards_fetched_int)

        return (ratings_dict, genre, release_date, wins, noms,
                awards_fetched, festival_keyword, age_rating,
                bool(is_cult_int), bool(is_true_story_int), bool(is_metacritic_int))

    except Exception as exc:
        logger.error(f"Cache read error: {exc}")
        return None


def set_cached_rating(
    imdb_id: str,
    ratings_dict: dict,
    genre: str,
    rel: str | None,
    award_wins: list[str],
    award_noms: list[str],
    awards_fetched: bool = False,
    festival_keyword: str | None = None,
    age_rating: int | None = None,
    is_cult: bool = False,
    is_true_story: bool = False,
    is_metacritic: bool = False,
) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO rating_cache
                    (
                        imdb_id,
                        ratings_json,
                        genre,
                        cached_at,
                        release_date,
                        award_wins,
                        award_noms,
                        awards_fetched,
                        festival_keyword,
                        age_rating,
                        is_cult,
                        is_true_story,
                        is_metacritic,
                        rating_min_votes
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    imdb_id,
                    json.dumps(ratings_dict),
                    genre,
                    int(time.time()),
                    rel,
                    "|".join(award_wins or []),
                    "|".join(award_noms or []),
                    int(awards_fetched),
                    festival_keyword,
                    age_rating,
                    int(is_cult),
                    int(is_true_story),
                    int(is_metacritic),
                    RATING_MIN_VOTES,
                ),
            )
            get_db().commit()

    except Exception as exc:
        logger.error(f"Cache write error: {exc}")


# ---------------------------------------------------------------------------
# Quality cache
# ---------------------------------------------------------------------------

def _quality_cache_context() -> str:
    """Policy identity for cached tokens, without storing credentials."""
    source = _cfg.QUALITY_SOURCE
    if source == "qualicache":
        # The token fold in quality.fetch_quality_from_qualicache is part of the
        # policy: bump the suffix whenever it changes so stored answers refresh.
        return f"qualicache:{_cfg.QUALICACHE_MIN_TRUST}:fold1"
    return source if source in ("aiostreams", "scraper") else "aiostreams"

def get_cached_quality(imdb_id: str, release_date: str | None = None) -> list[str] | None:
    try:
        row = get_db().execute(
            """SELECT tokens, cached_at, release_date, cache_context
               FROM quality_cache WHERE imdb_id = ?""",
            (imdb_id,),
        ).fetchone()
        if row is None:
            return None

        tokens_raw, cached_at, stored_release, stored_context = row
        if stored_context != _quality_cache_context():
            logger.info(f"Quality cache policy changed for {imdb_id}; refreshing")
            with _db_lock:
                get_db().execute("DELETE FROM quality_cache WHERE imdb_id = ?", (imdb_id,))
                get_db().commit()
            return None
        ttl_release = release_date or stored_release
        age_days    = (time.time() - cached_at) / 86400
        if age_days > _quality_ttl(ttl_release):
            logger.info(f"Quality cache expired for {imdb_id} ({age_days:.1f}d old)")
            with _db_lock:
                get_db().execute("DELETE FROM quality_cache WHERE imdb_id = ?", (imdb_id,))
                get_db().commit()
            return None

        return [t for t in (tokens_raw or "").split("|") if t]

    except Exception as exc:
        logger.error(f"Quality cache read error: {exc}")
        return None


def set_cached_quality(
    imdb_id: str,
    tokens: list[str],
    release_date: str | None = None,
) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO quality_cache
                    (imdb_id, tokens, cached_at, release_date, cache_context)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    imdb_id,
                    "|".join(tokens),
                    int(time.time()),
                    release_date,
                    _quality_cache_context(),
                ),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Quality cache write error: {exc}")


# ---------------------------------------------------------------------------
# Trending cache  (snapshot-based — one row per media type)
#
# NOTE: The old per-item get_cached_trending / set_cached_trending helpers
# referenced columns ("rank", "tmdb_id") that never existed in the actual
# schema and always raised OperationalError at runtime.  They are removed.
# All callers use get_cached_trending_snapshot / set_cached_trending_snapshot.
# ---------------------------------------------------------------------------

def get_cached_trending_snapshot(
    media_type: str, source_sig: str | None = None
) -> dict[str, int] | None:
    """Cached rankings for *media_type*, or None if absent, stale, or from a
    different source.

    *source_sig* identifies where the snapshot came from (see
    tmdb.trending_source_signature).  A mismatch is treated as expired so that
    changing TRENDING_SOURCE_* takes effect on the next request rather than
    whenever the day-long TTL happens to lapse.
    """
    try:
        row = get_db().execute(
            """
            SELECT rankings_json, cached_at, source_sig
            FROM trending_cache
            WHERE media_type = ?
            """,
            (media_type,),
        ).fetchone()

        if not row:
            return None

        rankings_json, cached_at, stored_sig = row
        age_days = (time.time() - cached_at) / 86400

        if age_days > TRENDING_CACHE_DURATION:
            return None

        if source_sig is not None and (stored_sig or "") != source_sig:
            logger.info(
                f"Trending snapshot for {media_type} discarded: source changed "
                f"({stored_sig or 'unset'!r} -> {source_sig!r})"
            )
            return None

        return json.loads(rankings_json)
    except Exception as exc:
        logger.error(f"Trending snapshot cache read error: {exc}")
        return None


def set_cached_trending_snapshot(
    media_type: str,
    rankings: dict[str, int],
    source_sig: str | None = None,
) -> None:
    # Deliberately read without the signature: the point of this is to diff
    # against whatever was there before so changed titles get invalidated, and a
    # source switch changes the most ranks of all.
    old_rankings = get_cached_trending_snapshot(media_type) or {}
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO trending_cache
                (media_type, rankings_json, cached_at, source_sig)
                VALUES (?, ?, ?, ?)
                """,
                (
                    media_type,
                    json.dumps(rankings),
                    int(time.time()),
                    source_sig or "",
                ),
            )
            get_db().commit()

        # A snapshot that went from populated to empty is an unreadable source
        # far more often than a list that genuinely emptied overnight, and the
        # diff below would flush every trending composite on the strength of it.
        # Store it — a broken source should stay visible in the sash — but leave
        # the composites for the next successful refresh to invalidate.
        if not rankings and old_rankings:
            logger.warning(
                f"Trending snapshot for {media_type} is now empty (was "
                f"{len(old_rankings)} entries) — not invalidating composites"
            )
            return

        # Invalidate final posters for items that changed trending rank or dropped out.
        changed_ids = set()
        for t_id, r in rankings.items():
            if old_rankings.get(t_id) != r:
                changed_ids.add(t_id)
        for t_id in old_rankings:
            if t_id not in rankings:
                changed_ids.add(t_id)
                
        for t_id in changed_ids:
            invalidate_final_posters(t_id, media_type)
            
    except Exception as exc:
        logger.error(f"Trending snapshot cache write error: {exc}")


# ---------------------------------------------------------------------------
# Filesystem cache helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: str, data: bytes) -> None:
    """Atomically replace *path* so readers never observe partial image bytes."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=os.path.dirname(path), prefix=".tmp-", delete=False
        ) as tmp:
            temp_path = tmp.name
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _prune_file_cache(base_dir: str, ttl_days: float) -> None:
    cutoff = time.time() - ttl_days * 86400
    removed = 0
    try:
        for entry in os.scandir(base_dir):
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.remove(entry.path)
                    removed += 1
            except FileNotFoundError:
                pass
        if removed:
            logger.info(f"Pruned {removed} expired files from {base_dir}")
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning(f"File-cache prune failed for {base_dir}: {exc}")


# ---------------------------------------------------------------------------
# TMDB poster cache
# ---------------------------------------------------------------------------

def get_cached_tmdb_poster(cache_key: str) -> bytes | None:
    # Extension is now .jpg — posters are stored as JPEG for faster decode.
    path = _safe_cache_path(TMDB_POSTER_CACHE_DIR, cache_key)

    if not os.path.exists(path):
        return None

    age_days = (time.time() - os.path.getmtime(path)) / 86400
    effective_days = TMDB_POSTER_CACHE_DURATION + _ttl_jitter(cache_key, TMDB_IMAGE_CACHE_JITTER_DAYS)

    if age_days > effective_days:
        logger.info(f"TMDB poster cache expired for {cache_key}")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return None

    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as exc:
        logger.error(f"TMDB poster cache read error: {exc}")
        return None


def set_cached_tmdb_poster(cache_key: str, data: bytes) -> None:
    # Store as .jpg — written by tmdb.py as JPEG q=92 RGB, then converted
    # back to RGBA on load.  ~4x faster decode vs PNG, ~5x smaller on disk.
    try:
        path = _safe_cache_path(TMDB_POSTER_CACHE_DIR, cache_key)
        _atomic_write(path, data)
    except Exception as exc:
        logger.error(f"TMDB poster cache write error: {exc}")


# ---------------------------------------------------------------------------
# TMDB logo cache
# ---------------------------------------------------------------------------

def _remove_if_dir(path: str) -> bool:
    """Remove *path* if it is a directory (stale artefact from a previous bug).
    Returns True if a directory was found and removed."""
    if os.path.isdir(path):
        try:
            os.rmdir(path)
            logger.info(f"Removed stale cache directory at {path}")
        except OSError:
            pass
        return True
    return False


def get_cached_tmdb_logo(cache_key: str) -> bytes | None:
    path = _safe_cache_path(TMDB_LOGO_CACHE_DIR, cache_key)

    if _remove_if_dir(path):
        return None

    if not os.path.exists(path):
        return None

    age_days = (time.time() - os.path.getmtime(path)) / 86400
    effective_days = TMDB_LOGO_CACHE_DURATION + _ttl_jitter(cache_key, TMDB_IMAGE_CACHE_JITTER_DAYS)

    if age_days > effective_days:
        logger.info(f"TMDB logo cache expired for {cache_key}")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return None

    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as exc:
        logger.error(f"TMDB logo cache read error: {exc}")
        return None


def set_cached_tmdb_logo(cache_key: str, data: bytes) -> None:
    try:
        path = _safe_cache_path(TMDB_LOGO_CACHE_DIR, cache_key)
        _remove_if_dir(path)
        _atomic_write(path, data)
    except Exception as exc:
        logger.error(f"TMDB logo cache write error: {exc}")

def _safe_cache_path(base_dir: str, filename: str) -> str:
    if os.path.isabs(filename):
        raise ValueError(f"Absolute cache path rejected: {filename!r}")
    base = os.path.realpath(base_dir)
    path = os.path.realpath(os.path.join(base, filename))
    if os.path.commonpath((base, path)) != base:
        raise ValueError(f"Path traversal attempt: {filename!r}")
    return path

# ---------------------------------------------------------------------------
# TMDB metadata cache
# ---------------------------------------------------------------------------

def get_cached_tmdb_metadata(cache_key: str) -> dict | None:
    try:
        row = get_db().execute(
            """
            SELECT title, release_year, genre_ids, is_textless, poster_path,
                   logos_json, cached_at,
                   credits_json, production_cos_json,
                   runtime, number_of_seasons, number_of_episodes,
                   original_language, original_title, backdrop_path, tmdb_status, vote_count,
                   vote_average,
                   text_backdrop_path, original_poster_path,
                   poster_langs_json, imdb_id,
                   tmdb_release_date, last_air_date, next_episode_json,
                   last_episode_json, seasons_json, metadata_version
            FROM tmdb_metadata_cache
            WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()
        if not row:
            return None

        (
            title, release_year, genre_ids_raw, is_textless, poster_path,
            logos_json, cached_at,
            credits_json, production_cos_json,
            runtime, number_of_seasons, number_of_episodes,
            original_language, original_title, backdrop_path, tmdb_status, vote_count,
            vote_average,
            text_backdrop_path, original_poster_path,
            poster_langs_json, imdb_id,
            tmdb_release_date, last_air_date, next_episode_json,
            last_episode_json, seasons_json, metadata_version,
        ) = row

        age_days = (time.time() - cached_at) / 86400

        if tmdb_release_date:
            try:
                from datetime import timezone
                rel_dt = datetime.strptime(tmdb_release_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                rel_ts = rel_dt.timestamp()
                now_ts = time.time()
                
                # If cached before the release date, and it is now strictly on or after the release date
                if cached_at < rel_ts and now_ts >= rel_ts:
                    age_days = 9999  # Force expiration
                # If it's unreleased or released within the last 14 days, use a 1-day TTL
                elif (now_ts < rel_ts or (now_ts - rel_ts) < 14 * 86400) and age_days > 1.0:
                    age_days = 9999
            except Exception:
                pass

        if age_days > TMDB_METADATA_CACHE_DURATION:
            logger.info(f"TMDB metadata cache expired for {cache_key} ({age_days:.1f}d old)")
            with _db_lock:
                get_db().execute(
                    "DELETE FROM tmdb_metadata_cache WHERE cache_key = ?", (cache_key,)
                )
                get_db().commit()
                
            if age_days == 9999:
                parts = cache_key.split("_")
                if len(parts) >= 2:
                    m_type, t_id = parts[0], parts[1]
                    invalidate_final_posters(t_id, m_type)
                    
            return None

        # Rows created before newer metadata fields were added were migrated
        # with NULL. Refresh once so discovery sashes have complete title,
        # vote, and TV lifecycle fields.
        if vote_count is None or original_title is None or metadata_version != 4:
            logger.info(
                f"TMDB metadata cache missing current schema fields for {cache_key}; refreshing"
            )
            with _db_lock:
                get_db().execute(
                    "DELETE FROM tmdb_metadata_cache WHERE cache_key = ?", (cache_key,)
                )
                get_db().commit()
            return None

        return {
            "title":                title,
            "release_year":         release_year,
            "genre_ids":            json.loads(genre_ids_raw or "[]"),
            "is_textless":          bool(is_textless),
            "poster_path":          poster_path,
            "logos":                json.loads(logos_json or "[]"),
            "credits":              json.loads(credits_json or "{}"),
            "production_companies": json.loads(production_cos_json or "[]"),
            "runtime":              runtime,
            "number_of_seasons":    number_of_seasons,
            "number_of_episodes":   number_of_episodes,
            "original_language":    original_language,
            "original_title":       original_title,
            "backdrop_path":        backdrop_path,
            "tmdb_status":          tmdb_status,
            "vote_count":           vote_count,
            "vote_average":         vote_average,
            "text_backdrop_path":   text_backdrop_path,
            "original_poster_path": original_poster_path,
            "poster_langs":         json.loads(poster_langs_json or "{}"),
            "imdb_id":              imdb_id,
            "tmdb_release_date":    tmdb_release_date,
            "last_air_date":        last_air_date,
            "next_episode":         json.loads(next_episode_json or "null"),
            "last_episode":         json.loads(last_episode_json or "null"),
            "seasons":              json.loads(seasons_json or "[]"),
            "metadata_version":     metadata_version,
        }
    except Exception as exc:
        logger.error(f"TMDB metadata cache read error: {exc}")
        return None


def set_cached_tmdb_metadata(
    cache_key: str,
    title: str,
    release_year: str | None,
    genre_ids: list[int],
    is_textless: bool,
    poster_path: str,
    logos: list[dict],
    *,
    credits: dict | None = None,
    production_companies: list[dict] | None = None,
    original_language: str | None = None,
    original_title: str | None = None,
    runtime: int | None = None,
    number_of_seasons: int | None = None,
    number_of_episodes: int | None = None,
    backdrop_path: str | None = None,
    tmdb_status: str | None = None,
    vote_count: int | None = None,
    vote_average: float | None = None,
    text_backdrop_path: str | None = None,
    original_poster_path: str | None = None,
    poster_langs: dict | None = None,
    imdb_id: str | None = None,
    tmdb_release_date: str | None = None,
    last_air_date: str | None = None,
    next_episode: dict | None = None,
    last_episode: dict | None = None,
    seasons: list[dict] | None = None,
    metadata_version: int = 4,
) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO tmdb_metadata_cache
                    (cache_key, title, release_year, genre_ids, is_textless,
                     poster_path, logos_json, cached_at,
                     credits_json, production_cos_json,
                     runtime, number_of_seasons, number_of_episodes,
                     original_language, original_title, backdrop_path, tmdb_status, vote_count,
                     vote_average,
                     text_backdrop_path, original_poster_path,
                     poster_langs_json, imdb_id,
                     tmdb_release_date, last_air_date, next_episode_json,
                     last_episode_json, seasons_json, metadata_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cache_key,
                    title,
                    release_year,
                    json.dumps(genre_ids),
                    int(is_textless),
                    poster_path,
                    json.dumps(logos),
                    int(time.time()),
                    json.dumps(credits or {}),
                    json.dumps(production_companies or []),
                    runtime,
                    number_of_seasons,
                    number_of_episodes,
                    original_language,
                    original_title,
                    backdrop_path,
                    tmdb_status,
                    vote_count,
                    vote_average,
                    text_backdrop_path,
                    original_poster_path,
                    json.dumps(poster_langs or {}),
                    imdb_id,
                    tmdb_release_date,
                    last_air_date,
                    json.dumps(next_episode) if next_episode else None,
                    json.dumps(last_episode) if last_episode else None,
                    json.dumps(seasons or []),
                    metadata_version,
                ),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"TMDB metadata cache write error: {exc}")


def delete_cached_tmdb_metadata(cache_key: str) -> None:
    """Remove a single TMDB metadata entry so the next request re-fetches from TMDB."""
    try:
        with _db_lock:
            get_db().execute(
                "DELETE FROM tmdb_metadata_cache WHERE cache_key = ?", (cache_key,)
            )
            get_db().commit()
        logger.info(f"TMDB metadata cache invalidated for {cache_key}")
    except Exception as exc:
        logger.error(f"TMDB metadata cache delete error: {exc}")


# ---------------------------------------------------------------------------
# TVDB generic JSON cache (resolved ids, artwork indexes, type catalogue, token)
# ---------------------------------------------------------------------------

def get_cached_tvdb_json(cache_key: str) -> dict | None:
    """Return the cached JSON object for *cache_key*, or None on miss/expiry.
    Expired rows are deleted on read so stale data never lingers."""
    try:
        row = get_db().execute(
            "SELECT value_json, cached_at, ttl_seconds FROM tvdb_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        value_json, cached_at, ttl_seconds = row
        if (time.time() - cached_at) > ttl_seconds:
            with _db_lock:
                get_db().execute("DELETE FROM tvdb_cache WHERE cache_key = ?", (cache_key,))
                get_db().commit()
            return None
        return json.loads(value_json)
    except Exception as exc:
        logger.error(f"TVDB cache read error: {exc}")
        return None


def set_cached_tvdb_json(cache_key: str, value: dict, ttl_seconds: int) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO tvdb_cache
                    (cache_key, value_json, cached_at, ttl_seconds)
                VALUES (?, ?, ?, ?)
                """,
                (cache_key, json.dumps(value), int(time.time()), int(ttl_seconds)),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"TVDB cache write error: {exc}")


# ---------------------------------------------------------------------------
# Digital release cache
# ---------------------------------------------------------------------------

def is_digital_release(imdb_id: str) -> bool:
    """Return True if the IMDB ID has a matching entry in the digital release cache."""
    try:
        row = get_db().execute(
            "SELECT 1 FROM digital_release_cache WHERE imdb_id = ?", (imdb_id,)
        ).fetchone()
        return row is not None
    except Exception as exc:
        logger.error(f"Digital release cache lookup error: {exc}")
        return False


def add_digital_releases(entries: list[tuple[str, int]]) -> int:
    """
    Insert (imdb_id, posted_at) pairs. Uses INSERT OR IGNORE so the
    original posted_at is never overwritten. Returns the number of new rows inserted.
    """
    if not entries:
        return 0
    inserted = 0
    try:
        with _db_lock:
            for imdb_id, posted_at in entries:
                r = get_db().execute(
                    "INSERT OR IGNORE INTO digital_release_cache (imdb_id, posted_at) VALUES (?, ?)",
                    (imdb_id, posted_at),
                )
                inserted += r.rowcount
            get_db().commit()
    except Exception as exc:
        logger.error(f"Digital release cache write error: {exc}")
    return inserted


# ---------------------------------------------------------------------------
# Release status cache
# ---------------------------------------------------------------------------
# Cached separately from main metadata so the extra TMDB /release_dates call
# only happens for users who have enabled the "release_status" sash slot.
#
# TTL is tiered by status rather than flat, because the progression
# Cinema -> Streaming -> Physical is one-way and slows down as it goes.  A film
# that reached Physical two years ago cannot change again, so re-asking TMDB
# every week was pure waste; a film still in cinemas can flip to Streaming any
# day TMDB publishes a digital date, and a weekly TTL meant showing "Cinema" for
# up to a week after it was wrong.
_RELEASE_STATUS_TTL_DAYS = {
    # Terminal or near-terminal — nothing further to observe.
    "Physical":   90,
    "Cancelled":  90,
    "Ended":      60,
    # Can still gain a physical date, but not urgently.
    "Streaming":  30,
    # Actively awaiting a transition TMDB may publish at any time.
    "Cinema":      1,
    "Production":  1,
    # TV that is still running: episode-level facts move faster than film status.
    "Airing":      3,
    "Returning":   3,
}
_RELEASE_STATUS_TTL_FALLBACK_DAYS = 7
# Longest a row may sleep on the strength of a published future date.  TMDB
# revises dates, and a film dated six months out should not go unverified that
# whole time, so a known boundary buys at most this much quiet.
_RELEASE_BOUNDARY_MAX_WAIT_DAYS = 14


def release_status_ttl_seconds(status: str | None) -> int:
    """How long a *status* is allowed to stand before it is re-checked.

    Exported because a rendered composite is derived from this: a poster whose
    sash or greyscale treatment came from a "Cinema" status must not outlive the
    status row that produced it.
    """
    return _RELEASE_STATUS_TTL_DAYS.get(
        status or "", _RELEASE_STATUS_TTL_FALLBACK_DAYS
    ) * 86400


def _release_row_expiry(status: str | None, cached_at: int) -> int:
    """Deadline for a release row that predates the expires_at column."""
    return int(cached_at) + release_status_ttl_seconds(status)


def release_status_expiry(
    status: str | None,
    *,
    upcoming_dates: "list[int] | None" = None,
    now: int | None = None,
) -> int:
    """When a cached release status should next be re-checked, as a unix time.

    Starts from the status tier, then clamps to the soonest *future* release date
    TMDB has already told us about.  That is what makes "releasing soon" cheap to
    handle: we do not have to predict anything, because a film with a digital
    date next Friday is a film whose status is known to change next Friday, so
    the row is simply set to expire then.  A leak that beats the published date
    is still invisible to us — that is what the r/movieleaks feed in
    digital_release.py is for — but the *scheduled* transitions land on time.

    ``upcoming_dates`` are unix timestamps of known future boundaries
    (theatrical / digital / physical).  Past dates should not be passed; they
    have already been folded into the status.

    A known boundary REPLACES the tier rather than being min'd with it, which is
    the whole point: the short "Cinema" tier exists because TMDB might publish a
    digital date any day, so once it has published one there is nothing left to
    poll for and the row can simply sleep until that date.  Min'ing the two
    would keep re-asking daily for an answer we already have.  The wait is still
    capped, because published dates do get revised.
    """
    now = int(time.time() if now is None else now)
    future = sorted(ts for ts in (upcoming_dates or ()) if int(ts) > now)
    if future:
        # The boundary IS midnight at the start of the release day, and the
        # status is computed against a local calendar date, so that instant is
        # exactly when the row becomes wrong.  Expiring a day later — which this
        # used to do — held a film at "Cinema" for the whole of its own digital
        # release day.  The one-hour floor below stops a boundary that is
        # minutes away from turning into a re-fetch loop.
        deadline = int(future[0])
        deadline = min(deadline, now + _RELEASE_BOUNDARY_MAX_WAIT_DAYS * 86400)
    else:
        deadline = _release_row_expiry(status, now)
    # Never thrash: a boundary that is hours away still gets a minimum dwell.
    return max(deadline, now + 3600)


def get_cached_movie_release_info(cache_key: str) -> dict | None:
    """Return cached movie release info JSON, or None if absent / expired."""
    try:
        row = get_db().execute(
            "SELECT info_json, cached_at, expires_at FROM movie_release_info_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        info_json, cached_at, expires_at = row
        info = json.loads(info_json or "{}")
        # The stored status is only a snapshot; callers recompute it from the
        # dates.  Tier this row's TTL off that same stored status so a finished
        # title is not re-fetched weekly for dates that can no longer move.
        deadline = expires_at or _release_row_expiry(info.get("status"), cached_at)
        if time.time() > deadline:
            logger.info(
                f"Movie release info cache expired for {cache_key} "
                f"({(time.time() - cached_at) / 86400:.1f}d old)"
            )
            return None
        return info
    except Exception as exc:
        logger.error(f"Movie release info cache read error: {exc}")
        return None


def set_cached_movie_release_info(
    cache_key: str, info: dict, expires_at: int | None = None
) -> None:
    """Upsert richer TMDB movie release-date information."""
    try:
        now = int(time.time())
        if expires_at is None:
            expires_at = _release_row_expiry(info.get("status"), now)
        with _db_lock:
            get_db().execute(
                """
                INSERT INTO movie_release_info_cache (cache_key, info_json, cached_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    info_json=excluded.info_json,
                    cached_at=excluded.cached_at,
                    expires_at=excluded.expires_at
                """,
                (cache_key, json.dumps(info), now, int(expires_at)),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Movie release info cache write error: {exc}")


def get_cached_release_status(cache_key: str) -> str | None:
    """Return the cached release status string, or None if absent / expired."""
    try:
        row = get_db().execute(
            "SELECT status, cached_at, expires_at FROM release_status_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        status, cached_at, expires_at = row
        deadline = expires_at or _release_row_expiry(status, cached_at)
        if time.time() > deadline:
            logger.info(
                f"Release status cache expired for {cache_key} "
                f"({(time.time() - cached_at) / 86400:.1f}d old, status={status})"
            )
            return None
        return status
    except Exception as exc:
        logger.error(f"Release status cache read error: {exc}")
        return None


def set_cached_release_status(
    cache_key: str, status: str, expires_at: int | None = None
) -> None:
    """Upsert a release status entry.

    *expires_at* comes from release_status_expiry() at the call site, which knows
    the title's upcoming release dates; omitting it falls back to the status tier.
    """
    try:
        now = int(time.time())
        if expires_at is None:
            expires_at = _release_row_expiry(status, now)
        with _db_lock:
            get_db().execute(
                """
                INSERT INTO release_status_cache (cache_key, status, cached_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    status=excluded.status,
                    cached_at=excluded.cached_at,
                    expires_at=excluded.expires_at
                """,
                (cache_key, status, now, int(expires_at)),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Release status cache write error: {exc}")


def get_cached_text_detection(cache_key: str) -> bool | None:
    """Return the cached burned-in-text result (True/False), or None if absent.

    Results never expire — they're keyed by an immutable TMDB image path plus the
    detection params, so the answer can't change for a given key.
    """
    try:
        row = get_db().execute(
            "SELECT has_text FROM text_detection_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        return None if row is None else bool(row[0])
    except Exception as exc:
        logger.error(f"Text-detection cache read error: {exc}")
        return None


def set_cached_text_detection(cache_key: str, has_text: bool) -> None:
    """Upsert a burned-in-text detection result."""
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT INTO text_detection_cache (cache_key, has_text, cached_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET has_text=excluded.has_text, cached_at=excluded.cached_at
                """,
                (cache_key, int(has_text), int(time.time())),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Text-detection cache write error: {exc}")


# ---------------------------------------------------------------------------
# App state — small key/value store for cross-restart bookkeeping
# ---------------------------------------------------------------------------

def get_app_state(key: str) -> str | None:
    """Return the stored string value for *key*, or None if unset/on error."""
    try:
        row = get_db().execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else row[0]
    except Exception as exc:
        logger.error(f"App state read error ({key}): {exc}")
        return None


def claim_app_state_slot(key: str, now: float, min_interval: float) -> bool:
    """Atomically claim a periodic job slot; True if this caller won it.

    Every uvicorn worker runs its own copy of each background loop, and they
    all share this database. For a cheap job that duplication is harmless, but
    a job that downloads tens of megabytes and rewrites a table wants exactly
    one runner per interval.

    The check and the write are one statement so two workers waking together
    cannot both see a stale timestamp and both proceed — the conditional
    UPDATE is evaluated against the committed row, and only one connection's
    write survives. `changes()` then tells the caller whether it was theirs.
    """
    try:
        with _db_lock:
            db = get_db()
            cur = db.execute(
                """
                INSERT INTO app_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                WHERE CAST(app_state.value AS REAL) <= ?
                """,
                (key, str(now), now - min_interval),
            )
            db.commit()
            return cur.rowcount > 0
    except Exception as exc:
        # Never let bookkeeping stop the job — a failure here degrades to the
        # old behaviour (every worker runs it), not to nothing running.
        logger.error(f"App state claim error ({key}): {exc}")
        return True


def set_app_state(key: str, value: str) -> None:
    """Upsert a string value in the app-state key/value store."""
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT INTO app_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"App state write error ({key}): {exc}")


# ---------------------------------------------------------------------------
# ElfHosted fork additions
# ---------------------------------------------------------------------------

def list_composite_request_params() -> list[tuple[str, str]]:
    """(cache_key, request_params) for every composite that recorded the query
    that produced it.

    Upstream's trending refresh reaches for cache.get_db() and runs this SELECT
    inline, which only works against SQLite. Exposing it as a backend function
    keeps that background job working on either backend.
    """
    try:
        return [
            (r[0], r[1]) for r in get_db().execute(
                "SELECT cache_key, request_params FROM final_poster_cache "
                "WHERE request_params IS NOT NULL"
            ).fetchall()
        ]
    except Exception as exc:
        logger.error(f"Composite request-params query error: {exc}")
        return []


def count_digital_releases() -> int:
    """Row count for the digital-release cache — surfaced on /stats."""
    try:
        (count,) = get_db().execute(
            "SELECT COUNT(*) FROM digital_release_cache"
        ).fetchone()
        return count
    except Exception as exc:
        logger.error(f"Digital release cache count error: {exc}")
        return 0


def get_cached_imdb_to_tmdb(imdb_id: str, media_type: str) -> str | None:
    """Look up the cached tmdb_id for an imdb_id + media_type (no TTL)."""
    try:
        row = get_db().execute(
            "SELECT tmdb_id FROM imdb_to_tmdb_cache WHERE imdb_id = ? AND media_type = ?",
            (imdb_id, media_type),
        ).fetchone()
        return row[0] if row else None
    except Exception as exc:
        logger.error(f"imdb_to_tmdb cache read error: {exc}")
        return None


def set_cached_imdb_to_tmdb(imdb_id: str, media_type: str, tmdb_id: str) -> None:
    """Upsert an imdb_id -> tmdb_id mapping."""
    try:
        with _db_lock:
            get_db().execute(
                "INSERT OR REPLACE INTO imdb_to_tmdb_cache (imdb_id, media_type, tmdb_id) "
                "VALUES (?, ?, ?)",
                (imdb_id, media_type, tmdb_id),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"imdb_to_tmdb cache write error: {exc}")


def ping() -> bool:
    """Cheap connectivity check for /ready probes."""
    try:
        get_db().execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def close() -> None:
    """Close this thread's connection. Called from lifespan shutdown.

    Connections are thread-local (see get_db); closing the calling thread's
    handle is sufficient for a clean shutdown — others are dropped when their
    threads end. Flips _initialised so a late get_db() fails loudly rather
    than resurrecting a half-torn-down connection."""
    global _initialised
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
    _initialised = False
