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
import asyncio
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
from festivals import LEGACY_LABEL_KEYWORDS

# Pure helpers (TTL math, the composite L1 RAM cache, filesystem-cache
# primitives) shared with the SQLite backend so we don't duplicate them. These
# have no relational state of their own — they are static functions over a
# cache key, plus one process-local LRU — so both backends can use the same
# copy and upstream's TTL policy stays defined in exactly one place.
from storage.sqlite_backend import (
    _rating_ttl,
    _quality_ttl,
    _ttl_jitter,
    _composite_expiry,
    _composite_l1,
    _composite_l1_lock,
    _l1_get,
    _l1_put,
    composite_l1_stats,
    _quality_cache_context,
    _release_row_expiry,
    release_status_ttl_seconds,
    release_status_expiry,
    _safe_cache_path,
    _remove_if_dir,
    _prune_file_cache,
    get_cached_tmdb_poster,
    set_cached_tmdb_poster,
    get_cached_tmdb_logo,
    set_cached_tmdb_logo,
)


_pool: ConnectionPool | None = None

# Release-status TTLs are per-status and per-row now (upstream v1.2.0): the
# deadline is computed at write time by release_status_expiry and stored in
# expires_at. Only the legacy fallback cutoff is needed here, for rows written
# before that column existed.
from storage.sqlite_backend import _RELEASE_STATUS_TTL_DAYS

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
                is_metacritic  INTEGER NOT NULL DEFAULT 0,
                rating_min_votes INTEGER,
                festival_keyword TEXT
            )
        """)
        # Idempotent rating_min_votes migration for instances that pre-date the
        # rating-policy invalidation feature. Safe to run on every startup.
        cur.execute("""
            ALTER TABLE rating_cache ADD COLUMN IF NOT EXISTS rating_min_votes INTEGER
        """)
        # festival_keyword replaces festival_label: the cache now remembers
        # *which festival* a title won something at and lets festivals.py decide
        # the wording at render time. Storing the wording was the bug — rows
        # written while "festival-cannes-winner" read as "Palme d'Or" kept
        # saying Palme d'Or long after the code stopped believing it.
        #
        # The old labels map back to their keyword exactly, so existing rows
        # convert in place instead of costing one MDblist request each. Unlike
        # SQLite there is no "did I just add the column" signal to gate on, so
        # the backfill is written to be idempotent instead: it only touches rows
        # that still have no keyword, which makes re-running it on every startup
        # a no-op once converted.
        cur.execute("""
            ALTER TABLE rating_cache ADD COLUMN IF NOT EXISTS festival_keyword TEXT
        """)
        cur.executemany(
            "UPDATE rating_cache SET festival_keyword = %s "
            "WHERE festival_label = %s AND festival_keyword IS NULL",
            [(keyword, label) for label, keyword in LEGACY_LABEL_KEYWORDS.items()],
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quality_cache (
                imdb_id      TEXT PRIMARY KEY,
                tokens       TEXT,
                cached_at    BIGINT,
                release_date TEXT,
                cache_context TEXT NOT NULL DEFAULT ''
            )
        """)
        cur.execute(
            "ALTER TABLE quality_cache ADD COLUMN IF NOT EXISTS "
            "cache_context TEXT NOT NULL DEFAULT ''"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trending_cache (
                media_type    TEXT PRIMARY KEY,
                rankings_json TEXT,
                cached_at     BIGINT,
                source_sig    TEXT
            )
        """)
        # Which source a snapshot came from. Without this, changing
        # TRENDING_SOURCE_* had no visible effect until the snapshot aged out on
        # its own — up to a day of an operator setting the variable, restarting,
        # seeing the old rankings and concluding the feature was broken.
        cur.execute(
            "ALTER TABLE trending_cache ADD COLUMN IF NOT EXISTS source_sig TEXT"
        )
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
                original_language   TEXT,
                original_title      TEXT,
                backdrop_path       TEXT,
                tmdb_status         TEXT,
                vote_count          INTEGER,
                vote_average        REAL,
                text_backdrop_path  TEXT,
                original_poster_path TEXT,
                poster_langs_json   TEXT,
                imdb_id             TEXT,
                tmdb_release_date   TEXT,
                last_air_date       TEXT,
                next_episode_json   TEXT,
                last_episode_json   TEXT,
                seasons_json        TEXT,
                metadata_version    INTEGER
            )
        """)
        # Idempotent additive migrations for instances that pre-date these
        # columns. Postgres supports ADD COLUMN IF NOT EXISTS, so each is a
        # no-op on installs that already have the column. Safe on every startup.
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
            cur.execute(
                f"ALTER TABLE tmdb_metadata_cache ADD COLUMN IF NOT EXISTS {col} {definition}"
            )
        # Phase 10: composite-poster bytes live in blobstore (FS or S3); the
        # table holds only metadata so the relational backend can do TTL
        # bookkeeping cheaply.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS final_poster_cache (
                cache_key  TEXT PRIMARY KEY,
                cached_at  BIGINT NOT NULL,
                request_params TEXT,
                expires_at BIGINT
            )
        """)
        # request_params lets the trending refresh replay the exact /poster
        # query that produced a composite; expires_at gives each composite its
        # own deadline, because a render derived from a trending rank or a
        # release status must not outlive the fact it was derived from. NULL
        # rows predate the column and fall back to cached_at + TTL + jitter.
        for _col, _definition in (
            ("request_params", "TEXT"),
            ("expires_at",     "BIGINT"),
        ):
            cur.execute(
                f"ALTER TABLE final_poster_cache "
                f"ADD COLUMN IF NOT EXISTS {_col} {_definition}"
            )
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
        # Phase 11: imdb_id -> tmdb_id mapping for the preset endpoint.
        # No TTL — these mappings are effectively permanent.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS imdb_to_tmdb_cache (
                imdb_id    TEXT NOT NULL,
                media_type TEXT NOT NULL,
                tmdb_id    TEXT NOT NULL,
                PRIMARY KEY (imdb_id, media_type)
            )
        """)
        # Release status cache — populated on demand when the "release_status"
        # sash slot is enabled. Stored separately from the main metadata cache
        # so users who don't enable the feature never pay the extra API call.
        # cache_key = "{media_type}_{tmdb_id}", status =
        # "BluRay"|"Streaming"|"Cinema"|"Production". TTL: 7 days.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS release_status_cache (
                cache_key  TEXT PRIMARY KEY,
                status     TEXT NOT NULL,
                cached_at  BIGINT NOT NULL,
                expires_at BIGINT
            )
        """)
        # Richer sibling of release_status_cache for TMDB /release_dates data.
        # Stores JSON with status plus theatrical, digital/TV and physical dates
        # so multiple sash slots share one TMDB lookup.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movie_release_info_cache (
                cache_key  TEXT PRIMARY KEY,
                info_json  TEXT NOT NULL,
                cached_at  BIGINT NOT NULL,
                expires_at BIGINT
            )
        """)
        # Per-row expiry for both release caches: the TTL is no longer a single
        # constant — it depends on the status and on when TMDB says the title
        # next moves — so the deadline is computed at write time and stored.
        for _table in ("release_status_cache", "movie_release_info_cache"):
            cur.execute(
                f"ALTER TABLE {_table} ADD COLUMN IF NOT EXISTS expires_at BIGINT"
            )
        # Small generic key/value store for app-level bookkeeping (e.g. the last
        # cache-warm cycle's timestamp) that doesn't warrant its own table.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Generic JSON cache for TVDB bookkeeping: resolved TVDB ids (incl.
        # negative "no match" results), per-title artwork indexes, the
        # artwork-type catalogue, and the auth token. Each row carries its own
        # TTL so different record kinds coexist in one table.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tvdb_cache (
                cache_key   TEXT PRIMARY KEY,
                value_json  TEXT NOT NULL,
                cached_at   BIGINT NOT NULL,
                ttl_seconds BIGINT NOT NULL
            )
        """)
        # Burned-in-text detection results, keyed by source asset + detection
        # params. TMDB image paths are content-addressed (immutable), so the
        # answer never goes stale; cached_at exists only for housekeeping.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS text_detection_cache (
                cache_key TEXT PRIMARY KEY,
                has_text  INTEGER NOT NULL,
                cached_at BIGINT NOT NULL
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

def _peek_final_poster(cache_key: str) -> int | None:
    """Row-only TTL check. Returns the row's expires_at if a fresh row exists,
    else None; deletes the row on expiry. Caller handles blob cleanup."""
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cached_at, expires_at FROM final_poster_cache WHERE cache_key = %s",
                (cache_key,),
            )
            row = cur.fetchone()
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
                cur.execute(
                    "DELETE FROM final_poster_cache WHERE cache_key = %s",
                    (cache_key,),
                )
                conn.commit()
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
    never touches the blobstore. Lets /poster emit a 302 to the CDN without
    pulling the bytes through the app pod.

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
    """Return cached JPEG bytes for a fully composited poster, or None on
    miss/expiry."""
    entry = await get_cached_final_poster_entry(cache_key)
    return None if entry is None else entry[0]


async def get_cached_final_poster_entry(cache_key: str) -> "tuple[bytes, int] | None":
    """Return (jpeg_bytes, expires_at) for a composited poster, or None on miss.

    L1 (process-local RAM, shared with the SQLite backend) first, then the
    metadata row plus a blobstore fetch. Used as the inline-serve fallback
    when no CDN URL is configured.
    """
    try:
        hit = _l1_get(cache_key, time.time())
        if hit is not None:
            return hit

        expires_at = _peek_final_poster(cache_key)
        if expires_at is None:
            # Queue rather than delete inline: this read can interleave with
            # another request mid-write for the same key, and an inline delete
            # would remove the blob it had just published. The drain re-checks
            # the row first, which is the check that makes it safe.
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
    """Public CDN URL for the composite, or None when no URL is configured."""
    return blobstore.url_for(blobstore.BUCKET_COMPOSITES, cache_key)


async def set_cached_final_poster(
    cache_key: str,
    jpeg_bytes: bytes,
    request_params: str = None,
    ttl_override: int = None,
) -> int:
    """Store a composited JPEG: bytes to the blobstore, metadata row to
    Postgres, bytes into L1. Returns the unix time this composite expires.

    *ttl_override* caps the jittered lifetime for a render derived from
    something shorter-lived than COMPOSITE_CACHE_TTL.
    """
    now = int(time.time())
    ttl = COMPOSITE_CACHE_TTL + _ttl_jitter(cache_key, COMPOSITE_CACHE_TTL_JITTER)
    if ttl_override is not None:
        ttl = min(ttl, ttl_override)
    expires_at = now + int(ttl)

    try:
        # Write bytes first so the metadata row is never present without
        # a backing blob.
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
        # never landed would serve this replica a poster the rest of the fleet
        # cannot see, and would mask the failure until it aged out.
        _l1_put(cache_key, jpeg_bytes, expires_at)

        evict_keys: list[str] = []
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO final_poster_cache
                        (cache_key, cached_at, request_params, expires_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        cached_at      = EXCLUDED.cached_at,
                        request_params = EXCLUDED.request_params,
                        expires_at     = EXCLUDED.expires_at
                    """,
                    (cache_key, now, request_params, expires_at),
                )
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

        # Best-effort blob + L1 cleanup for evicted keys, outside the pool ctx.
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
    """Remove a composited poster from L1 and the metadata row, and queue its
    blob for deletion.

    Sync, like upstream: the blob delete is an object-store round trip, so it
    goes on blobstore's deferred queue rather than forcing every caller of this
    onto the event loop. See blobstore.delete_later.
    """
    if COMPOSITE_MEM_ENTRIES > 0:
        with _composite_l1_lock:
            _composite_l1.pop(cache_key, None)
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM final_poster_cache WHERE cache_key = %s", (cache_key,)
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Final poster cache delete error: {exc}")
    blobstore.delete_later(blobstore.BUCKET_COMPOSITES, cache_key)


def invalidate_final_posters(tmdb_id: str, media_type: str | None = None) -> None:
    """Invalidate all composited posters for a specific TMDB ID, so the next
    request re-renders with updated badges."""
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

    if type_variants is None:
        patterns = [f"%:{tmdb_id}:%"]
    else:
        patterns = [f"%:{tmdb_id}:{_tv}:%" for _tv in type_variants]

    # DELETE ... RETURNING gives us the keys and removes the rows in one
    # statement, so there is no window in which another replica could insert a
    # row between our SELECT and our DELETE and have it silently dropped.
    blob_keys: list[str] = []
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                for pattern in patterns:
                    cur.execute(
                        "DELETE FROM final_poster_cache WHERE cache_key LIKE %s "
                        "RETURNING cache_key",
                        (pattern,),
                    )
                    blob_keys.extend(r[0] for r in cur.fetchall())
            conn.commit()
        logger.info(f"Invalidated final poster cache for tmdb_id={tmdb_id}")
    except Exception as exc:
        logger.error(f"Final poster cache invalidate error: {exc}")

    for k in blob_keys:
        blobstore.delete_later(blobstore.BUCKET_COMPOSITES, k)


def list_composite_request_params() -> list[tuple[str, str]]:
    """(cache_key, request_params) for every composite that recorded the query
    that produced it.

    Upstream's trending refresh reaches for cache.get_db() and runs this SELECT
    inline, which only works against SQLite. Exposing it as a backend function
    keeps that background job working on either backend.
    """
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cache_key, request_params FROM final_poster_cache "
                    "WHERE request_params IS NOT NULL"
                )
                return [(r[0], r[1]) for r in cur.fetchall()]
    except Exception as exc:
        logger.error(f"Composite request-params query error: {exc}")
        return []


async def prune_caches() -> None:
    """Prune every cache, without stalling the event loop.

    Same split as the SQLite backend: the relational deletes and the local
    filesystem sweep are synchronous and can be slow on a large cache, so they
    run on an executor thread. The pool is thread-safe, so unlike SQLite there
    is no per-thread connection to tidy up afterwards. Only the blob-store work
    stays on the loop.
    """
    expired_composite_keys = await asyncio.to_thread(_prune_sync)

    # Best-effort blob deletion for the expired composite rows, plus anything
    # the synchronous delete/invalidate paths queued since the last sweep.
    # Through the same queue rather than deleted here, so they get the same
    # row check: a title re-rendered between the prune's DELETE and this line
    # has a live row again, and its blob must survive.
    for k in expired_composite_keys:
        blobstore.delete_later(blobstore.BUCKET_COMPOSITES, k)
    await blobstore.drain_deferred_deletes(is_live=_composite_row_is_live)
    blobstore._forget_generations_if_idle()


async def prune_local_caches() -> None:
    """Pod-local cleanup: the deferred blob queue and the TMDB artwork files.

    TMDB poster/logo bytes are a per-POD filesystem cache on this backend too
    (see the module docstring) — only the relational data is shared. They were
    never swept on Postgres deployments at all, so a long-running pod grew
    artwork for every title it ever rendered, ignoring the configured TTLs.
    Being pod-local, this has to run on followers as well as the prune leader.
    """
    await blobstore.drain_deferred_deletes(is_live=_composite_row_is_live)
    blobstore._forget_generations_if_idle()
    await asyncio.to_thread(_prune_local_files)


def _prune_local_files() -> None:
    # Same jitter allowance as the SQLite backend, so this never deletes a file
    # the read path would still consider fresh.
    _prune_file_cache(
        TMDB_POSTER_CACHE_DIR,
        TMDB_POSTER_CACHE_DURATION + TMDB_IMAGE_CACHE_JITTER_DAYS / 2,
    )
    _prune_file_cache(
        TMDB_LOGO_CACHE_DIR,
        TMDB_LOGO_CACHE_DURATION + TMDB_IMAGE_CACHE_JITTER_DAYS / 2,
    )


def _prune_sync() -> list[str]:
    """Relational + filesystem pruning. Runs on an executor thread."""
    now = int(time.time())
    expired_composite_keys: list[str] = []
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                # Phase 10: capture composite keys before deletion so we can
                # drop the corresponding blobs after the txn commits.
                # Per-row deadline: a render can be pinned to a trending rank
                # or a release status that expires well before
                # COMPOSITE_CACHE_TTL. Rows predating expires_at fall back to
                # the flat TTL plus the largest jitter any key can draw, so
                # this never deletes one the read path would still call fresh.
                # DELETE ... RETURNING keeps the row removal and the blob-key
                # capture in one statement, so no row can slip between them.
                cur.execute(
                    "DELETE FROM final_poster_cache WHERE "
                    "(expires_at IS NOT NULL AND expires_at < %s) OR "
                    "(expires_at IS NULL AND cached_at < %s) "
                    "RETURNING cache_key",
                    (now, now - COMPOSITE_CACHE_TTL - COMPOSITE_CACHE_TTL_JITTER // 2),
                )
                expired_composite_keys = [r[0] for r in cur.fetchall()]
                if expired_composite_keys:
                    logger.info(
                        f"Pruned {len(expired_composite_keys)} expired composite cache entries"
                    )

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

                # Expiry is per-row now (see release_status_expiry), so prune on
                # the stored deadline. Rows predating expires_at are only
                # dropped once past the LONGEST tier, since their real deadline
                # depends on a status this SQL cannot evaluate — the read path
                # tiers them correctly in the meantime and rewrites them with a
                # deadline as soon as they are refreshed.
                legacy_cutoff = now - max(_RELEASE_STATUS_TTL_DAYS.values()) * 86400
                for _table, _label in (
                    ("release_status_cache",     "release status"),
                    ("movie_release_info_cache", "movie release info"),
                ):
                    cur.execute(
                        f"DELETE FROM {_table} WHERE "
                        "(expires_at IS NOT NULL AND expires_at < %s) OR "
                        "(expires_at IS NULL AND cached_at < %s)",
                        (now, legacy_cutoff),
                    )
                    if cur.rowcount:
                        logger.info(f"Pruned {cur.rowcount} expired {_label} cache entries")

                detection_cutoff = now - 180 * 86400
                cur.execute(
                    "DELETE FROM text_detection_cache WHERE cached_at < %s",
                    (detection_cutoff,),
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} old text-detection cache entries")

                # Each tvdb_cache row stores its own TTL, so expiry is per-row
                # rather than a single cutoff.
                cur.execute(
                    "DELETE FROM tvdb_cache WHERE (%s - cached_at) > ttl_seconds",
                    (now,),
                )
                if cur.rowcount:
                    logger.info(f"Pruned {cur.rowcount} expired TVDB cache entries")
            conn.commit()
        # Postgres autovacuum handles space reclamation — no explicit VACUUM here.

    except Exception as exc:
        logger.error(f"Cache prune error: {exc}")
    return expired_composite_keys


# ---------------------------------------------------------------------------
# Rating cache
# ---------------------------------------------------------------------------

def get_cached_rating(imdb_id: str):
    """11-tuple, or None if the row is absent or expired.

    Position 7 is the raw MDblist keyword ("festival-cannes-winner"), not a
    sash label — festivals.py turns it into wording at render time.
    """
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ratings_json, genre, cached_at, release_date,
                           award_wins, award_noms, awards_fetched, festival_keyword,
                           age_rating, is_cult, is_true_story, is_metacritic,
                           rating_min_votes
                    FROM rating_cache
                    WHERE imdb_id = %s
                    """,
                    (imdb_id,),
                )
                row = cur.fetchone()
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
                    cur.execute(
                        "DELETE FROM rating_cache WHERE imdb_id = %s",
                        (imdb_id,),
                    )
                    conn.commit()
                    return None

                age_days = (time.time() - cached_at) / 86400

                if age_days > _rating_ttl(release_date):
                    logger.info(f"Rating cache expired for {imdb_id} ({age_days:.1f}d old)")
                    cur.execute(
                        "DELETE FROM rating_cache WHERE imdb_id = %s",
                        (imdb_id,),
                    )
                    conn.commit()
                    return None

                if rating_min_votes is None:
                    # Rows created before policy tracking are still valid until
                    # their normal TTL expires. Backfill in place instead of
                    # consuming one MDBList request per legacy cache entry after
                    # an upgrade.
                    cur.execute(
                        "UPDATE rating_cache SET rating_min_votes = %s "
                        "WHERE imdb_id = %s AND rating_min_votes IS NULL",
                        (RATING_MIN_VOTES, imdb_id),
                    )
                    conn.commit()
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
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO rating_cache
                        (imdb_id, ratings_json, genre, cached_at, release_date,
                         award_wins, award_noms, awards_fetched, festival_keyword,
                         age_rating, is_cult, is_true_story, is_metacritic,
                         rating_min_votes)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (imdb_id) DO UPDATE SET
                        ratings_json     = EXCLUDED.ratings_json,
                        genre            = EXCLUDED.genre,
                        cached_at        = EXCLUDED.cached_at,
                        release_date     = EXCLUDED.release_date,
                        award_wins       = EXCLUDED.award_wins,
                        award_noms       = EXCLUDED.award_noms,
                        awards_fetched   = EXCLUDED.awards_fetched,
                        festival_keyword   = EXCLUDED.festival_keyword,
                        age_rating       = EXCLUDED.age_rating,
                        is_cult          = EXCLUDED.is_cult,
                        is_true_story    = EXCLUDED.is_true_story,
                        is_metacritic    = EXCLUDED.is_metacritic,
                        rating_min_votes = EXCLUDED.rating_min_votes
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
                    "SELECT tokens, cached_at, release_date, cache_context "
                    "FROM quality_cache WHERE imdb_id = %s",
                    (imdb_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return None

                tokens_raw, cached_at, stored_release, stored_context = row
                if stored_context != _quality_cache_context():
                    logger.info(f"Quality cache policy changed for {imdb_id}; refreshing")
                    cur.execute("DELETE FROM quality_cache WHERE imdb_id = %s", (imdb_id,))
                    conn.commit()
                    return None
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
                    INSERT INTO quality_cache
                        (imdb_id, tokens, cached_at, release_date, cache_context)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (imdb_id) DO UPDATE SET
                        tokens        = EXCLUDED.tokens,
                        cached_at     = EXCLUDED.cached_at,
                        release_date  = EXCLUDED.release_date,
                        cache_context = EXCLUDED.cache_context
                    """,
                    (
                        imdb_id,
                        "|".join(tokens),
                        int(time.time()),
                        release_date,
                        _quality_cache_context(),
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Quality cache write error: {exc}")


# ---------------------------------------------------------------------------
# Trending snapshot cache
# ---------------------------------------------------------------------------

def get_cached_trending_snapshot(
    media_type: str, source_sig: str | None = None
) -> dict[str, int] | None:
    """Cached rankings for *media_type*, or None if absent, stale, or from a
    different source.

    *source_sig* identifies where the snapshot came from (see
    tmdb.trending_source_signature). A mismatch is treated as expired so that
    changing TRENDING_SOURCE_* takes effect on the next request rather than
    whenever the day-long TTL happens to lapse.
    """
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rankings_json, cached_at, source_sig "
                    "FROM trending_cache WHERE media_type = %s",
                    (media_type,),
                )
                row = cur.fetchone()
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
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO trending_cache
                        (media_type, rankings_json, cached_at, source_sig)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (media_type) DO UPDATE SET
                        rankings_json = EXCLUDED.rankings_json,
                        cached_at     = EXCLUDED.cached_at,
                        source_sig    = EXCLUDED.source_sig
                    """,
                    (
                        media_type,
                        json.dumps(rankings),
                        int(time.time()),
                        source_sig or "",
                    ),
                )
            conn.commit()

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

        # Invalidate final posters for items that changed trending rank or
        # dropped out.
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
                           original_language, original_title, backdrop_path,
                           tmdb_status, vote_count, vote_average,
                           text_backdrop_path, original_poster_path,
                           poster_langs_json, imdb_id,
                           tmdb_release_date, last_air_date, next_episode_json,
                           last_episode_json, seasons_json, metadata_version
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
                    original_language, original_title, backdrop_path,
                    tmdb_status, vote_count, vote_average,
                    text_backdrop_path, original_poster_path,
                    poster_langs_json, imdb_id,
                    tmdb_release_date, last_air_date, next_episode_json,
                    last_episode_json, seasons_json, metadata_version,
                ) = row

                age_days = (time.time() - cached_at) / 86400

                # A title crossing its own release date changes what the poster
                # should say, so the row is force-expired at that boundary and
                # kept on a one-day TTL either side of it.
                if tmdb_release_date:
                    try:
                        from datetime import timezone
                        rel_dt = datetime.strptime(
                            tmdb_release_date, "%Y-%m-%d"
                        ).replace(tzinfo=timezone.utc)
                        rel_ts = rel_dt.timestamp()
                        now_ts = time.time()

                        if cached_at < rel_ts and now_ts >= rel_ts:
                            age_days = 9999  # Force expiration
                        elif (now_ts < rel_ts or (now_ts - rel_ts) < 14 * 86400) and age_days > 1.0:
                            age_days = 9999
                    except Exception:
                        pass

                if age_days > TMDB_METADATA_CACHE_DURATION:
                    logger.info(
                        f"TMDB metadata cache expired for {cache_key} ({age_days:.1f}d old)"
                    )
                    cur.execute(
                        "DELETE FROM tmdb_metadata_cache WHERE cache_key = %s",
                        (cache_key,),
                    )
                    conn.commit()

                    if age_days == 9999:
                        parts = cache_key.split("_")
                        if len(parts) >= 2:
                            m_type, t_id = parts[0], parts[1]
                            invalidate_final_posters(t_id, m_type)

                    return None

                # Rows created before newer metadata fields were added were
                # migrated with NULL. Refresh once so discovery sashes have
                # complete title, vote, and TV lifecycle fields.
                if vote_count is None or original_title is None or metadata_version != 4:
                    logger.info(
                        f"TMDB metadata cache missing current schema fields for "
                        f"{cache_key}; refreshing"
                    )
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
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tmdb_metadata_cache
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
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        title                 = EXCLUDED.title,
                        release_year          = EXCLUDED.release_year,
                        genre_ids             = EXCLUDED.genre_ids,
                        is_textless           = EXCLUDED.is_textless,
                        poster_path           = EXCLUDED.poster_path,
                        logos_json            = EXCLUDED.logos_json,
                        cached_at             = EXCLUDED.cached_at,
                        credits_json          = EXCLUDED.credits_json,
                        production_cos_json   = EXCLUDED.production_cos_json,
                        runtime               = EXCLUDED.runtime,
                        number_of_seasons     = EXCLUDED.number_of_seasons,
                        number_of_episodes    = EXCLUDED.number_of_episodes,
                        original_language     = EXCLUDED.original_language,
                        original_title        = EXCLUDED.original_title,
                        backdrop_path         = EXCLUDED.backdrop_path,
                        tmdb_status           = EXCLUDED.tmdb_status,
                        vote_count            = EXCLUDED.vote_count,
                        vote_average          = EXCLUDED.vote_average,
                        text_backdrop_path    = EXCLUDED.text_backdrop_path,
                        original_poster_path  = EXCLUDED.original_poster_path,
                        poster_langs_json     = EXCLUDED.poster_langs_json,
                        imdb_id               = EXCLUDED.imdb_id,
                        tmdb_release_date     = EXCLUDED.tmdb_release_date,
                        last_air_date         = EXCLUDED.last_air_date,
                        next_episode_json     = EXCLUDED.next_episode_json,
                        last_episode_json     = EXCLUDED.last_episode_json,
                        seasons_json          = EXCLUDED.seasons_json,
                        metadata_version      = EXCLUDED.metadata_version
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


# ---------------------------------------------------------------------------
# Release status cache
# ---------------------------------------------------------------------------
# Cached separately from main metadata so the extra TMDB /release_dates call
# only happens for users who have enabled the "release_status" sash slot.
# TTL: 7 days — status changes slowly (Cinema → Streaming → BluRay is one-way).

def get_cached_movie_release_info(cache_key: str) -> dict | None:
    """Return cached movie release info JSON, or None if absent / expired."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT info_json, cached_at, expires_at "
                    "FROM movie_release_info_cache WHERE cache_key = %s",
                    (cache_key,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                info_json, cached_at, expires_at = row
                info = json.loads(info_json or "{}")
                # The stored status is only a snapshot; callers recompute it
                # from the dates. Tier this row's TTL off that same stored
                # status so a finished title is not re-fetched weekly for dates
                # that can no longer move.
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
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO movie_release_info_cache
                        (cache_key, info_json, cached_at, expires_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        info_json  = EXCLUDED.info_json,
                        cached_at  = EXCLUDED.cached_at,
                        expires_at = EXCLUDED.expires_at
                    """,
                    (cache_key, json.dumps(info), now, int(expires_at)),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Movie release info cache write error: {exc}")


def get_cached_release_status(cache_key: str) -> str | None:
    """Return the cached release status string, or None if absent / expired."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, cached_at, expires_at "
                    "FROM release_status_cache WHERE cache_key = %s",
                    (cache_key,),
                )
                row = cur.fetchone()
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

    *expires_at* comes from release_status_expiry() at the call site, which
    knows the title's upcoming release dates; omitting it falls back to the
    status tier.
    """
    try:
        now = int(time.time())
        if expires_at is None:
            expires_at = _release_row_expiry(status, now)
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO release_status_cache
                        (cache_key, status, cached_at, expires_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        status     = EXCLUDED.status,
                        cached_at  = EXCLUDED.cached_at,
                        expires_at = EXCLUDED.expires_at
                    """,
                    (cache_key, status, now, int(expires_at)),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Release status cache write error: {exc}")


# ---------------------------------------------------------------------------
# TVDB JSON cache  (per-row TTL)
# ---------------------------------------------------------------------------

def get_cached_tvdb_json(cache_key: str) -> dict | None:
    """Return the cached JSON object for *cache_key*, or None on miss/expiry.
    Expired rows are deleted on read so stale data never lingers."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT value_json, cached_at, ttl_seconds "
                    "FROM tvdb_cache WHERE cache_key = %s",
                    (cache_key,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                value_json, cached_at, ttl_seconds = row
                if (time.time() - cached_at) > ttl_seconds:
                    cur.execute(
                        "DELETE FROM tvdb_cache WHERE cache_key = %s", (cache_key,)
                    )
                    conn.commit()
                    return None
                return json.loads(value_json)
    except Exception as exc:
        logger.error(f"TVDB cache read error: {exc}")
        return None


def set_cached_tvdb_json(cache_key: str, value: dict, ttl_seconds: int) -> None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tvdb_cache
                        (cache_key, value_json, cached_at, ttl_seconds)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        value_json  = EXCLUDED.value_json,
                        cached_at   = EXCLUDED.cached_at,
                        ttl_seconds = EXCLUDED.ttl_seconds
                    """,
                    (cache_key, json.dumps(value), int(time.time()), int(ttl_seconds)),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"TVDB cache write error: {exc}")


# ---------------------------------------------------------------------------
# App state — small key/value store for cross-restart bookkeeping
# ---------------------------------------------------------------------------

def get_app_state(key: str) -> str | None:
    """Return the stored string value for *key*, or None if unset/on error."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_state WHERE key = %s", (key,))
                row = cur.fetchone()
                return None if row is None else row[0]
    except Exception as exc:
        logger.error(f"App state read error ({key}): {exc}")
        return None


def claim_app_state_slot(key: str, now: float, min_interval: float) -> bool:
    """Atomically claim a periodic job slot; True if this caller won it.

    Every worker — and, here, every replica — runs its own copy of each
    background loop against one shared database. For a cheap job that
    duplication is harmless, but a job that downloads tens of megabytes and
    rewrites a table wants exactly one runner per interval.

    The check and the write are one statement so two claimants waking together
    cannot both see a stale timestamp and both proceed. The conditional upsert
    is evaluated against the committed row and only one write survives;
    rowcount then tells the caller whether it was theirs.

    Note the Postgres-specific hazard the SQLite version does not have: two
    concurrent INSERTs on the same key make the loser wait on the winner's row
    lock, and when it resolves to DO UPDATE its WHERE sees the winner's fresh
    value — so it correctly loses rather than both claiming.

    The CASE is not decoration. A bare ``value::double precision`` raises on any
    row this key ever held that was not a number, and Postgres does not promise
    to short-circuit an ``OR`` guard around the cast — only CASE is evaluated in
    order. Falling back to 0 also matches SQLite's ``CAST(value AS REAL)``,
    which yields 0 for non-numeric text: an unparseable timestamp reads as
    "claimable" on both backends rather than wedging the job forever.
    """
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    r"""
                    INSERT INTO app_state (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    WHERE (CASE
                             WHEN app_state.value ~ '^-?[0-9]+(\.[0-9]+)?$'
                               THEN app_state.value::double precision
                             ELSE 0
                           END) <= %s
                    """,
                    (key, str(now), now - min_interval),
                )
                won = cur.rowcount > 0
            conn.commit()
            return won
    except Exception as exc:
        # Never let bookkeeping stop the job — a failure here degrades to the
        # old behaviour (every worker runs it), not to nothing running.
        logger.error(f"App state claim error ({key}): {exc}")
        return True


def set_app_state(key: str, value: str) -> None:
    """Upsert a string value in the app-state key/value store."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app_state (key, value) VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (key, value),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"App state write error ({key}): {exc}")


def get_cached_text_detection(cache_key: str) -> bool | None:
    """Return the cached burned-in-text result (True/False), or None if absent.

    Results never expire — they're keyed by an immutable TMDB image path plus the
    detection params, so the answer can't change for a given key.
    """
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT has_text FROM text_detection_cache WHERE cache_key = %s",
                    (cache_key,),
                )
                row = cur.fetchone()
                return None if row is None else bool(row[0])
    except Exception as exc:
        logger.error(f"Text-detection cache read error: {exc}")
        return None


def set_cached_text_detection(cache_key: str, has_text: bool) -> None:
    """Upsert a burned-in-text detection result."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO text_detection_cache (cache_key, has_text, cached_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        has_text  = EXCLUDED.has_text,
                        cached_at = EXCLUDED.cached_at
                    """,
                    (cache_key, int(has_text), int(time.time())),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"Text-detection cache write error: {exc}")


# ---------------------------------------------------------------------------
# Cache stats
# ---------------------------------------------------------------------------

def get_cache_stats() -> dict:
    """
    Return row counts for every cache table.  Used by the /stats endpoint so
    operators can see cache health at a glance.  Never raises.

    composite_bytes / db_file_bytes are not meaningful for the Postgres
    backend (composite bytes live in the blobstore, and the DB is a shared
    server with no single file size to report), so both are reported as None
    to keep the response shape identical to the SQLite backend.
    """
    stats: dict = {}
    try:
        with _get_pool().connection() as conn:
            for table in (
                "rating_cache", "quality_cache", "trending_cache",
                "tmdb_metadata_cache", "final_poster_cache",
                "digital_release_cache", "release_status_cache",
                "movie_release_info_cache", "text_detection_cache",
                "tvdb_cache", "imdb_to_tmdb_cache",
            ):
                try:
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT COUNT(*) FROM {table}")
                        (n,) = cur.fetchone()
                    stats[table] = n
                except Exception:
                    # A failed COUNT aborts the transaction; roll back so the
                    # next table's query runs on a clean connection.
                    conn.rollback()
                    stats[table] = None

        # Composite bytes live in the blobstore, not Postgres — the relational
        # backend only holds metadata rows. And there is no single DB file to
        # stat on a shared server. Reported as None to match the SQLite shape.
        stats["composite_bytes"] = None
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


def get_cached_imdb_to_tmdb(imdb_id: str, media_type: str) -> str | None:
    """Phase 11: look up the cached tmdb_id for an imdb_id + media_type."""
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tmdb_id FROM imdb_to_tmdb_cache "
                    "WHERE imdb_id = %s AND media_type = %s",
                    (imdb_id, media_type),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as exc:
        logger.error(f"imdb_to_tmdb cache read error: {exc}")
        return None


def set_cached_imdb_to_tmdb(imdb_id: str, media_type: str, tmdb_id: str) -> None:
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO imdb_to_tmdb_cache (imdb_id, media_type, tmdb_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (imdb_id, media_type) DO UPDATE SET tmdb_id = EXCLUDED.tmdb_id
                    """,
                    (imdb_id, media_type, tmdb_id),
                )
            conn.commit()
    except Exception as exc:
        logger.error(f"imdb_to_tmdb cache write error: {exc}")


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
