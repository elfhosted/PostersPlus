"""Blob store backend selector.

Active backend chosen at import time by config.OBJECT_STORE_URL:

  * unset / empty  → blobstore.local (filesystem, upstream default)
  * s3:// URL      → blobstore.s3 (S3-compatible, opt-in)

Holds **final composite posters** — the fully-rendered, watermarked /poster
output. Each unique render-param combination is a separate entry. With S3
+ a CDN public URL (OBJECT_STORE_PUBLIC_URL), /poster redirects clients
straight to the CDN on a cache hit and the app pod isn't on the read path
at all.

Not held here:

  * TMDB poster/logo bytes — those land on the pod's local filesystem
    under TMDB_POSTER_CACHE_DIR / TMDB_LOGO_CACHE_DIR. They're a per-pod
    latency-optimisation cache in front of TMDB's own CDN; sharing them
    across replicas via S3 buys very little (TMDB's CDN is already fast)
    while complicating the data path. Pod restarts re-warm them in the
    first few minutes of traffic.
  * Rating / quality / metadata / digital-release / trending — small
    JSON-ish data, stays in the relational backend.
"""
import logging

from config import OBJECT_STORE_URL

logger = logging.getLogger(__name__)


_PUBLIC_API = (
    "init",
    "close",
    "ping",
    "get",
    "put",
    "delete",
    "url_for",
)


def _select_backend():
    url = (OBJECT_STORE_URL or "").strip()
    if url:
        if url.startswith(("s3://", "s3+http://", "s3+https://")):
            from blobstore import s3
            logger.info("Blob store backend: s3 (OBJECT_STORE_URL detected)")
            return s3
        raise RuntimeError(
            f"Unsupported OBJECT_STORE_URL scheme: {url.split('://', 1)[0]!r}. "
            "Set an s3:// URL or unset OBJECT_STORE_URL to use the local filesystem."
        )
    from blobstore import local
    logger.info("Blob store backend: local (default)")
    return local


_backend = _select_backend()

for _name in _PUBLIC_API:
    globals()[_name] = getattr(_backend, _name)

BACKEND_KIND: str = "s3" if _backend.__name__.endswith(".s3") else "local"

__all__ = list(_PUBLIC_API) + ["BACKEND_KIND"]


# Bucket constants — single source of truth.
#
# Today only the composite-poster bytes live here. Earlier Phase 3 also
# proxied TMDB poster/logo through this layer; Phase 10 reverted those
# to direct filesystem access (per-pod ephemeral cache).
BUCKET_COMPOSITES: str = "composites"


# ---------------------------------------------------------------------------
# Deferred deletes
# ---------------------------------------------------------------------------
#
# Upstream's cache API deletes composites from synchronous code — a metadata
# refresh that finds a stale row calls invalidate_final_posters() from a plain
# def, and a trending snapshot diff can invalidate hundreds of keys in one go.
# Upstream can do that cheaply because the bytes are a column in the row it is
# already deleting; here they are an object-store round trip each.
#
# Rather than make those call sites async (which would fork the signatures away
# from upstream and make every future merge harder), sync callers drop the key
# here and the periodic prune drains the queue on the event loop.
#
# What makes a deferred delete safe is that composite blob keys are VERSIONED
# (storage backends write each render under a fresh key and record it in the
# row). A key only lands here after the row stopped naming it, and no row can
# ever name it again, so a delete can never hit a live blob — regardless of
# which worker or replica queued it, and with no locking. An earlier design
# guarded reused keys with per-process locks and a row re-check instead; that
# cannot be made atomic across pods.
#
# An orphaned blob is a storage cost, never a correctness bug, so when the
# queue is full we log and discard rather than block a cache write on
# object-store latency.
import threading as _threading

DEFERRED_DELETE_MAX = 50_000

_deferred_deletes: "dict[tuple[str, str], None]" = {}
_deferred_lock = _threading.Lock()
_deferred_dropped = 0


def delete_later(bucket: str, key: str) -> None:
    """Queue a blob for deletion by the next drain. Safe from any thread and
    from code that is not on the event loop."""
    global _deferred_dropped
    with _deferred_lock:
        if (bucket, key) in _deferred_deletes:
            return
        if len(_deferred_deletes) >= DEFERRED_DELETE_MAX:
            _deferred_dropped += 1
            if _deferred_dropped % 1000 == 1:
                logger.warning(
                    "Deferred blob-delete queue full (%d); dropped %d keys so far. "
                    "Orphaned objects will remain until an object-store lifecycle "
                    "rule reaps them.",
                    DEFERRED_DELETE_MAX, _deferred_dropped,
                )
            return
        _deferred_deletes[(bucket, key)] = None


def deferred_delete_stats() -> dict:
    with _deferred_lock:
        return {"queued": len(_deferred_deletes), "dropped": _deferred_dropped}


async def drain_deferred_deletes(limit: int | None = None) -> int:
    """Delete queued blobs. Returns how many were removed. Best effort: a key
    whose delete raises is dropped rather than retried forever, because the
    row that named it is already gone."""
    removed = 0
    while limit is None or removed < limit:
        with _deferred_lock:
            if not _deferred_deletes:
                break
            (bucket, key), _ = _deferred_deletes.popitem()
        try:
            await delete(bucket, key)
        except Exception as exc:
            logger.debug("Deferred blob delete failed for %s:%s — %s", bucket, key, exc)
        removed += 1
    if removed:
        logger.info("Drained %d deferred blob deletes", removed)
    return removed
