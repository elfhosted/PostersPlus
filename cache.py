"""Storage facade.

Upstream owns all cache logic in this module. The ElfHosted fork moves the
per-backend implementation into the ``storage`` package so a Postgres backend
can be selected via ``DATABASE_URL``, while composite poster BYTES move to the
``blobstore`` package (local FS default, S3/CDN via ``OBJECT_STORE_URL``).

Public function names and signatures are preserved exactly so every
``from cache import …`` callsite works unchanged on either backend. A thin
metrics wrapper around the most-trafficked lookups feeds cache hit/miss
counters to /metrics; the wrappers are otherwise pass-throughs. Backend
selection lives in storage/__init__.py.

Note: the final-poster trio (get/set/is_fresh) is ASYNC here because the
bytes live in the blobstore. Callers must ``await`` them.

Cherry-pick guide:
  * Upstream changes to cache logic map almost 1-to-1 to
    storage/sqlite_backend.py (which is seeded from upstream cache.py).
  * The Postgres backend mirrors the same signatures; a new upstream cache
    function needs a parallel addition in storage/postgres_backend.py and an
    entry in storage/__init__.py's _PUBLIC_API.
  * ``get_db`` is deliberately NOT re-exported. Upstream hands callers a raw
    SQLite connection; here the backend may be Postgres, so any upstream code
    that reaches for get_db() needs a named backend function instead (see
    list_composite_request_params, which replaces the one such query in
    main.py's trending refresh).
"""
# Pass-through re-exports (no instrumentation).
from storage import (
    BACKEND_KIND,
    init_db,
    prune_caches,
    prune_local_caches,
    ping,
    close,
    get_cached_final_poster_url,
    get_cached_final_poster_redirect,
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
    delete_cached_final_poster,
    invalidate_final_posters,
    composite_l1_stats,
    release_status_ttl_seconds,
    release_status_expiry,
    get_cached_movie_release_info,
    set_cached_movie_release_info,
    get_cached_tvdb_json,
    set_cached_tvdb_json,
    get_app_state,
    set_app_state,
    claim_app_state_slot,
    list_composite_request_params,
    get_cached_imdb_to_tmdb,
    set_cached_imdb_to_tmdb,
    set_cached_release_status,
    set_cached_text_detection,
    get_cache_stats,
)
# Instrumented lookups — imported under private aliases, wrapped below.
from storage import (
    get_cached_final_poster       as _raw_get_final_poster,
    get_cached_final_poster_entry as _raw_get_final_poster_entry,
    set_cached_final_poster       as _raw_set_final_poster,
    is_cached_final_poster_fresh as _raw_is_final_fresh,
    get_cached_rating            as _raw_get_rating,
    get_cached_quality           as _raw_get_quality,
    get_cached_tmdb_metadata     as _raw_get_tmdb_metadata,
    get_cached_tmdb_poster       as _raw_get_tmdb_poster,
    get_cached_tmdb_logo         as _raw_get_tmdb_logo,
    get_cached_release_status    as _raw_get_release_status,
    get_cached_text_detection    as _raw_get_text_detection,
)
import metrics as _metrics


def _record(table: str, hit: bool) -> None:
    _metrics.cache_lookups_total.labels(
        table=table, result="hit" if hit else "miss",
    ).inc()


# --- Final composite poster (async — bytes live in the blobstore) ---------

async def get_cached_final_poster(cache_key):
    r = await _raw_get_final_poster(cache_key)
    _record("final_poster", r is not None)
    return r


async def get_cached_final_poster_entry(cache_key):
    """(jpeg_bytes, expires_at) or None. Upstream reads composites through this
    so the response can carry the composite's own Cache-Control deadline."""
    r = await _raw_get_final_poster_entry(cache_key)
    _record("final_poster", r is not None)
    return r


async def is_cached_final_poster_fresh(cache_key) -> int | None:
    """Lightweight freshness probe — metadata row + TTL only, no blob fetch.
    Lets /poster and /p 302 straight to the CDN when a public URL exists.
    Returns the composite's expires_at when fresh, else None (truthy/falsy, so
    boolean call sites are unaffected)."""
    expires_at = await _raw_is_final_fresh(cache_key)
    _record("final_poster", expires_at is not None)
    return expires_at


async def set_cached_final_poster(
    cache_key, jpeg_bytes, request_params=None, ttl_override=None
):
    """Async pass-through to the storage backend's blobstore-aware writer.
    Returns the unix time this composite expires."""
    return await _raw_set_final_poster(
        cache_key, jpeg_bytes, request_params, ttl_override
    )


# --- Sync lookups ----------------------------------------------------------

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


def get_cached_release_status(cache_key):
    r = _raw_get_release_status(cache_key)
    _record("release_status", r is not None)
    return r


def get_cached_text_detection(cache_key):
    # None means "not cached"; True/False are both cache hits.
    r = _raw_get_text_detection(cache_key)
    _record("text_detection", r is not None)
    return r


__all__ = [
    "BACKEND_KIND",
    "init_db",
    "prune_caches",
    "prune_local_caches",
    "ping",
    "close",
    "get_cache_stats",
    "get_cached_final_poster",
    "get_cached_final_poster_entry",
    "get_cached_final_poster_url",
    "is_cached_final_poster_fresh",
    "get_cached_final_poster_redirect",
    "set_cached_final_poster",
    "delete_cached_final_poster",
    "invalidate_final_posters",
    "composite_l1_stats",
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
    "get_cached_release_status",
    "set_cached_release_status",
    "get_cached_text_detection",
    "set_cached_text_detection",
    "is_digital_release",
    "count_digital_releases",
    "add_digital_releases",
    "get_cached_imdb_to_tmdb",
    "set_cached_imdb_to_tmdb",
    "release_status_ttl_seconds",
    "release_status_expiry",
    "get_cached_movie_release_info",
    "set_cached_movie_release_info",
    "get_cached_tvdb_json",
    "set_cached_tvdb_json",
    "get_app_state",
    "set_app_state",
    "claim_app_state_slot",
    "list_composite_request_params",
]


# ---------------------------------------------------------------------------
# Backend attribute proxy
# ---------------------------------------------------------------------------
#
# Upstream owns every cache internal in this module, and upstream's tests reach
# straight for them: `cache._safe_cache_path`, `cache._quality_cache_context`,
# and `cache._initialised` / `cache._local.conn` to point the connection at an
# in-memory database. Splitting the implementation into storage/ moved all of
# that out from under them.
#
# The previous port answered that by editing each test to import the backend
# instead. That does not scale — v1.2.0 added four more test classes that poke
# at internals, and every upstream release would hand us more test files to
# re-patch and then re-merge.
#
# So instead the facade forwards: anything this module does not define itself
# is read from, and written to, the active backend. Upstream's tests run
# unmodified against either backend, and the only fork-owned test file is the
# one testing a fork-only feature.
#
# Reads that hit a name defined above (the wrapped lookups, the re-exports)
# resolve normally and never reach __getattr__; writes to those names stay
# local, which is what a caller monkeypatching the facade would mean.
import sys as _sys
import types as _types

import storage as _storage


class _BackendFacade(_types.ModuleType):
    def __getattr__(self, name):
        # Only called when normal module lookup has already failed.
        try:
            return getattr(_storage._backend, name)
        except AttributeError:
            raise AttributeError(
                f"module 'cache' has no attribute {name!r} "
                f"(nor does the active backend "
                f"{_storage._backend.__name__!r})"
            ) from None

    def __setattr__(self, name, value):
        if name in self.__dict__ or name.startswith("__"):
            object.__setattr__(self, name, value)
        else:
            setattr(_storage._backend, name, value)

    def __delattr__(self, name):
        if name in self.__dict__:
            object.__delattr__(self, name)
        else:
            delattr(_storage._backend, name)


_sys.modules[__name__].__class__ = _BackendFacade
