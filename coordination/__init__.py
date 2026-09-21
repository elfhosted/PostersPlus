"""Coordination backend selector.

Active backend is chosen at import time by config.REDIS_URL:

  * unset / empty → coordination.inprocess (upstream default)
  * redis:// or rediss:// → coordination.redis_backend (opt-in)

main.py uses the re-exported async functions; the call shape is identical for
both backends so a private deployment and a multi-replica hosted deployment
share the same code paths.
"""
import logging

from config import REDIS_URL

logger = logging.getLogger(__name__)

_PUBLIC_API = (
    "init",
    "close",
    "ping",
    "is_backoff_active",
    "set_backoff",
    "clear_backoff",
    "claim_inflight",
    "release_inflight",
    "try_acquire_lease",
    "refresh_lease",
    "release_lease",
    "check_rate_limit",
    "prune_expired",
)


def _select_backend():
    """Return the active backend module. The redis backend is lazy-imported so
    SQLite-only / inprocess-only deployments don't need the redis dep loaded."""
    url = (REDIS_URL or "").strip()
    if url:
        if url.startswith(("redis://", "rediss://", "unix://")):
            from coordination import redis_backend
            logger.info("Coordinator backend: redis (REDIS_URL detected)")
            return redis_backend
        raise RuntimeError(
            f"Unsupported REDIS_URL scheme: {url.split('://', 1)[0]!r}. "
            "Set a redis:// / rediss:// / unix:// URL or unset REDIS_URL "
            "to use the in-process coordinator."
        )
    from coordination import inprocess
    logger.info("Coordinator backend: inprocess (default)")
    return inprocess


_backend = _select_backend()

for _name in _PUBLIC_API:
    globals()[_name] = getattr(_backend, _name)

BACKEND_KIND: str = "redis" if _backend.__name__.endswith("redis_backend") else "inprocess"

__all__ = list(_PUBLIC_API) + ["BACKEND_KIND"]


# Namespace constants — single source of truth so a typo doesn't silently
# create a parallel keyspace. Add new entries here.
NS_RATING_BACKOFF: str = "rating-backoff"
NS_QUALITY_BG: str     = "quality-bg-inflight"
# Single-key namespace for the MDBlist-wide cooldown. Set after any 429,
# checked before every MDBlist call so one rate-limited replica throttles
# the whole fleet rather than letting each queued title trip its own 429.
NS_MDBLIST_GLOBAL_COOLDOWN: str = "mdblist-global"
MDBLIST_GLOBAL_KEY: str = "all"

# Lease names for the periodic background tasks. Phase 5 leader-elects each
# of these so multi-replica hosted deployments only run them on one replica
# at a time. With the in-process coordinator backend each worker is still
# its own leader (no cross-process state), which matches upstream behaviour.
LEASE_CACHE_PRUNE: str   = "cache-prune"
LEASE_DIGITAL_RELEASE: str = "digital-release-poll"
