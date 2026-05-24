"""PostgreSQL storage backend — opt-in via DATABASE_URL.

Mirrors the public surface of storage.sqlite_backend so upstream's API is
preserved exactly. Operates against a connection pool so multiple replicas /
workers can share a single Postgres instance.

Phase 10 split (matches the SQLite backend):
  * TMDB poster/logo bytes — per-pod filesystem (TMDB's own CDN is the
    source of truth; sharing this cache across replicas is not worth
    the complexity).
  * Composite (rendered) bytes — delegated to the ``blobstore`` package
    so they can land in S3 + Cloudflare instead of clogging the Postgres
    backups / replication stream as BYTEA. This module keeps only the
    cache metadata row.
"""
import logging
import os
import time
import json
from datetime import datetime

logger = logging.getLogger(__name__)

import psycopg
from psycopg_pool import ConnectionPool

import blobstore
from config import (
    DATABASE_URL,
    DB_POOL_MIN_SIZE,
    DB_POOL_MAX_SIZE,
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

# Pure helpers (TTL math + filesystem-cache primitives) shared with the
# SQLite backend so we don't duplicate them. These have no Postgres-side
# state, just static functions that take a cache key.
from storage.sqlite_backend import (
    _rating_ttl,
    _quality_ttl,
    _safe_cache_path,
    _remove_if_dir,
    get_cached_tmdb_poster,
    set_cached_tmdb_poster,
    get_cached_tmdb_logo,
    set_cached_tmdb_logo,
)


_pool: ConnectionPool | None = None

# Postgres advisory lock key for serialising schema bootstrap across replicas.
# Arbitrary 64-bit int unique to this app.
_SCHEMA_LOCK_KEY = 0x504F5354_2B505553  # "POST+PUS"


def _get_pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("Database not initialized")
    return _pool


def _bootstrap_schema(conn) -> None:
    """Idempotent DDL. Wrapped in a single transaction; called with the
    advisory lock held."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rating_cache (
                imdb_id        TEXT PRIMARY KEY,
                ratings_json   TEXT,
                genre          TEXT,
                cached_at      BIGINT,
                release_date   TEXT,
                award_wins     TEXT NOT NULL DEFAULT '',
                award_noms     TEXT NOT NULL DEFAULT '',
                awards_fetched INTEGER NOT NULL DEFAULT 0,
                festival_label TEXT,
                age_rating     INTEGER,
                is_cult        INTEGER NOT NULL DEFAULT 0,
                is_true_story  INTEGER NOT NULL DEFAULT 0,
                is_metacritic  INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quality_cache (
                imdb_id      TEXT PRIMARY KEY,
                tokens       TEXT,
                cached_at    BIGINT,
                release_date TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trending_cache (
                media_type    TEXT PRIMARY KEY,
                rankings_json TEXT,
                cached_at     BIGINT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tmdb_metadata_cache (
                cache_key           TEXT PRIMARY KEY,
                title               TEXT,
                release_year        TEXT,
                genre_ids           TEXT,
                is_textless         INTEGER,
                poster_path         TEXT,
                logos_json          TEXT,
                cached_at           BIGINT,
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
        # bookkeeping cheaply.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS final_poster_cache (
                cache_key  TEXT PRIMARY KEY,
                cached_at  BIGINT NOT NULL
            )
        """)
        # Migrate pre-Phase-10 deployments that have the BYTEA column.
        # Idempotent: IF EXISTS makes it a no-op on new installs.
        cur.execute("""
            ALTER TABLE final_poster_cache DROP COLUMN IF EXISTS jpeg_bytes
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS final_poster_cache_cached_at_idx
                ON final_poster_cache (cached_at)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS digital_release_cache (
                imdb_id   TEXT PRIMARY KEY,
                posted_at BIGINT NOT NULL
            )
        """)
    conn.commit()


def init_db() -> None:
    """Create the connection pool and run idempotent schema bootstrap."""
    global _pool

    # Filesystem cache dirs — same as SQLite backend (Phase 3 removes these).
    os.makedirs(TMDB_POSTER_CACHE_DIR, exist_ok=True)
    os.makedirs(TMDB_LOGO_CACHE_DIR, exist_ok=True)

    _pool = ConnectionPool(
        conninfo=DATABASE_URL,
        min_size=DB_POOL_MIN_SIZE,
        max_size=DB_POOL_MAX_SIZE,
        kwargs={"autocommit": False},
        open=True,
        timeout=10.0,
    )

    # Serialise schema bootstrap across replicas with a transaction-scoped
    # advisory lock. pg_advisory_xact_lock blocks until the lock is acquired
    # and releases automatically at commit / rollback.
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_KEY,))
        _bootstrap_schema(conn)

    logger.info("Postgres backend initialised (pool %d-%d)", DB_POOL_MIN_SIZE, DB_POOL_MAX_SIZE)


# ---------------------------------------------------------------------------
# Final composite poster cache
# ---------------------------------------------------------------------------

def _peek_final_poster(cache_key: str) -> bool:
    """Row-only TTL check. True if a fresh row exists; deletes the row
    on expiry. Caller handles blob cleanup."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cached_at FROM final_poster_cache WHERE cache_key = %s",
                (cache_key,),
            )
            row = cur.fetchone()
            if not row:
                return False
            (cached_at,) = row
            age_secs = time.time() - cached_at
            if age_secs > COMPOSITE_CACHE_TTL:
                logger.info(f"Final poster cache expired for {cache_key} ({age_secs/86400:.1f}d old)")
                cur.execute(
                    "DELETE FROM final_poster_cache WHERE cache_key = %s",
                    (cache_key,),
                )
                conn.commit()
                return False
            return True


async def is_cached_final_poster_fresh(cache_key: str) -> bool:
    """Lightweight freshness probe — checks the metadata row + TTL only,
    never touches the blobstore. Lets /poster emit a 302 to the CDN
    without pulling the bytes through the app pod."""
    try:
        fresh = _peek_final_poster(cache_key)
        if not fresh:
            await blobstore.delete(blobstore.BUCKET_COMPOSITES, cache_key)
        return fresh
    except Exception as exc:
        logger.error(f"Final poster freshness probe error: {exc}")
        return False


async def get_cached_final_poster(cache_key: str) -> bytes | None:
    """Full read: metadata TTL check + blob fetch. Used as the
    inline-serve fallback when no CDN URL is configured."""
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
    """Public CDN URL for the composite, or None when no URL is configured."""
    return blobstore.url_for(blobstore.BUCKET_COMPOSITES, cache_key)


async def set_cached_final_poster(cache_key: str, jpeg_bytes: bytes) -> None:
    try:
        # Write bytes first so the metadata row is never present without
        # a backing blob.
        await blobstore.put(
            blobstore.BUCKET_COMPOSITES, cache_key, jpeg_bytes,
            content_type="image/jpeg",
        )
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO final_poster_cache (cache_key, cached_at)
                    VALUES (%s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        cached_at  = EXCLUDED.cached_at
                    """,
                    (cache_key, int(time.time())),
                )
                evict_keys: list[str] = []
                if COMPOSITE_MAX_ENTRIES > 0:
                    cur.execute("SELECT COUNT(*) FROM final_poster_cache")
                    (count,) = cur.fetchone()
                    overflow = count - COMPOSITE_MAX_ENTRIES
                    if overflow > 0:
                        cur.execute(
                            "SELECT cache_key FROM final_poster_cache "
                            "ORDER BY cached_at ASC LIMIT %s",
                            (overflow,),
                        )
                        evict_keys = [r[0] for r in cur.fetchall()]
                        cur.execute(
                            """
                            DELETE FROM final_poster_cache WHERE cache_key = ANY(%s)
                            """,
                            (evict_keys,),
                        )
                        logger.info(f"Composite cache cap: evicted {overflow} oldest entries")
            conn.commit()
        # Best-effort blob cleanup for evicted keys, outside the pool ctx.
        for k in evict_keys:
            try:
                await blobstore.delete(blobstore.BUCKET_COMPOSITES, k)
            except Exception:
                pass
    except Exception as exc:
        logger.error(f"Final poster cache write error: {exc}")


async def prune_caches() -> None:
    now = int(time.time())
    expired_composite_keys: list[str] = []
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                # Phase 10: capture composite keys before deletion so we can
                # drop the corresponding blobs after the txn commits.
                cur.execute(
                    "SELECT cache_key FROM final_poster_cache WHERE cached_at < %s",
                    (now - COMPOSITE_CACHE_TTL,),
                )
                expired_composite_keys = [r[0] for r in cur.fetchall()]
                if expired_composite_keys:
                    cur.execute(
                        "DELETE FROM final_poster_cache WHERE cache_key = ANY(%s)",
                        (expired_composite_keys,),
                    )
                    logger.info(f"Pruned {cur.rowcount} expired composite cache entries")

                rating_cutoff   = now - OLD_CACHE_DURATION           * 86400
                quality_cutoff  = now - QUALITY_OLD_CACHE_DURATION   * 86400
                metadata_cutoff = now - TMDB_METADATA_CACHE_DURATION * 86400

                cur.execute(
                    "DELETE FROM rating_cache WHERE cached_at < %s", (rating_cutoff,)
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} expired rating cache entries")

                cur.execute(
                    "DELETE FROM quality_cache WHERE cached_at < %s", (quality_cutoff,)
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} expired quality cache entries")

                cur.execute(
                    "DELETE FROM tmdb_metadata_cache WHERE cached_at < %s",
                    (metadata_cutoff,),
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} expired TMDB metadata cache entries")

                digital_cutoff = now - DIGITAL_RELEASE_MAX_AGE_DAYS * 86400
                cur.execute(
                    "DELETE FROM digital_release_cache WHERE posted_at < %s",
                    (digital_cutoff,),
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} expired digital release cache entries")
            conn.commit()
        # Postgres autovacuum handles space reclamation — no explicit VACUUM here.

        # Phase 10: best-effort blob deletion for evicted composite rows,
        # outside the connection-pool ctx so a slow S3 doesn't hold the
        # connection.
        for k in expired_composite_keys:
            try:
                await blobstore.delete(blobstore.BUCKET_COMPOSITES, k)
            except Exception as exc:
                logger.warning(f"Composite blob delete error for {k}: {exc}")
    except Exception as exc:
        logger.error(f"Cache prune error: {exc}")


# ---------------------------------------------------------------------------
# Rating cache
# ---------------------------------------------------------------------------

def get_cached_rating(imdb_id: str):
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ratings_json, genre, cached_at, release_date,
                           award_wins, award_noms, awards_fetched, festival_label,
                           age_rating, is_cult, is_true_story, is_metacritic
                    FROM rating_cache
                    WHERE imdb_id = %s
                    """,
                    (imdb_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None

                (ratings_json, genre, cached_at, release_date,
                 wins_raw, noms_raw, awards_fetched_int, festival_label,
                 age_rating, is_cult_int, is_true_story_int, is_metacritic_int) = row

                age_days = (time.time() - cached_at) / 86400

                if age_days > _rating_ttl(release_date):
                    logger.info(f"Rating cache expired for {imdb_id} ({age_days:.1f}d old)")
                    cur.execute(
                        "DELETE FROM rating_cache WHERE imdb_id = %s",
                        (imdb_id,),
                    )
                    conn.commit()
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
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rating_cache
                        (imdb_id, ratings_json, genre, cached_at, release_date,
                         award_wins, award_noms, awards_fetched, festival_label,
                         age_rating, is_cult, is_true_story, is_metacritic)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (imdb_id) DO UPDATE SET
                        ratings_json   = EXCLUDED.ratings_json,
                        genre          = EXCLUDED.genre,
                        cached_at      = EXCLUDED.cached_at,
                        release_date   = EXCLUDED.release_date,
                        award_wins     = EXCLUDED.award_wins,
                        award_noms     = EXCLUDED.award_noms,
                        awards_fetched = EXCLUDED.awards_fetched,
                        festival_label = EXCLUDED.festival_label,
                        age_rating     = EXCLUDED.age_rating,
                        is_cult        = EXCLUDED.is_cult,
                        is_true_story  = EXCLUDED.is_true_story,
                        is_metacritic  = EXCLUDED.is_metacritic
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
            conn.commit()
    except Exception as exc:
        logger.error(f"Cache write error: {exc}")


# ---------------------------------------------------------------------------
# Quality cache
# ---------------------------------------------------------------------------

def get_cached_quality(imdb_id: str, release_date: str | None = None) -> list[str] | None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tokens, cached_at, release_date FROM quality_cache WHERE imdb_id = %s",
                    (imdb_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None

                tokens_raw, cached_at, stored_release = row
                ttl_release = release_date or stored_release
                age_days    = (time.time() - cached_at) / 86400
                if age_days > _quality_ttl(ttl_release):
                    logger.info(f"Quality cache expired for {imdb_id} ({age_days:.1f}d old)")
                    cur.execute("DELETE FROM quality_cache WHERE imdb_id = %s", (imdb_id,))
                    conn.commit()
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
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO quality_cache (imdb_id, tokens, cached_at, release_date)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (imdb_id) DO UPDATE SET
                        tokens       = EXCLUDED.tokens,
                        cached_at    = EXCLUDED.cached_at,
                        release_date = EXCLUDED.release_date
                    """,
                    (imdb_id, "|".join(tokens), int(time.time()), release_date),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Quality cache write error: {exc}")


# ---------------------------------------------------------------------------
# Trending snapshot cache
# ---------------------------------------------------------------------------

def get_cached_trending_snapshot(media_type: str) -> dict[str, int] | None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rankings_json, cached_at FROM trending_cache WHERE media_type = %s",
                    (media_type,),
                )
                row = cur.fetchone()
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
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trending_cache (media_type, rankings_json, cached_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (media_type) DO UPDATE SET
                        rankings_json = EXCLUDED.rankings_json,
                        cached_at     = EXCLUDED.cached_at
                    """,
                    (media_type, json.dumps(rankings), int(time.time())),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Trending snapshot cache write error: {exc}")


# ---------------------------------------------------------------------------
# TMDB metadata cache
# ---------------------------------------------------------------------------

def get_cached_tmdb_metadata(cache_key: str) -> dict | None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT title, release_year, genre_ids, is_textless, poster_path,
                           logos_json, cached_at,
                           credits_json, production_cos_json,
                           runtime, number_of_seasons, number_of_episodes,
                           original_language
                    FROM tmdb_metadata_cache
                    WHERE cache_key = %s
                    """,
                    (cache_key,),
                )
                row = cur.fetchone()
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
                    cur.execute(
                        "DELETE FROM tmdb_metadata_cache WHERE cache_key = %s",
                        (cache_key,),
                    )
                    conn.commit()
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
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tmdb_metadata_cache
                        (cache_key, title, release_year, genre_ids, is_textless,
                         poster_path, logos_json, cached_at,
                         credits_json, production_cos_json,
                         runtime, number_of_seasons, number_of_episodes,
                         original_language)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        title               = EXCLUDED.title,
                        release_year        = EXCLUDED.release_year,
                        genre_ids           = EXCLUDED.genre_ids,
                        is_textless         = EXCLUDED.is_textless,
                        poster_path         = EXCLUDED.poster_path,
                        logos_json          = EXCLUDED.logos_json,
                        cached_at           = EXCLUDED.cached_at,
                        credits_json        = EXCLUDED.credits_json,
                        production_cos_json = EXCLUDED.production_cos_json,
                        runtime             = EXCLUDED.runtime,
                        number_of_seasons   = EXCLUDED.number_of_seasons,
                        number_of_episodes  = EXCLUDED.number_of_episodes,
                        original_language   = EXCLUDED.original_language
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
            conn.commit()
    except Exception as exc:
        logger.error(f"TMDB metadata cache write error: {exc}")


def delete_cached_tmdb_metadata(cache_key: str) -> None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM tmdb_metadata_cache WHERE cache_key = %s",
                    (cache_key,),
                )
            conn.commit()
        logger.info(f"TMDB metadata cache invalidated for {cache_key}")
    except Exception as exc:
        logger.error(f"TMDB metadata cache delete error: {exc}")


# ---------------------------------------------------------------------------
# Digital release cache
# ---------------------------------------------------------------------------

def is_digital_release(imdb_id: str) -> bool:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM digital_release_cache WHERE imdb_id = %s",
                    (imdb_id,),
                )
                return cur.fetchone() is not None
    except Exception as exc:
        logger.error(f"Digital release cache lookup error: {exc}")
        return False


def count_digital_releases() -> int:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM digital_release_cache")
                (count,) = cur.fetchone()
                return count
    except Exception as exc:
        logger.error(f"Digital release cache count error: {exc}")
        return 0


def add_digital_releases(entries: list[tuple[str, int]]) -> int:
    if not entries:
        return 0
    inserted = 0
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                for imdb_id, posted_at in entries:
                    cur.execute(
                        """
                        INSERT INTO digital_release_cache (imdb_id, posted_at)
                        VALUES (%s, %s)
                        ON CONFLICT (imdb_id) DO NOTHING
                        """,
                        (imdb_id, posted_at),
                    )
                    if cur.rowcount:
                        inserted += cur.rowcount
            conn.commit()
    except Exception as exc:
        logger.error(f"Digital release cache write error: {exc}")
    return inserted


def ping() -> bool:
    """Cheap connectivity check for /ready probes (Phase 4)."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False


def close() -> None:
    """Close the pool — called from lifespan shutdown."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
