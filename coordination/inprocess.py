"""In-process coordination backend (upstream default).

Holds the per-worker dicts that upstream uses for MDBList rate-limit backoff and
background-quality-fetch deduplication. Behaviour is identical to upstream;
this module just gives the state a stable address so a Redis backend can
substitute for it in hosted mode.

Render-coalescing and rating-fetch-coalescing primitives (asyncio.Future,
asyncio.Event) remain in main.py — they are inherently per-process and
shared-storage equivalents are out of scope (see RESILIENCE.md).
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

# (namespace, key) -> expiry timestamp (monotonic loop time).
_backoff: dict[tuple[str, str], float] = {}

# (namespace, key) -> expiry timestamp (monotonic loop time). Used as a
# set-with-TTL so a stuck claim eventually clears.
_inflight: dict[tuple[str, str], float] = {}


async def init() -> None:
    """No-op for the in-process backend. Present so the public API is uniform."""
    return None


async def close() -> None:
    _backoff.clear()
    _inflight.clear()
    _leases.clear()
    _rl_buckets.clear()


def ping() -> bool:
    return True


async def is_backoff_active(namespace: str, key: str) -> bool:
    """True if a backoff window for this (namespace, key) is currently active."""
    until = _backoff.get((namespace, key))
    if until is None:
        return False
    now = asyncio.get_running_loop().time()
    if until <= now:
        # Lazy expiry — matches upstream behaviour.
        _backoff.pop((namespace, key), None)
        return False
    return True


async def set_backoff(namespace: str, key: str, ttl_seconds: float) -> None:
    """Mark a backoff window of ttl_seconds starting now."""
    until = asyncio.get_running_loop().time() + ttl_seconds
    _backoff[(namespace, key)] = until


async def clear_backoff(namespace: str, key: str) -> None:
    _backoff.pop((namespace, key), None)


async def claim_inflight(namespace: str, key: str, ttl_seconds: float = 300.0) -> bool:
    """Atomically claim a single-flight slot. Returns True if the caller now
    owns the slot, False if another caller holds it. Stuck claims are released
    after ttl_seconds so a crashed worker doesn't deadlock the slot forever."""
    now = asyncio.get_running_loop().time()
    expiry = _inflight.get((namespace, key))
    if expiry is not None and expiry > now:
        return False
    _inflight[(namespace, key)] = now + ttl_seconds
    return True


async def release_inflight(namespace: str, key: str) -> None:
    _inflight.pop((namespace, key), None)


# ---------------------------------------------------------------------------
# Named leases — used by Phase 5 leader election for periodic jobs.
#
# In-process scope only. A multi-worker uvicorn deployment ends up with one
# leader per worker process (no shared memory between workers), which means
# the prune/poll loops still run N times. That matches upstream's existing
# behaviour. True single-leader-across-workers requires the Redis backend.
# ---------------------------------------------------------------------------

_leases: dict[str, tuple[str, float]] = {}   # name -> (token, expiry)


async def try_acquire_lease(name: str, ttl_seconds: float) -> str | None:
    """Acquire a named lease. Returns an opaque token on success, None if
    another holder already has it.
    """
    import os
    import uuid
    now = asyncio.get_running_loop().time()
    existing = _leases.get(name)
    if existing is not None and existing[1] > now:
        return None
    token = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
    _leases[name] = (token, now + ttl_seconds)
    return token


async def refresh_lease(name: str, token: str, ttl_seconds: float) -> bool:
    """Renew the lease iff we still hold the token. Returns True on success."""
    existing = _leases.get(name)
    if existing is None or existing[0] != token:
        return False
    now = asyncio.get_running_loop().time()
    _leases[name] = (token, now + ttl_seconds)
    return True


async def release_lease(name: str, token: str) -> None:
    existing = _leases.get(name)
    if existing is not None and existing[0] == token:
        _leases.pop(name, None)


# ---------------------------------------------------------------------------
# Per-tenant rate limit (fixed window, 1-second buckets).
#
# Returns (allowed, retry_after_seconds). retry_after is wall-clock seconds
# until the current window rolls over, clamped to 1.
# ---------------------------------------------------------------------------

_rl_buckets: dict[str, tuple[int, int]] = {}   # tenant -> (window_start_epoch, count)


async def check_rate_limit(tenant: str, rps: int) -> tuple[bool, int]:
    if rps <= 0:
        return True, 0
    import time as _t
    now = int(_t.time())
    # Opportunistic cleanup of stale buckets so an attacker spamming unique
    # tmdb_key values can't grow the dict unbounded. Triggered when the
    # bucket count exceeds a soft cap; bounded O(n) sweep that drops every
    # bucket whose window has already rolled over.
    if len(_rl_buckets) > 1024:
        stale = [k for k, (w, _c) in _rl_buckets.items() if w != now]
        for k in stale:
            _rl_buckets.pop(k, None)
    window, count = _rl_buckets.get(tenant, (now, 0))
    if now != window:
        # Reset; new window started.
        _rl_buckets[tenant] = (now, 1)
        return True, 0
    if count >= rps:
        return False, 1
    _rl_buckets[tenant] = (window, count + 1)
    return True, 0


async def prune_expired() -> None:
    """Best-effort cleanup of expired keys. Cheap on the in-process backend;
    the cache-prune loop calls this periodically. Redis backend has no-op
    because the server expires keys for us."""
    import time as _t
    now = asyncio.get_running_loop().time()
    wall_now = int(_t.time())
    expired_backoff = [k for k, v in _backoff.items() if v <= now]
    for k in expired_backoff:
        _backoff.pop(k, None)
    expired_inflight = [k for k, v in _inflight.items() if v <= now]
    for k in expired_inflight:
        _inflight.pop(k, None)
    expired_rl = [k for k, (w, _c) in _rl_buckets.items() if w != wall_now]
    for k in expired_rl:
        _rl_buckets.pop(k, None)
    if expired_backoff or expired_inflight or expired_rl:
        logger.debug(
            "Coordinator prune: %d backoff, %d inflight, %d rate-limit",
            len(expired_backoff), len(expired_inflight), len(expired_rl),
        )
