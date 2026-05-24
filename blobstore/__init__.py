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
