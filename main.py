#main.py
import asyncio
import hashlib
import hmac
import io
import logging
import os
import re
import httpx
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont, ImageOps

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
# ElfHosted fork: optional structured JSON logs (LOG_FORMAT=json) for shipping
# to Loki/Elasticsearch. Read straight from the env so logging is configured
# before config.py is imported. Falls back to the text format above if
# python-json-logger isn't installed. Default (text) is upstream behaviour.
if os.environ.get("LOG_FORMAT", "text").strip().lower() == "json":
    try:
        from pythonjsonlogger.json import JsonFormatter
    except Exception:
        try:
            from pythonjsonlogger.jsonlogger import JsonFormatter  # older versions
        except Exception:
            JsonFormatter = None
    if JsonFormatter is not None:
        _json_fmt = JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        for _h in logging.getLogger().handlers:
            _h.setFormatter(_json_fmt)
    else:
        logging.getLogger(__name__).warning(
            "LOG_FORMAT=json but python-json-logger is not installed — "
            "falling back to text logs."
        )
# Pull uvicorn's loggers into our root handler so all output shares the same format.
for _uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _uv_logger = logging.getLogger(_uv_name)
    _uv_logger.handlers = []
    _uv_logger.propagate = True


class _TruncateUrlFilter(logging.Filter):
    """
    Redact API keys and truncate long URL paths in log records.

    Two responsibilities:
      1. For uvicorn.access records, truncate the request path so long URLs
         don't fill the log.
      2. For ALL records, redact every common API-key query parameter pattern
         in both record.msg and record.args.  This catches keys that slip
         through when an httpx exception is logged (its __str__ includes the
         full upstream URL with our outbound api_key=) as well as anything
         else that might inadvertently include a key.
    """
    _MAX = 80
    # Match query params we hold (tmdb_key, mdblist_key, access_key) AND the
    # upstream parameter names we forward keys under (api_key, apikey).
    _KEY_RE = re.compile(
        r'((?:tmdb_key|mdblist_key|access_key|api_key|apikey)=)[^&\s\'\"]*',
        re.IGNORECASE,
    )

    @classmethod
    def _redact(cls, value):
        if isinstance(value, str):
            return cls._KEY_RE.sub(r'\1***', value)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access records: args = (client_addr, method, path, http_version, status_code, ...)
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) >= 3
        ):
            path = record.args[2]
            if isinstance(path, str):
                path = self._KEY_RE.sub(r'\1***', path)
                if len(path) > self._MAX:
                    path = path[: self._MAX] + "…"
                record.args = (record.args[0], record.args[1], path) + record.args[3:]

        # Generic redaction for every other record (application logs).
        # We redact in msg and args so the formatted output is safe regardless
        # of whether the record uses % substitution or pre-formatted strings.
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._redact(v) for k, v in record.args.items()}

        # Tracebacks (logger.exception / exc_info=True) are formatted lazily
        # by the handler.  Pre-format and redact exc_text here so the
        # downstream formatter uses our sanitised copy rather than re-rendering.
        if record.exc_info and not record.exc_text:
            import traceback
            record.exc_text = self._redact(
                "".join(traceback.format_exception(*record.exc_info))
            )
        elif record.exc_text:
            record.exc_text = self._redact(record.exc_text)

        return True


# Attach to the root handler, not the root logger — propagation calls
# callHandlers() directly on parent loggers, skipping their logger-level filters.
_url_filter = _TruncateUrlFilter()
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_url_filter)

# httpx logs every outbound HTTP request at INFO level, including full URLs with
# API keys in query strings.  Raise its level to WARNING so those lines are never
# written to the log — our own try/except blocks capture errors explicitly.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request coalescing
# ---------------------------------------------------------------------------
# Maps final_cache_key -> Future[bytes] for in-flight renders.
# When multiple requests arrive simultaneously for the same uncached poster
# (common during a burst from AIOMetadata loading a library), only the first
# runs the full pipeline; the rest await its Future and get the result for free.
# This dict is per-worker-process — cross-process deduplication would require
# a shared store like Redis, but intra-process coalescing handles the common
# burst pattern well enough at this scale.
_render_inflight: dict[str, "asyncio.Future[bytes]"] = {}

# ElfHosted fork: bounds Pillow renders in flight (config.RENDER_CONCURRENCY).
# Created lazily inside the event loop on first /poster (or /p) render.
_render_semaphore: "asyncio.Semaphore | None" = None

# ---------------------------------------------------------------------------
# Background quality fetching
# ---------------------------------------------------------------------------
# Quality data (AIOStreams / scrapers) is fetched in the background so poster
# responses are never blocked by a slow scraper call.  The poster is served
# immediately without quality badges on a cache miss; the next request for the
# same title will find the quality cached and render badges normally.
#
# _quality_bg_inflight: tracks imdb_ids with an active background fetch so
#   scroll bursts don't launch duplicate fetches for the same title.
# _quality_bg_semaphore: caps concurrent AIOStreams calls so a large burst
#   doesn't hammer the scrapers with hundreds of simultaneous requests.

_quality_bg_inflight: set[str] = set()
_quality_bg_semaphore: "asyncio.Semaphore | None" = None   # created inside event loop
_quality_source_backoff_until: dict[str, float] = {}
_quality_source_fail_count: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Rating fetch deduplication
# ---------------------------------------------------------------------------
# Prevents concurrent requests for the same imdb_id (different raw_params /
# final_cache_key) from triggering duplicate MDBlist API calls.  The most
# common burst: AIOMetadata requests many posters simultaneously; several
# share an uncached title with different user-config hashes so render
# coalescing alone doesn't protect them.
#
# _rating_fetch_inflight: maps imdb_id -> asyncio.Event that fires once the
#   first fetch completes.  Subsequent requests wait, then re-read the DB.
# _rating_backoff: maps (imdb_id, API key) -> loop-time after which a new
#   attempt is allowed. Scoping by key lets a rotated or replaced key retry
#   the same title immediately. Network failures use an escalating ladder
#   (30s/2m/8m/1h); rate-limit responses use Retry-After or 1h flat.

_rating_fetch_inflight:         dict[str, asyncio.Event] = {}
_rating_backoff:                dict[tuple[str, str], float] = {}
_rating_fail_count:             dict[tuple[str, str], int]   = {}
_mdblist_semaphore:             "asyncio.Semaphore | None" = None  # caps concurrent MDBlist HTTP calls; created inside event loop
# Caps parallel burned-in-text scans. Each slot owns an independent RapidOCR
# session in a dedicated executor, so cold-cache OCR cannot occupy render workers.
# Created inside the event loop.
_detect_semaphore:              "asyncio.Semaphore | None" = None
_detect_executor:               "ThreadPoolExecutor | None" = None
# Maps immutable image/detector keys to active OCR tasks. Different poster
# configurations often render the same source image during a burst; they should
# share one scan even when their final composite cache keys differ.
_text_detection_inflight:       dict[str, "asyncio.Task[bool | None]"] = {}
_foreground_detection_count = 0
_active_poster_renders = 0
_background_detection_queue: "asyncio.Queue[_DeferredTextDetection] | None" = None
_background_detection_keys: set[str] = set()
_background_detection_task: "asyncio.Task[None] | None" = None


@dataclass(frozen=True)
class _DeferredTextDetection:
    cache_key: str
    image_cache_key: str
    title: tuple[str, ...]
    source: str
    tmdb_id: str
    media_type: str
    image_path: str
    vote_count: int | None
    source_key: str


def _get_detect_semaphore() -> "asyncio.Semaphore":
    """Lazily create the detection-admission semaphore inside the event loop."""
    global _detect_semaphore
    if _detect_semaphore is None:
        _detect_semaphore = asyncio.Semaphore(_cfg.TEXTLESS_DETECTION_CONCURRENCY)
    return _detect_semaphore


def _get_detect_executor() -> ThreadPoolExecutor:
    """Dedicated workers so OCR bursts cannot starve poster compositing."""
    global _detect_executor
    if _detect_executor is None:
        _detect_executor = ThreadPoolExecutor(
            max_workers=_cfg.TEXTLESS_DETECTION_CONCURRENCY,
            thread_name_prefix="text-detect",
        )
    return _detect_executor


def _shutdown_detect_executor() -> None:
    global _detect_executor
    if _detect_executor is not None:
        _detect_executor.shutdown(wait=True, cancel_futures=True)
        _detect_executor = None


def _reserve_foreground_detection() -> None:
    global _foreground_detection_count
    _foreground_detection_count += 1


def _release_foreground_detection() -> None:
    global _foreground_detection_count
    _foreground_detection_count = max(0, _foreground_detection_count - 1)


def _start_text_detection(
    cache_key: str,
    image: Image.Image,
    *,
    title: tuple[str, ...],
    source: str,
    tmdb_id: str,
    vote_count: int | None,
    source_key: str,
    media_type: str | None = None,
    image_path: str | None = None,
    foreground: bool = True,
    foreground_reserved: bool = False,
) -> "asyncio.Task[bool | None]":
    """Start or join one OCR scan for an immutable source image."""
    cached = get_cached_text_detection(cache_key)
    if cached is not None:
        if foreground and foreground_reserved:
            _release_foreground_detection()
        async def _cached_result() -> bool:
            return cached
        return asyncio.create_task(_cached_result())

    existing = _text_detection_inflight.get(cache_key)
    if existing is not None:
        if foreground and foreground_reserved:
            _release_foreground_detection()
        logger.info(
            f"Coalescing burned-in text scan for {tmdb_id} "
            f"(votes={vote_count}, source={source_key})"
        )
        return existing

    if foreground and not foreground_reserved:
        _reserve_foreground_detection()

    async def _scan() -> bool | None:
        from text_detect import poster_has_burned_in_text

        try:
            async with _get_detect_semaphore():
                result = await asyncio.get_running_loop().run_in_executor(
                    _get_detect_executor(),
                    lambda: poster_has_burned_in_text(
                        image,
                        conf=_cfg.PPOCR_BOX_THRESHOLD,
                        title=title,
                        source=source,
                        debug=True,
                    ),
                )
            if result is not None:
                set_cached_text_detection(cache_key, result)
            if result is True and source == "poster" and media_type and image_path:
                from textless_report import report_fake_textless_poster
                report_fake_textless_poster(
                    media_type=media_type,
                    tmdb_id=tmdb_id,
                    image_path=image_path,
                    vote_count=vote_count,
                )
            return result
        finally:
            if foreground:
                _release_foreground_detection()

    logger.info(
        f"Scanning textless poster {tmdb_id} for burned-in text "
        f"(votes={vote_count}, source={source_key}, "
        f"priority={'foreground' if foreground else 'background'})"
    )
    task = asyncio.create_task(_scan())
    _text_detection_inflight[cache_key] = task

    def _cleanup(done: "asyncio.Task[bool | None]") -> None:
        if _text_detection_inflight.get(cache_key) is done:
            _text_detection_inflight.pop(cache_key, None)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(_cleanup)
    return task


def _queue_background_text_detection(item: _DeferredTextDetection) -> None:
    """Queue one vote-gated scan without retaining its decoded image."""
    if get_cached_text_detection(item.cache_key) is not None:
        return
    if item.cache_key in _background_detection_keys:
        return
    if _background_detection_queue is None:
        logger.warning(
            f"Background text-detection queue unavailable for {item.tmdb_id}; "
            "scan will retry on the next request"
        )
        return
    _background_detection_keys.add(item.cache_key)
    _background_detection_queue.put_nowait(item)
    logger.info(
        f"Queued vote-gated text scan for {item.tmdb_id} "
        f"(votes={item.vote_count}, pending={_background_detection_queue.qsize()})"
    )


def _load_detection_image(image_cache_key: str) -> Image.Image | None:
    cached_bytes = get_cached_tmdb_poster(image_cache_key)
    if not cached_bytes:
        return None
    return Image.open(io.BytesIO(cached_bytes)).convert("RGBA")


async def _background_text_detection_worker() -> None:
    """Drain vote-gated scans only while no foreground scan is queued or running."""
    assert _background_detection_queue is not None
    while True:
        item = await _background_detection_queue.get()
        try:
            if get_cached_text_detection(item.cache_key) is not None:
                continue
            while _foreground_detection_count > 0 or _active_poster_renders > 0:
                await asyncio.sleep(0.1)

            image = await asyncio.get_running_loop().run_in_executor(
                None, _load_detection_image, item.image_cache_key
            )
            if image is None:
                logger.warning(
                    f"Deferred text scan source unavailable for {item.tmdb_id}; "
                    "scan will retry on the next request"
                )
                continue

            # A poster render may have arrived while the image was loading.
            while _foreground_detection_count > 0 or _active_poster_renders > 0:
                await asyncio.sleep(0.1)

            await asyncio.shield(_start_text_detection(
                item.cache_key,
                image,
                title=item.title,
                source=item.source,
                tmdb_id=item.tmdb_id,
                vote_count=item.vote_count,
                media_type=item.media_type,
                image_path=item.image_path,
                source_key=item.source_key,
                foreground=False,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                f"Deferred text scan failed for {item.tmdb_id}: {exc}"
            )
        finally:
            _background_detection_keys.discard(item.cache_key)
            _background_detection_queue.task_done()


# Per-key cooldown timestamps (event-loop time). Keyed by the API key string so
# rotation is independent — a rate-limited key stands down while the other serves.
_mdblist_key_cooldown: dict[str, float] = {}
# Index into _cfg.SERVER_MDBLIST_KEYS for the currently active server-side key.
_mdblist_active_key_idx: int = 0


def _quality_source_name() -> str:
    return "scraper" if _cfg.QUALITY_SOURCE == "scraper" else "aiostreams"


def _quality_backoff_remaining(now: float | None = None) -> float:
    if now is None:
        now = asyncio.get_running_loop().time()
    return max(0.0, _quality_source_backoff_until.get(_quality_source_name(), 0.0) - now)


def _record_quality_result(result) -> None:
    source = _quality_source_name()
    if result is not FETCH_FAILED:
        _quality_source_backoff_until.pop(source, None)
        _quality_source_fail_count.pop(source, None)
        return
    now = asyncio.get_running_loop().time()
    if _quality_source_backoff_until.get(source, 0.0) > now:
        return
    failures = _quality_source_fail_count.get(source, 0) + 1
    _quality_source_fail_count[source] = failures
    delay = min(30.0 * (4 ** (failures - 1)), 1800.0)
    _quality_source_backoff_until[source] = now + delay
    logger.warning(f"Quality source {source} unavailable; backing off for {delay:.0f}s")


def _next_mdblist_server_key(current_key: str, now: float | None = None) -> str | None:
    """Select a healthy configured server key after *current_key*."""
    global _mdblist_active_key_idx
    keys = _cfg.SERVER_MDBLIST_KEYS
    if len(keys) < 2 or current_key not in keys:
        return None
    if now is None:
        now = asyncio.get_running_loop().time()
    start = keys.index(current_key)
    for offset in range(1, len(keys)):
        idx = (start + offset) % len(keys)
        candidate = keys[idx]
        if now >= _mdblist_key_cooldown.get(candidate, 0.0):
            _mdblist_active_key_idx = idx
            return candidate
    return None


def _mark_mdblist_rate_limit(
    imdb_id: str, key: str, result
) -> tuple[float, str | None]:
    """Cool down a rate-limited key and select a healthy configured fallback."""
    if result.retry_after:
        backoff_secs = min(float(result.retry_after), 3600.0)
    else:
        backoff_secs = 3600.0
    now = asyncio.get_running_loop().time()
    _mdblist_key_cooldown[key] = now + backoff_secs
    _rating_backoff[_rating_retry_key(imdb_id, key)] = now + backoff_secs
    return backoff_secs, _next_mdblist_server_key(key, now)


async def _background_quality_fetch(
    imdb_id: str,
    media_type: str,
    season: int,
    episode: int,
    release_date: str | None,
) -> None:
    """Fetch quality tokens from the configured quality source and cache them.  Never raises."""
    global _quality_bg_semaphore
    if _quality_bg_semaphore is None:
        _quality_bg_semaphore = asyncio.Semaphore(_cfg.QUALITY_BG_CONCURRENCY)
    try:
        async with _quality_bg_semaphore:
            if _HTTP_CLIENT is None:
                return
            remaining = _quality_backoff_remaining()
            if remaining > 0:
                logger.debug(
                    f"Quality fetch skipped for {imdb_id}; source cooldown has {remaining:.0f}s remaining"
                )
                return
            if _cfg.QUALITY_SOURCE == "scraper" and _cfg.SCRAPER_URL:
                result = await _with_retry(
                    fetch_quality_from_scraper,
                    _HTTP_CLIENT, _cfg.SCRAPER_URL, imdb_id, media_type, season, episode, release_date,
                )
            else:
                result = await _with_retry(
                    fetch_quality_from_aiostreams,
                    _HTTP_CLIENT, imdb_id, media_type, season, episode, release_date,
                )
            _record_quality_result(result)
            if result is not FETCH_FAILED:
                logger.info(f"Background quality fetch complete for {imdb_id}")
    except Exception as exc:
        _record_quality_result(FETCH_FAILED)
        logger.warning(f"Background quality fetch failed for {imdb_id}: {exc}")
    finally:
        _quality_bg_inflight.discard(imdb_id)

# Local imports
from age_badge import draw_quality_age_badge, draw_tier_bar, _score_points
from awards import sample_frosted_notch_rgb, sample_frosted_sash_rgb
from ratings import sample_frosted_bar_rgb
from awards import FETCH_FAILED, _RateLimited, draw_award_badge, draw_award_sash, parse_mdblist_awards, _STAR_WIN_AWARDS
from i18n import load_languages, translate_genre, translate_sash
from cache import (
    get_cached_quality,
    get_cached_rating,
    get_cached_final_poster,
    get_cached_final_poster_url,
    is_cached_final_poster_fresh,
    get_cached_tmdb_poster,
    set_cached_final_poster,
    get_cached_text_detection,
    set_cached_text_detection,
    init_db,
    close as close_db,
    BACKEND_KIND as _STORAGE_KIND,
    is_digital_release,
    set_cached_rating,
    delete_cached_tmdb_metadata,
    prune_caches,
    get_cache_stats,
)
import blobstore
import coordination as coord
import metrics as _metrics
from digital_release import digital_release_poll_loop
import config as _cfg
from discovery import (
    ALL_PRIORITY_SLOTS,
    FESTIVAL_KEYWORDS,
    DiscoveryMeta,
    extract_discovery_meta,
    pick_sash,
)
from quality import (
    BadgeItem,
    fetch_quality_from_aiostreams,
    fetch_quality_from_scraper,
    get_resized_badge,
    parse_quality,
    render_badges_left,
)
from ratings import calculate_weighted_score, draw_score_bar, fetch_rating, draw_score_bar_vertical, _draw_solid_pip, draw_frosted_bar, _score_color, _score_color_alt, _score_color_metal
from tmdb import composite_logo, logo_centre_y, fetch_logo, image_language_order, fetch_poster_metadata, fetch_poster_image, fetch_backdrop_image, fetch_trending_rank, fetch_release_status, svg_logo_supported, tmdb_metadata_cache_key, _CROP_VERSION, resolve_imdb_to_tmdb
from presets import get_preset, preset_names, preset_catalog

# ---------------------------------------------------------------------------
# Persistent HTTP client
# ---------------------------------------------------------------------------
# One client for the lifetime of the process. httpx keeps TCP connections
# alive in its connection pool, so repeated requests to the same host
# (TMDB, MDblist, AIOStreams) reuse the existing socket rather than paying
# TLS + TCP handshake overhead on every poster request.
#
# Timeouts are split:
#   connect=5s  — fail fast when a host is unreachable
#   read=12s    — allow slow responses from external APIs
#   pool=5s     — don't block forever waiting for a pool slot

_HTTP_CLIENT: httpx.AsyncClient | None = None

def _make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0),
        limits=httpx.Limits(
            max_connections=40,
            max_keepalive_connections=20,
            keepalive_expiry=30,
        ),
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        http2=False,   # most poster APIs don't support h2; skip the negotiation
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

_TMDB_ID_RE  = re.compile(r'^\d{1,10}$')
_IMDB_ID_RE  = re.compile(r'^tt\d{1,10}$')
_VALID_TYPES = frozenset({"movie", "tv", "series"})


def _check_tmdb_id(val: str) -> None:
    if not _TMDB_ID_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid tmdb_id")


def _check_imdb_id(val: str) -> None:
    if not _IMDB_ID_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid imdb_id")


def _check_type(val: str) -> None:
    if val not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="Invalid type")


# ---------------------------------------------------------------------------
# Key resolution helpers
# ---------------------------------------------------------------------------

def _resolve_tmdb_key(query_key: str) -> str | None:
    if query_key:
        return query_key
    if _cfg.SERVER_TMDB_KEY:
        return _cfg.SERVER_TMDB_KEY
    return None


def _resolve_mdblist_key(query_key: str) -> str | None:
    if query_key:
        return query_key
    if _cfg.SERVER_MDBLIST_KEYS:
        return _cfg.SERVER_MDBLIST_KEYS[_mdblist_active_key_idx % len(_cfg.SERVER_MDBLIST_KEYS)]
    return None


def _rating_retry_key(imdb_id: str, mdblist_key: str) -> tuple[str, str]:
    """Identify retry state for one title on one MDBList API key."""
    return imdb_id, mdblist_key


def _detection_vote_ok(vote_count: int | None) -> bool:
    """True when an asset should be scanned during the foreground request."""
    return vote_count is not None and vote_count <= _cfg.TEXTLESS_DETECTION_MAX_VOTES


# ---------------------------------------------------------------------------
# Per-request configuration
# ---------------------------------------------------------------------------

_CLIENT_EDGE_INSETS = {
    "stremio_tv_nuvio": (0.0, 0.0),
    "stremio_desktop_web": (0.007, 0.004),
    # Plex renders posters uncropped in its grid/details views — no edge
    # compensation needed. Used by the plex_sync.py companion script.
    "plex": (0.0, 0.0),
    # Same story for Jellyfin's web/desktop clients — posters render
    # uncropped in the library grid and detail views. Used by the
    # jellyfin_sync.py companion script.
    "jellyfin": (0.0, 0.0),
}


@dataclass
class RequestConfig:
    """
    Holds all user-tuneable config values for a single request.
    Defaults come from the global config module; query params override them.
    """
    show_award_sash:     bool = field(default_factory=lambda: _cfg.SHOW_AWARD_SASH)
    sash_poster_color:   bool = False   # diagonal sash colour derived from poster art
    cinema_greyscale:    bool = True    # greyscale art when release_status == "Cinema"
    cinema_greyscale_skip_if_available: bool = False  # keep colour if Web/Remux source found
    release_status_cinema_only: bool = True   # only show release status when "Cinema"
    badge_display_mode:  int  = field(default_factory=lambda: _cfg.BADGE_DISPLAY_MODE)
    rating_display_mode: int  = field(default_factory=lambda: _cfg.SHOW_RATING_DISPLAY_MODE)

    accent_bar_font_size_ratio:    float = field(default_factory=lambda: _cfg.ACCENT_BAR_MODE_FONT_SIZE_RATIO)
    # Score Bar mode label suffix: 0 = Year (legacy default), 1 = Info sash, 2 = Year + Info sash
    accent_bar_append_mode:        int   = 0
    # Score Bar position knob — distance from poster bottom edge as fraction of height.
    # Default matches the legacy hardcoded 30px on a 500x750 poster.
    accent_bar_bottom_ratio:       float = 0.04
    numeric_score_font_size_ratio: float = field(default_factory=lambda: _cfg.NUMERIC_SCORE_MODE_FONT_SIZE_RATIO)
    # Clean mode (mode 2) numeric format.  When True, the rating is divided by
    # 10 and shown to one decimal (87 → "8.7", 100 → "10.0").  Default keeps
    # the legacy 0-100 integer form.
    score_out_of_10: bool = False
    accent_bar_y_offset:           float = field(default_factory=lambda: _cfg.ACCENT_BAR_MODE_FONT_Y_OFFSET)
    numeric_score_y_offset:        float = field(default_factory=lambda: _cfg.NUMERIC_SCORE_MODE_FONT_Y_OFFSET)
    score_glow_threshold:          int   = field(default_factory=lambda: _cfg.SCORE_GLOW_THRESHOLD)
    score_glow_blur:               int   = field(default_factory=lambda: _cfg.SCORE_GLOW_BLUR)
    score_glow_alpha:              int   = field(default_factory=lambda: _cfg.SCORE_GLOW_ALPHA)
    minimalist_mode_font_size_ratio:  float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_SIZE_RATIO)
    minimalist_mode_font_x_offset: float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_X_OFFSET)
    minimalist_mode_font_y_offset: float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_Y_OFFSET)
    # What to append after the genre in Minimalist mode:
    #   0 = Year (Genre + year, rating as a colour-coded pip — the original look)
    #   1 = Rating (Genre | Score, score printed as text)
    #   2 = Year + Rating (Genre | Year | Score)
    minimalist_append_mode: int = 0

    # Frosted bar (rating_display_mode == 4)
    bar_height_ratio:        float = 0.080
    bar_font_size_ratio:     float = 0.55
    bar_frost_opacity:       float = 0.85
    bar_bottom_inset:        float = 0.0
    bar_style:               str   = "frosted"  # "frosted"|"silver"|"gold"|"rating_black"|"rating_frosted"
    bar_accent:              str   = "silver"   # "silver"|"gold"|"palette_0"|"palette_1"|"palette_2"
    bar_score_out_of_10:     bool  = False
    bar_match_notch:         bool  = False  # share one frosted tint with the sash notch
    bar_append:              str   = "rating_year"  # "rating_year"|"rating"|"year"|"sash"

    logo_max_w_ratio:  float = field(default_factory=lambda: _cfg.LOGO_MAX_W_RATIO)
    logo_max_h_ratio:  float = field(default_factory=lambda: _cfg.LOGO_MAX_H_RATIO)
    logo_bottom_ratio: float = field(default_factory=lambda: _cfg.LOGO_BOTTOM_RATIO)

    badge_height:            int   = field(default_factory=lambda: _cfg.BADGE_HEIGHT)
    badge_gap:               int   = field(default_factory=lambda: _cfg.BADGE_GAP)
    badge_anchor_x:          float = field(default_factory=lambda: _cfg.BADGE_ANCHOR_X_RATIO)
    badge_anchor_y:          float = field(default_factory=lambda: _cfg.BADGE_ANCHOR_Y_RATIO)
    badge_min_score:          int  = 2
    combined_badge_stacked:   bool = False

    movie_weights: dict | None = None
    tv_weights:    dict | None = None
    fallback_to_imdb: bool = False

    logo_language: str = field(default_factory=lambda: _cfg.DEFAULT_LOGO_LANGUAGE)
    # Logo resolution priority.  "native" = the viewer's chosen logo_language
    # (e.g. en); "original" = the content's own original language (e.g. ja for
    # an anime).  "text" = render the translated title as text.
    #   "native_original" (default): native → original → text
    #   "original_native":           original → native → text
    #   "native_if_original_english": native if content is native, else English
    #                                 → original → text
    #   "native_text":               native → text (no original-language logo)
    logo_priority: str = "native_original"
    # Fallback-poster style for titles with no art: "minimal" (procedural textured
    # backdrop) or "photoreal" (hand-made photographic art that blends with real
    # posters).  Missing photoreal art degrades to the minimal set.
    fallback_bg_style: str = "minimal"
    # Original-art mode: serve TMDB's primary poster (title/logo baked into the
    # art) as-is, skipping our own logo overlay, text detection and the textless/
    # backdrop fallbacks.  The logo is part of the art in this mode.
    use_original_art: bool = False
    # Which poster original-art mode serves:
    #   "primary"   = TMDB's designated default poster (most recognisable)
    #   "top_rated" = highest-voted poster, by logo_priority language order
    original_art_source: str = "primary"
    sash_priority: list[str] = field(default_factory=lambda: list(_cfg.SASH_PRIORITY))
    muted: bool = False
    textless: bool = False
    score_color_mode: int = 2
    top_gradient:    str = "high"   # off | low | medium | high — strength of the top vignette
    bottom_gradient: str = "high"   # off | low | medium | high — strength of the bottom vignette
    sash_badge: bool = False              # legacy; superseded by sash_mode (kept for back-compat parsing)
    sash_mode: str = "sash"               # "sash" (diagonal) | "notch"
    sash_badge_style:  str   = "frosted" # "silver" | "gold" | "frosted"
    sash_badge_size_w: float = 1.05      # horizontal scale of badge
    sash_badge_size_h: float = 1.05      # vertical scale of badge
    sash_badge_inset: float = 0.0          # top-edge offset as fraction of poster height (± small)
    sash_badge_font_ratio:   float = 0.43  # font size as fraction of badge height
    sash_badge_frost_opacity: float = 0.75 # frosted overlay opacity (0.0–1.0)
    sash_length_ratio: float = 1.15  # diagonal sash length as fraction of poster width
    sash_height_ratio: float = 0.12  # diagonal sash height (thickness) as fraction of poster width
    wait_for_quality: bool = False  # block response until quality is fetched (for poster-warm workflows)
    greyscale_no_quality: bool = False  # greyscale art when no quality found (needs wait_for_quality)


def _parse_bool(val: str | None, default: bool) -> bool:
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no")


def _parse_weights(raw: str | None, sources: list[str]) -> dict | None:
    if not raw:
        return None
    out = {}
    try:
        for part in raw.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            key, val = part.split(":", 1)
            key = key.strip().lower()
            if key in sources:
                out[key] = max(0.0, min(1.0, float(val)))
    except Exception:
        return None
    return out if out else None


def _parse_sash_priority(raw: str | None) -> list[str]:
    if not raw:
        return list(_cfg.SASH_PRIORITY)
    tokens = [s.strip() for s in raw.split(",") if s.strip()]
    # Tokens prefixed with "-" are explicit exclusions
    excluded  = {t[1:] for t in tokens if t.startswith("-") and t[1:] in ALL_PRIORITY_SLOTS}
    active    = [t      for t in tokens if not t.startswith("-") and t in ALL_PRIORITY_SLOTS]
    if not active and not excluded:
        return list(_cfg.SASH_PRIORITY)
    # Append any default slots that weren't explicitly listed or excluded
    active_set = set(active)
    for slot in _cfg.SASH_PRIORITY:
        if slot not in active_set and slot not in excluded:
            active.append(slot)
    return active


def build_request_config(params: dict) -> RequestConfig:
    """Build a RequestConfig from raw query-param strings.

    All numeric overrides are clamped to a sensible range so a malicious or
    careless caller can't pass values that would melt a worker (e.g.
    score_glow_blur=99999 turning into a Gaussian kernel of that radius, or
    badge_height=99999 triggering a multi-GB image resize).  Bounds are
    deliberately a little more generous than the configurator sliders so
    power users can push past UI limits without bypassing safety.
    """
    cfg = RequestConfig()

    # Client profiles provide defaults only; explicit inset parameters below
    # remain authoritative for users who fine-tune either edge manually.
    _client_insets = _CLIENT_EDGE_INSETS.get(
        (params.get("primary_client") or "").strip().lower()
    )
    if _client_insets is not None:
        cfg.bar_bottom_inset, cfg.sash_badge_inset = _client_insets

    def _b(key, default): return _parse_bool(params.get(key), default)

    def _f(key, default, lo: float, hi: float):
        """Float param with hard clamp to [lo, hi]; invalid → default."""
        try:
            return max(lo, min(hi, float(params[key]))) if key in params else default
        except (ValueError, TypeError):
            return default

    def _i(key, default, lo: int, hi: int):
        """Int param with hard clamp to [lo, hi]; invalid → default."""
        try:
            return max(lo, min(hi, int(params[key]))) if key in params else default
        except (ValueError, TypeError):
            return default

    cfg.show_award_sash         = _b("show_award_sash",        cfg.show_award_sash)
    cfg.sash_poster_color       = _b("sash_poster_color",      cfg.sash_poster_color)
    cfg.cinema_greyscale        = _b("cinema_greyscale",       cfg.cinema_greyscale)
    cfg.cinema_greyscale_skip_if_available = _b("cinema_greyscale_skip_if_available", cfg.cinema_greyscale_skip_if_available)
    cfg.release_status_cinema_only = _b("release_status_cinema_only", cfg.release_status_cinema_only)
    cfg.muted                   = _b("muted",                  cfg.muted)
    cfg.score_out_of_10         = _b("score_out_of_10",        cfg.score_out_of_10)
    cfg.textless                = _b("textless",               cfg.textless)
    # top_gradient accepts off / low / medium / high.  Legacy boolean values
    # (true / false) from pre-v1.0.4 URLs map to high / off respectively so
    # cached configurator links keep working.
    _tg_raw = (params.get("top_gradient") or "").strip().lower()
    if _tg_raw in _TOP_GRADIENT_LEVELS:
        cfg.top_gradient = _tg_raw
    elif _tg_raw in ("true", "1", "yes"):
        cfg.top_gradient = "high"
    elif _tg_raw in ("false", "0", "no"):
        cfg.top_gradient = "off"
    # else: leave RequestConfig default ("high")

    # bottom_gradient — same four-level enum as top.  Brand-new param so no
    # legacy boolean form to honour; unknown values fall through to the
    # RequestConfig default ("high") which matches the legacy behaviour.
    _bg_raw = (params.get("bottom_gradient") or "").strip().lower()
    if _bg_raw in _BOTTOM_GRADIENT_LEVELS:
        cfg.bottom_gradient = _bg_raw
    cfg.sash_badge              = _b("sash_badge",              cfg.sash_badge)
    # sash_mode supersedes the legacy sash_badge bool; fall back to it for old
    # URLs/presets (sash_badge=true → notch, false → diagonal sash).
    _sm_raw = (params.get("sash_mode") or "").strip().lower()
    if _sm_raw in ("hidden", "sash", "notch"):
        cfg.sash_mode = _sm_raw
    elif "show_award_sash" in params and not cfg.show_award_sash:
        cfg.sash_mode = "hidden"   # legacy: sashes turned off
    elif "sash_badge" in params:
        cfg.sash_mode = "notch" if cfg.sash_badge else "sash"
    cfg.sash_badge_inset         = _f("sash_badge_inset",         cfg.sash_badge_inset,         -0.02, 0.02)
    cfg.sash_badge_font_ratio    = _f("sash_badge_font_ratio",    cfg.sash_badge_font_ratio,    0.10, 1.0)
    cfg.sash_badge_frost_opacity = _f("sash_badge_frost_opacity", cfg.sash_badge_frost_opacity, 0.0, 1.0)
    cfg.sash_badge_size_w       = _f("sash_badge_size_w",       cfg.sash_badge_size_w,       0.5, 2.0)
    cfg.sash_badge_size_h       = _f("sash_badge_size_h",       cfg.sash_badge_size_h,       0.5, 2.0)
    _style_raw = params.get("sash_badge_style", cfg.sash_badge_style)
    if _style_raw in ("silver", "gold", "frosted", "black"):
        cfg.sash_badge_style = _style_raw
    cfg.sash_length_ratio       = _f("sash_length_ratio",      cfg.sash_length_ratio,      0.8, 1.5)
    cfg.sash_height_ratio       = _f("sash_height_ratio",      cfg.sash_height_ratio,      0.06, 0.20)
    cfg.wait_for_quality        = _b("wait_for_quality",        cfg.wait_for_quality)
    cfg.greyscale_no_quality    = _b("greyscale_no_quality",    cfg.greyscale_no_quality)
    cfg.score_color_mode        = _i("score_color_mode",       cfg.score_color_mode,       0,   2)
    cfg.badge_display_mode      = _i("badge_display_mode",     cfg.badge_display_mode,     0,   5)
    cfg.rating_display_mode     = _i("rating_display_mode",    cfg.rating_display_mode,    0,   4)

    if "show_quality_badges" in params and "badge_display_mode" not in params:
        if _parse_bool(params.get("show_quality_badges"), True):
            cfg.badge_display_mode = 1
        else:
            cfg.badge_display_mode = 0

    # Font-size ratios are multiplied by the poster width — anything above ~0.3
    # would overflow the poster; we cap at 0.5 to leave headroom for experimentation.
    cfg.accent_bar_font_size_ratio    = _f("accent_bar_font_size_ratio",    cfg.accent_bar_font_size_ratio,    0.0, 0.5)
    cfg.accent_bar_append_mode        = _i("accent_bar_append_mode",        cfg.accent_bar_append_mode,        0,   2)
    cfg.accent_bar_bottom_ratio       = _f("accent_bar_bottom_ratio",       cfg.accent_bar_bottom_ratio,       0.0, 0.5)
    cfg.numeric_score_font_size_ratio = _f("numeric_score_font_size_ratio", cfg.numeric_score_font_size_ratio, 0.0, 0.5)
    cfg.accent_bar_y_offset           = _f("accent_bar_y_offset",           cfg.accent_bar_y_offset,           0.0, 1.0)
    cfg.numeric_score_y_offset        = _f("numeric_score_y_offset",        cfg.numeric_score_y_offset,        0.0, 1.0)
    cfg.score_glow_threshold          = _i("score_glow_threshold",          cfg.score_glow_threshold,          0,   100)
    # Glow blur is a Gaussian kernel radius — cost is O(r²) per pixel, so anything
    # above ~50 starts measurably slowing the render.  Hard cap at 50.
    cfg.score_glow_blur               = _i("score_glow_blur",               cfg.score_glow_blur,               0,   50)
    cfg.score_glow_alpha              = _i("score_glow_alpha",              cfg.score_glow_alpha,              0,   255)
    cfg.minimalist_mode_font_size_ratio = _f("minimalist_mode_font_size_ratio", cfg.minimalist_mode_font_size_ratio, 0.0, 0.5)
    cfg.minimalist_mode_font_x_offset = _f("minimalist_mode_font_x_offset", cfg.minimalist_mode_font_x_offset, 0.0, 1.0)
    cfg.minimalist_mode_font_y_offset = _f("minimalist_mode_font_y_offset", cfg.minimalist_mode_font_y_offset, 0.0, 1.0)
    cfg.minimalist_append_mode = _i("minimalist_append_mode", cfg.minimalist_append_mode, 0, 2)

    cfg.bar_height_ratio        = _f("bar_height_ratio",        cfg.bar_height_ratio,        0.04, 0.20)
    cfg.bar_font_size_ratio     = _f("bar_font_size_ratio",     cfg.bar_font_size_ratio,     0.15, 0.70)
    cfg.bar_frost_opacity       = _f("bar_frost_opacity",       cfg.bar_frost_opacity,       0.0,  1.0)
    cfg.bar_bottom_inset        = _f("bar_bottom_inset",        cfg.bar_bottom_inset,        0.0,  0.10)
    _bst = (params.get("bar_style") or "").strip().lower()
    if _bst in ("frosted", "pure_black", "silver", "gold", "rating_black", "rating_frosted"):
        cfg.bar_style = _bst
    _bac = (params.get("bar_accent") or "").strip().lower()
    if _bac in ("silver", "gold", "sample", "palette_0", "palette_1", "palette_2"):
        cfg.bar_accent = _bac
    cfg.bar_score_out_of_10     = _b("bar_score_out_of_10",     cfg.bar_score_out_of_10)
    cfg.bar_match_notch         = _b("bar_match_notch",         cfg.bar_match_notch)
    _bap = (params.get("bar_append") or "").strip().lower()
    if _bap in ("rating_year", "rating", "year", "sash"):
        cfg.bar_append = _bap

    cfg.logo_max_w_ratio  = _f("logo_max_w_ratio",  cfg.logo_max_w_ratio,  0.0, 1.5)
    cfg.logo_max_h_ratio  = _f("logo_max_h_ratio",  cfg.logo_max_h_ratio,  0.0, 1.0)
    cfg.logo_bottom_ratio = _f("logo_bottom_ratio", cfg.logo_bottom_ratio, 0.0, 1.0)

    # badge_height in pixels — generous enough to cover any reasonable customisation
    # but well below the size that would cost real memory on resize.
    cfg.badge_height             = _i("badge_height",             cfg.badge_height,             1,   200)
    cfg.badge_gap                = _i("badge_gap",                cfg.badge_gap,                0,   100)
    cfg.badge_anchor_x           = _f("badge_anchor_x",           cfg.badge_anchor_x,           0.0, 1.0)
    cfg.badge_anchor_y           = _f("badge_anchor_y",           cfg.badge_anchor_y,           0.0, 1.0)
    cfg.badge_min_score      = _i("badge_min_score",
                                  _i("combined_badge_min_score", cfg.badge_min_score, 2, 6),
                                  2, 6)
    cfg.combined_badge_stacked   = _b("combined_badge_stacked",   cfg.combined_badge_stacked)

    all_sources = list(_cfg.MOVIE_WEIGHTS.keys())
    cfg.movie_weights = _parse_weights(params.get("movie_weights"), all_sources)

    tv_sources = list(_cfg.TV_WEIGHTS.keys())
    cfg.tv_weights = _parse_weights(params.get("tv_weights"), tv_sources)
    cfg.fallback_to_imdb = _b("fallback_to_imdb", cfg.fallback_to_imdb)

    cfg.logo_language        = (params.get("logo_language", cfg.logo_language).strip().lower())
    _lp = params.get("logo_priority")
    if _lp in (
        "native_original",
        "original_native",
        "native_if_original_english",
        "native_text",
    ):
        cfg.logo_priority = _lp
    elif "logo_native_fallback" in params:
        # Legacy param (boolean): true → native_original, false → native_text.
        cfg.logo_priority = "native_original" if _b("logo_native_fallback", True) else "native_text"
    _fbs = (params.get("fallback_bg_style") or "").strip().lower()
    if _fbs in ("minimal", "photoreal"):
        cfg.fallback_bg_style = _fbs
    cfg.use_original_art      = _b("use_original_art", cfg.use_original_art)
    _oas = (params.get("original_art_source") or "").strip().lower()
    if _oas in ("primary", "top_rated"):
        cfg.original_art_source = _oas
    cfg.sash_priority        = _parse_sash_priority(params.get("sash_priority"))

    return cfg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolved(value):
    return value


async def _with_retry(coro_fn, *args, **kwargs):
    """Call coro_fn(*args, **kwargs) and retry once if FETCH_FAILED is returned."""
    result = await coro_fn(*args, **kwargs)
    if result is FETCH_FAILED:
        result = await coro_fn(*args, **kwargs)
    return result


def _text_center(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    cx: float,
    cy: float,
) -> tuple[float, float]:
    bbox = draw.textbbox((0, 0), text, font=font)
    bbox_width = bbox[2] - bbox[0]
    ascent, descent = font.getmetrics()
    x = cx - bbox_width / 2 - bbox[0]
    optical_adjust = int(ascent * 0.22)
    y = cy - (ascent + descent) / 2 - descent + optical_adjust
    return x, y


# ---------------------------------------------------------------------------
# Poster composition
# ---------------------------------------------------------------------------

# Top-vignette strength.  Each entry maps a level name to
# (top_height_ratio, top_max_alpha).  None means "don't draw the gradient
# at all".  The "high" preset matches the legacy always-on behaviour so
# existing URLs / cached posters render identically when top_gradient is
# omitted.  Tweak the values here to retune any preset.
_TOP_GRADIENT_LEVELS: dict[str, tuple[float, int] | None] = {
    "off":    None,
    "low":    (0.20, 150),
    "medium": (0.25, 190),
    "high":   (0.40, 220),
}

# Bottom-vignette strength.  Same shape as the top gradient — (height_ratio,
# max_alpha).  Defaults to "high" which matches the legacy alpha-255 / 50%-
# height fade.  The previous auto-softening for Minimalist/Compact rating
# modes is dropped now that users can pick the level themselves; if you
# liked the softer look on those modes, set bottom_vignette=medium.
_BOTTOM_GRADIENT_LEVELS: dict[str, tuple[float, int] | None] = {
    "off":    None,
    "low":    (0.30, 180),
    "medium": (0.40, 210),
    "high":   (0.50, 225),
}
# Easing exponent shared across all bottom-gradient presets — controls the
# curve shape (1.0 = linear; >1 starts darker at the bottom and fades faster
# at the top).  Decoupled from strength so retuning one doesn't affect the
# other.
_BOTTOM_GRADIENT_CURVE = 1.5

# Genre-specific tint multipliers (R, G, B) for the fallback canvas.
# Applied to a dark base luminance of 10–18, so the dominant channel peaks
# around 30–55 at canvas midpoint — atmospheric rather than vivid.
# Names must match GENRE_MAP values exactly.
_GENRE_TINT: dict[str, tuple[float, float, float]] = {
    "Horror":      (3.2, 0.3, 0.3),   # deep blood red
    "Thriller":    (0.4, 2.2, 0.5),   # dark hunter green
    "Mystery":     (1.0, 0.3, 3.0),   # deep indigo
    "Sci-Fi":      (0.3, 1.2, 3.2),   # cold cyan-blue
    "Fantasy":     (1.6, 0.3, 3.0),   # purple-violet
    "Action":      (3.0, 0.8, 0.3),   # orange-red
    "Adventure":   (2.6, 1.5, 0.3),   # warm amber
    "Animation":   (0.4, 0.8, 3.2),   # electric blue
    "Comedy":      (2.6, 2.4, 0.3),   # golden yellow
    "Crime":       (2.4, 0.2, 0.2),   # dark crimson
    "Documentary": (0.3, 2.2, 2.4),   # teal
    "Drama":       (0.3, 0.3, 2.6),   # deep blue
    "Family":      (2.6, 1.2, 0.3),   # warm orange
    "History":     (2.2, 1.1, 0.3),   # sepia
    "Music":       (2.8, 0.3, 2.2),   # magenta
    "Romance":     (3.0, 0.3, 0.9),   # rose
    "War":         (0.9, 1.6, 0.3),   # olive green
    "Western":     (2.8, 1.1, 0.2),   # burnt sienna
    "Kids":        (0.3, 1.1, 3.0),   # bright blue
    "Reality":     (2.4, 0.8, 0.3),   # orange
    "Soap":        (2.6, 0.3, 0.9),   # rose-pink
    "Talk":        (0.3, 1.6, 2.4),   # teal-blue
    "News":        (0.3, 0.5, 2.6),   # steel blue
}
_FALLBACK_DEFAULT_TINT = (1.0, 1.0, 1.4)   # neutral cool blue

# Display-only label shortenings.  Some genre names are too wide for the poster
# label strip; shortening them reads better than shrinking the font.  These map
# the genre name to its *printed* form only — the original genre key is still
# used for font / colour / background lookups.
_GENRE_LABEL_OVERRIDES: dict[str, str] = {
    "Documentary": "Doc",
}


def _make_fallback_canvas(genre_ids: list[int] | None = None) -> Image.Image:
    """
    Dark gradient canvas served when a title has no poster art on TMDB.

    Applies a genre-derived colour tint so the canvas feels atmospheric rather
    than generically dark.  The base luminance is 10–18 (very dark) so even the
    dominant channel stays below ~55 — readable against white text overlays.
    """
    # Resolve genre → tint by walking GENRE_PRIORITY so higher-priority genres
    # win when a title belongs to multiple genres (same order as the score label).
    tint = _FALLBACK_DEFAULT_TINT
    if genre_ids:
        gid_set = set(genre_ids)
        for gid in _cfg.GENRE_PRIORITY:
            if gid in gid_set:
                name = _cfg.GENRE_MAP.get(gid)
                if name and name in _GENRE_TINT:
                    tint = _GENRE_TINT[name]
                    break

    r_mult, g_mult, b_mult = tint
    W, H = _cfg.POSTER_WIDTH, _cfg.POSTER_HEIGHT
    t    = np.linspace(0, np.pi, H, dtype=np.float32)
    # sin curve: peaks at midheight (~18), dark at top/bottom (~10)
    v    = (10 + 8 * np.sin(t)).astype(np.float32)
    arr  = np.zeros((H, W, 4), dtype=np.uint8)
    # Clamp BEFORE casting to uint8 — casting first would wrap mod-256 on
    # any value above 255, silently inverting colour for high-multiplier tints.
    arr[:, :, 0] = np.minimum(255, v * r_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 1] = np.minimum(255, v * g_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 2] = np.minimum(255, v * b_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 3] = 255
    return Image.fromarray(arr, "RGBA")


def _draw_combined_text_badge(
    image: Image.Image,
    tokens: list[str],
    *,
    x: int,
    y: int,
    font_size: int,
    min_score: int = 2,
    stacked: bool = False,
) -> None:
    """Minimalist quality badge: Resolution [sep] Visual Tag

    Horizontal layout: "4K  |  HDR"  — vertical pip coloured by source.
    Stacked layout:    "4K / HDR"    — horizontal rule coloured by source,
                       stacked like a division formula (for tight notch space).

    The separator colour encodes the source — gold for Remux, silver for Web.
    Nothing is drawn if resolution or source tokens are absent, or if the
    combined quality score is below *min_score*.
    """
    token_set = set(tokens)

    if tokens and _score_points(tokens) < min_score:
        return

    if "4K" in token_set:
        res = "4K"
    elif "1080P" in token_set:
        res = "HD"
    else:
        return

    if "REMUX" in token_set:
        sep_color = (255, 210,  60)   # gold
    elif "WEBDL" in token_set:
        sep_color = (192, 192, 200)   # silver
    else:
        return

    if "DV" in token_set:
        fmt = "DV"
    elif "HDR10+" in token_set:
        fmt = "HDR+"
    elif "HDR10" in token_set:
        fmt = "HDR"
    else:
        fmt = "SDR"

    try:
        font = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
    except IOError:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(image)
    ink  = (235, 235, 235, 255)

    if stacked:
        # Use textbbox so spacing is based on actual rendered glyph bounds,
        # not the full em-square (which includes invisible descender space and
        # would pin the line visually against the resolution text).
        b_res   = draw.textbbox((0, 0), res, font=font)
        b_fmt   = draw.textbbox((0, 0), fmt, font=font)
        w_res   = b_res[2] - b_res[0]
        w_fmt   = b_fmt[2] - b_fmt[0]
        h_res   = b_res[3] - b_res[1]   # actual glyph height, no dead space
        total_w = max(w_res, w_fmt)
        line_h  = max(2, font_size // 12)
        v_gap   = max(4, font_size // 5)

        # Resolution — draw so its visual top sits at y
        res_x = x + (total_w - w_res) // 2 - b_res[0]
        res_y = y - b_res[1]
        draw.text((res_x, res_y), res, font=font, fill=ink)

        # Horizontal rule — v_gap below the actual glyph bottom
        ly = y + h_res + v_gap
        draw.rounded_rectangle(
            [x, ly, x + total_w, ly + line_h],
            radius=line_h // 2,
            fill=sep_color,
        )

        # Visual tag — v_gap below the rule, aligned to its own visual top
        fmt_x = x + (total_w - w_fmt) // 2 - b_fmt[0]
        fmt_y = ly + line_h + v_gap - b_fmt[1]
        draw.text((fmt_x, fmt_y), fmt, font=font, fill=ink)

    else:
        pip_gap = int(font_size * 0.55)
        pip_w   = max(3, int(font_size * 0.15))
        pip_h   = int(font_size * 1.3)
        pip_cy  = y + round(font_size * 0.60)

        cx = x
        draw.text((cx, y), res, font=font, fill=ink)
        cx += round(draw.textlength(res, font=font)) + pip_gap
        _draw_solid_pip(image, x=cx, y_center=pip_cy, width=pip_w, height=pip_h, color=sep_color)
        cx += pip_w + pip_gap
        draw.text((cx, y), fmt, font=font, fill=ink)


def build_poster(
    image: Image.Image,
    score: int | str,
    genre: str,
    cfg: RequestConfig,
    logo: Image.Image | None = None,
    fallback_title: str | None = None,
    discovery_meta: DiscoveryMeta | None = None,
    quality_tokens: list[str] | None = None,
    release_year: str | None = None,
    age_rating: int | None = None,
    no_poster: bool = False,
) -> Image.Image:

    width, height = image.size

    # Greyscale the base art to flag "not available".  Overlays drawn afterwards
    # (sashes, badges, ratings, logo) stay in colour.  Two independent triggers:
    #   - cinema_greyscale: title still in cinemas / production (release_status,
    #     so implicitly gated on the release-status sash being enabled).
    #   - greyscale_no_quality: no stream quality was found.  Only meaningful
    #     when wait_for_quality is on (otherwise tokens may just not be fetched
    #     yet), so it's gated on it.
    _cinema_grey = (cfg.cinema_greyscale and discovery_meta is not None
                    and discovery_meta.release_status in ("Cinema", "Production"))
    # Override: if a real digital source (Web / Remux) was found, the title is
    # actually available — keep it in colour despite the cinema/production status.
    if (_cinema_grey and cfg.cinema_greyscale_skip_if_available and quality_tokens
            and any(t in ("WEBDL", "REMUX") for t in quality_tokens)):
        _cinema_grey = False
    _noquality_grey = (cfg.greyscale_no_quality and cfg.wait_for_quality and not quality_tokens)
    if _cinema_grey or _noquality_grey:
        image = ImageOps.grayscale(image).convert("RGBA")

    draw = ImageDraw.Draw(image)

    # Printed form of the genre.  Translate the canonical English name when a
    # translation exists for the request language; otherwise keep the English
    # path including the space-saving override (e.g. "Documentary" → "Doc").
    _genre_tr = translate_genre(genre, cfg.logo_language)
    if _genre_tr != genre:
        genre_label = _genre_tr
    else:
        genre_label = _GENRE_LABEL_OVERRIDES.get(genre, genre)

    # --- TOP GRADIENT (vectorised) ---
    # Darkens the top of the poster so the age-rating numeral and quality
    # badges stay legible over bright art.  Strength is one of four presets
    # (off / low / medium / high) — see _TOP_GRADIENT_LEVELS for the
    # (height_ratio, max_alpha) tuple each level uses.  Unknown level is
    # treated as "high" rather than skipped so a typo in a URL doesn't
    # silently disable the vignette.
    _tg_preset = _TOP_GRADIENT_LEVELS.get(cfg.top_gradient, _TOP_GRADIENT_LEVELS["high"])
    if _tg_preset is not None:
        top_height_ratio, top_max_alpha = _tg_preset
        top_height = int(height * top_height_ratio)
        t_top = np.linspace(0, 1, top_height, dtype=np.float32)
        eased_top = ((1 - t_top) * top_max_alpha).astype(np.uint8)
        top_array = np.broadcast_to(eased_top[:, np.newaxis], (top_height, width)).copy()
        top_overlay = Image.fromarray(top_array, mode="L")
        top_tinted = Image.new("RGBA", (width, top_height), (0, 0, 0, 0))
        top_tinted.putalpha(top_overlay)
        image.paste(top_tinted, (0, 0), mask=top_tinted)

    # --- BOTTOM GRADIENT (vectorised) ---
    # Strength is one of four presets (off / low / medium / high) — see
    # _BOTTOM_GRADIENT_LEVELS for the (height_ratio, max_alpha) tuple each
    # level uses.  The previous auto-softening for Minimalist / Compact modes
    # is dropped now that the user can pick the level themselves; if you'd
    # like the lighter fade those modes used to get for free, pick "medium".
    # Unknown level falls back to "high" so a typo can't accidentally turn
    # the fade off entirely (which would break label legibility).
    _bg_preset = _BOTTOM_GRADIENT_LEVELS.get(cfg.bottom_gradient, _BOTTOM_GRADIENT_LEVELS["high"])
    if _bg_preset is not None:
        bottom_height_ratio, bottom_max_alpha = _bg_preset
        bottom_height = int(height * bottom_height_ratio)
        bottom_start  = height - bottom_height
        t_bot         = np.linspace(0, 1, bottom_height, dtype=np.float32)
        eased_bot     = ((1 - (1 - t_bot) ** _BOTTOM_GRADIENT_CURVE) * bottom_max_alpha).astype(np.uint8)
        bottom_array  = np.broadcast_to(eased_bot[:, np.newaxis], (bottom_height, width)).copy()
        bottom_overlay = Image.fromarray(bottom_array, mode="L")
        bottom_tinted  = Image.new("RGBA", (width, bottom_height), (0, 0, 0, 0))
        bottom_tinted.putalpha(bottom_overlay)
        image.paste(bottom_tinted, (0, bottom_start), mask=bottom_tinted)

    # --- Badge / quality overlay ---
    mode   = cfg.badge_display_mode
    tokens = quality_tokens or []

    if mode == 1:
        # If quality is below the threshold, strip the quality tokens so the
        # badge renders silver/default rather than a misleadingly coloured tier.
        _tokens_1 = (
            tokens
            if (not tokens or _score_points(tokens) >= cfg.badge_min_score)
            else []
        )
        draw_quality_age_badge(
            image,
            age_rating,
            _tokens_1,
            anchor_x_ratio=cfg.badge_anchor_x,
            anchor_y_ratio=cfg.badge_anchor_y,
            badge_height=cfg.badge_height,
        )

    elif mode == 3:
        # Age rating only — always silver, no quality dependency
        draw_quality_age_badge(
            image,
            age_rating,
            [],
            anchor_x_ratio=cfg.badge_anchor_x,
            anchor_y_ratio=cfg.badge_anchor_y,
            badge_height=cfg.badge_height,
            always_silver=True,
        )

    elif mode == 4:
        # Accent bar — small vertical pill in tier colour, no text
        if not tokens or _score_points(tokens) >= cfg.badge_min_score:
            draw_tier_bar(
                image,
                tokens,
                anchor_x_ratio=cfg.badge_anchor_x,
                anchor_y_ratio=cfg.badge_anchor_y,
                bar_height=cfg.badge_height,
            )

    elif mode == 2:
        allowed_tokens  = {"4K", "1080P", "REMUX", "WEBDL", "DV", "HDR10+", "HDR10"}
        filtered_tokens = [t for t in tokens if t in allowed_tokens]

        if filtered_tokens and _score_points(tokens) >= cfg.badge_min_score:
            bx = int(width  * cfg.badge_anchor_x)
            by = int(height * cfg.badge_anchor_y)

            badge_items: list[BadgeItem] = [
                (get_resized_badge(token, cfg.badge_height), _cfg.QUALITY_LABELS.get(token, token))
                for token in filtered_tokens
            ]

            render_badges_left(
                image, badge_items,
                x_start=bx, y_top=by,
                badge_height=cfg.badge_height,
                badge_gap=cfg.badge_gap,
            )

    elif mode == 5:
        _draw_combined_text_badge(
            image, tokens,
            x=int(width  * cfg.badge_anchor_x),
            y=int(height * cfg.badge_anchor_y),
            font_size=cfg.badge_height,
            min_score=cfg.badge_min_score,
            stacked=cfg.combined_badge_stacked,
        )

    # --- Logo / fallback title ---
    if logo:
        composite_logo(
            image, logo,
            max_w_ratio=cfg.logo_max_w_ratio,
            max_h_ratio=cfg.logo_max_h_ratio,
            bottom_ratio=cfg.logo_bottom_ratio,
        )
    elif fallback_title:
        # ── Genre-aware font selection ────────────────────────────────────────
        # Titles are bucketed by genre and rendered in a thematically matching
        # font so different content categories feel distinct.
        #
        # Bucket → font mapping:
        #   Horror / Thriller / Mystery  → Creepster  (gothic, unsettling)
        #   Action / Sci-Fi / Adventure  → Bebas Neue (bold, cinematic)
        #   Comedy / Animation / Family  → Pacifico   (friendly, rounded)
        #   Drama / Romance / History    → Playfair   (elegant, literary)
        #   Crime / War / Documentary    → Oswald     (authoritative, strong)
        #   Default                      → NotoSerif  (neutral, readable)
        _GENRE_FONTS: dict[str, str] = {
            "Horror":           "Creepster-Regular.ttf",
            "Thriller":         "Creepster-Regular.ttf",
            "Mystery":          "Creepster-Regular.ttf",
            "Action":           "BebasNeue-Bold.ttf",
            "Sci-Fi":           "BebasNeue-Bold.ttf",
            "Adventure":        "BebasNeue-Bold.ttf",
            "Fantasy":          "BebasNeue-Bold.ttf",
            "Western":          "BebasNeue-Bold.ttf",
            "Comedy":           "Pacifico-Regular.ttf",
            "Animation":        "Pacifico-Regular.ttf",
            "Family":           "Pacifico-Regular.ttf",
            "Drama":            "PlayfairDisplay-Bold.ttf",
            "Romance":          "PlayfairDisplay-Bold.ttf",
            "History":          "PlayfairDisplay-Bold.ttf",
            "Music":            "PlayfairDisplay-Bold.ttf",
            "Crime":            "Oswald-Bold.ttf",
            "War":              "Oswald-Bold.ttf",
            "Documentary":      "Oswald-Bold.ttf",
        }
        _font_file = _GENRE_FONTS.get(genre, "NotoSerif-Bold.ttf")

        # Fallback-title rendering, sized to fill the SAME envelope a logo fills
        # (cfg.logo_max_w_ratio width × logo_max_h_ratio height) so a text title
        # looks as substantial as a logo would — short titles like "SELF-HELP"
        # grow to fill the width instead of being pinned tiny by a char-count
        # heuristic.  The logo size ratios therefore tune the fallback text too.
        max_w          = int(width  * cfg.logo_max_w_ratio)
        max_h          = int(height * cfg.logo_max_h_ratio)
        # Sit on the exact same vertical centre line composite_logo uses, so a
        # text-title poster lines up with a logo poster in the same row.
        title_cy       = logo_centre_y(height, cfg.logo_bottom_ratio)
        MIN_FONT_SIZE  = 22
        MAX_LINES      = 2
        FONT_PATH      = os.path.join(_FONTS_DIR, _font_file)

        def _wrap_lines(text: str, current_font) -> list[str]:
            """Greedy word-wrap: each line packs as many words as fit within max_w."""
            words = text.split()
            if not words:
                return []
            lines: list[str] = []
            current: list[str] = []
            for word in words:
                candidate = " ".join(current + [word])
                if draw.textlength(candidate, font=current_font) <= max_w or not current:
                    current.append(word)
                else:
                    lines.append(" ".join(current))
                    current = [word]
            if current:
                lines.append(" ".join(current))
            return lines

        # Pick the largest font whose wrapped block fits the logo envelope: scan
        # high→low and take the first fit.  A 2-line block gets a taller budget
        # than a single logo, since two stacked lines read fine a bit beyond one
        # logo's height.  Falls back to the wrapped layout at MIN_FONT_SIZE.
        try:
            font_size = MIN_FONT_SIZE
            font      = ImageFont.truetype(FONT_PATH, font_size)
            lines     = _wrap_lines(fallback_title, font)
            for _fs in range(int(height * 0.26), MIN_FONT_SIZE - 1, -2):
                _f  = ImageFont.truetype(FONT_PATH, _fs)
                _ls = _wrap_lines(fallback_title, _f)
                if len(_ls) > MAX_LINES:
                    continue
                _widest  = max((draw.textlength(ln, font=_f) for ln in _ls), default=0)
                _block_h = int(_fs * 1.15) * len(_ls)
                _budget  = max_h if len(_ls) == 1 else int(max_h * 1.7)
                if _widest <= max_w and _block_h <= _budget:
                    font, font_size, lines = _f, _fs, _ls
                    break
        except OSError:
            font      = ImageFont.load_default()
            font_size = MIN_FONT_SIZE
            lines     = [fallback_title]

        # Centre the multi-line block vertically around title_cy.
        line_height    = int(font_size * 1.15)
        total_height   = line_height * len(lines)
        block_top      = title_cy - total_height // 2
        shadow_offset  = max(2, int(font_size * 0.04))

        for i, line in enumerate(lines):
            line_cy = block_top + i * line_height + line_height // 2
            tx, ty  = _text_center(draw, line, font, width / 2, line_cy)  # type: ignore
            draw.text((tx + shadow_offset, ty + shadow_offset), line, font=font, fill=(0, 0, 0, 180))
            draw.text((tx, ty),                                  line, font=font, fill=(255, 255, 255, 255))

    # Resolve the info-sash pick once, regardless of whether the diagonal sash
    # itself is rendered independently.
    #
    # When greyscale is active on an unreleased title (Cinema / Production),
    # force the release-status slot to the front so its badge always wins — that
    # tells the user the poster is greyscale because it's unavailable, rather
    # than a title whose art happens to be black & white.
    _sash_priority = cfg.sash_priority
    if (cfg.cinema_greyscale and discovery_meta is not None
            and discovery_meta.release_status in ("Cinema", "Production")
            and "release_status" in _sash_priority):
        _sash_priority = ["release_status"] + [s for s in _sash_priority if s != "release_status"]
    sash_result = (
        pick_sash(discovery_meta, _sash_priority)
        if discovery_meta is not None
        else None
    )

    # --- Shared frosted tint (Match Notch Colour) ---------------------------
    # When the frosted rating bar (mode 4) and a poster-coloured sash element are
    # both on (a frosted notch badge OR a poster-coloured diagonal sash) and
    # bar_match_notch is set, sample each region now (before either is drawn) and
    # force both to the more saturated of the two colours so they match.
    _shared_tint: tuple[float, float, float] | None = None
    _sash_shown    = cfg.sash_mode != "hidden" and sash_result is not None
    _notch_frosted = _sash_shown and cfg.sash_mode == "notch" and cfg.sash_badge_style == "frosted"
    _sash_poster   = _sash_shown and cfg.sash_mode == "sash" and cfg.sash_poster_color
    if (
        cfg.bar_match_notch
        and cfg.rating_display_mode == 4
        and cfg.bar_style in ("frosted", "rating_frosted")
        and (_notch_frosted or _sash_poster)
    ):
        _bar_rgb = sample_frosted_bar_rgb(image, cfg.bar_height_ratio, cfg.bar_bottom_inset)
        if _notch_frosted:
            _sash_rgb = sample_frosted_notch_rgb(
                image, translate_sash(sash_result[0], cfg.logo_language), sash_type=sash_result[1],
                size_ratio_w=cfg.sash_badge_size_w, size_ratio_h=cfg.sash_badge_size_h,
                font_size_ratio=cfg.sash_badge_font_ratio, notch_inset=cfg.sash_badge_inset,
                star=(sash_result[1] == "win" and sash_result[0] in _STAR_WIN_AWARDS),
            )
        else:
            _sash_rgb = sample_frosted_sash_rgb(image)
        def _sat(rgb: tuple[float, float, float]) -> float:
            import colorsys
            return colorsys.rgb_to_hsv(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)[1]
        _shared_tint = _bar_rgb if _sat(_bar_rgb) >= _sat(_sash_rgb) else _sash_rgb

    # --- Rating / genre label ---
    if cfg.rating_display_mode != 0:

        if cfg.rating_display_mode == 1:
            font_size = int(width * cfg.accent_bar_font_size_ratio)
            # Label suffix is configurable: append year, append sash text, or
            # append both joined by " · ".  Missing data degrades gracefully —
            # if "sash" is requested but no sash triggered, we just show the
            # genre; if "both" but only one is present, we show whichever did.
            #
            # The separator immediately before the sash text becomes "★" when
            # the sash is a winner (sash_type == "win") rather than "·".  Same
            # disambiguation trick used by Compact mode — Best Picture /
            # Golden Globe / festival wins and nominees share their label
            # text, so without this they'd be indistinguishable here.
            _append_year = cfg.accent_bar_append_mode in (0, 2)
            _append_sash = cfg.accent_bar_append_mode in (1, 2)
            _sash_text_for_label, _sash_type_for_label = (
                sash_result if (_append_sash and sash_result) else (None, None)
            )

            _pre_sash = [genre_label]
            if _append_year and release_year:
                _pre_sash.append(str(release_year))
            _label_main = " · ".join(_pre_sash)

            if _sash_text_for_label:
                _sash_sep = " ★ " if _sash_type_for_label == "win" else " · "
                label = _label_main + _sash_sep + translate_sash(_sash_text_for_label, cfg.logo_language)
            else:
                label = _label_main
            rating_cy = height * cfg.accent_bar_y_offset

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            tx, ty = _text_center(draw, label, font_meta, width / 2, rating_cy)  # type: ignore
            draw.text(
                (tx, ty - int(font_size * 0.10)),
                label,
                font=font_meta,
                fill=(200, 200, 200, 255),
            )
            draw_score_bar(
                image, score,
                bottom_margin=int(height * cfg.accent_bar_bottom_ratio),
                glow_threshold=cfg.score_glow_threshold,
                glow_blur=cfg.score_glow_blur,
                glow_alpha=cfg.score_glow_alpha,
                color_mode=cfg.score_color_mode,
            )

        elif cfg.rating_display_mode == 2:
            font_size = int(width * cfg.numeric_score_font_size_ratio)
            # Score formatting:
            #   out of 100 (default): "87", "100", "N/A"
            #   out of 10:            "8.7", "8.0" (always one decimal), "10"
            #                         (no decimal — already two glyphs wide)
            # Non-numeric scores ("N/A") pass through unchanged in either mode.
            if cfg.score_out_of_10 and isinstance(score, (int, float)):
                _score_text = "10" if score >= 100 else f"{score / 10:.1f}"
            else:
                _score_text = str(score)
            label = f"{genre_label} ★ {_score_text}"
            rating_cy = height * cfg.numeric_score_y_offset

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            tx, ty = _text_center(draw, label, font_meta, width / 2, rating_cy)  # type: ignore
            draw.text(
                (tx, ty - int(font_size * 0.10)),
                label,
                font=font_meta,
                fill=(200, 200, 200, 255),
            )

        elif cfg.rating_display_mode == 3:
            font_size = int(width * cfg.minimalist_mode_font_size_ratio)

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            y = round(height * cfg.minimalist_mode_font_y_offset)
            right_edge = width - int(width * cfg.minimalist_mode_font_x_offset)
            _ink = (235, 235, 235, 255)

            # Segments, each tagged with the SEPARATOR that precedes it:
            #   "pip"  — silver vertical pip (before the year)
            #   "star" — ★ glyph (before the rating/score)
            #   "rpip" — pip COLOURED by score (mode 0 only: the rating shown
            #            purely by colour, no number)
            # Mode 0 ("Year"): genre [rating-pip] year
            # Mode 1 ("Rating"): genre ★ score
            # Mode 2 ("Year + Rating"): genre [pip] year ★ score
            _has_score = score not in ("N/A", None)
            parts = [(genre_label, None)]   # (text, separator_before)
            if cfg.minimalist_append_mode == 0:
                if release_year:
                    parts.append((str(release_year), "rpip"))
            elif cfg.minimalist_append_mode == 1:
                if _has_score:
                    parts.append((str(score), "star"))
            else:  # 2 — Year + Rating
                if release_year:
                    parts.append((str(release_year), "pip"))
                if _has_score:
                    parts.append((str(score), "star"))

            pip_gap = int(font_size * 0.55)
            pip_w   = max(4, int(font_size * 0.18))
            pip_h   = int(font_size * 1.4)
            pip_cy  = round(y + font_size * 0.60)
            star_w  = draw.textlength("★", font=font_meta)

            # Lay out right-to-left: each segment, with its separator to its left.
            cursor = right_edge
            ops    = []   # (kind, x[, text]); kind in text|pip|rpip|star
            for i in range(len(parts) - 1, -1, -1):
                seg, sep = parts[i]
                seg_x = cursor - draw.textlength(seg, font=font_meta)
                ops.append(("text", seg_x, seg))
                cursor = seg_x
                if sep:
                    cursor -= pip_gap
                    sep_w  = star_w if sep == "star" else pip_w
                    sep_x  = cursor - sep_w
                    ops.append((sep, sep_x))
                    cursor = sep_x - pip_gap

            for op in ops:
                kind, ox = op[0], op[1]
                if kind == "text":
                    draw.text((ox, y), op[2], font=font_meta, fill=_ink)
                elif kind == "star":
                    draw.text((ox, y), "★", font=font_meta, fill=_ink)
                elif kind == "rpip":
                    draw_score_bar_vertical(image, score, x=ox, y_center=pip_cy,
                                            height=pip_h, width=pip_w,
                                            color_mode=cfg.score_color_mode)
                else:  # "pip"
                    _draw_solid_pip(image, x=ox, y_center=pip_cy,
                                    width=pip_w, height=pip_h, color=(192, 192, 200))

        elif cfg.rating_display_mode == 4:
            # Frosted bar — centred dot-separated label at the bottom.
            # Format: Year · Genre · ★ Rating  (omit any missing field)
            _has_score = score not in ("N/A", None)
            if _has_score:
                if cfg.bar_score_out_of_10:
                    _score_str = "10" if int(score) >= 100 else f"{int(score) / 10:.1f}"
                else:
                    _score_str = str(score)
            else:
                _score_str = ""
            _year_str  = str(release_year) if release_year else ""
            _bar_sash, _ = sash_result if sash_result else (None, None)
            if cfg.bar_append == "rating_year":
                _parts = [_year_str, genre_label or "", f"★ {_score_str}" if _score_str else ""]
            elif cfg.bar_append == "rating":
                _parts = [genre_label or "", f"★ {_score_str}" if _score_str else ""]
            elif cfg.bar_append == "year":
                _parts = [_year_str, genre_label or ""]
            else:  # "sash"
                _parts = [genre_label or "", translate_sash(_bar_sash, cfg.logo_language) if _bar_sash else ""]
            _parts = [p for p in _parts if p]
            _sep = "  ·  " if len(_parts) <= 2 else " · "
            image = draw_frosted_bar(
                image,
                left_text   = "",
                center_text = _sep.join(_parts),
                right_text  = "",
                bar_height_ratio = cfg.bar_height_ratio,
                font_size_ratio  = cfg.bar_font_size_ratio,
                frost_opacity    = cfg.bar_frost_opacity,
                bottom_inset     = cfg.bar_bottom_inset,
                style            = cfg.bar_style,
                score            = score if score not in ("N/A", None) else None,
                fill_color       = (
                    None  # "sample" → let draw_frosted_bar derive from bar tint
                    if cfg.bar_accent == "sample" else
                    {"silver": (210, 210, 218), "gold": (212, 175, 55)}.get(cfg.bar_accent)
                    or (
                        {0: _score_color, 1: _score_color_alt, 2: _score_color_metal}
                        .get(int(cfg.bar_accent[-1]), _score_color)(int(score))[0]
                        if score not in ("N/A", None) else (210, 210, 218)
                    )
                ) if cfg.bar_style in ("rating_black", "rating_frosted") else None,
                tint_rgb         = _shared_tint,
            )

    # --- Discovery sash / badge ---
    if cfg.sash_mode != "hidden" and sash_result is not None:
        label, sash_type = sash_result
        # Decide the ★ winner marker on the CANONICAL English label, then render
        # the translated label.  (The renderers' own English set-match would miss
        # a translated label and drop the star.)
        _is_star  = sash_type == "win" and label in _STAR_WIN_AWARDS
        _label_tr = translate_sash(label, cfg.logo_language)
        if cfg.sash_mode == "notch":
            image = draw_award_badge(image, _label_tr, sash_type=sash_type,
                                     size_ratio_w=cfg.sash_badge_size_w,
                                     size_ratio_h=cfg.sash_badge_size_h,
                                     notch_style=cfg.sash_badge_style,
                                     notch_inset=cfg.sash_badge_inset,
                                     font_size_ratio=cfg.sash_badge_font_ratio,
                                     frost_opacity=cfg.sash_badge_frost_opacity,
                                     tint_rgb=_shared_tint,
                                     star=_is_star)
        else:  # "sash" — diagonal
            _poster_color = None
            if cfg.sash_poster_color:
                _poster_color = _shared_tint or sample_frosted_sash_rgb(image)
            image = draw_award_sash(image, _label_tr, sash_type=sash_type, muted=cfg.muted,
                                    length_ratio=cfg.sash_length_ratio,
                                    height_ratio=cfg.sash_height_ratio,
                                    poster_color=_poster_color)

    return image


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def _with_lease(lease_name: str, ttl_seconds: float) -> str | None:
    """Best-effort leader election (ElfHosted fork). Returns a lease token if
    this replica/worker is currently the leader, else None. With the
    in-process coordinator each worker is its own leader (no cross-process
    state); with the Redis coordinator exactly one replica holds the lease."""
    token = await coord.try_acquire_lease(lease_name, ttl_seconds)
    if token is not None:
        logger.info(f"Acquired lease {lease_name!r} (token={token})")
    return token


async def _cache_prune_loop() -> None:
    """Periodically prune expired rows from all cache tables. Leader-elected
    so multi-replica deployments only run one prune per cycle."""
    # Lease TTL covers one iteration's sleep + run, plus a buffer so a slow
    # SQLite VACUUM doesn't release the lease mid-run.
    lease_ttl = 8 * 3600.0
    lease_token: str | None = None

    # Wait a few minutes after startup before the first run so the service
    # is fully warmed before taking the SQLite write lock.
    await asyncio.sleep(300)
    try:
        while True:
            if lease_token is None:
                lease_token = await _with_lease(coord.LEASE_CACHE_PRUNE, lease_ttl)
            elif not await coord.refresh_lease(coord.LEASE_CACHE_PRUNE, lease_token, lease_ttl):
                logger.info(f"Lease {coord.LEASE_CACHE_PRUNE!r} lost")
                lease_token = None

            if lease_token is not None:
                logger.info("Running scheduled cache prune (leader)")
                # prune_caches is async (it deletes composite blobs from the
                # blobstore alongside the metadata rows); the DB-side work
                # inside it is sync, fine at a 6h cadence.
                await prune_caches()
                # Coordinator-managed state (rating backoff, bg-fetch claims):
                # evicts expired per-worker entries on the in-process backend;
                # a no-op on Redis (the server expires keys itself).
                await coord.prune_expired()
            else:
                logger.debug("Cache prune skipped — another replica holds the lease")

            # Evict expired entries from the in-process rating backoff dict.
            # Done every cycle regardless of leadership since the dict is
            # per-worker (entries are also removed lazily on access).
            _now = asyncio.get_running_loop().time()
            expired = [k for k, v in _rating_backoff.items() if v <= _now]
            for k in expired:
                del _rating_backoff[k]
            if expired:
                logger.debug(f"Pruned {len(expired)} expired rating backoff entries")

            await asyncio.sleep(6 * 3600)   # every 6 hours
    finally:
        if lease_token is not None:
            await coord.release_lease(coord.LEASE_CACHE_PRUNE, lease_token)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _HTTP_CLIENT, _configurator_html, _render_assets_signature
    global _background_detection_queue, _background_detection_task
    init_db()
    await blobstore.init()
    await coord.init()
    with suppress(Exception):
        _metrics.backend_info.labels(
            storage=_STORAGE_KIND,
            coordinator=getattr(coord, "BACKEND_KIND", "inprocess"),
            blobstore=getattr(blobstore, "BACKEND_KIND", "local"),
        ).set(1)
    logger.info(
        f"Cache initialised (storage={_STORAGE_KIND}, "
        f"blobstore={getattr(blobstore, 'BACKEND_KIND', 'local')}, "
        f"coordination={getattr(coord, 'BACKEND_KIND', 'inprocess')}; "
        f"composite TTL {_cfg.COMPOSITE_CACHE_TTL}s / "
        f"{_cfg.COMPOSITE_CACHE_TTL / 86400:.1f}d)"
    )
    _HTTP_CLIENT = _make_http_client()
    logger.info("HTTP client initialised")
    # Warn on quality source misconfiguration
    if _cfg.QUALITY_SOURCE == "scraper" and (bool(_cfg.AIOSTREAMS_URL) or bool(_cfg.AIOSTREAMS_AUTH)):
        logger.warning(
            "QUALITY_SOURCE=scraper but AIOSTREAMS_URL/AIOSTREAMS_AUTH are also set — "
            "scraper will be used; AIOSTREAMS settings are ignored. "
            "Unset AIOSTREAMS_URL and AIOSTREAMS_AUTH to silence this warning."
        )
    if _cfg.QUALITY_SOURCE == "scraper" and not _cfg.SCRAPER_URL:
        logger.warning("QUALITY_SOURCE=scraper but SCRAPER_URL is not set — quality fetching is disabled.")
    if _cfg.QUALITY_SOURCE not in ("aiostreams", "scraper"):
        logger.warning(f"Unknown QUALITY_SOURCE={_cfg.QUALITY_SOURCE!r} — defaulting to aiostreams behaviour.")
    _configurator_html = _load_configurator_html()
    load_languages()   # poster-output translations (English fallback if absent)
    _render_assets_signature = _compute_render_assets_signature()
    # Warm the genre fallback backgrounds into memory so no-art posters render
    # with zero extra latency (same idea as the badge cache warm-up).
    try:
        _warmed = 0
        for _style in _GENRE_BG_STYLES:
            _sdir = os.path.join(_GENRE_BG_DIR, _style)
            if not os.path.isdir(_sdir):
                continue
            for _fn in os.listdir(_sdir):
                if _fn.lower().endswith(".png"):
                    if _load_genre_background(_fn[:-4], _style) is not None:
                        _warmed += 1
        if _warmed:
            logger.info(f"Genre backgrounds warmed: {_warmed} entries")
        else:
            logger.info("No genre background art found — using gradient fallbacks")
    except Exception as exc:
        logger.warning(f"Genre background warm-up skipped: {exc}")
    # Burned-in-text detection: fetch + load PP-OCRv5 Mobile in the background so
    # the first textless request isn't blocked by the one-time ~4.6 MB
    # download.  On by default; skipped when the operator has opted out.
    if _cfg.TEXTLESS_TEXT_DETECTION:
        _background_detection_queue = asyncio.Queue()
        _background_detection_task = asyncio.create_task(
            _background_text_detection_worker()
        )

        async def _warm_text_detector():
            try:
                from text_detect import text_detection_status, warm_model
                ok = await asyncio.get_running_loop().run_in_executor(_get_detect_executor(), warm_model)
                log = logger.info if ok else logger.warning
                log(f"Burned-in-text detection: {text_detection_status()}")
            except Exception as exc:
                logger.warning(f"PP-OCR warm-up failed: {exc}")
        asyncio.create_task(_warm_text_detector())

    prune_task   = asyncio.create_task(_cache_prune_loop())
    digital_task = asyncio.create_task(digital_release_poll_loop(_HTTP_CLIENT))
    yield
    prune_task.cancel()
    digital_task.cancel()
    if _background_detection_task is not None:
        _background_detection_task.cancel()
    # Await the cancelled tasks so their finally: blocks finish unwinding
    # before we close the HTTP client they may still be using.
    with suppress(asyncio.CancelledError):
        await prune_task
    with suppress(asyncio.CancelledError):
        await digital_task
    if _background_detection_task is not None:
        with suppress(asyncio.CancelledError):
            await _background_detection_task
        _background_detection_task = None
    _background_detection_queue = None
    _background_detection_keys.clear()
    _shutdown_detect_executor()
    await _HTTP_CLIENT.aclose()
    logger.info("HTTP client closed")
    with suppress(Exception):
        await coord.close()
    with suppress(Exception):
        await blobstore.close()
    with suppress(Exception):
        close_db()
    logger.info("Storage + blobstore + coordination closed")


app = FastAPI(lifespan=lifespan)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_FONTS_DIR = os.path.join(BASE_DIR, "fonts")


def _read_app_version() -> str:
    """ElfHosted fork: the app version for the configurator's build footer +
    /server-caps. Read once at import from version.txt (release-please keeps it
    in sync with the cut tag, e.g. 1.1.0-elf.1). Falls back to 'dev' when the
    file is absent (local checkout before a release)."""
    try:
        with open(os.path.join(BASE_DIR, "version.txt"), "r", encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except Exception:
        return "dev"


_APP_VERSION = _read_app_version()


# ── Genre fallback backgrounds ────────────────────────────────────────────
# Atmospheric 500x750 PNGs (procedurally generated by genre_backgrounds.py, or
# hand-made overrides dropped into the same folder) used as the base for no-art
# fallback posters instead of the flat gradient.  Cached in memory; a *copy* is
# returned per request because build_poster draws onto the base.
_GENRE_BG_DIR = os.path.join(BASE_DIR, "static", "genre_bg")
# Two interchangeable fallback-background sets, chosen per request via
# fallback_bg_style: "minimal" (procedural textured) or "photoreal" (hand-made
# photographic art that blends with real posters).
_GENRE_BG_STYLES = ("minimal", "photoreal")
_genre_bg_cache: dict[str, "Image.Image | None"] = {}   # keyed "style/genre"


def _genre_bg_path(style: str, name: str) -> "str | None":
    """Filesystem path to a genre-background PNG, or None if it doesn't exist."""
    p = os.path.join(_GENRE_BG_DIR, style, f"{name}.png")
    return p if os.path.exists(p) else None


def _load_genre_background(genre: str, style: str = "minimal") -> "Image.Image | None":
    """Return a fresh RGBA copy of the genre fallback background for *style*, or
    None if none exists.  A missing image degrades gracefully: the style's
    default.png → the minimal set's genre/default → None (caller then renders the
    procedural gradient canvas).  So selecting a not-yet-populated style never
    breaks — it just falls back to minimal."""
    if style not in _GENRE_BG_STYLES:
        style = "minimal"
    key = f"{style}/{genre}"
    if key not in _genre_bg_cache:
        path = (
            _genre_bg_path(style, genre)
            or _genre_bg_path(style, "default")
            or (_genre_bg_path("minimal", genre) if style != "minimal" else None)
            or _genre_bg_path("minimal", "default")
        )
        try:
            _genre_bg_cache[key] = Image.open(path).convert("RGBA") if path else None
        except Exception:
            _genre_bg_cache[key] = None
    base = _genre_bg_cache[key]
    return base.copy() if base is not None else None


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.middleware("http")
async def remove_server_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["server"] = "unknown"
    return response


# ---------------------------------------------------------------------------
# Server capability endpoint
# ---------------------------------------------------------------------------

@app.get("/server-caps")
async def server_caps(access_key: str = ""):
    # access_key_valid is the escape-hatch signal for the configurator lock:
    # true only when an ACCESS_KEY is configured AND the caller supplied it.
    access_key_valid = bool(
        _cfg.ACCESS_KEY and access_key
        and hmac.compare_digest(access_key, _cfg.ACCESS_KEY)
    )
    # On a public-tier instance (PRESET_ENABLED) /server-caps is anonymous so
    # the locked configurator can discover the preset catalogue. Otherwise the
    # original access_key gate stays.
    if _cfg.ACCESS_KEY and not _cfg.PRESET_ENABLED and not access_key_valid:
        raise HTTPException(status_code=403, detail="Unauthorized")
    return {
        "tmdb_key_set":          bool(_cfg.SERVER_TMDB_KEY),
        "mdblist_key_set":       bool(_cfg.SERVER_MDBLIST_KEYS),
        "mdblist_key_count":     len(_cfg.SERVER_MDBLIST_KEYS),
        "aiostreams_configured": bool(_cfg.AIOSTREAMS_URL and _cfg.AIOSTREAMS_AUTH),
        "quality_configured":    (
            bool(_cfg.AIOSTREAMS_URL and _cfg.AIOSTREAMS_AUTH)
            or (_cfg.QUALITY_SOURCE == "scraper" and bool(_cfg.SCRAPER_URL))
        ),
        # ElfHosted fork — static-preset moat / public-tier lock signals.
        "preset_enabled":        _cfg.PRESET_ENABLED,
        "presets":               preset_catalog() if _cfg.PRESET_ENABLED else [],
        "access_key_valid":      access_key_valid,
        "version":               _APP_VERSION,
    }


# ---------------------------------------------------------------------------
# Configurator HTML
# ---------------------------------------------------------------------------

_configurator_html: str | None = None
# Strong ETag for the configurator HTML — short hash of its bytes so the
# browser can revalidate cheaply.  Without this, browsers heuristically
# cache the page and keep serving stale HTML after a container rebuild,
# which is what made sliders / dropdowns drift out of sync with the new
# defaults until a manual Reset.
_configurator_etag: str | None = None
_RENDER_CACHE_VERSION = "2"
_render_assets_signature = "startup"


def _compute_render_assets_signature() -> str:
    digest = hashlib.sha256()
    roots = (
        os.path.join(BASE_DIR, "languages"),
        os.path.join(BASE_DIR, "static", "genre_bg"),
    )
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for filename in sorted(filenames):
                path = os.path.join(dirpath, filename)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                digest.update(os.path.relpath(path, BASE_DIR).encode())
                digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    override_path = os.environ.get(
        "DISCOVERY_OVERRIDES_PATH", "/app/cache/discovery_overrides.json"
    )
    try:
        with open(override_path, "rb") as override_file:
            digest.update(override_file.read())
    except OSError:
        pass
    return digest.hexdigest()[:16]


def _server_render_signature() -> str:
    return "|".join((
        f"render={_RENDER_CACHE_VERSION}",
        f"jpeg={_cfg.JPEG_QUALITY}",
        f"contrast={int(_cfg.LOGO_CONTRAST_RESCUE)}",
        f"stretch={int(_cfg.LOGO_STRETCH_DISABLED)}:{_cfg.LOGO_STRETCH_FACTOR:g}",
        f"assets={_render_assets_signature}",
    ))


def _composite_cache_key(
    imdb_id: str,
    tmdb_id: str,
    media_type: str,
    raw_params: dict,
    fallback_to_imdb: bool,
) -> str:
    """Build the composite blobstore key. Shared by /poster and the /p preset
    route so an equivalent preset and /poster render share one composite — no
    duplicate bytes. Server-side settings that affect the output but aren't URL
    params (detection thresholds, poster-selection policy, rating policy, render
    signature) are folded into the hash so changing them auto-busts stale
    composites."""
    if _cfg.TEXTLESS_TEXT_DETECTION:
        from text_detect import DETECT_RES_SIG
        _detect_sig = (
            f"|td={_cfg.PPOCR_BOX_THRESHOLD}:{_cfg.TEXTLESS_DETECTION_MAX_VOTES}:{DETECT_RES_SIG}"
        )
    else:
        _detect_sig = ""
    _poster_selection_sig = (
        f"|ps={_cfg.TMDB_POSTER_MIN_VOTES}:{_cfg.TMDB_POSTER_MAX_SCORE_DROP:g}"
    )
    _rating_policy_sig = f"|rp={_cfg.RATING_MIN_VOTES}:{int(fallback_to_imdb)}"
    _server_sig = "|server=" + _server_render_signature()
    _params_hash = hashlib.sha256(
        (
            "&".join(f"{k}={v}" for k, v in sorted(raw_params.items()))
            + _detect_sig
            + _poster_selection_sig
            + _rating_policy_sig
            + _server_sig
        ).encode()
    ).hexdigest()[:16]
    return f"{imdb_id}:{tmdb_id}:{media_type}:{_params_hash}"


def _load_configurator_html() -> str:
    global _configurator_etag
    html_path = os.path.join(os.path.dirname(__file__), "configurator.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()
        _configurator_etag = '"' + hashlib.md5(content.encode("utf-8")).hexdigest()[:16] + '"'
        return content
    except FileNotFoundError:
        _configurator_etag = '"missing"'
        return "<h1>Configurator not found</h1><p>Place configurator.html alongside main.py</p>"


@app.get("/health")
@app.get("/live")
async def health_check():
    """Lightweight liveness probe — no auth required, used by Docker healthcheck.
    /live is a Kubernetes-idiomatic alias; pair it with /ready for readiness."""
    return {"status": "ok"}


@app.get("/ready")
async def readiness_probe():
    """Readiness probe (ElfHosted fork) — checks the backends this replica
    depends on so a load balancer can pull it from rotation when a dependency
    is unhealthy, without killing the process. Each probe runs concurrently;
    sync probes go to the threadpool so a stalled backend can't pin the loop."""
    import cache as _cache_mod
    import blobstore as _blob_mod

    async def _coord_check() -> bool:
        if coord.BACKEND_KIND == "redis":
            from coordination import redis_backend as _rb
            return await _rb.aping()
        return coord.ping()

    db_ok, blob_ok, coord_ok = await asyncio.gather(
        asyncio.to_thread(_cache_mod.ping),
        asyncio.to_thread(_blob_mod.ping),
        _coord_check(),
    )
    body = {
        "storage":     {"kind": _cache_mod.BACKEND_KIND, "ok": db_ok},
        "coordinator": {"kind": coord.BACKEND_KIND,      "ok": coord_ok},
        "blobstore":   {"kind": _blob_mod.BACKEND_KIND,  "ok": blob_ok},
    }
    if not (db_ok and coord_ok and blob_ok):
        return JSONResponse(status_code=503, content={"status": "degraded", **body})
    return {"status": "ok", **body}


def _metrics_key_supplied(request: Request, access_key: str) -> str:
    """The key a /metrics caller supplied: an `Authorization: Bearer <key>`
    header, else the `access_key` query parameter.

    The header form exists so scrapers can read the key from a Secret
    (prometheus-operator's `authorization.credentials`) instead of carrying it
    in a URL, which would put it in the ServiceMonitor, git and access logs.
    """
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return access_key


@app.get("/metrics")
async def metrics_endpoint(request: Request, access_key: str = ""):
    """Prometheus exposition (ElfHosted fork). Optional shared-secret guard via
    METRICS_ACCESS_KEY (defence-in-depth when /metrics is publicly reachable —
    operators should still gate it at the ingress), supplied as a Bearer token
    or `?access_key=`. Aggregates across uvicorn workers when
    PROMETHEUS_MULTIPROC_DIR is set."""
    if _cfg.METRICS_ACCESS_KEY:
        supplied = _metrics_key_supplied(request, access_key)
        # Compare bytes: compare_digest raises TypeError on non-ASCII str,
        # which would turn a junk header into a 500 instead of a 403.
        if not hmac.compare_digest(supplied.encode("utf-8"), _cfg.METRICS_ACCESS_KEY.encode("utf-8")):
            raise HTTPException(status_code=403, detail="Unauthorized")
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, CollectorRegistry
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        payload = generate_latest(registry)
    else:
        payload = generate_latest()
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


@app.api_route("/llms.txt", methods=["GET", "HEAD"], response_class=Response)
async def get_llms_txt():
    """llmstxt.org convention (ElfHosted fork) — serve the LLM-readable site
    summary at /llms.txt. The file lives under static/ for editability but must
    be reachable at the root path per the spec. HEAD is supported so crawlers
    that probe before fetching don't get a 405."""
    path = os.path.join(BASE_DIR, "static", "llms.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/markdown")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="llms.txt not found")


@app.get("/stats")
async def stats(access_key: str = ""):
    """
    Operator diagnostics: cache row counts / sizes plus live runtime state
    (in-flight renders, background quality fetches, MDBList key cooldowns).
    Gated behind the access key when one is configured.
    """
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")

    now = asyncio.get_running_loop().time()
    keys = _cfg.SERVER_MDBLIST_KEYS
    mdblist_keys = []
    for i, k in enumerate(keys):
        cd = _mdblist_key_cooldown.get(k, 0.0)
        mdblist_keys.append({
            "index":         i + 1,
            "active":        i == (_mdblist_active_key_idx % len(keys)),
            "cooling_down":  now < cd,
            "cooldown_secs": max(0, round(cd - now)),
        })

    return {
        "cache":   get_cache_stats(),
        "runtime": {
            "renders_in_flight":        len(_render_inflight),
            "quality_fetches_in_flight": len(_quality_bg_inflight),
            "quality_source_backoff_secs": round(_quality_backoff_remaining(now)),
            "rating_fetches_in_flight":  len(_rating_fetch_inflight),
            "rating_backoff_titles":     len({imdb_id for imdb_id, _ in _rating_backoff}),
            "rating_backoff_entries":    len(_rating_backoff),
            "mdblist_keys":              mdblist_keys,
            "composite_cache_disabled":  _cfg.DISABLE_COMPOSITE_CACHE,
            "svg_logo_support":          svg_logo_supported(),
        },
    }


# TMDB genre name → id, used only by the debug canvas preview below.
_DEBUG_GENRE_IDS = {
    "Action": 28, "Adventure": 12, "Animation": 16, "Comedy": 35, "Crime": 80,
    "Documentary": 99, "Drama": 18, "Family": 10751, "Fantasy": 14, "History": 36,
    "Horror": 27, "Music": 10402, "Mystery": 9648, "Romance": 10749,
    "Sci-Fi": 878, "Thriller": 53, "War": 10752, "Western": 37,
}
_DEBUG_CANVAS_TTL = 300.0
_DEBUG_CANVAS_MAX_ENTRIES = 128
_debug_canvas_cache: dict[tuple[str, str, str, str, str], tuple[float, bytes]] = {}


@app.get("/debug/canvas")
async def debug_canvas(genre: str = "Action", title: str = "Sample Title",
                       style: str = "minimal", year: str = "2024",
                       score: str = "84", access_key: str = ""):
    """
    Render a no-art fallback card exactly as a poster-less title would: the genre
    fallback background (minimal or photoreal set) with the genre-aware title and
    the usual rating label composited on top.  Lets you eyeball any genre/style
    without hunting for a title that happens to lack poster art.
    """
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="Title too long")
    cache_key = (genre, title, style, year, score)
    now = asyncio.get_running_loop().time()
    cached = _debug_canvas_cache.get(cache_key)
    if cached is not None and now - cached[0] <= _DEBUG_CANVAS_TTL:
        return Response(
            content=cached[1], media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=300"},
        )
    gid = _DEBUG_GENRE_IDS.get(genre)
    canvas = _load_genre_background(genre, style)
    if canvas is None:
        canvas = _make_fallback_canvas([gid] if gid else None).convert("RGBA")
    cfg = RequestConfig()
    _score = int(score) if score.isdigit() else "—"
    img = build_poster(canvas, _score, genre, cfg, fallback_title=title,
                       release_year=(year or None), no_poster=True)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=90)
    jpeg = buf.getvalue()
    if len(_debug_canvas_cache) >= _DEBUG_CANVAS_MAX_ENTRIES:
        oldest = min(_debug_canvas_cache, key=lambda key: _debug_canvas_cache[key][0])
        _debug_canvas_cache.pop(oldest, None)
    _debug_canvas_cache[cache_key] = (now, jpeg)
    return Response(
        content=jpeg, media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=300"},
    )


@app.get("/debug/fallback-gallery", response_class=HTMLResponse)
async def fallback_gallery(style: str = "minimal", access_key: str = ""):
    """
    Self-contained gallery of every genre's no-art fallback card (live
    /debug/canvas renders), so an operator can review the fallback backgrounds +
    genre fonts at a glance and compare the minimal vs photoreal sets.  Gated
    behind the access key when configured.
    """
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized. Provide ?access_key=<key>")
    if style not in _GENRE_BG_STYLES:
        style = "minimal"
    _ak = f"&access_key={access_key}" if access_key else ""

    # Every genre that has a background (covers the full genre map + any future
    # additions), derived from the minimal set so the gallery is never stale.
    try:
        _genres = sorted(
            f[:-4] for f in os.listdir(os.path.join(_GENRE_BG_DIR, "minimal"))
            if f.lower().endswith(".png") and f[:-4].lower() != "default"
        )
    except OSError:
        _genres = sorted(_DEBUG_GENRE_IDS)

    tiles = "".join(
        f'<figure><img loading="lazy" src="/debug/canvas?genre={g}'
        f'&title={g.replace(" ", "+")}&style={style}{_ak}" alt="{g}">'
        f'<figcaption>{g}</figcaption></figure>'
        for g in _genres
    )
    _tabs = "".join(
        f'<a class="{"on" if s == style else ""}" '
        f'href="/debug/fallback-gallery?style={s}{_ak}">{s.capitalize()}</a>'
        for s in _GENRE_BG_STYLES
    )
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fallback art preview</title>
<style>
  body {{ margin:0; background:#0e0e10; color:#e8e8ea;
         font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
  header {{ padding:18px 20px; border-bottom:1px solid #2a2a2e;
           display:flex; align-items:center; gap:16px; flex-wrap:wrap; }}
  h1 {{ font-size:18px; margin:0; }} p {{ color:#9a9aa0; margin:0; font-size:13px; }}
  .tabs a {{ display:inline-block; padding:5px 12px; margin-right:6px; border-radius:8px;
            font-size:13px; text-decoration:none; color:#c7c7cc; background:#1c1c20;
            border:1px solid #2a2a2e; }}
  .tabs a.on {{ background:#3a3a44; color:#fff; border-color:#4a4a56; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr));
           gap:16px; padding:20px; }}
  figure {{ margin:0; }}
  img {{ width:100%; border-radius:10px; display:block; background:#1a1a1d; }}
  figcaption {{ text-align:center; padding-top:8px; font-size:13px; color:#c7c7cc; }}
</style></head><body>
<header>
  <h1>Fallback art preview</h1>
  <div class="tabs">{_tabs}</div>
  <p>Live no-art render for every genre — {len(_genres)} genres, "{style}" set.</p>
</header>
<div class="grid">{tiles}</div></body></html>"""
    return HTMLResponse(content=html)


@app.get("/", response_class=HTMLResponse)
async def get_configurator(request: Request, access_key: str = "", reload: str = ""):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized. Provide ?access_key=<key>")
    # ?reload=1 re-reads configurator.html from disk — useful while iterating on
    # the UI without restarting the container.  Gated on the access key so it's
    # not a public DoS vector via disk re-reads.
    global _configurator_html
    if reload:
        _configurator_html = _load_configurator_html()
        logger.info("Configurator HTML reloaded from disk")

    if _configurator_html is None:
        _load_configurator_html()  # populates the global

    # 304 short-circuit when the browser's cached copy still matches —
    # saves the 130 KB body re-download on every navigation while still
    # forcing a fresh fetch as soon as the file's contents change.
    _cache_headers = {
        "Cache-Control": "no-cache, must-revalidate",
        "ETag":          _configurator_etag or '""',
    }
    if (
        _configurator_etag
        and request.headers.get("if-none-match") == _configurator_etag
    ):
        return Response(status_code=304, headers=_cache_headers)

    return HTMLResponse(
        content=_configurator_html or _load_configurator_html(),
        headers=_cache_headers,
    )


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------

def _gate_anonymous_tmdb_proxy(access_key: str) -> None:
    """ElfHosted fork: when ACCESS_KEY is configured, /search and /resolve-imdb
    are anonymous ONLY if PRESET_ENABLED (the public preset flow needs the title
    picker). On instances without presets, the original access_key gate stays so
    the server TMDB key isn't exposed to unthrottled public traffic against a
    backend with no anonymous user-facing endpoint."""
    if not _cfg.ACCESS_KEY:
        return                                       # nothing gated
    if _cfg.PRESET_ENABLED:
        return                                       # public preset flow: anonymous OK
    if access_key and hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        return                                       # authenticated tenant
    raise HTTPException(status_code=403, detail="Unauthorized")


async def _anonymous_tmdb_rate_limit(tmdb_key: str, access_key: str) -> None:
    """ElfHosted fork: throttle the TMDB-proxy endpoints, which become anonymous
    when PRESET_ENABLED. Three buckets, in priority order:
      * caller tmdb_key → per-key bucket (RATE_LIMIT_RPS; their own quota is the
        real backstop, so skipped when RATE_LIMIT_RPS=0),
      * valid access_key → per-tenant bucket (RATE_LIMIT_RPS),
      * anonymous → shared "anonymous" bucket sized by ANONYMOUS_TMDB_RPS,
        independent of RATE_LIMIT_RPS so the floor applies even when the
        operator never configured a poster rate limit."""
    if tmdb_key:
        if _cfg.RATE_LIMIT_RPS <= 0:
            return
        tenant_id = hashlib.sha256(tmdb_key.encode("utf-8")).hexdigest()[:16]
        rps = _cfg.RATE_LIMIT_RPS
    elif _cfg.ACCESS_KEY and access_key and hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        if _cfg.RATE_LIMIT_RPS <= 0:
            return
        tenant_id = hashlib.sha256(access_key.encode("utf-8")).hexdigest()[:16]
        rps = _cfg.RATE_LIMIT_RPS
    else:
        if _cfg.ANONYMOUS_TMDB_RPS <= 0:
            return
        tenant_id = "anonymous"
        rps = _cfg.ANONYMOUS_TMDB_RPS
    allowed, retry_after = await coord.check_rate_limit(tenant_id, rps)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({rps} req/s)",
            headers={"Retry-After": str(retry_after)},
        )


@app.get("/search")
async def search_proxy(
    q: str,
    tmdb_key: str = "",
    access_key: str = "",
):
    _gate_anonymous_tmdb_proxy(access_key)
    await _anonymous_tmdb_rate_limit(tmdb_key, access_key)
    if len(q) > 200:
        raise HTTPException(status_code=400, detail="Query too long")

    effective_key = _resolve_tmdb_key(tmdb_key)
    if not effective_key:
        raise HTTPException(status_code=400, detail="No TMDB API key available")

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    resp = await _HTTP_CLIENT.get(
        "https://api.themoviedb.org/3/search/multi",
        params={
            "api_key": effective_key,
            "query": q,
            "include_adult": "false",
            "page": "1",
        },
    )
    return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)


@app.get("/resolve-imdb")
async def resolve_imdb(
    tmdb_id: str,
    type: str = "movie",
    tmdb_key: str = "",
    access_key: str = "",
):
    _gate_anonymous_tmdb_proxy(access_key)
    await _anonymous_tmdb_rate_limit(tmdb_key, access_key)

    _check_tmdb_id(tmdb_id)
    _check_type(type)

    effective_key = _resolve_tmdb_key(tmdb_key)
    if not effective_key:
        raise HTTPException(status_code=400, detail="No TMDB API key available")

    endpoint = (
        f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids"
        if type == "tv"
        else f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids"
    )

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    resp = await _HTTP_CLIENT.get(endpoint, params={"api_key": effective_key})
    return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)


# ---------------------------------------------------------------------------
# Static-preset overload moat (ElfHosted fork)
# ---------------------------------------------------------------------------
# /p/{preset}/{type}/{imdb_id}.jpg — anonymous, CDN-cacheable, no per-user
# keys. Presets ship in presets.py (mirroring the configurator gallery). The
# render reuses /poster's helpers but enforces the moat invariants: rating,
# quality, and burned-in-text detection are used ONLY if already cached — an
# anonymous hit never fans out to MDBList/AIOStreams and never triggers a
# foreground OCR scan. An incomplete (not-fully-determined) render is served
# with a short Cache-Control and is NOT persisted, so it can't poison the CDN
# with a long TTL and will be replaced once the title is warmed by a paid
# /poster hit or the background detector. A fully-determined preset render
# shares its composite cache key with the equivalent /poster render.

_PRESET_ROUTE_VALID_TYPES = frozenset({"movie", "tv"})
_PRESET_INCOMPLETE_TTL = 60   # short Cache-Control (s) for not-yet-warm renders


@app.get("/p/{preset}/{type}/{imdb_id}.jpg")
async def get_preset_poster(preset: str, type: str, imdb_id: str):
    if not _cfg.PRESET_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    preset_params = get_preset(preset)
    if preset_params is None:
        raise HTTPException(status_code=404, detail=f"Unknown preset '{preset}'")

    # AIOMetadata fills {type} with 'series' for TV; fold to the canonical 'tv'.
    if type == "series":
        type = "tv"
    if type not in _PRESET_ROUTE_VALID_TYPES:
        raise HTTPException(status_code=400, detail="Invalid type (movie|tv)")
    _check_imdb_id(imdb_id)

    effective_tmdb_key = _resolve_tmdb_key("")
    if not effective_tmdb_key:
        raise HTTPException(status_code=503, detail="Server TMDB key not configured")

    # All anonymous preset traffic shares one operator-wide "preset" bucket so a
    # runaway integration can't drag /poster down.
    if _cfg.RATE_LIMIT_RPS > 0:
        allowed, retry_after = await coord.check_rate_limit("preset", _cfg.RATE_LIMIT_RPS)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({_cfg.RATE_LIMIT_RPS} req/s)",
                headers={"Retry-After": str(retry_after)},
            )

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    client = _HTTP_CLIENT

    tmdb_id = await resolve_imdb_to_tmdb(client, imdb_id, effective_tmdb_key, type)
    if not tmdb_id:
        raise HTTPException(status_code=404, detail="Title not found on TMDB")

    raw_params = dict(preset_params)
    rcfg = build_request_config(raw_params)

    # Same key shape as /poster → a preset and an equivalent /poster render
    # share one composite.
    final_cache_key = _composite_cache_key(
        imdb_id, tmdb_id, type, raw_params, rcfg.fallback_to_imdb
    )

    def _preset_header(persisted: bool) -> dict:
        ttl = _cfg.PRESET_CDN_CACHE_TTL if persisted else _PRESET_INCOMPLETE_TTL
        return {"Cache-Control": f"public, max-age={ttl}"} if ttl > 0 else {}

    # Cache hit: prefer a 302 to the CDN when a public URL is configured,
    # else serve the bytes inline.
    cdn_url = get_cached_final_poster_url(final_cache_key)
    if cdn_url is not None:
        if await is_cached_final_poster_fresh(final_cache_key):
            logger.info(f"Preset {preset} cache hit (CDN redirect) for {final_cache_key}")
            resp = Response(status_code=302)
            resp.headers["Location"] = cdn_url
            for k, v in _preset_header(True).items():
                resp.headers[k] = v
            return resp
    else:
        cached_jpeg = await get_cached_final_poster(final_cache_key)
        if cached_jpeg is not None:
            logger.info(f"Preset {preset} cache hit (inline) for {final_cache_key}")
            return Response(content=cached_jpeg, media_type="image/jpeg",
                            headers=_preset_header(True))

    # Rating + quality: cached-only. None means "never queried" (suppress
    # persistence); [] means "queried, nothing available" (safe to persist).
    cached_rating = get_cached_rating(imdb_id)
    cached_release_date = cached_rating[2] if cached_rating is not None else None
    cached_quality = get_cached_quality(imdb_id, cached_release_date)
    quality_tokens = cached_quality or []
    # Any non-hidden badge mode reads quality_tokens; persisting before quality
    # is cached would lock in an empty/grey badge for the long preset TTL.
    wants_badges = rcfg.badge_display_mode != 0
    quality_missing = wants_badges and cached_quality is None

    try:
        genre_ids, is_textless, logos, release_year, title, poster_path, backdrop_path, tmdb_data = (
            await fetch_poster_metadata(client, tmdb_id, effective_tmdb_key, type)
        )

        # TMDB-derived genre fallback — the preset path never calls MDBList, so
        # for never-warmed titles derive the genre label from TMDB genre_ids
        # (same logic /poster uses) rather than rendering "Unknown".
        if cached_rating is not None:
            (
                ratings_dict, cached_genre, _crd, award_wins, award_noms, _af,
                festival_label, age_rating, is_cult, is_true_story, is_metacritic,
            ) = cached_rating
            weights = (
                (rcfg.tv_weights or _cfg.TV_WEIGHTS) if type in ("tv", "series")
                else (rcfg.movie_weights or _cfg.MOVIE_WEIGHTS)
            )
            score = (
                calculate_weighted_score(ratings_dict, weights,
                                         fallback_to_imdb=rcfg.fallback_to_imdb)
                if isinstance(ratings_dict, dict) else "N/A"
            )
            genre = cached_genre or "Unknown"
            rel = cached_release_date
        else:
            ratings_dict, score, genre, rel = {}, "N/A", "Unknown", None
            award_wins, award_noms = [], []
            festival_label = age_rating = None
            is_cult = is_true_story = is_metacritic = False

        if genre == "Unknown" and genre_ids:
            _gid_set = set(genre_ids)
            for _gid in _cfg.GENRE_PRIORITY:
                if _gid in _gid_set and _cfg.GENRE_MAP.get(_gid):
                    genre = _cfg.GENRE_MAP[_gid]
                    break

        # No-poster ladder: poster → backdrop → gradient canvas.
        is_no_poster = poster_path is None
        use_backdrop = is_no_poster and backdrop_path is not None
        if use_backdrop:
            image_coro = fetch_backdrop_image(client, tmdb_id, backdrop_path)
        elif is_no_poster:
            image_coro = _resolved(_make_fallback_canvas(genre_ids))
        else:
            image_coro = fetch_poster_image(client, tmdb_id, type, poster_path)

        # Burned-in-text detection: CACHED ONLY. Never scan in the foreground on
        # an anonymous hit. If a textless poster's detection isn't cached, the
        # render is "undetermined" (it might differ from what /poster produces
        # once OCR runs), so we don't persist it and we queue a background scan
        # to warm it — matching /poster's det cache key exactly so the result
        # is shared.
        _suppress_overlay = False
        _ocr_undetermined = False
        _scan_selected = (
            _cfg.TEXTLESS_TEXT_DETECTION and is_textless and not is_no_poster
        )
        if _scan_selected:
            from text_detect import DETECT_RES_SIG
            _det_src = f"ps:{poster_path}"
            _det_key = f"{_det_src}|conf={_cfg.PPOCR_BOX_THRESHOLD}:{DETECT_RES_SIG}"
            _det_cached = get_cached_text_detection(_det_key)
            if _det_cached is None:
                _ocr_undetermined = True
                _queue_background_text_detection(_DeferredTextDetection(
                    cache_key=_det_key,
                    image_cache_key=f"{type}_{tmdb_id}_{poster_path.strip('/')}",
                    title=[title] if title else [],
                    source="poster",
                    tmdb_id=tmdb_id,
                    media_type=type,
                    image_path=poster_path,
                    vote_count=tmdb_data.get("vote_count"),
                    source_key=_det_src,
                ))
            else:
                _suppress_overlay = bool(_det_cached)

        will_persist = (
            cached_rating is not None and not quality_missing and not _ocr_undetermined
        )

        # Coalesce concurrent uncached renders — only on the will-persist path
        # (an incomplete render must not be shared under the long preset TTL).
        _render_fut: "asyncio.Future[bytes] | None" = None
        if will_persist:
            _existing_fut = _render_inflight.get(final_cache_key)
            if _existing_fut is not None:
                logger.info(f"Preset {preset} coalescing on {final_cache_key}")
                try:
                    return Response(content=await _existing_fut, media_type="image/jpeg",
                                    headers=_preset_header(True))
                except Exception:
                    pass   # in-flight render failed; fall through and try ourselves
            _render_fut = asyncio.get_running_loop().create_future()
            _render_fut.add_done_callback(
                lambda f: f.exception() if not f.cancelled() and f.exception() else None
            )
            _render_inflight[final_cache_key] = _render_fut

        _overlay_logo = (
            is_textless and not is_no_poster and not rcfg.textless and not _suppress_overlay
        )
        image, logo, trending_rank = await asyncio.gather(
            image_coro,
            fetch_logo(
                client, logos, rcfg.logo_language,
                imdb_id=imdb_id, original_language=tmdb_data.get("original_language"),
                logo_priority=rcfg.logo_priority,
            ) if _overlay_logo else _resolved(None),
            fetch_trending_rank(client, tmdb_id, effective_tmdb_key, type),
        )

        discovery_meta = extract_discovery_meta(
            tmdb_data=tmdb_data, media_type=type,
            award_wins=award_wins, award_noms=award_noms,
            trending_rank=trending_rank, release_date=rel, keywords=[],
            festival_label_override=festival_label,
            is_cult_override=is_cult, is_true_story_override=is_true_story,
            is_metacritic_override=is_metacritic,
            is_digital_release_override=is_digital_release(imdb_id),
        )

        _bp_args = dict(
            logo=logo if (_overlay_logo and logo) else None,
            fallback_title=(
                title if is_no_poster
                else (title if is_textless and not logo and not rcfg.textless
                      and not _suppress_overlay else None)
            ),
            discovery_meta=discovery_meta,
            quality_tokens=quality_tokens,
            release_year=release_year,
            age_rating=age_rating,
            no_poster=is_no_poster,
        )

        def _composite_and_encode() -> bytes:
            result = build_poster(image, score, genre, rcfg, **_bp_args)
            buf = io.BytesIO()
            result.convert("RGB").save(buf, format="JPEG", quality=_cfg.JPEG_QUALITY)
            return buf.getvalue()

        global _render_semaphore
        if _render_semaphore is None:
            _render_semaphore = asyncio.Semaphore(_cfg.RENDER_CONCURRENCY)
        try:
            if _cfg.RENDER_QUEUE_TIMEOUT > 0:
                await asyncio.wait_for(_render_semaphore.acquire(),
                                       timeout=_cfg.RENDER_QUEUE_TIMEOUT)
            else:
                await _render_semaphore.acquire()
        except asyncio.TimeoutError:
            _metrics.render_saturated_total.inc()
            logger.warning(f"Render queue saturated (cap={_cfg.RENDER_CONCURRENCY}); "
                           f"503 for preset={preset} imdb={imdb_id}")
            if _render_fut is not None and not _render_fut.done():
                _render_fut.set_exception(HTTPException(status_code=503))
                _render_inflight.pop(final_cache_key, None)
            raise HTTPException(status_code=503, detail="Server saturated — try again shortly",
                                headers={"Retry-After": "5"})
        try:
            _metrics.render_inflight.inc()
            with _metrics.render_duration_seconds.time():
                img_bytes = await asyncio.get_running_loop().run_in_executor(
                    None, _composite_and_encode)
        finally:
            _metrics.render_inflight.dec()
            _render_semaphore.release()

        if will_persist:
            await set_cached_final_poster(final_cache_key, img_bytes)
            logger.info(f"Preset {preset} rendered + cached {final_cache_key}")
            if _render_fut is not None and not _render_fut.done():
                _render_fut.set_result(img_bytes)
        else:
            logger.info(
                f"Preset {preset} rendered (not persisted: "
                f"rating_cached={cached_rating is not None}, "
                f"quality_missing={quality_missing}, ocr_undetermined={_ocr_undetermined}) "
                f"{final_cache_key}"
            )
        return Response(content=img_bytes, media_type="image/jpeg",
                        headers=_preset_header(will_persist))
    finally:
        # Clear our coalescing slot. Waiters already hold the future object, so
        # popping after a successful set_result is safe; on an error path we
        # fail the future first so waiters don't hang.
        _f = _render_inflight.get(final_cache_key)
        if _f is not None:
            if not _f.done():
                _f.set_exception(HTTPException(status_code=500))
            _render_inflight.pop(final_cache_key, None)


# ---------------------------------------------------------------------------
# Poster endpoint
# ---------------------------------------------------------------------------

@app.get("/poster")
async def get_poster(
    request: Request,
    tmdb_id: str,
    imdb_id: str,
    type: str = "movie",
    quality: str = "",
    season: int = 1,
    episode: int = 1,
    access_key: str = "",
    mdblist_key: str = "",
    tmdb_key: str = "",
    show_award_sash: str | None = None,
    badge_display_mode: str | None = None,
    show_quality_badges: str | None = None,
    rating_display_mode: str | None = None,
    accent_bar_font_size_ratio: str | None = None,
    numeric_score_font_size_ratio: str | None = None,
    accent_bar_y_offset: str | None = None,
    numeric_score_y_offset: str | None = None,
    minimalist_mode_font_size_ratio: str | None = None,
    minimalist_mode_font_x_offset: str | None = None,
    minimalist_mode_font_y_offset: str | None = None,
    score_glow_threshold: str | None = None,
    score_glow_blur: str | None = None,
    score_glow_alpha: str | None = None,
    logo_max_w_ratio: str | None = None,
    logo_max_h_ratio: str | None = None,
    logo_bottom_ratio: str | None = None,
    badge_height: str | None = None,
    badge_gap: str | None = None,
    badge_anchor_x: str | None = None,
    badge_anchor_y: str | None = None,
    movie_weights: str | None = None,
    tv_weights: str | None = None,
    logo_language: str | None = None,
    sash_priority: str | None = None,
    muted: str | None = None,
    textless: str | None = None,
    score_color_mode: str | None = None,
    debug: str | None = None,
    nocache: str | None = None,
):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized, your access key is not valid for this instance.")

    # ElfHosted fork: per-tenant rate limit. Tenant identity is derived from
    # the user-supplied key when the user brings their own (so a noisy tenant
    # throttles itself without dragging others down); requests on the
    # operator's keys share the "operator" bucket. Disabled when
    # RATE_LIMIT_RPS=0 (upstream behaviour).
    if _cfg.RATE_LIMIT_RPS > 0:
        if tmdb_key:
            tenant_id = hashlib.sha256(tmdb_key.encode("utf-8")).hexdigest()[:16]
        elif mdblist_key:
            tenant_id = hashlib.sha256(mdblist_key.encode("utf-8")).hexdigest()[:16]
        else:
            tenant_id = "operator"
        allowed, retry_after = await coord.check_rate_limit(tenant_id, _cfg.RATE_LIMIT_RPS)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({_cfg.RATE_LIMIT_RPS} req/s)",
                headers={"Retry-After": str(retry_after)},
            )

    _check_tmdb_id(tmdb_id)
    _check_imdb_id(imdb_id)
    _check_type(type)

    # -----------------------------------------------------------------------
    # Single-user mode: check for a cached final poster first.
    # The cache key includes imdb_id and type; quality is intentionally
    # excluded because in single-user mode the quality tokens come from
    # AIOStreams (not from query params) and are themselves cached per-title.
    # If the caller passes an explicit quality= override this bypass is
    # skipped so they always get the exact poster they asked for.
    # -----------------------------------------------------------------------
    effective_tmdb_key    = _resolve_tmdb_key(tmdb_key)
    effective_mdblist_key = _resolve_mdblist_key(mdblist_key)

    if not effective_tmdb_key:
        raise HTTPException(
            status_code=400,
            detail=(
                "No TMDB API key available. Either provide tmdb_key= as a query parameter "
                "or configure the TMDB_API_KEY environment variable on the server."
            ),
        )

    raw_params = {
        k: v for k, v in request.query_params.items()
        if k not in (
            "tmdb_id", "imdb_id", "mdblist_key", "tmdb_key", "type",
            "quality", "season", "episode", "access_key", "debug", "nocache",
        )
    }
    rcfg = build_request_config(raw_params)

    # Operator force-refresh: ?nocache=1 skips the composite cache READ so a fresh
    # render is produced (and re-cached), letting an operator invalidate a single
    # title without flushing the whole cache.  Only honoured when an ACCESS_KEY is
    # configured (and therefore already validated above) so open instances can't
    # be made to burn CPU on forced re-renders.
    _force_refresh = bool(
        nocache and nocache.strip().lower() in ("1", "true", "yes") and _cfg.ACCESS_KEY
    )

    # ------------------------------------------------------------------
    # Final poster cache — keyed on imdb_id, type, and a short hash of
    # all rendering parameters so different visual configs don't collide.
    # Skipped when an explicit quality= override is supplied (one-off).
    # ------------------------------------------------------------------
    if not quality and not _cfg.DISABLE_COMPOSITE_CACHE:
        final_cache_key = _composite_cache_key(
            imdb_id, tmdb_id, type, raw_params, rcfg.fallback_to_imdb
        )
        if _force_refresh:
            logger.info(f"Force refresh (nocache) for {final_cache_key} — bypassing cache read")
        # ElfHosted fork: when a CDN public URL is configured (OBJECT_STORE_PUBLIC_URL),
        # serve cache hits by 302 to the CDN instead of pulling the bytes through
        # the app pod + an S3 GET on every warm request. Only the cheap metadata
        # freshness probe runs here — no blob fetch. Falls through to the inline
        # path when no public URL is set or the row isn't fresh.
        if not _force_refresh:
            _cdn_url = get_cached_final_poster_url(final_cache_key)
            if _cdn_url is not None and await is_cached_final_poster_fresh(final_cache_key):
                logger.info(f"Final poster cache hit (CDN redirect) for {final_cache_key}")
                _redir = Response(status_code=302)
                _redir.headers["Location"] = _cdn_url
                if _cfg.CDN_CACHE_TTL > 0:
                    _redir.headers["Cache-Control"] = f"public, max-age={_cfg.CDN_CACHE_TTL}"
                return _redir
        cached_jpeg = None if _force_refresh else await get_cached_final_poster(final_cache_key)
        if cached_jpeg is not None:
            logger.info(f"Final poster cache hit for {final_cache_key}")
            etag = f'"{final_cache_key}"'
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304)
            _hit_resp = Response(content=cached_jpeg, media_type="image/jpeg")
            _hit_resp.headers["ETag"] = etag
            # This path is only reached when composite caching is enabled, so a
            # no-store branch would be dead here — CDN TTL is the only option.
            if _cfg.CDN_CACHE_TTL > 0:
                _hit_resp.headers["Cache-Control"] = f"public, max-age={_cfg.CDN_CACHE_TTL}"
            return _hit_resp
    else:
        final_cache_key = None

    # ------------------------------------------------------------------
    # Request coalescing: if another request in this worker is already
    # rendering the same poster, await its result instead of duplicating
    # the pipeline.  Quality-override requests (final_cache_key=None) are
    # always rendered independently.
    # ------------------------------------------------------------------
    _render_fut: "asyncio.Future[bytes] | None" = None
    if final_cache_key is not None:
        _existing_fut = _render_inflight.get(final_cache_key)
        if _existing_fut is not None:
            logger.info(f"Coalescing request for {final_cache_key}")
            try:
                _coal_resp = Response(content=await _existing_fut, media_type="image/jpeg")
                _coal_resp.headers["ETag"] = f'"{final_cache_key}"'
                # Coalescing only happens when caching is on (final_cache_key set),
                # so no-store can't apply here — CDN TTL only.
                if _cfg.CDN_CACHE_TTL > 0:
                    _coal_resp.headers["Cache-Control"] = f"public, max-age={_cfg.CDN_CACHE_TTL}"
                return _coal_resp
            except Exception:
                # The in-flight render failed; fall through and try ourselves.
                pass
        _render_fut = asyncio.get_running_loop().create_future()
        # Suppress asyncio's "Future exception was never retrieved" warning when
        # the render fails and no other request is coalesced onto this future.
        _render_fut.add_done_callback(
            lambda f: f.exception() if not f.cancelled() and f.exception() else None
        )
        _render_inflight[final_cache_key] = _render_fut

    # Declare globals that are both read and written in this function so Python
    # doesn't complain about use-before-global-declaration.
    global _mdblist_active_key_idx

    cached_rating = get_cached_rating(imdb_id)

    if cached_rating is not None:
        (
            cached_ratings_dict,
            cached_genre,
            cached_release_date,
            cached_award_wins,
            cached_award_noms,
            cached_awards_fetched,
            cached_festival_label,
            cached_age_rating,
            cached_is_cult,
            cached_is_true_story,
            cached_is_metacritic,
        ) = cached_rating
    else:
        cached_ratings_dict   = None
        cached_genre          = None
        cached_release_date   = None
        cached_award_wins     = []
        cached_award_noms     = []
        cached_awards_fetched = False
        cached_festival_label = None
        cached_age_rating     = None
        cached_is_cult        = False
        cached_is_true_story  = False
        cached_is_metacritic  = False

    release_date_for_quality_ttl = cached_release_date
    rating_already_cached        = cached_rating is not None

    # ------------------------------------------------------------------
    # Rating fetch coalescing + back-off
    #
    # Goal: ensure at most one MDBList call per imdb_id per worker at a
    # time, and suppress repeated failures with key-scoped cooldowns.
    #
    # Back-off check: if a recent fetch failed, skip that title-key pair
    # until its escalating retry delay expires.
    #
    # Coalescing: if another coroutine in this worker is already fetching
    # the same imdb_id, wait for its asyncio.Event, then re-read the DB.
    # If it succeeded we get the cached data for free; if it failed we
    # re-check the back-off (now set by the other coroutine) before
    # deciding whether to attempt our own call.
    # ------------------------------------------------------------------
    _rating_event_to_set: asyncio.Event | None = None
    _rating_backoff_active = False  # set when backoff nullifies the key; used to suppress final-poster caching
    _mdblist_unavailable_reason = "no API key configured"

    if not rating_already_cached and effective_mdblist_key:
        _loop_now = asyncio.get_running_loop().time()

        # ElfHosted fork: fleet-wide MDBList cooldown. When any replica has been
        # 429'd recently, every replica backs off — cross-worker via the
        # coordination layer (Redis), or per-process on the in-process default
        # (where it overlaps the per-key cooldown below). Suppresses the fetch
        # and the composite persist so an N/A score isn't locked in.
        if await coord.is_backoff_active(
            coord.NS_MDBLIST_GLOBAL_COOLDOWN, coord.MDBLIST_GLOBAL_KEY
        ):
            logger.debug(f"Rating fetch for {imdb_id} skipped (fleet-wide MDBList cooldown)")
            effective_mdblist_key = None
            _rating_backoff_active = True
            _mdblist_unavailable_reason = "fleet-wide MDBList cooldown active"

        # Per-key cooldown: configured server keys may rotate; request-supplied
        # keys remain isolated and simply wait for their own cooldown to expire.
        if effective_mdblist_key and _loop_now < _mdblist_key_cooldown.get(effective_mdblist_key, 0.0):
            _cooling_key = effective_mdblist_key
            _replacement = _next_mdblist_server_key(_cooling_key, _loop_now)
            if _replacement is not None:
                effective_mdblist_key = _replacement
                logger.info(
                    f"MDBList key rotated to key #{_mdblist_active_key_idx + 1} for {imdb_id}"
                )
            else:
                _remaining = _mdblist_key_cooldown.get(_cooling_key, 0.0) - _loop_now
                logger.debug(
                    f"Rating fetch for {imdb_id} skipped "
                    f"(selected MDBList key cooling down; {_remaining:.0f}s remaining)"
                )
                effective_mdblist_key = None
                _rating_backoff_active = True
                _mdblist_unavailable_reason = "selected key is cooling down"

        # Per-title and key backoff (network failures, or this title-key pair's last 429).
        if effective_mdblist_key:
            _retry_key = _rating_retry_key(imdb_id, effective_mdblist_key)
            _backoff_until = _rating_backoff.get(_retry_key)
            if _backoff_until is not None:
                if _loop_now < _backoff_until:
                    logger.debug(f"Rating fetch for {imdb_id} skipped (MDBList back-off active for selected key)")
                    effective_mdblist_key = None
                    _rating_backoff_active = True
                    _mdblist_unavailable_reason = "selected key is in back-off for this title"
                else:
                    del _rating_backoff[_retry_key]       # expired — allow a fresh attempt
                    _rating_fail_count.pop(_retry_key, None)  # reset escalation for clean slate

    if not rating_already_cached and effective_mdblist_key:
        _inflight_event = _rating_fetch_inflight.get(imdb_id)
        if _inflight_event is not None:
            # Another coroutine is mid-fetch — wait and piggyback on its result.
            logger.info(f"Rating fetch coalesced for {imdb_id} — awaiting in-flight fetch")
            await _inflight_event.wait()
            _refreshed = get_cached_rating(imdb_id)
            if _refreshed is not None:
                (
                    cached_ratings_dict,
                    cached_genre,
                    cached_release_date,
                    cached_award_wins,
                    cached_award_noms,
                    cached_awards_fetched,
                    cached_festival_label,
                    cached_age_rating,
                    cached_is_cult,
                    cached_is_true_story,
                    cached_is_metacritic,
                ) = _refreshed
                rating_already_cached        = True
                release_date_for_quality_ttl = cached_release_date
                logger.info(f"Rating coalesce succeeded for {imdb_id} — using cached result")
            else:
                # The other fetch also failed; re-check back-off it may have set.
                _loop_now2    = asyncio.get_running_loop().time()
                _retry_key2 = _rating_retry_key(imdb_id, effective_mdblist_key)
                _backoff_now2 = _rating_backoff.get(_retry_key2)
                if _backoff_now2 is not None and _loop_now2 < _backoff_now2:
                    logger.debug(
                        f"Rating fetch for {imdb_id} suppressed after coalescence (back-off active)"
                    )
                    effective_mdblist_key = None
                    _rating_backoff_active = True
                    _mdblist_unavailable_reason = "selected key is in back-off for this title"
        else:
            # First request for this imdb_id — claim the fetch slot.
            _rating_event_to_set              = asyncio.Event()
            _rating_fetch_inflight[imdb_id]   = _rating_event_to_set

    # Quality tokens — cache checked exactly once here; fetch fn only writes.
    if quality:
        quality_tokens = parse_quality(quality)
        cached_tokens  = None
    else:
        cached_tokens  = get_cached_quality(imdb_id, release_date_for_quality_ttl)
        quality_tokens = cached_tokens or []

    # A quality source is available when the server has AIOStreams configured,
    # or QUALITY_SOURCE=scraper with a valid SCRAPER_URL.
    _has_quality_source = (
        bool(_cfg.AIOSTREAMS_URL and _cfg.AIOSTREAMS_AUTH)
        or (_cfg.QUALITY_SOURCE == "scraper" and bool(_cfg.SCRAPER_URL))
    )
    _quality_cooldown_active = _has_quality_source and _quality_backoff_remaining() > 0
    quality_needs_fetch = (
        rcfg.badge_display_mode in (1, 2, 4, 5)
        and not quality
        and cached_tokens is None
        and _has_quality_source
        and not _quality_cooldown_active
    )

    quality_pending = bool(_quality_cooldown_active and cached_tokens is None)
    if quality_needs_fetch and not rcfg.wait_for_quality:
        # Fire-and-forget background fetch — poster is served immediately
        # without badges; the cache will be warm on the next request.
        if imdb_id not in _quality_bg_inflight:
            _quality_bg_inflight.add(imdb_id)
            asyncio.create_task(
                _background_quality_fetch(
                    imdb_id, type, season, episode,
                    release_date_for_quality_ttl,
                )
            )
            logger.info(f"Quality fetch deferred to background for {imdb_id}")
        else:
            logger.info(f"Quality background fetch already in progress for {imdb_id}")
        quality_needs_fetch = False
        quality_pending = True

    if not rating_already_cached and not effective_mdblist_key:
        logger.warning(
            f"MDBList unavailable for {imdb_id}: {_mdblist_unavailable_reason} — "
            "poster will be served without rating/award data."
        )

    effective_movie_weights = rcfg.movie_weights or _cfg.MOVIE_WEIGHTS
    effective_tv_weights    = rcfg.tv_weights    or _cfg.TV_WEIGHTS

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    client = _HTTP_CLIENT

    global _active_poster_renders
    _active_poster_renders += 1
    try:
        genre_ids, is_textless, logos, release_year, title, poster_path, backdrop_path, tmdb_data = (
            await fetch_poster_metadata(client, tmdb_id, effective_tmdb_key, type, rcfg.logo_language)
        )
        _text_titles = tuple(dict.fromkeys(
            value for value in (title, tmdb_data.get("original_title")) if value
        ))

        # Resolve genre string from TMDB genre_ids immediately — this is always
        # available regardless of MDBlist status, so we can use it as a reliable
        # fallback if the rating fetch fails or is skipped entirely.
        _gid_set = set(genre_ids)
        _tmdb_genre = "Unknown"
        for _gid in _cfg.GENRE_PRIORITY:
            if _gid in _gid_set:
                _candidate = _cfg.GENRE_MAP.get(_gid, "")
                if _candidate:
                    _tmdb_genre = _candidate
                    break

        # Backdrop fallback: when no null-language textless poster exists, use
        # the landscape backdrop cropped to portrait.  Backdrops are almost always
        # textless by design and TMDB coverage is near-universal, so this recovers
        # the vast majority of titles that would otherwise fall back to a textual
        # poster — OR, when no poster art exists at all, a genre-tinted canvas.
        #   poster missing entirely  → prefer backdrop over the canvas
        #   poster exists with text  → prefer backdrop over the text-burned poster
        _use_backdrop = bool(backdrop_path) and (poster_path is None or not is_textless)
        if _use_backdrop:
            logger.info(f"No textless poster for {tmdb_id} — using backdrop crop as portrait fallback")
            is_textless = True          # backdrop is textless; enable logo compositing

        # Original-art mode: serve a TMDB poster (title baked into the art) as-is.
        # Override the textless/backdrop selection, force is_textless=False so the
        # existing gates skip our logo, text detection and the backdrop rescue.
        # Poster language reuses logo_priority (there's no text fallback here).
        # "native" is the REQUEST's logo_language (selected from poster_langs at
        # render time, so it isn't baked to whatever language first cached this
        # title).  Both fall back to the primary poster; off if none exist.
        _plangs    = tmdb_data.get("poster_langs") or {}
        _p_default = tmdb_data.get("original_poster_path")
        _original_lang = tmdb_data.get("original_language") or ""
        _poster_language_order = image_language_order(
            rcfg.logo_language, _original_lang, rcfg.logo_priority
        )
        _priority_lang = _poster_language_order[0] if _poster_language_order else ""
        _ranked_posters = [
            _plangs[language]
            for language in _poster_language_order
            if _plangs.get(language)
        ]
        # art_source only matters when the priority-first language is English —
        # the two TMDB English poster candidates (editorial primary vs
        # community top-rated) can differ meaningfully.  For non-English
        # priority languages TMDB has no separate "primary" concept so we
        # always use the vote-ranked poster regardless of art_source.
        _use_primary = (
            _priority_lang == "en"
            and rcfg.original_art_source == "primary"
        )
        if _use_primary:
            _orig_art = _p_default or next(iter(_ranked_posters), None)
        else:
            _orig_art = next(iter(_ranked_posters), None) or _p_default
        _use_original_art = rcfg.use_original_art and bool(_orig_art)
        if _use_original_art:
            poster_path   = _orig_art
            is_textless   = False
            _use_backdrop = False
            logger.info(f"Original-art mode for {tmdb_id} — poster {poster_path} "
                        f"(priority={rcfg.logo_priority})")

        if rating_already_cached or not effective_mdblist_key:
            rating_coro = _resolved(
                (cached_ratings_dict, cached_genre, cached_release_date, [], cached_age_rating)
            )
        else:
            global _mdblist_semaphore
            if _mdblist_semaphore is None:
                _mdblist_semaphore = asyncio.Semaphore(_cfg.MDBLIST_CONCURRENCY)

            async def _fetch_rating_gated(
                _key: str, _client=client, _imdb_id=imdb_id,
                _gids=genre_ids, _type=type,
                _mw=effective_movie_weights, _tw=effective_tv_weights,
            ):
                async with _mdblist_semaphore:
                    return await _with_retry(
                        fetch_rating,
                        _client, _imdb_id, _key, _gids, _type,
                        movie_weights=_mw, tv_weights=_tw,
                    )

            rating_coro = _fetch_rating_gated(effective_mdblist_key)

        # Quality is normally fetched in the background (not in this gather).
        # The one exception — wait_for_quality — is handled inline after the
        # gather completes so it never blocks rating coalescing.
        _backdrop_rescued = False
        _detection_deferred = False
        _vc = tmdb_data.get("vote_count")
        _vote_detection_ok = _detection_vote_ok(_vc)
        is_no_poster = poster_path is None and not _use_backdrop
        if _use_backdrop:
            # Text-aware backdrop cropping also invokes PP-OCR, so apply the
            # same foreground vote gate used by the final burned-in-text scan.
            _backdrop_avoid_text = (
                _cfg.TEXTLESS_TEXT_DETECTION and _vote_detection_ok
            )
            _image_coro = fetch_backdrop_image(
                client, tmdb_id, backdrop_path, avoid_text=_backdrop_avoid_text)
        elif is_no_poster:
            # Prefer the atmospheric genre background (minimal or photoreal set,
            # per the request); fall back to the flat genre-tinted gradient if no
            # background art exists for this genre in either set.
            _bg = _load_genre_background(_tmdb_genre, rcfg.fallback_bg_style)
            _image_coro = _resolved(_bg if _bg is not None else _make_fallback_canvas(genre_ids))
        else:
            # Option A: the title has only text-bearing art (no textless poster
            # or backdrop).  Before settling for the busy official poster, try a
            # text-aware crop of a text-bearing backdrop; if it comes out clean
            # we get a nicer image plus our own logo.  Gated to low-vote titles.
            _rescued = None
            _tbp = tmdb_data.get("text_backdrop_path")
            if (_cfg.TEXTLESS_TEXT_DETECTION and not is_textless and _tbp
                    and not _use_original_art
                    and _detection_vote_ok(tmdb_data.get("vote_count"))):
                try:
                    _cand = await fetch_backdrop_image(client, tmdb_id, _tbp, avoid_text=True)
                    # Memoise per (candidate backdrop and detector settings)
                    # — same rationale as the suppress path: config-independent.
                    from text_detect import DETECT_RES_SIG
                    _resc_src = f"bd:{_tbp}:{_CROP_VERSION}:ta"
                    _resc_key = f"{_resc_src}|conf={_cfg.PPOCR_BOX_THRESHOLD}:{DETECT_RES_SIG}"
                    _still_text = get_cached_text_detection(_resc_key)
                    if _still_text is None:
                        _still_text = await asyncio.shield(_start_text_detection(
                            _resc_key,
                            _cand,
                            title=_text_titles,
                            source="backdrop",
                            tmdb_id=tmdb_id,
                            vote_count=_vc,
                            source_key=_resc_src,
                        ))
                    if _still_text is False:
                        _rescued = _cand
                        logger.info(f"Text-aware backdrop crop clean for {tmdb_id} — using it with logo")
                    else:
                        logger.info(f"Text-aware backdrop crop still has text for {tmdb_id} — keeping official poster")
                except Exception as exc:
                    logger.warning(f"Backdrop rescue failed for {tmdb_id}: {exc}")
            if _rescued is not None:
                is_textless = True            # we now have textless art → composite logo
                _backdrop_rescued = True
                _image_coro = _resolved(_rescued)
            else:
                _image_coro = fetch_poster_image(client, tmdb_id, type, poster_path)

        # Start eligible foreground OCR as soon as the image arrives. Higher-vote
        # assets are recorded as deferred work instead: the request keeps waiting
        # for logo/rating/info, but never waits for their textless scan.
        _detection_task: "asyncio.Task[bool | None] | None" = None
        _detection_result: bool | None = False
        _det_src: str | None = None
        _det_key: str | None = None
        _scan_selected_image = (
            _cfg.TEXTLESS_TEXT_DETECTION
            and is_textless
            and not is_no_poster
            and not _backdrop_rescued
        )
        if _scan_selected_image:
            from text_detect import DETECT_RES_SIG

            if _use_backdrop:
                _crop_variant = "ta" if _backdrop_avoid_text else "plain"
                _det_src = f"bd:{backdrop_path}:{_CROP_VERSION}:{_crop_variant}"
                _image_cache_key = (
                    f"backdrop_{tmdb_id}_{backdrop_path.strip('/')}_{_CROP_VERSION}"
                    + ("_ta" if _backdrop_avoid_text else "")
                )
                _det_source = "backdrop"
            else:
                _det_src = f"ps:{poster_path}"
                _image_cache_key = f"{type}_{tmdb_id}_{poster_path.strip('/')}"
                _det_source = "poster"

            _det_key = (
                f"{_det_src}|conf={_cfg.PPOCR_BOX_THRESHOLD}:{DETECT_RES_SIG}"
            )
            _detection_result = get_cached_text_detection(_det_key)
            if _detection_result is None:
                _base_image_coro = _image_coro
                if _vote_detection_ok:
                    _reserve_foreground_detection()

                async def _fetch_image_and_schedule_detection():
                    nonlocal _detection_task, _detection_deferred
                    try:
                        fetched_image = await _base_image_coro
                    except BaseException:
                        if _vote_detection_ok:
                            _release_foreground_detection()
                        raise
                    if _vote_detection_ok:
                        _detection_task = _start_text_detection(
                            _det_key,
                            fetched_image,
                            title=_text_titles,
                            source=_det_source,
                            tmdb_id=tmdb_id,
                            vote_count=_vc,
                            source_key=_det_src,
                            media_type=type,
                            image_path=poster_path,
                            foreground_reserved=True,
                        )
                    else:
                        _detection_deferred = True
                        _queue_background_text_detection(_DeferredTextDetection(
                            cache_key=_det_key,
                            image_cache_key=_image_cache_key,
                            title=_text_titles,
                            source=_det_source,
                            tmdb_id=tmdb_id,
                            media_type=type,
                            image_path=poster_path,
                            vote_count=_vc,
                            source_key=_det_src,
                        ))
                    return fetched_image

                _image_coro = _fetch_image_and_schedule_detection()

        (
            image,
            logo,
            rating_result,
            trending_rank,
        ) = await asyncio.gather(
            _image_coro,
            fetch_logo(client, logos, rcfg.logo_language, imdb_id=imdb_id, original_language=tmdb_data.get("original_language"), logo_priority=rcfg.logo_priority) if (is_textless and not is_no_poster) else _resolved(None),
            rating_coro,
            fetch_trending_rank(client, tmdb_id, effective_tmdb_key, type),
        )

        # A rate-limited server key gets one same-request rescue attempt on the
        # next healthy configured key. Query-supplied keys remain isolated.
        if isinstance(rating_result, _RateLimited) and effective_mdblist_key:
            _failed_key = effective_mdblist_key
            _backoff_secs, _rescue_key = _mark_mdblist_rate_limit(
                imdb_id, _failed_key, rating_result
            )
            logger.warning(
                f"MDBList key rate-limited for {imdb_id}; cooling down for "
                f"{_backoff_secs:.0f}s"
            )
            # ElfHosted fork: raise a fleet-wide cooldown too, so sibling
            # replicas (sharing the same MDBList keys) back off instead of
            # each discovering the 429 independently. No-op beyond the
            # per-process cooldown above on the in-process coordinator.
            with suppress(Exception):
                await coord.set_backoff(
                    coord.NS_MDBLIST_GLOBAL_COOLDOWN, coord.MDBLIST_GLOBAL_KEY,
                    ttl_seconds=_backoff_secs,
                )
            if _rescue_key is not None:
                effective_mdblist_key = _rescue_key
                logger.warning(
                    f"Retrying MDBList for {imdb_id} with configured key "
                    f"#{_mdblist_active_key_idx + 1}"
                )
                rating_result = await _fetch_rating_gated(_rescue_key)

        # Inline quality wait — runs after gather so rating coalescing is never
        # blocked.  Used for poster-warm workflows where latency doesn't matter.
        if quality_needs_fetch and rcfg.wait_for_quality:
            async def _inline_fetch():
                if _cfg.QUALITY_SOURCE == "scraper" and _cfg.SCRAPER_URL:
                    return await _with_retry(
                        fetch_quality_from_scraper,
                        client, _cfg.SCRAPER_URL,
                        imdb_id, type, season, episode, release_date_for_quality_ttl,
                    )
                return await _with_retry(
                    fetch_quality_from_aiostreams,
                    client, imdb_id, type, season, episode, release_date_for_quality_ttl,
                )
            try:
                fetched = await asyncio.wait_for(
                    _inline_fetch(), timeout=_cfg.QUALITY_WAIT_TIMEOUT
                )
                _record_quality_result(fetched)
                if fetched is not FETCH_FAILED:
                    quality_tokens = fetched
                    logger.info(f"Inline quality fetch complete for {imdb_id}: {quality_tokens}")
                else:
                    # AIOStreams/scraper returned a transient error — don't cache
                    # the composite poster without quality so the next request retries.
                    logger.warning(
                        f"Inline quality fetch failed for {imdb_id} "
                        "— serving without quality, composite not cached"
                    )
                    quality_pending = True
            except asyncio.TimeoutError:
                _record_quality_result(FETCH_FAILED)
                logger.warning(
                    f"Quality wait timed out for {imdb_id} "
                    f"after {_cfg.QUALITY_WAIT_TIMEOUT:.0f}s — serving without quality, "
                    "composite not cached so next request retries"
                )
                quality_pending = True
            quality_needs_fetch = False

        # ------------------------------------------------------------------
        # Unpack results
        # ------------------------------------------------------------------
        rate_limited  = isinstance(rating_result, _RateLimited)
        rating_failed = (
            not rating_already_cached
            and effective_mdblist_key
            and (rating_result is FETCH_FAILED or rate_limited)
        )

        if rating_failed:
            if rate_limited:
                _retry_key = _rating_retry_key(imdb_id, effective_mdblist_key)
                if _retry_key not in _rating_backoff:
                    backoff_secs, _ = _mark_mdblist_rate_limit(
                        imdb_id, effective_mdblist_key, rating_result
                    )
                    logger.warning(
                        f"MDBList rate-limited {imdb_id}; key cooling down for "
                        f"{backoff_secs:.0f}s"
                    )
            else:
                # Network / timeout failure — escalating back-off so a transient
                # hiccup retries quickly while a sustained outage backs off further.
                # Ladder: 30 s → 2 min → 8 min → 1 h (cap), using 4× multiplier.
                _failed_retry_key = _rating_retry_key(imdb_id, effective_mdblist_key)
                fail_n = _rating_fail_count.get(_failed_retry_key, 0) + 1
                _rating_fail_count[_failed_retry_key] = fail_n
                backoff_secs = min(30 * (4 ** (fail_n - 1)), 3600.0)
                logger.warning(
                    f"Rating fetch failed for {imdb_id} (attempt {fail_n}) "
                    f"— back-off {backoff_secs:.0f}s"
                )
            if not rate_limited:
                _failed_retry_key = _rating_retry_key(imdb_id, effective_mdblist_key)
                _rating_backoff[_failed_retry_key] = asyncio.get_running_loop().time() + backoff_secs
            ratings_dict   = {}
            genre          = cached_genre or _tmdb_genre
            rel            = cached_release_date
            score          = "N/A"
            keywords       = []
            award_wins     = cached_award_wins
            award_noms     = cached_award_noms
            festival_label = cached_festival_label
            age_rating     = cached_age_rating
            is_cult        = cached_is_cult
            is_true_story  = cached_is_true_story
            is_metacritic  = cached_is_metacritic
        else:
            ratings_dict, genre, rel, keywords, age_rating = rating_result
            # genre from MDBlist/cache may be None when the key is absent and
            # nothing is cached yet — fall back to the TMDB-derived genre.
            genre = genre or _tmdb_genre

            # Fresh successful fetch — clear any escalation state so future
            # failures start back at the shortest interval.
            if (
                not rating_already_cached
                and not _rating_backoff_active
                and effective_mdblist_key
            ):
                _rating_fail_count.pop(
                    _rating_retry_key(imdb_id, effective_mdblist_key), None
                )

            if isinstance(ratings_dict, dict):
                weights = (
                    effective_tv_weights
                    if type in ("tv", "series")
                    else effective_movie_weights
                )
                score = calculate_weighted_score(
                    ratings_dict,
                    weights,
                    fallback_to_imdb=rcfg.fallback_to_imdb,
                )
            else:
                score = ratings_dict

            if rating_already_cached:
                award_wins     = cached_award_wins
                award_noms     = cached_award_noms
                festival_label = cached_festival_label
                age_rating     = cached_age_rating
                is_cult        = cached_is_cult
                is_true_story  = cached_is_true_story
                is_metacritic  = cached_is_metacritic
            else:
                award_wins, award_noms = parse_mdblist_awards(
                    keywords,
                    tmdb_id=tmdb_id,
                )
                kw_names = {(kw.get("name") or "").lower().strip() for kw in keywords}
                festival_label = next(
                    (label for kw, label in FESTIVAL_KEYWORDS.items() if kw in kw_names),
                    None,
                )
                is_cult       = bool({"cult-classic", "cult-film"} & kw_names)
                is_true_story = "based-on-true-story" in kw_names
                is_metacritic = "metacritic-must-see" in kw_names
                logger.info(f"Awards for {imdb_id}: wins={award_wins} noms={award_noms} "
                            f"festival={festival_label} age_rating={age_rating} "
                            f"cult={is_cult} true_story={is_true_story} metacritic={is_metacritic}")

        # ------------------------------------------------------------------
        # Write rating + awards to cache (only on a fresh fetch).
        # ------------------------------------------------------------------
        if not rating_failed and not rating_already_cached and effective_mdblist_key:
            set_cached_rating(
                imdb_id,
                ratings_dict if isinstance(ratings_dict, dict) else {},
                genre,
                rel,
                award_wins,
                award_noms,
                awards_fetched=True,
                festival_label=festival_label,
                age_rating=age_rating,
                is_cult=is_cult,
                is_true_story=is_true_story,
                is_metacritic=is_metacritic,
            )
            logger.info(f"Rating cached for {imdb_id}: score={score} genre={genre} "
                        f"wins={award_wins} noms={award_noms} festival={festival_label} "
                        f"age_rating={age_rating}")

        # Publish completion only after success is cached or failure backoff is
        # established. Otherwise a waiter can wake, miss the row, and duplicate
        # the same MDBList request.
        if _rating_event_to_set is not None:
            _rating_event_to_set.set()
            _rating_fetch_inflight.pop(imdb_id, None)
            _rating_event_to_set = None

        logger.info(f"Quality for {imdb_id}: tokens={quality_tokens} year={release_year}")

        # ------------------------------------------------------------------
        # Release status (opt-in via sash_priority — movies make an extra
        # /release_dates API call; TV is free, mapped from tmdb_status)
        # ------------------------------------------------------------------
        _release_status: str | None = None
        if "release_status" in rcfg.sash_priority:
            _release_status = await fetch_release_status(
                client, tmdb_id, effective_tmdb_key, type,
                tmdb_data.get("tmdb_status"),
            )
            # r/movieleaks confirmation overrides TMDB's theatrical/production
            # status — if the film is in the digital-release cache it's already
            # streaming regardless of what the official release dates say.
            if _release_status in ("Cinema", "Production") and is_digital_release(imdb_id):
                _release_status = "Streaming"
            # Cinema-only mode: keep the badge purely as an "unavailable" marker —
            # show only Cinema / Production and drop the rest so the slot is
            # skipped (and lower-priority sashes can surface) for released titles.
            if rcfg.release_status_cinema_only and _release_status not in ("Cinema", "Production"):
                _release_status = None

        # ------------------------------------------------------------------
        # Build DiscoveryMeta
        # ------------------------------------------------------------------
        discovery_meta = extract_discovery_meta(
            tmdb_data=tmdb_data,
            media_type=type,
            award_wins=award_wins,
            award_noms=award_noms,
            trending_rank=trending_rank,
            release_date=rel,
            keywords=keywords if not rating_already_cached else [],
            festival_label_override=festival_label,
            is_cult_override=is_cult,
            is_true_story_override=is_true_story,
            is_metacritic_override=is_metacritic,
            is_digital_release_override=is_digital_release(imdb_id),
            release_status_override=_release_status,
        )

        # ------------------------------------------------------------------
        # Debug mode: return diagnostic JSON instead of rendering the poster.
        # Useful for troubleshooting wrong sashes, missing ratings, etc.
        # Activate with ?debug=1 (never cached, never stored).
        # ------------------------------------------------------------------
        if debug and debug.strip() in ("1", "true"):
            _sash_result = pick_sash(discovery_meta, rcfg.sash_priority)
            return JSONResponse({
                "imdb_id":           imdb_id,
                "tmdb_id":           tmdb_id,
                "type":              type,
                "score":             score if isinstance(score, str) else int(score),
                "genre":             genre,
                "release_year":      release_year,
                "release_date":      rel,
                "quality_tokens":    quality_tokens,
                "age_rating":        age_rating,
                "award_wins":        award_wins,
                "award_noms":        award_noms,
                "festival_label":    festival_label,
                "sash":              {"label": _sash_result[0], "type": _sash_result[1]} if _sash_result else None,
                "is_cult":           discovery_meta.is_cult,
                "is_true_story":     discovery_meta.is_true_story,
                "is_metacritic":     discovery_meta.is_metacritic_must_see,
                "is_new_release":    discovery_meta.is_new_release,
                "is_digital_release":discovery_meta.is_digital_release,
                "trending_rank":     discovery_meta.trending_rank,
                "original_language": discovery_meta.original_language,
                "matched_studios":   discovery_meta.matched_studios,
                "matched_directors": discovery_meta.matched_directors,
                "matched_cast":      discovery_meta.matched_cast,
                "release_status":    discovery_meta.release_status,
                "sash_priority":     rcfg.sash_priority,
                "badge_display_mode":rcfg.badge_display_mode,
                "rating_display_mode":rcfg.rating_display_mode,
            })

        # ------------------------------------------------------------------
        # Burned-in-text detection. When a poster TMDB
        # tagged "textless" actually has the title burned in, compositing our
        # own logo/title would double it — so detect that and skip our overlay.
        # Cached results are always used. Uncached assets above the vote gate
        # are deferred until foreground poster rendering is idle.
        # ------------------------------------------------------------------
        _suppress_overlay = False
        if _scan_selected_image:
            _suppress_overlay = _detection_result
            if _suppress_overlay is None and _detection_task is not None:
                _suppress_overlay = await asyncio.shield(_detection_task)

            if _detection_deferred:
                logger.info(
                    f"Foreground text detection skipped for {tmdb_id}: "
                    f"vote_count={_vc!r} is outside foreground limit "
                    f"{_cfg.TEXTLESS_DETECTION_MAX_VOTES}; background scan queued"
                )
                _suppress_overlay = False
            elif _suppress_overlay is True:
                if not _use_backdrop and poster_path:
                    from textless_report import report_fake_textless_poster
                    report_fake_textless_poster(
                        media_type=type,
                        tmdb_id=tmdb_id,
                        image_path=poster_path,
                        vote_count=_vc,
                    )
                logger.info(
                    f"Burned-in text detected on textless poster {tmdb_id} "
                    f"(votes={_vc}); skipping logo/title overlay"
                )
            elif _suppress_overlay is False:
                logger.info(
                    f"No burned-in text detected on textless poster {tmdb_id} "
                    f"(votes={_vc})"
                )
            else:
                from text_detect import text_detection_status
                logger.warning(
                    f"Burned-in text scan unavailable for {tmdb_id}; "
                    f"result was not cached ({text_detection_status()})"
                )
                _suppress_overlay = False

        # Offload CPU-bound PIL compositing + JPEG encoding to the thread pool
        # so the event loop stays free for concurrent requests.
        _bp_args = dict(
            logo=logo if (is_textless and not is_no_poster and not rcfg.textless
                          and not _suppress_overlay) else None,
            fallback_title=(
                title if is_no_poster
                else (title if is_textless and not logo and not rcfg.textless
                      and not _suppress_overlay else None)
            ),
            discovery_meta=discovery_meta,
            quality_tokens=quality_tokens,
            release_year=release_year,
            age_rating=age_rating,
            no_poster=is_no_poster,
        )

        def _composite_and_encode() -> bytes:
            result = build_poster(image, score, genre, rcfg, **_bp_args)
            buf = io.BytesIO()
            result.convert("RGB").save(buf, format="JPEG", quality=_cfg.JPEG_QUALITY)
            return buf.getvalue()

        # ElfHosted fork: bounded render concurrency. Acquire a render slot;
        # if RENDER_QUEUE_TIMEOUT is exceeded, return 503 + Retry-After so the
        # client backs off instead of compounding the saturation. The
        # semaphore is created lazily inside the event loop on first use.
        global _render_semaphore
        if _render_semaphore is None:
            _render_semaphore = asyncio.Semaphore(_cfg.RENDER_CONCURRENCY)
        try:
            if _cfg.RENDER_QUEUE_TIMEOUT > 0:
                await asyncio.wait_for(
                    _render_semaphore.acquire(),
                    timeout=_cfg.RENDER_QUEUE_TIMEOUT,
                )
            else:
                await _render_semaphore.acquire()
        except asyncio.TimeoutError:
            _metrics.render_saturated_total.inc()
            logger.warning(
                f"Render queue saturated (cap={_cfg.RENDER_CONCURRENCY}); "
                f"returning 503 for tmdb_id={tmdb_id}"
            )
            if _render_fut is not None and not _render_fut.done():
                _render_fut.set_exception(HTTPException(status_code=503))
            raise HTTPException(
                status_code=503,
                detail="Server saturated — try again shortly",
                headers={"Retry-After": "5"},
            )
        try:
            _metrics.render_inflight.inc()
            with _metrics.render_duration_seconds.time():
                img_bytes = await asyncio.get_running_loop().run_in_executor(
                    None, _composite_and_encode
                )
        finally:
            _metrics.render_inflight.dec()
            _render_semaphore.release()

        # Persist the finished poster so future requests skip the pipeline.
        # Skipped when:
        #   quality_pending      — badges would be missing; next request caches properly
        #   _detection_deferred — vote-gated OCR is queued in the background
        #   rating_failed        — MDBlist returned a hard failure; don't lock in N/A score
        #   _rating_backoff_active — a previous failure is still in its cool-down window;
        #                            backoff nullifies effective_mdblist_key so rating_failed
        #                            would evaluate False without this separate flag
        if (final_cache_key is not None and not quality_pending and not _detection_deferred
                and not rating_failed and not _rating_backoff_active):
            await set_cached_final_poster(final_cache_key, img_bytes)
            logger.info(f"Final poster cached for {final_cache_key}")

        if _render_fut is not None:
            _render_fut.set_result(img_bytes)

        response = Response(content=img_bytes, media_type="image/jpeg")
        if final_cache_key is not None:
            response.headers["ETag"] = f'"{final_cache_key}"'
        if _cfg.DISABLE_COMPOSITE_CACHE:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        elif _cfg.CDN_CACHE_TTL > 0:
            response.headers["Cache-Control"] = f"public, max-age={_cfg.CDN_CACHE_TTL}"
        return response

    except ValueError as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.warning(f"No poster available for tmdb_id={tmdb_id}: {exc}")
        raise HTTPException(status_code=404, detail=str(exc))
    except httpx.TimeoutException as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.warning(f"Upstream timeout for tmdb_id={tmdb_id}: {type(exc).__name__}")
        raise HTTPException(status_code=504, detail="Upstream request timed out")
    except httpx.HTTPStatusError as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        status = exc.response.status_code
        if status == 404:
            # TMDB returned metadata with a poster/image path that no longer exists.
            # Invalidate the (per-language) metadata cache so the next request
            # re-fetches fresh data.
            _endpoint = "tv" if type in ("tv", "series") else "movie"
            delete_cached_tmdb_metadata(tmdb_metadata_cache_key(
                _endpoint, tmdb_id, rcfg.logo_language
            ))
            logger.warning(
                f"TMDB image 404 for tmdb_id={tmdb_id} — metadata cache invalidated, "
                f"will self-heal on next request"
            )
            raise HTTPException(status_code=404, detail="Poster image not found on TMDB")
        logger.error(f"Upstream HTTP {status} for tmdb_id={tmdb_id}: {exc}")
        raise HTTPException(status_code=502, detail=f"Upstream error {status}")
    except Exception as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.exception(f"Error building poster for tmdb_id={tmdb_id}")
        raise HTTPException(status_code=500, detail="Failed to build poster")
    finally:
        _active_poster_renders = max(0, _active_poster_renders - 1)
        # Fire the rating event so any coalesced waiters unblock. Under normal
        # operation this was set after cache persistence; this is the safety
        # net for error paths that exit before reaching that point.
        if _rating_event_to_set is not None:
            _rating_event_to_set.set()
            _rating_fetch_inflight.pop(imdb_id, None)
        if final_cache_key is not None:
            _render_inflight.pop(final_cache_key, None)