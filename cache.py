"""Storage facade.

Historically this module owned all SQLite cache code. The ElfHosted fork moves
the per-backend logic into the ``storage`` package so a Postgres backend can be
selected via ``DATABASE_URL``. Public function names and signatures are
preserved exactly so every ``from cache import …`` callsite works unchanged on
either backend.

Phase 6 (observability) adds thin instrumentation wrappers around the
most-trafficked lookup functions so cache hit/miss counters show up on the
/metrics endpoint. The wrappers are pass-throughs; backend selection stays
in storage/__init__.py.

Phase 10 split:
  * Composite (rendered) poster bytes — async, delegate to the blobstore
    package. Bytes live in S3 / B2 + CDN; the relational backend holds
    only metadata rows.
  * TMDB poster/logo bytes — sync, local filesystem only (per-pod
    ephemeral cache in front of TMDB's own CDN).

Cherry-pick guide:
  * Upstream changes to ``cache.py`` map almost 1-to-1 to
    ``storage/sqlite_backend.py``.
  * The Postgres backend (``storage/postgres_backend.py``) mirrors the same
    signatures; new upstream functions need a parallel addition there.
"""
from storage import (
    BACKEND_KIND,
    init_db,
    prune_caches,
    ping,
    close,
    set_cached_rating,
    set_cached_quality,
    get_cached_trending_snapshot,
    set_cached_trending_snapshot,
    set_cached_tmdb_poster,
    set_cached_tmdb_logo,
    set_cached_tmdb_metadata,
    delete_cached_tmdb_metadata,
    is_digital_release,
    count_digital_releases,
    add_digital_releases,
)
from storage import (
    get_cached_final_poster      as _raw_get_final_poster,
    set_cached_final_poster      as _raw_set_final_poster,
    get_cached_final_poster_url,
    is_cached_final_poster_fresh as _raw_is_final_fresh,
    get_cached_rating            as _raw_get_rating,
    get_cached_quality           as _raw_get_quality,
    get_cached_tmdb_metadata     as _raw_get_tmdb_metadata,
    get_cached_tmdb_poster       as _raw_get_tmdb_poster,
    get_cached_tmdb_logo         as _raw_get_tmdb_logo,
)
import metrics as _metrics


def _record(table: str, hit: bool) -> None:
    _metrics.cache_lookups_total.labels(
        table=table, result="hit" if hit else "miss",
    ).inc()


# Instrumented lookup wrappers. Behaviour identical to the underlying
# storage call; only side-effect is a counter increment.

async def get_cached_final_poster(cache_key):
    r = await _raw_get_final_poster(cache_key)
    _record("final_poster", r is not None)
    return r


async def is_cached_final_poster_fresh(cache_key) -> bool:
    """Lightweight freshness probe. Returns True if a fresh metadata
    row exists for this cache_key (no blob fetch). Used by /poster to
    skip the byte download when a CDN URL is available and we can 302
    straight to the CDN."""
    fresh = await _raw_is_final_fresh(cache_key)
    # Same `final_poster` table for hit/miss accounting.
    _record("final_poster", fresh)
    return fresh


async def set_cached_final_poster(cache_key, jpeg_bytes):
    """Async pass-through to the storage backend's blobstore-aware writer."""
    await _raw_set_final_poster(cache_key, jpeg_bytes)


def get_cached_rating(imdb_id):
    r = _raw_get_rating(imdb_id)
    _record("rating", r is not None)
    return r


def get_cached_quality(imdb_id, release_date=None):
    r = _raw_get_quality(imdb_id, release_date)
    _record("quality", r is not None)
    return r


def get_cached_tmdb_metadata(cache_key):
    r = _raw_get_tmdb_metadata(cache_key)
    _record("tmdb_metadata", r is not None)
    return r


def get_cached_tmdb_poster(cache_key):
    r = _raw_get_tmdb_poster(cache_key)
    _record("tmdb_poster", r is not None)
    return r


def get_cached_tmdb_logo(cache_key):
    r = _raw_get_tmdb_logo(cache_key)
    _record("tmdb_logo", r is not None)
    return r


__all__ = [
    "BACKEND_KIND",
    "init_db",
    "prune_caches",
    "ping",
    "close",
    "get_cached_final_poster",
    "get_cached_final_poster_url",
    "is_cached_final_poster_fresh",
    "set_cached_final_poster",
    "get_cached_rating",
    "set_cached_rating",
    "get_cached_quality",
    "set_cached_quality",
    "get_cached_trending_snapshot",
    "set_cached_trending_snapshot",
    "get_cached_tmdb_poster",
    "set_cached_tmdb_poster",
    "get_cached_tmdb_logo",
    "set_cached_tmdb_logo",
    "get_cached_tmdb_metadata",
    "set_cached_tmdb_metadata",
    "delete_cached_tmdb_metadata",
    "is_digital_release",
    "count_digital_releases",
    "add_digital_releases",
]
