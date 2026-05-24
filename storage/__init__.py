"""Storage backend selector.

The active backend is chosen at import time based on the DATABASE_URL env var
(via config.DATABASE_URL):

  * Unset / empty  → storage.sqlite_backend (upstream default)
  * postgres://… or postgresql://…  → storage.postgres_backend

The cache.py module re-exports the public API from here so all callsites
(``from cache import …``) work unchanged regardless of backend.

Adding a third backend in the future means: implement a module that mirrors
the same public surface, dispatch to it here based on the URL scheme.
"""
import logging

from config import DATABASE_URL

logger = logging.getLogger(__name__)

_PUBLIC_API = (
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
)


def _select_backend():
    """Return the active backend module. Import is lazy so a SQLite-only deploy
    does not need psycopg installed (the postgres_backend module is only
    imported when DATABASE_URL is set)."""
    url = (DATABASE_URL or "").strip()
    if url:
        if url.startswith(("postgresql://", "postgres://")):
            # psycopg only accepts postgresql://. Normalise the Heroku-style
            # postgres:// alias before handing the URL to the pool.
            if url.startswith("postgres://"):
                normalised = "postgresql://" + url[len("postgres://"):]
                import config as _cfg
                _cfg.DATABASE_URL = normalised
            from storage import postgres_backend
            logger.info("Storage backend: postgresql (DATABASE_URL detected)")
            return postgres_backend
        raise RuntimeError(
            f"Unsupported DATABASE_URL scheme: {url.split('://', 1)[0]!r}. "
            "Set a postgresql:// URL or unset DATABASE_URL to use SQLite."
        )
    from storage import sqlite_backend
    logger.info("Storage backend: sqlite (default)")
    return sqlite_backend


_backend = _select_backend()

# Re-export the public API verbatim.
for _name in _PUBLIC_API:
    globals()[_name] = getattr(_backend, _name)

# Identifier callers (and /ready probes) can use to know which backend is active.
BACKEND_KIND: str = "postgresql" if _backend.__name__.endswith("postgres_backend") else "sqlite"

__all__ = list(_PUBLIC_API) + ["BACKEND_KIND"]
