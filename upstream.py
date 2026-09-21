"""Resilient upstream HTTP calls — retries + circuit breaker + metrics.

Phase 7 of the ElfHosted fork. Wraps the existing httpx client with three
concerns:

  * Exponential backoff with jitter on transient failures (5xx, timeouts,
    connection errors) — limited to a small number of attempts so cascading
    failures don't keep the request thread tied up.
  * In-process per-service circuit breaker. After N consecutive failures
    within a window the breaker opens for a cool-off period; calls during
    that period fail fast without touching the network.
  * postersplus_upstream_calls_total{service, status} counter.

In-process scope: each replica has its own breaker state. Cross-replica
breaker state (e.g. via Redis) is overkill — a single replica recovering
quickly is fine for our use case.

Serve-stale (return cached rows past TTL when the breaker is open) is left
to a future iteration; would require an `allow_stale` flag on the storage
backend lookups and is invasive.
"""
import asyncio
import logging
import random
import time
from dataclasses import dataclass

import httpx

import metrics as _metrics

logger = logging.getLogger(__name__)


# Service identifiers. Use these so a typo doesn't silently fork the metric
# namespace.
SVC_TMDB        = "tmdb"
SVC_MDBLIST     = "mdblist"
SVC_AIOSTREAMS  = "aiostreams"


# Retry policy. Keep small so a saturated TMDB doesn't tie up a worker for
# 30 seconds.
_MAX_ATTEMPTS = 3
_BASE_BACKOFF = 0.5    # seconds
_MAX_BACKOFF  = 4.0    # seconds


# Circuit breaker — simple state per service.
@dataclass
class _Breaker:
    fail_threshold: int = 5      # consecutive failures to trip
    cool_off_seconds: float = 30.0
    consecutive_failures: int = 0
    opened_at: float = 0.0

    def is_open(self) -> bool:
        if self.opened_at == 0.0:
            return False
        if time.monotonic() - self.opened_at > self.cool_off_seconds:
            # Half-open: allow one attempt by clearing the trip flag. The
            # next failure will re-trip; the next success will fully reset.
            return False
        return True

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = 0.0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.fail_threshold:
            # Always refresh opened_at on failures past threshold — without
            # this, a failed half-open probe (after cool_off expires) would
            # leave a stale opened_at and is_open() would let every call
            # through for the rest of the outage.
            was_open = self.opened_at != 0.0
            self.opened_at = time.monotonic()
            if not was_open:
                logger.warning(
                    "Circuit breaker tripped after %d consecutive failures",
                    self.consecutive_failures,
                )


_breakers: dict[str, _Breaker] = {}


def _breaker(service: str) -> _Breaker:
    b = _breakers.get(service)
    if b is None:
        b = _Breaker()
        _breakers[service] = b
    return b


def _classify(resp: httpx.Response | None, exc: Exception | None) -> str:
    """Map a response or exception to a status label for the metric."""
    if exc is not None:
        if isinstance(exc, (httpx.TimeoutException,)):
            return "timeout"
        return "error"
    return _metrics.status_class(resp.status_code if resp is not None else None)


class CircuitOpenError(Exception):
    """Raised when the breaker is open and a call is fast-failed."""


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    service: str,
    raise_for_status: bool = True,
    **kwargs,
) -> httpx.Response:
    """Make an upstream HTTP request with retries + circuit breaker.

    Same return shape as ``client.request``. Raises ``CircuitOpenError`` when
    the breaker is open. Otherwise, on exhausted retries, propagates the
    last httpx exception.
    """
    breaker = _breaker(service)
    if breaker.is_open():
        _metrics.upstream_calls_total.labels(service=service, status="circuit_open").inc()
        raise CircuitOpenError(f"{service} circuit open")

    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            last_exc = exc
            _metrics.upstream_calls_total.labels(service=service, status="timeout").inc()
        except httpx.HTTPError as exc:
            last_exc = exc
            _metrics.upstream_calls_total.labels(service=service, status="error").inc()
        else:
            _metrics.upstream_calls_total.labels(
                service=service, status=_metrics.status_class(resp.status_code),
            ).inc()
            if resp.status_code >= 500:
                # Retryable — fall through to backoff (or to failure if last attempt).
                last_exc = httpx.HTTPStatusError(
                    f"{service} returned {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            else:
                # 1xx/2xx/3xx/4xx — terminal. 4xx isn't a circuit-breaker
                # failure (the network is fine; the request was bad), so we
                # record success here.
                if raise_for_status:
                    resp.raise_for_status()
                breaker.record_success()
                return resp
        if attempt < _MAX_ATTEMPTS:
            delay = min(_MAX_BACKOFF, _BASE_BACKOFF * (2 ** (attempt - 1)))
            # ±25% jitter so synchronized retries don't dogpile a recovering upstream.
            delay = delay * (0.75 + 0.5 * random.random())
            await asyncio.sleep(delay)

    # Exhausted retries — all attempts failed or returned 5xx.
    breaker.record_failure()
    assert last_exc is not None
    raise last_exc
