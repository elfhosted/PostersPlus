"""S3-compatible blob store — opt-in via OBJECT_STORE_URL.

URL format::

    s3://<bucket>?endpoint=<https://...>&region=<region>&prefix=<prefix>
    s3://postersplus-composites?endpoint=https://s3.us-west-002.backblazeb2.com&region=us-west-002

Credentials come from the standard AWS env vars (AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY) or the IAM role the pod is running under.

Holds the composite-poster bytes. With ``OBJECT_STORE_PUBLIC_URL`` set
(e.g. ``https://posters.postersplus.elfhosted.com`` — a Cloudflare custom
domain in front of the same B2 bucket), the /poster endpoint can return
a 302 to that URL on a cache hit and the app pod isn't on the read path
at all. With Cloudflare's Bandwidth Alliance, B2→Cloudflare egress is
free; Cloudflare→user is standard CF bandwidth.

I/O uses boto3 (sync) wrapped in ``asyncio.to_thread`` so the event loop
stays responsive on cache misses.
"""
import asyncio
import logging
import time
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from config import (
    OBJECT_STORE_URL,
    OBJECT_STORE_PUBLIC_URL,
)


_client = None        # boto3 S3 client
_bucket_name: str = ""
_key_prefix: str = ""
_public_url: str = ""


def _parse_url(url: str) -> tuple[str, dict[str, str]]:
    parsed = urlparse(url)
    if parsed.scheme not in ("s3", "s3+http", "s3+https"):
        raise ValueError(f"Unsupported scheme for object store: {parsed.scheme!r}")
    if not parsed.netloc:
        raise ValueError(f"OBJECT_STORE_URL must include a bucket name: {url!r}")
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
    return parsed.netloc, qs


async def init() -> None:
    global _client, _bucket_name, _key_prefix, _public_url

    _bucket_name, params = _parse_url(OBJECT_STORE_URL)
    endpoint = params.get("endpoint")
    region = params.get("region") or "us-east-1"
    _key_prefix = params.get("prefix", "").strip("/")
    _public_url = OBJECT_STORE_PUBLIC_URL.strip("/")

    def _build_client():
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            config=BotoConfig(
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=15,
                signature_version="s3v4",
            ),
        )

    _client = await asyncio.to_thread(_build_client)

    # Validate the bucket is reachable. Fail fast at boot rather than silently
    # turning every poster fetch into an S3 error.
    def _probe():
        _client.head_bucket(Bucket=_bucket_name)
    try:
        await asyncio.to_thread(_probe)
    except ClientError as exc:
        raise RuntimeError(
            f"S3 blob store unreachable (bucket={_bucket_name}, endpoint={endpoint}): {exc}"
        ) from exc

    logger.info(
        "S3 blob store initialised (bucket=%s endpoint=%s prefix=%s public_url=%s)",
        _bucket_name, endpoint or "AWS", _key_prefix or "<none>", _public_url or "<none>",
    )


async def close() -> None:
    global _client
    _client = None


def ping() -> bool:
    return _client is not None


def _object_key(bucket: str, key: str) -> str:
    parts = []
    if _key_prefix:
        parts.append(_key_prefix)
    parts.append(bucket)
    parts.append(key)
    return "/".join(parts)


def _get_sync(bucket: str, key: str, max_age_seconds: int) -> bytes | None:
    obj_key = _object_key(bucket, key)
    try:
        resp = _client.get_object(Bucket=_bucket_name, Key=obj_key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            return None
        logger.warning(f"S3 GET error for {obj_key}: {exc}")
        return None

    body = resp["Body"]
    try:
        last_modified = resp.get("LastModified")
        if last_modified is not None:
            age = time.time() - last_modified.timestamp()
            if age > max_age_seconds:
                # Stale — delete (best-effort) and miss. Close the body
                # first so the underlying HTTP connection returns to the
                # pool rather than leaking until GC.
                try:
                    _client.delete_object(Bucket=_bucket_name, Key=obj_key)
                except ClientError:
                    pass
                return None
        return body.read()
    finally:
        body.close()


def _put_sync(bucket: str, key: str, data: bytes, content_type: str | None) -> None:
    obj_key = _object_key(bucket, key)
    kwargs = {"Bucket": _bucket_name, "Key": obj_key, "Body": data}
    if content_type:
        kwargs["ContentType"] = content_type
    try:
        _client.put_object(**kwargs)
    except ClientError as exc:
        # Propagate so set_cached_final_poster can skip writing the
        # metadata row. Swallowing here left an orphaned row pointing at
        # an object that was never written — subsequent cache hits 302'd
        # clients at a missing CDN URL until COMPOSITE_CACHE_TTL elapsed.
        logger.warning(f"S3 PUT error for {obj_key}: {exc}")
        raise


def _delete_sync(bucket: str, key: str) -> None:
    obj_key = _object_key(bucket, key)
    try:
        _client.delete_object(Bucket=_bucket_name, Key=obj_key)
    except ClientError as exc:
        logger.warning(f"S3 DELETE error for {obj_key}: {exc}")


async def get(bucket: str, key: str, max_age_seconds: int) -> bytes | None:
    if _client is None:
        return None
    return await asyncio.to_thread(_get_sync, bucket, key, max_age_seconds)


async def put(bucket: str, key: str, data: bytes, content_type: str | None = None) -> None:
    if _client is None:
        return
    await asyncio.to_thread(_put_sync, bucket, key, data, content_type)


async def delete(bucket: str, key: str) -> None:
    if _client is None:
        return
    await asyncio.to_thread(_delete_sync, bucket, key)


def url_for(bucket: str, key: str) -> str | None:
    """Return a public CDN URL when OBJECT_STORE_PUBLIC_URL is configured.

    Used by the /poster endpoint to optionally serve composite bytes by
    302 redirect rather than proxying them through the app. Returns None
    if no public URL is configured — caller falls back to fetching the
    bytes inline.
    """
    if not _public_url:
        return None
    return f"{_public_url}/{_object_key(bucket, key)}"
