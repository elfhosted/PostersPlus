"""Prometheus metrics for the ElfHosted fork.

Always defined. The /metrics endpoint is wired up in main.py and is
unauthenticated by default — operators should bind it to a non-public port
or gate it at the ingress (or set ``METRICS_ACCESS_KEY`` for query-param
auth — see main.py).

Importing prometheus_client is cheap even when /metrics isn't being scraped,
so we always carry the cost. Counters in hot paths use the no-label form
where labels would add per-request dict allocations.
"""
import os

from prometheus_client import Counter, Histogram, Gauge


# When PROMETHEUS_MULTIPROC_DIR is set (the default in this fork — see
# entrypoint.sh), prometheus_client puts shared metric storage under that
# directory so all uvicorn workers contribute to the same counters and
# histograms. Gauges need an explicit aggregation mode in multiproc mode.
_MP_MODE = bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR", "").strip())


# Cache layer
cache_lookups_total = Counter(
    "postersplus_cache_lookups_total",
    "Cache lookups by table and result.",
    ("table", "result"),   # result: hit | miss | stale
)

# Upstream API calls (TMDB / MDBList / AIOStreams / Arctic Shift).
upstream_calls_total = Counter(
    "postersplus_upstream_calls_total",
    "Outbound HTTP calls to external services by status class.",
    ("service", "status"),  # status: 2xx | 3xx | 4xx | 5xx | timeout | error
)

# Render hot path
render_duration_seconds = Histogram(
    "postersplus_render_duration_seconds",
    "Time spent compositing + JPEG-encoding a poster.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
render_inflight = Gauge(
    "postersplus_render_inflight",
    "Renders currently executing (inside the concurrency semaphore).",
    multiprocess_mode="livesum" if _MP_MODE else "all",
)
render_saturated_total = Counter(
    "postersplus_render_saturated_total",
    "Render requests rejected with 503 due to queue saturation.",
)

# Backend identity — set once at startup so dashboards can group by mode.
backend_info = Gauge(
    "postersplus_backend_info",
    "Active backends. Constant 1; the labels carry the info.",
    ("storage", "coordinator", "blobstore"),
    multiprocess_mode="max" if _MP_MODE else "all",
)


def status_class(status: int | None) -> str:
    if status is None:
        return "error"
    if 200 <= status < 300: return "2xx"
    if 300 <= status < 400: return "3xx"
    if 400 <= status < 500: return "4xx"
    return "5xx"
