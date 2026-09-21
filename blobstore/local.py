"""Local-filesystem blob store (default).

Bytes for the composite-poster cache land under
``/app/cache/composites/<key>.jpg``. Behaviour matches the S3 backend's
signature so the public selector can swap them transparently.

I/O is wrapped in ``asyncio.to_thread`` so the event loop isn't blocked
by disk reads on a slow volume.
"""
import asyncio
import logging
import os
import time

logger = logging.getLogger(__name__)

from config import COMPOSITE_BLOB_DIR


# Bucket → on-disk base directory.
_BUCKETS: dict[str, str] = {
    "composites": COMPOSITE_BLOB_DIR,
}


def _base_for(bucket: str) -> str:
    try:
        return _BUCKETS[bucket]
    except KeyError as exc:
        raise ValueError(f"Unknown blob bucket: {bucket!r}") from exc


def _safe_path(base_dir: str, filename: str) -> str:
    """Defensive: resolve symlinks and ensure the result stays inside base_dir."""
    path = os.path.realpath(os.path.join(base_dir, filename))
    if not path.startswith(os.path.realpath(base_dir)):
        raise ValueError(f"Path traversal attempt: {filename!r}")
    return path


async def init() -> None:
    for base in _BUCKETS.values():
        os.makedirs(base, exist_ok=True)


async def close() -> None:
    return None


def ping() -> bool:
    return all(os.path.isdir(base) for base in _BUCKETS.values())


def _get_sync(bucket: str, key: str, max_age_seconds: int) -> bytes | None:
    base = _base_for(bucket)
    path = _safe_path(base, key)

    if not os.path.exists(path):
        return None

    age_secs = time.time() - os.path.getmtime(path)
    if age_secs > max_age_seconds:
        logger.info(f"Blob cache expired for {bucket}:{key}")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return None

    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception as exc:
        logger.error(f"Blob cache read error for {bucket}:{key}: {exc}")
        return None


def _put_sync(bucket: str, key: str, data: bytes) -> None:
    base = _base_for(bucket)
    path = _safe_path(base, key)
    try:
        os.makedirs(os.path.dirname(path) or base, exist_ok=True)
        # Atomic write: temp file in same dir, then rename.
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except Exception as exc:
        # Propagate so set_cached_final_poster can skip writing the
        # metadata row. Same contract as the S3 backend — a metadata
        # row pointing at a missing blob would cause subsequent cache
        # hits to serve nothing (or 302 to a missing URL) until TTL.
        logger.error(f"Blob cache write error for {bucket}:{key}: {exc}")
        raise


def _delete_sync(bucket: str, key: str) -> None:
    base = _base_for(bucket)
    path = _safe_path(base, key)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"Blob cache delete error for {bucket}:{key}: {exc}")


async def get(bucket: str, key: str, max_age_seconds: int) -> bytes | None:
    return await asyncio.to_thread(_get_sync, bucket, key, max_age_seconds)


async def put(bucket: str, key: str, data: bytes, content_type: str | None = None) -> None:
    # content_type is irrelevant for the FS backend but accepted so the
    # signature matches the S3 backend.
    await asyncio.to_thread(_put_sync, bucket, key, data)


async def delete(bucket: str, key: str) -> None:
    await asyncio.to_thread(_delete_sync, bucket, key)


def url_for(bucket: str, key: str) -> str | None:
    """The local backend never serves CDN URLs."""
    return None
