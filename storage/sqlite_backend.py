"""SQLite storage backend — extracted from upstream cache.py.

Default backend, used when DATABASE_URL is unset. Kept close to upstream's
cache.py so cherry-picks from upstream apply with minimal friction.

Phase 10 split:
  * TMDB poster/logo bytes  — direct filesystem (per-pod ephemeral cache
    in front of TMDB's own CDN; no remote-shared storage benefit since
    TMDB's CDN is the source of truth).
  * Composite (rendered) bytes — delegated to the blobstore package
    so they can live in S3 + a CDN custom domain instead of bloating
    the relational backend with BYTEA. This module keeps only the
    cache metadata row (cache_key + cached_at).
"""
import logging
import os
import sqlite3
import threading
import time
import json
from datetime import datetime

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
    TMDB_METADATA_CACHE_DURATION,
    COMPOSITE_CACHE_TTL,
    COMPOSITE_MAX_ENTRIES,
    QUALITY_OLD_CACHE_DURATION,
    DIGITAL_RELEASE_MAX_AGE_DAYS,
)

_db_conn: sqlite3.Connection | None = None
_db_lock = threading.Lock()   # used only for writes; WAL allows concurrent reads


def get_db() -> sqlite3.Connection:
    if _db_conn is None:
        raise RuntimeError("Database not initialized")
    return _db_conn


def init_db() -> None:
    global _db_conn
    os.makedirs(TMDB_POSTER_CACHE_DIR, exist_ok=True)
    os.makedirs(TMDB_LOGO_CACHE_DIR, exist_ok=True)
    _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute("PRAGMA synchronous=NORMAL")       # safe with WAL; avoids unnecessary fsyncs
    _db_conn.execute("PRAGMA cache_size=-32000")        # 32 MB in-process page cache
    _db_conn.execute("PRAGMA temp_store=MEMORY")        # temp tables/indices stay in RAM
    _db_conn.execute("PRAGMA busy_timeout=5000")        # wait up to 5s if another worker holds the write lock
    _db_conn.execute("PRAGMA wal_autocheckpoint=1000")  # fold WAL back into main DB at 1000 pages (~4 MB)

    _db_conn.execute("""
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
        is_metacritic  INTEGER NOT NULL DEFAULT 0
    )
    """)

    existing_cols = {
        row[1]
        for row in _db_conn.execute("PRAGMA table_info(rating_cache)").fetchall()
    }
    for col, definition in (
        ("award_wins",     "TEXT NOT NULL DEFAULT ''"),
        ("award_noms",     "TEXT NOT NULL DEFAULT ''"),
        ("awards_fetched", "INTEGER NOT NULL DEFAULT 0"),
        ("festival_label", "TEXT"),
        ("age_rating",     "INTEGER"),
        ("is_cult",        "INTEGER NOT NULL DEFAULT 0"),
        ("is_true_story",  "INTEGER NOT NULL DEFAULT 0"),
        ("is_metacritic",  "INTEGER NOT NULL DEFAULT 0"),
    ):
        if col not in existing_cols:
            _db_conn.execute(
                f"ALTER TABLE rating_cache ADD COLUMN {col} {definition}"
            )

    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS quality_cache (
            imdb_id      TEXT PRIMARY KEY,
            tokens       TEXT,
            cached_at    INTEGER,
            release_date TEXT
        )
    """)

    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS trending_cache (
            media_type    TEXT PRIMARY KEY,
            rankings_json TEXT,
            cached_at     INTEGER
        )
    """)

    _db_conn.execute("""
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
            original_language   TEXT
        )
    """)

    # Phase 10: composite-poster bytes live in blobstore (FS or S3); the
    # table holds only metadata so the relational backend can do TTL
    # bookkeeping cheaply. New deployments get the metadata-only schema;
    # existing deployments need the migration below.
    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS final_poster_cache (
            cache_key  TEXT PRIMARY KEY,
            cached_at  INTEGER NOT NULL
        )
    """)
    # Migrate the pre-Phase-10 schema if present. SQLite 3.35+ supports
    # DROP COLUMN; existing bytes are discarded (cache refills via TTL).
    existing_composite_cols = {
        row[1]
        for row in _db_conn.execute("PRAGMA table_info(final_poster_cache)").fetchall()
    }
    if "jpeg_bytes" in existing_composite_cols:
        try:
            _db_conn.execute("ALTER TABLE final_poster_cache DROP COLUMN jpeg_bytes")
            logger.info(
                "Migrated final_poster_cache: dropped jpeg_bytes BLOB; "
                "cached composites will re-render on next request"
            )
        except sqlite3.OperationalError as exc:
            # SQLite < 3.35: rebuild the table without the column.
            logger.info(
                "SQLite DROP COLUMN unsupported (%s); recreating final_poster_cache "
                "without jpeg_bytes (existing cached composites discarded)", exc,
            )
            _db_conn.execute("ALTER TABLE final_poster_cache RENAME TO _final_poster_cache_old")
            _db_conn.execute("""
                CREATE TABLE final_poster_cache (
                    cache_key TEXT PRIMARY KEY,
                    cached_at INTEGER NOT NULL
                )
            """)
            _db_conn.execute("""
                INSERT INTO final_poster_cache (cache_key, cached_at)
                SELECT cache_key, cached_at FROM _final_poster_cache_old
            """)
            _db_conn.execute("DROP TABLE _final_poster_cache_old")

    _db_conn.execute("""
        CREATE TABLE IF NOT EXISTS digital_release_cache (
            imdb_id   TEXT PRIMARY KEY,
            posted_at INTEGER NOT NULL
        )
    """)

    existing_meta_cols = {
        row[1]
        for row in _db_conn.execute("PRAGMA table_info(tmdb_metadata_cache)").fetchall()
    }
    for col, definition in (
        ("credits_json",        "TEXT"),
        ("production_cos_json", "TEXT"),
        ("runtime",             "INTEGER"),
        ("number_of_seasons",   "INTEGER"),
        ("number_of_episodes",  "INTEGER"),
        ("original_language",   "TEXT"),
    ):
        if col not in existing_meta_cols:
            _db_conn.execute(
                f"ALTER TABLE tmdb_metadata_cache ADD COLUMN {col} {definition}"
            )

    _db_conn.commit()


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


def _peek_final_poster(cache_key: str) -> bool:
    """Internal: row-only TTL check. Returns True if a fresh row exists.

    Used by both the cheap freshness probe (CDN-redirect path) and the
    full bytes fetcher. Side-effect: deletes the row on expiry, schedules
    the blob deletion via the caller's async context.
    """
    row = get_db().execute(
        "SELECT cached_at FROM final_poster_cache WHERE cache_key = ?",
        (cache_key,),
    ).fetchone()
    if not row:
        return False
    (cached_at,) = row
    age_secs = time.time() - cached_at
    if age_secs > COMPOSITE_CACHE_TTL:
        logger.info(f"Final poster cache expired for {cache_key} ({age_secs/86400:.1f}d old)")
        with _db_lock:
            get_db().execute(
                "DELETE FROM final_poster_cache WHERE cache_key = ?", (cache_key,)
            )
            get_db().commit()
        return False
    return True


async def is_cached_final_poster_fresh(cache_key: str) -> bool:
    """Lightweight freshness probe — checks the metadata row + TTL only,
    never touches the blobstore. Used by /poster to decide whether to
    emit a 302 to the CDN without pulling the bytes through the app
    pod.

    On expiry: deletes both the metadata row and the orphaned blob.
    """
    try:
        fresh = _peek_final_poster(cache_key)
        if not fresh:
            # Best-effort blob cleanup. Safe no-op when no blob exists.
            await blobstore.delete(blobstore.BUCKET_COMPOSITES, cache_key)
        return fresh
    except Exception as exc:
        logger.error(f"Final poster freshness probe error: {exc}")
        return False


async def get_cached_final_poster(cache_key: str) -> bytes | None:
    """Full read: metadata TTL check + blob fetch. Use the freshness
    probe + url_for + 302 path instead when a CDN URL is configured;
    this function is only the inline-serve fallback."""
    try:
        if not _peek_final_poster(cache_key):
            await blobstore.delete(blobstore.BUCKET_COMPOSITES, cache_key)
            return None
        return await blobstore.get(
            blobstore.BUCKET_COMPOSITES, cache_key, max_age_seconds=COMPOSITE_CACHE_TTL,
        )
    except Exception as exc:
        logger.error(f"Final poster cache read error: {exc}")
        return None


def get_cached_final_poster_url(cache_key: str) -> str | None:
    """Return a public CDN URL for the composite if the blobstore backend
    provides one (OBJECT_STORE_PUBLIC_URL set). Sync — doesn't touch S3,
    just constructs the URL from the configured prefix. Caller still
    needs to confirm the row exists + isn't expired before redirecting."""
    return blobstore.url_for(blobstore.BUCKET_COMPOSITES, cache_key)


async def set_cached_final_poster(cache_key: str, jpeg_bytes: bytes) -> None:
    try:
        # Write the bytes first so the metadata row is never present
        # without a corresponding blob (which would race a reader into
        # a 502-ish state on the very first hit).
        await blobstore.put(
            blobstore.BUCKET_COMPOSITES, cache_key, jpeg_bytes,
            content_type="image/jpeg",
        )
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO final_poster_cache (cache_key, cached_at)
                VALUES (?, ?)
                """,
                (cache_key, int(time.time())),
            )
            if COMPOSITE_MAX_ENTRIES > 0:
                (count,) = get_db().execute(
                    "SELECT COUNT(*) FROM final_poster_cache"
                ).fetchone()
                overflow = count - COMPOSITE_MAX_ENTRIES
                if overflow > 0:
                    evict = get_db().execute(
                        "SELECT cache_key FROM final_poster_cache "
                        "ORDER BY cached_at ASC LIMIT ?",
                        (overflow,),
                    ).fetchall()
                    evict_keys = [r[0] for r in evict]
                    get_db().execute(
                        "DELETE FROM final_poster_cache WHERE cache_key IN "
                        f"({','.join('?' * len(evict_keys))})",
                        evict_keys,
                    )
                    logger.info(f"Composite cache cap: evicted {overflow} oldest entries")
                    # Best-effort blob cleanup for evicted keys.
                    for k in evict_keys:
                        try:
                            await blobstore.delete(blobstore.BUCKET_COMPOSITES, k)
                        except Exception:
                            pass
            get_db().commit()
    except Exception as exc:
        logger.error(f"Final poster cache write error: {exc}")


async def prune_caches() -> None:
    now = int(time.time())
    try:
        expired_composite_keys: list[str] = []
        with _db_lock:
            db = get_db()

            # Phase 10: collect the composite cache_keys before deletion so
            # we can drop the corresponding blobs. Without this the metadata
            # row goes away but the S3/B2 object lingers forever.
            cutoff = now - COMPOSITE_CACHE_TTL
            expired_composite_keys = [
                r[0] for r in db.execute(
                    "SELECT cache_key FROM final_poster_cache WHERE cached_at < ?",
                    (cutoff,),
                ).fetchall()
            ]
            if expired_composite_keys:
                placeholders = ",".join("?" * len(expired_composite_keys))
                r = db.execute(
                    f"DELETE FROM final_poster_cache WHERE cache_key IN ({placeholders})",
                    expired_composite_keys,
                )
                logger.info(f"Pruned {r.rowcount} expired composite cache entries")

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

            db.commit()

        with _db_lock:
            get_db().execute("PRAGMA incremental_vacuum(100)")
            get_db().commit()

        # Phase 10: best-effort blob deletion for the composite rows we
        # just evicted. Done outside the db lock + after commit so a
        # slow S3 doesn't hold up the write lock.
        for k in expired_composite_keys:
            try:
                await blobstore.delete(blobstore.BUCKET_COMPOSITES, k)
            except Exception as exc:
                logger.warning(f"Composite blob delete error for {k}: {exc}")

    except Exception as exc:
        logger.error(f"Cache prune error: {exc}")


def get_cached_rating(imdb_id: str):
    try:
        row = get_db().execute(
            """
            SELECT ratings_json, genre, cached_at, release_date,
                   award_wins, award_noms, awards_fetched, festival_label,
                   age_rating, is_cult, is_true_story, is_metacritic
            FROM rating_cache
            WHERE imdb_id = ?
            """,
            (imdb_id,),
        ).fetchone()

        if not row:
            return None

        (ratings_json, genre, cached_at, release_date,
         wins_raw, noms_raw, awards_fetched_int, festival_label,
         age_rating, is_cult_int, is_true_story_int, is_metacritic_int) = row

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

        ratings_dict = json.loads(ratings_json or "{}")
        wins = [w for w in (wins_raw or "").split("|") if w]
        noms = [n for n in (noms_raw or "").split("|") if n]
        awards_fetched = bool(awards_fetched_int)

        return (ratings_dict, genre, release_date, wins, noms,
                awards_fetched, festival_label, age_rating,
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
    festival_label: str | None = None,
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
                        festival_label,
                        age_rating,
                        is_cult,
                        is_true_story,
                        is_metacritic
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    festival_label,
                    age_rating,
                    int(is_cult),
                    int(is_true_story),
                    int(is_metacritic),
                ),
            )
            get_db().commit()

    except Exception as exc:
        logger.error(f"Cache write error: {exc}")


def get_cached_quality(imdb_id: str, release_date: str | None = None) -> list[str] | None:
    try:
        row = get_db().execute(
            "SELECT tokens, cached_at, release_date FROM quality_cache WHERE imdb_id = ?",
            (imdb_id,),
        ).fetchone()
        if row is None:
            return None

        tokens_raw, cached_at, stored_release = row
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
                    (imdb_id, tokens, cached_at, release_date)
                VALUES (?, ?, ?, ?)
                """,
                (imdb_id, "|".join(tokens), int(time.time()), release_date),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Quality cache write error: {exc}")


def get_cached_trending_snapshot(media_type: str) -> dict[str, int] | None:
    try:
        row = get_db().execute(
            """
            SELECT rankings_json, cached_at
            FROM trending_cache
            WHERE media_type = ?
            """,
            (media_type,),
        ).fetchone()

        if not row:
            return None

        rankings_json, cached_at = row
        age_days = (time.time() - cached_at) / 86400

        if age_days > TRENDING_CACHE_DURATION:
            return None

        return json.loads(rankings_json)
    except Exception as exc:
        logger.error(f"Trending snapshot cache read error: {exc}")
        return None


def set_cached_trending_snapshot(
    media_type: str,
    rankings: dict[str, int],
) -> None:
    try:
        with _db_lock:
            get_db().execute(
                """
                INSERT OR REPLACE INTO trending_cache
                (media_type, rankings_json, cached_at)
                VALUES (?, ?, ?)
                """,
                (
                    media_type,
                    json.dumps(rankings),
                    int(time.time()),
                ),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"Trending snapshot cache write error: {exc}")


# ---------------------------------------------------------------------------
# TMDB poster/logo cache — local filesystem, per-pod ephemeral.
#
# Phase 10: reverted from blobstore delegation. TMDB's own CDN is the
# source of truth; this cache is just a latency-optimisation in front of
# image.tmdb.org. Sharing it across replicas via S3 buys very little and
# complicates the data path. On pod restart the cache re-warms in the
# first few minutes of traffic.
#
# Synchronous (matching upstream's shape) so cherry-picks from upstream's
# cache.py apply with minimal churn.
# ---------------------------------------------------------------------------

def _safe_cache_path(base_dir: str, filename: str) -> str:
    path = os.path.realpath(os.path.join(base_dir, filename))
    if not path.startswith(os.path.realpath(base_dir)):
        raise ValueError(f"Path traversal attempt: {filename!r}")
    return path


def _remove_if_dir(path: str) -> bool:
    """Remove *path* if it is a directory (stale artefact from a previous bug)."""
    if os.path.isdir(path):
        try:
            os.rmdir(path)
            logger.info(f"Removed stale cache directory at {path}")
        except OSError:
            pass
        return True
    return False


def get_cached_tmdb_poster(cache_key: str) -> bytes | None:
    path = _safe_cache_path(TMDB_POSTER_CACHE_DIR, cache_key)

    if not os.path.exists(path):
        return None

    age_days = (time.time() - os.path.getmtime(path)) / 86400

    if age_days > TMDB_POSTER_CACHE_DURATION:
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
    try:
        path = _safe_cache_path(TMDB_POSTER_CACHE_DIR, cache_key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except Exception as exc:
        logger.error(f"TMDB poster cache write error: {exc}")


def get_cached_tmdb_logo(cache_key: str) -> bytes | None:
    path = _safe_cache_path(TMDB_LOGO_CACHE_DIR, cache_key)

    if _remove_if_dir(path):
        return None

    if not os.path.exists(path):
        return None

    age_days = (time.time() - os.path.getmtime(path)) / 86400

    if age_days > TMDB_LOGO_CACHE_DURATION:
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
        with open(path, "wb") as f:
            f.write(data)
    except Exception as exc:
        logger.error(f"TMDB logo cache write error: {exc}")


def get_cached_tmdb_metadata(cache_key: str) -> dict | None:
    try:
        row = get_db().execute(
            """
            SELECT title, release_year, genre_ids, is_textless, poster_path,
                   logos_json, cached_at,
                   credits_json, production_cos_json,
                   runtime, number_of_seasons, number_of_episodes,
                   original_language
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
            original_language,
        ) = row

        age_days = (time.time() - cached_at) / 86400
        if age_days > TMDB_METADATA_CACHE_DURATION:
            logger.info(f"TMDB metadata cache expired for {cache_key} ({age_days:.1f}d old)")
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
    runtime: int | None = None,
    number_of_seasons: int | None = None,
    number_of_episodes: int | None = None,
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
                     original_language)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            get_db().commit()
    except Exception as exc:
        logger.error(f"TMDB metadata cache write error: {exc}")


def delete_cached_tmdb_metadata(cache_key: str) -> None:
    try:
        with _db_lock:
            get_db().execute(
                "DELETE FROM tmdb_metadata_cache WHERE cache_key = ?", (cache_key,)
            )
            get_db().commit()
        logger.info(f"TMDB metadata cache invalidated for {cache_key}")
    except Exception as exc:
        logger.error(f"TMDB metadata cache delete error: {exc}")


def is_digital_release(imdb_id: str) -> bool:
    try:
        row = get_db().execute(
            "SELECT 1 FROM digital_release_cache WHERE imdb_id = ?", (imdb_id,)
        ).fetchone()
        return row is not None
    except Exception as exc:
        logger.error(f"Digital release cache lookup error: {exc}")
        return False


def count_digital_releases() -> int:
    try:
        (count,) = get_db().execute(
            "SELECT COUNT(*) FROM digital_release_cache"
        ).fetchone()
        return count
    except Exception as exc:
        logger.error(f"Digital release cache count error: {exc}")
        return 0


def add_digital_releases(entries: list[tuple[str, int]]) -> int:
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


def ping() -> bool:
    """Cheap connectivity check for /ready probes (Phase 4)."""
    try:
        get_db().execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def close() -> None:
    """Close the connection. Called from lifespan shutdown."""
    global _db_conn
    if _db_conn is not None:
        try:
            _db_conn.close()
        except Exception as exc:
            logger.warning(f"SQLite close error: {exc}")
        _db_conn = None
