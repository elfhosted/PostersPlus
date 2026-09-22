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
# An orphaned blob is a storage cost, never a correctness bug: the read path
# only ever reaches a blob via a live metadata row, and a row whose blob has
# gone is dropped on the read that finds it. So when the queue is full we log
# and discard rather than block a cache write on object-store latency.
import asyncio as _asyncio
import threading as _threading
import itertools as _itertools
from contextlib import asynccontextmanager as _asynccontextmanager

DEFERRED_DELETE_MAX = 50_000

# key -> the write generation current when the delete was queued.
_deferred_deletes: "dict[tuple[str, str], int]" = {}
# key -> the generation of its most recent successful put().
_blob_generation: "dict[tuple[str, str], int]" = {}
_generation_counter = _itertools.count(1)
_deferred_lock = _threading.Lock()
_deferred_dropped = 0


# Per-key mutexes shared by the write path and the drain.
#
# The generation check alone is not enough: the drain decides under the lock,
# then releases it to await the delete, and a write landing in that window is
# a live blob deleted after the check said it was safe. Serialising the two
# operations per key is what makes the check decisive.
_key_locks: "dict[tuple[str, str], _asyncio.Lock]" = {}
_key_lock_waiters: "dict[tuple[str, str], int]" = {}


@_asynccontextmanager
async def key_lock(bucket: str, key: str):
    """Serialise writes and deferred deletes of one blob key."""
    k = (bucket, key)
    with _deferred_lock:
        lock = _key_locks.get(k)
        if lock is None:
            lock = _key_locks[k] = _asyncio.Lock()
        _key_lock_waiters[k] = _key_lock_waiters.get(k, 0) + 1
    try:
        async with lock:
            yield
    finally:
        # Drop the lock object once nobody is queued on it, so this registry
        # does not grow by one entry per composite ever written.
        with _deferred_lock:
            remaining = _key_lock_waiters.get(k, 1) - 1
            if remaining <= 0:
                _key_lock_waiters.pop(k, None)
                _key_locks.pop(k, None)
            else:
                _key_lock_waiters[k] = remaining


def note_write(bucket: str, key: str) -> None:
    """Record that *key* has just been (re)written.

    This is what stops the queue deleting a live blob. Invalidation and
    regeneration are not hypothetical here — _run_trending_fetch_cycle does
    exactly that, in that order: it invalidates a composite (queueing the blob
    for deletion) and immediately replays the request, which writes a fresh
    blob under the SAME key. A later drain would then delete the replacement
    and leave a metadata row pointing at nothing.

    The inline read path recovers from that by dropping the row, but the CDN
    redirect path does not: it 302s on the strength of the row alone, so
    clients would be sent to a missing object until the row expired.
    """
    with _deferred_lock:
        _blob_generation[(bucket, key)] = next(_generation_counter)
        # A write supersedes any pending delete for the same key outright.
        _deferred_deletes.pop((bucket, key), None)


def delete_later(bucket: str, key: str) -> None:
    """Queue a blob for deletion by the next drain. Safe from any thread and
    from code that is not on the event loop."""
    global _deferred_dropped
    with _deferred_lock:
        if (
            len(_deferred_deletes) >= DEFERRED_DELETE_MAX
            and (bucket, key) not in _deferred_deletes
        ):
            _deferred_dropped += 1
            if _deferred_dropped % 1000 == 1:
                logger.warning(
                    "Deferred blob-delete queue full (%d); dropped %d keys so far. "
                    "Orphaned objects will remain until an object-store lifecycle "
                    "rule reaps them.",
                    DEFERRED_DELETE_MAX, _deferred_dropped,
                )
            return
        _deferred_deletes[(bucket, key)] = next(_generation_counter)


def deferred_delete_stats() -> dict:
    with _deferred_lock:
        return {"queued": len(_deferred_deletes), "dropped": _deferred_dropped}


async def drain_deferred_deletes(limit: int | None = None, is_live=None) -> int:
    """Delete queued blobs. Returns how many were removed. Best effort: a key
    whose delete raises is dropped rather than retried forever, because the
    row that named it is already gone.

    *is_live* is the authoritative guard: a callable taking (bucket, key) and
    returning True when a live metadata row still names that blob. The
    generation map and key lock below only see THIS process, so on their own
    they cannot stop worker A's queued delete from removing the composite
    worker B has just regenerated under the same key — a fresh row pointing at
    nothing, which the inline read recovers from but a CDN redirect does not.
    The metadata row is shared by every worker and replica, so asking it is
    what actually settles the question.
    """
    removed = 0
    while limit is None or removed < limit:
        with _deferred_lock:
            if not _deferred_deletes:
                break
            (bucket, key), queued_at = _deferred_deletes.popitem()

        # Hold the key's lock across the generation check AND the delete, so a
        # write cannot slip between the two.
        async with key_lock(bucket, key):
            with _deferred_lock:
                superseded = _blob_generation.get((bucket, key), 0) > queued_at
                if not superseded:
                    _blob_generation.pop((bucket, key), None)
            if superseded:
                continue
            if is_live is not None:
                try:
                    if await _asyncio.to_thread(is_live, bucket, key):
                        # Someone regenerated this composite. Its row names the
                        # blob, so the blob stays.
                        continue
                except Exception as exc:
                    # Cannot confirm the blob is unreferenced, so leave it. An
                    # orphan costs storage; deleting a live one costs a broken
                    # redirect.
                    logger.debug(
                        "Deferred blob delete skipped for %s:%s — liveness "
                        "check failed: %s", bucket, key, exc,
                    )
                    continue
            try:
                await delete(bucket, key)
            except Exception as exc:
                logger.debug(
                    "Deferred blob delete failed for %s:%s — %s", bucket, key, exc
                )
        removed += 1
    if removed:
        logger.info("Drained %d deferred blob deletes", removed)
    return removed


def _forget_generations_if_idle() -> None:
    """Drop write-generation bookkeeping once nothing is queued.

    _blob_generation would otherwise grow with every composite ever written.
    It only has to outlive pending deletes, so an empty queue means none of it
    is still needed.
    """
    with _deferred_lock:
        if not _deferred_deletes:
            _blob_generation.clear()
