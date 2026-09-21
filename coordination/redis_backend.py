"""Redis coordination backend — opt-in via REDIS_URL.

Cross-replica state for:

  * MDBList rate-limit backoff (so a 429 seen by one replica throttles all of
    them, not just one).
  * Background-quality-fetch single-flighting (so the same imdb_id isn't
    scheduled by two replicas simultaneously).

Render coalescing remains per-process (see inprocess.py).
"""
import logging

logger = logging.getLogger(__name__)

import redis.asyncio as aioredis

from config import REDIS_URL, REDIS_KEY_PREFIX


_client: aioredis.Redis | None = None


def _key(namespace: str, key: str) -> str:
    return f"{REDIS_KEY_PREFIX}:{namespace}:{key}"


async def init() -> None:
    """Open the Redis connection. Called from lifespan startup."""
    global _client
    _client = aioredis.from_url(
        REDIS_URL,
        decode_responses=False,
        socket_timeout=5.0,
        socket_connect_timeout=5.0,
        health_check_interval=30,
    )
    # Validate the connection up front so misconfiguration fails fast at boot
    # rather than silently turning every coord call into an error swallow.
    pong = await _client.ping()
    if not pong:
        raise RuntimeError("Redis PING returned a falsy reply")
    logger.info("Redis coordinator initialised (%s)", _sanitised_url())


def _sanitised_url() -> str:
    """REDIS_URL with credentials stripped for log lines."""
    url = REDIS_URL or ""
    if "@" in url:
        scheme, rest = url.split("://", 1)
        _, host = rest.split("@", 1)
        return f"{scheme}://***@{host}"
    return url


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception as exc:
            logger.warning("Redis close error: %s", exc)
        _client = None


def ping() -> bool:
    """Synchronous probe for /ready (Phase 4). The check itself is async, so
    callers wrap this in run_until_complete or use the async ping below."""
    return _client is not None


async def aping() -> bool:
    if _client is None:
        return False
    try:
        return bool(await _client.ping())
    except Exception:
        return False


async def is_backoff_active(namespace: str, key: str) -> bool:
    if _client is None:
        return False
    try:
        return bool(await _client.exists(_key(namespace, key)))
    except Exception as exc:
        # Fail-open: a coord outage must not break poster serving. Worst case
        # is one wasted MDBList call until the next attempt re-establishes
        # backoff on the live replica.
        logger.warning("Redis is_backoff_active error: %s", exc)
        return False


async def set_backoff(namespace: str, key: str, ttl_seconds: float) -> None:
    if _client is None:
        return
    try:
        # Redis TTL is integer seconds; round up to be safe.
        ttl = max(1, int(round(ttl_seconds)))
        await _client.set(_key(namespace, key), b"1", ex=ttl)
    except Exception as exc:
        logger.warning("Redis set_backoff error: %s", exc)


async def clear_backoff(namespace: str, key: str) -> None:
    if _client is None:
        return
    try:
        await _client.delete(_key(namespace, key))
    except Exception as exc:
        logger.warning("Redis clear_backoff error: %s", exc)


async def claim_inflight(namespace: str, key: str, ttl_seconds: float = 300.0) -> bool:
    """SET NX EX — atomic claim with auto-expiry. Returns True on success."""
    if _client is None:
        # No coordinator → allow the local claim. Caller will fall back to its
        # own per-process flag if it wants stricter behaviour.
        return True
    try:
        ttl = max(1, int(round(ttl_seconds)))
        # `nx=True` means only set if not exists. Returns truthy on claim.
        return bool(await _client.set(_key(namespace, key), b"1", nx=True, ex=ttl))
    except Exception as exc:
        # Fail-open on coord error: better to risk a duplicate background
        # fetch than to deadlock all backgound fetching.
        logger.warning("Redis claim_inflight error: %s", exc)
        return True


async def release_inflight(namespace: str, key: str) -> None:
    if _client is None:
        return
    try:
        await _client.delete(_key(namespace, key))
    except Exception as exc:
        logger.warning("Redis release_inflight error: %s", exc)


# ---------------------------------------------------------------------------
# Named leases — Phase 5 leader election. Survives single-replica restart
# via TTL; only the current token-holder can refresh or release the lease
# (compare-and-set via Lua) so a slow replica can't blast through a faster
# one's lock.
# ---------------------------------------------------------------------------

_LEASE_NS = "lease"

_LUA_REFRESH = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
else
    return 0
end
"""

_LUA_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


async def try_acquire_lease(name: str, ttl_seconds: float) -> str | None:
    if _client is None:
        return None
    import os
    import uuid
    token = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
    ttl_ms = max(1, int(round(ttl_seconds * 1000)))
    try:
        ok = await _client.set(
            _key(_LEASE_NS, name),
            token.encode("utf-8"),
            nx=True,
            px=ttl_ms,
        )
        return token if ok else None
    except Exception as exc:
        logger.warning("Redis try_acquire_lease error: %s", exc)
        return None


async def refresh_lease(name: str, token: str, ttl_seconds: float) -> bool:
    if _client is None:
        return False
    ttl_ms = max(1, int(round(ttl_seconds * 1000)))
    try:
        result = await _client.eval(
            _LUA_REFRESH, 1, _key(_LEASE_NS, name), token.encode("utf-8"), ttl_ms,
        )
        return bool(result)
    except Exception as exc:
        logger.warning("Redis refresh_lease error: %s", exc)
        return False


async def release_lease(name: str, token: str) -> None:
    if _client is None:
        return
    try:
        await _client.eval(
            _LUA_RELEASE, 1, _key(_LEASE_NS, name), token.encode("utf-8"),
        )
    except Exception as exc:
        logger.warning("Redis release_lease error: %s", exc)


# ---------------------------------------------------------------------------
# Per-tenant rate limit (fixed 1-second window, shared across replicas).
# Returns (allowed, retry_after_seconds).
# ---------------------------------------------------------------------------

async def check_rate_limit(tenant: str, rps: int) -> tuple[bool, int]:
    if _client is None or rps <= 0:
        return True, 0
    import time as _t
    window = int(_t.time())
    key = f"{REDIS_KEY_PREFIX}:rate:{tenant}:{window}"
    try:
        # INCR is atomic; EX seeds the TTL on the first hit of the window.
        # Pipeline so we only pay one RTT.
        async with _client.pipeline(transaction=False) as pipe:
            pipe.incr(key)
            pipe.expire(key, 2)   # 2s so the key survives until the window rolls
            count, _ = await pipe.execute()
        if count > rps:
            return False, 1
        return True, 0
    except Exception as exc:
        # Fail-open: a Redis hiccup must not 429 every request.
        logger.warning("Redis check_rate_limit error: %s", exc)
        return True, 0


async def prune_expired() -> None:
    """No-op — Redis expires keys server-side."""
    return None
