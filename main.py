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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont

_LOG_FORMAT_ENV = os.environ.get("LOG_FORMAT", "text").strip().lower()
if _LOG_FORMAT_ENV == "json":
    # Structured JSON logs for hosted-mode log aggregation. Falls back to
    # text format if python-json-logger isn't installed (which only happens
    # if someone strips the dep from requirements).
    try:
        from pythonjsonlogger import jsonlogger
        _json_handler = logging.StreamHandler()
        _json_handler.setFormatter(jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
        ))
        logging.basicConfig(level=logging.INFO, handlers=[_json_handler], force=True)
    except ImportError:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            force=True,
        )
else:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
# Pull uvicorn's loggers into our root handler so all output shares the same format.
for _uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _uv_logger = logging.getLogger(_uv_name)
    _uv_logger.handlers = []
    _uv_logger.propagate = True


class _TruncateUrlFilter(logging.Filter):
    """Redact API keys and truncate long URL paths in uvicorn access log records."""
    _MAX = 80
    # Redact any query param whose name ends in _key or is access_key
    _KEY_RE = re.compile(r'((?:tmdb_key|mdblist_key|access_key)=)[^&\s]*', re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access records: args = (client_addr, method, path, http_version, status_code, ...)
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) >= 3
        ):
            path = record.args[2]
            if isinstance(path, str):
                # Redact before truncating so keys are never logged regardless of length
                path = self._KEY_RE.sub(r'\1***', path)
                if len(path) > self._MAX:
                    path = path[: self._MAX] + "…"
                record.args = (record.args[0], record.args[1], path) + record.args[3:]
        return True


# Attach to the root handler, not the root logger — propagation calls
# callHandlers() directly on parent loggers, skipping their logger-level filters.
_url_filter = _TruncateUrlFilter()
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_url_filter)
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

# ---------------------------------------------------------------------------
# Render concurrency cap (ElfHosted fork — Phase 4)
# ---------------------------------------------------------------------------
# Pillow composite + JPEG encode pins one CPU core per render. Without a cap
# a burst of unique-param /poster requests fans out across the default
# asyncio thread-pool (min(32, ncpu+4) slots) and pins every core, starving
# /health and /ready. The semaphore here is created lazily inside the event
# loop on first /poster request, sized by config.RENDER_CONCURRENCY.
_render_semaphore: "asyncio.Semaphore | None" = None

# ---------------------------------------------------------------------------
# Background quality fetching
# ---------------------------------------------------------------------------
# Quality data (AIOStreams / scrapers) is fetched in the background so poster
# responses are never blocked by a slow scraper call.  The poster is served
# immediately without quality badges on a cache miss; the next request for the
# same title will find the quality cached and render badges normally.
#
# Background-fetch single-flight (so scroll bursts don't launch duplicate
# fetches for the same title) is delegated to the coordination layer — the
# default in-process backend uses a set under the hood; the Redis backend
# shares the claim across replicas.
# _quality_bg_semaphore: caps concurrent AIOStreams calls so a large burst
#   doesn't hammer the scrapers with hundreds of simultaneous requests.

_quality_bg_semaphore: "asyncio.Semaphore | None" = None   # created inside event loop

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
# Inherently per-process (asyncio.Event isn't shareable across replicas);
# cross-replica deduplication is approximated via the shared rating cache
# plus the shared backoff in the coordination layer.
#
# Backoff (rate-limit / FETCH_FAILED suppression) is delegated to the
# coordination layer so a 429 from MDBList throttles every replica, not just
# the one that hit it first.

_rating_fetch_inflight: dict[str, asyncio.Event] = {}


async def _background_quality_fetch(
    imdb_id: str,
    media_type: str,
    season: int,
    episode: int,
    release_date: str | None,
) -> None:
    """Fetch quality tokens from AIOStreams and cache them.  Never raises."""
    global _quality_bg_semaphore
    if _quality_bg_semaphore is None:
        _quality_bg_semaphore = asyncio.Semaphore(_cfg.QUALITY_BG_CONCURRENCY)
    try:
        async with _quality_bg_semaphore:
            if _HTTP_CLIENT is None:
                return
            await _with_retry(
                fetch_quality_from_aiostreams,
                _HTTP_CLIENT, imdb_id, media_type, season, episode, release_date,
            )
            logger.info(f"Background quality fetch complete for {imdb_id}")
    except Exception as exc:
        logger.warning(f"Background quality fetch failed for {imdb_id}: {exc}")
    finally:
        await coord.release_inflight(coord.NS_QUALITY_BG, imdb_id)

# Local imports
from age_badge import draw_quality_age_badge
from awards import FETCH_FAILED, draw_award_sash, parse_mdblist_awards
from cache import (
    get_cached_quality,
    get_cached_rating,
    get_cached_final_poster,
    get_cached_final_poster_url,
    is_cached_final_poster_fresh,
    set_cached_final_poster,
    init_db,
    is_digital_release,
    set_cached_rating,
    delete_cached_tmdb_metadata,
    prune_caches,
    close as close_db,
)
from digital_release import digital_release_poll_loop
import blobstore
import config as _cfg
import coordination as coord
import metrics as _metrics
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
    get_resized_badge,
    parse_quality,
    render_badges_left,
)
from ratings import calculate_weighted_score, draw_score_bar, fetch_rating, draw_score_bar_vertical
from tmdb import composite_logo, fetch_logo, fetch_poster_metadata, fetch_poster_image, fetch_trending_rank

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
        headers={"Accept-Encoding": "identity"},
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
    if _cfg.SERVER_MDBLIST_KEY:
        return _cfg.SERVER_MDBLIST_KEY
    return None


# ---------------------------------------------------------------------------
# Per-request configuration
# ---------------------------------------------------------------------------

@dataclass
class RequestConfig:
    """
    Holds all user-tuneable config values for a single request.
    Defaults come from the global config module; query params override them.
    """
    show_award_sash:     bool = field(default_factory=lambda: _cfg.SHOW_AWARD_SASH)
    badge_display_mode:  int  = field(default_factory=lambda: _cfg.BADGE_DISPLAY_MODE)
    rating_display_mode: int  = field(default_factory=lambda: _cfg.SHOW_RATING_DISPLAY_MODE)

    accent_bar_font_size_ratio:    float = field(default_factory=lambda: _cfg.ACCENT_BAR_MODE_FONT_SIZE_RATIO)
    numeric_score_font_size_ratio: float = field(default_factory=lambda: _cfg.NUMERIC_SCORE_MODE_FONT_SIZE_RATIO)
    accent_bar_y_offset:           float = field(default_factory=lambda: _cfg.ACCENT_BAR_MODE_FONT_Y_OFFSET)
    numeric_score_y_offset:        float = field(default_factory=lambda: _cfg.NUMERIC_SCORE_MODE_FONT_Y_OFFSET)
    score_glow_threshold:          int   = field(default_factory=lambda: _cfg.SCORE_GLOW_THRESHOLD)
    score_glow_blur:               int   = field(default_factory=lambda: _cfg.SCORE_GLOW_BLUR)
    score_glow_alpha:              int   = field(default_factory=lambda: _cfg.SCORE_GLOW_ALPHA)
    minimalist_mode_font_size_ratio:  float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_SIZE_RATIO)
    minimalist_mode_font_x_offset: float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_X_OFFSET)
    minimalist_mode_font_y_offset: float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_Y_OFFSET)

    logo_max_w_ratio:  float = field(default_factory=lambda: _cfg.LOGO_MAX_W_RATIO)
    logo_max_h_ratio:  float = field(default_factory=lambda: _cfg.LOGO_MAX_H_RATIO)
    logo_bottom_ratio: float = field(default_factory=lambda: _cfg.LOGO_BOTTOM_RATIO)

    badge_height:    int   = field(default_factory=lambda: _cfg.BADGE_HEIGHT)
    badge_gap:       int   = field(default_factory=lambda: _cfg.BADGE_GAP)
    badge_anchor_x:  float = field(default_factory=lambda: _cfg.BADGE_ANCHOR_X_RATIO)
    badge_anchor_y:  float = field(default_factory=lambda: _cfg.BADGE_ANCHOR_Y_RATIO)

    movie_weights: dict | None = None
    tv_weights:    dict | None = None

    logo_language: str = field(default_factory=lambda: _cfg.DEFAULT_LOGO_LANGUAGE)
    sash_priority: list[str] = field(default_factory=lambda: list(_cfg.SASH_PRIORITY))


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
    """Build a RequestConfig from raw query-param strings."""
    cfg = RequestConfig()

    def _b(key, default): return _parse_bool(params.get(key), default)
    def _f(key, default):
        try:    return float(params[key]) if key in params else default
        except: return default
    def _i(key, default):
        try:    return int(params[key]) if key in params else default
        except: return default

    cfg.show_award_sash         = _b("show_award_sash",        cfg.show_award_sash)
    cfg.badge_display_mode      = _i("badge_display_mode",     cfg.badge_display_mode)
    cfg.rating_display_mode     = _i("rating_display_mode",    cfg.rating_display_mode)

    if "show_quality_badges" in params and "badge_display_mode" not in params:
        if _parse_bool(params.get("show_quality_badges"), True):
            cfg.badge_display_mode = 1
        else:
            cfg.badge_display_mode = 0

    cfg.accent_bar_font_size_ratio    = _f("accent_bar_font_size_ratio",    cfg.accent_bar_font_size_ratio)
    cfg.numeric_score_font_size_ratio = _f("numeric_score_font_size_ratio", cfg.numeric_score_font_size_ratio)
    cfg.accent_bar_y_offset           = _f("accent_bar_y_offset",           cfg.accent_bar_y_offset)
    cfg.numeric_score_y_offset        = _f("numeric_score_y_offset",        cfg.numeric_score_y_offset)
    cfg.score_glow_threshold          = _i("score_glow_threshold",          cfg.score_glow_threshold)
    cfg.score_glow_blur               = _i("score_glow_blur",               cfg.score_glow_blur)
    cfg.score_glow_alpha              = _i("score_glow_alpha",              cfg.score_glow_alpha)
    cfg.minimalist_mode_font_size_ratio = _f("minimalist_mode_font_size_ratio", cfg.minimalist_mode_font_size_ratio)
    cfg.minimalist_mode_font_x_offset = _f("minimalist_mode_font_x_offset", cfg.minimalist_mode_font_x_offset)
    cfg.minimalist_mode_font_y_offset = _f("minimalist_mode_font_y_offset", cfg.minimalist_mode_font_y_offset)

    cfg.logo_max_w_ratio  = _f("logo_max_w_ratio",  cfg.logo_max_w_ratio)
    cfg.logo_max_h_ratio  = _f("logo_max_h_ratio",  cfg.logo_max_h_ratio)
    cfg.logo_bottom_ratio = _f("logo_bottom_ratio", cfg.logo_bottom_ratio)

    cfg.badge_height   = _i("badge_height",   cfg.badge_height)
    cfg.badge_gap      = _i("badge_gap",       cfg.badge_gap)
    cfg.badge_anchor_x = _f("badge_anchor_x", cfg.badge_anchor_x)
    cfg.badge_anchor_y = _f("badge_anchor_y", cfg.badge_anchor_y)

    all_sources = list(_cfg.MOVIE_WEIGHTS.keys())
    cfg.movie_weights = _parse_weights(params.get("movie_weights"), all_sources)

    tv_sources = list(_cfg.TV_WEIGHTS.keys())
    cfg.tv_weights = _parse_weights(params.get("tv_weights"), tv_sources)

    cfg.logo_language = (params.get("logo_language", cfg.logo_language).strip().lower())
    cfg.sash_priority = _parse_sash_priority(params.get("sash_priority"))

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
) -> Image.Image:

    width, height = image.size
    draw = ImageDraw.Draw(image)

    # --- TOP GRADIENT (vectorised) ---
    top_height = int(height * 0.4)
    top_max_alpha = 220
    t_top = np.linspace(0, 1, top_height, dtype=np.float32)
    eased_top = ((1 - t_top) * top_max_alpha).astype(np.uint8)
    top_array = np.broadcast_to(eased_top[:, np.newaxis], (top_height, width)).copy()
    top_overlay = Image.fromarray(top_array, mode="L")
    top_tinted = Image.new("RGBA", (width, top_height), (0, 0, 0, 0))
    top_tinted.putalpha(top_overlay)
    image.paste(top_tinted, (0, 0), mask=top_tinted)

    # --- BOTTOM GRADIENT (vectorised) ---
    # Minimalist mode sits closer to the bottom edge with a smaller label, so a
    # lighter fade is enough to keep contrast without over-darkening the poster.
    bottom_height = int(height * 0.5)
    bottom_start = height - bottom_height
    bottom_max_alpha = 200 if cfg.rating_display_mode == 3 else 235
    bottom_curve = 1.2
    t_bot = np.linspace(0, 1, bottom_height, dtype=np.float32)
    eased_bot = ((1 - (1 - t_bot) ** bottom_curve) * bottom_max_alpha).astype(np.uint8)
    bottom_array = np.broadcast_to(eased_bot[:, np.newaxis], (bottom_height, width)).copy()
    bottom_overlay = Image.fromarray(bottom_array, mode="L")
    bottom_tinted = Image.new("RGBA", (width, bottom_height), (0, 0, 0, 0))
    bottom_tinted.putalpha(bottom_overlay)
    image.paste(bottom_tinted, (0, bottom_start), mask=bottom_tinted)

    # --- Badge / quality overlay ---
    mode   = cfg.badge_display_mode
    tokens = quality_tokens or []

    if mode == 1:
        draw_quality_age_badge(
            image,
            age_rating,
            tokens,
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

    elif mode == 2:
        allowed_tokens  = {"4K", "1080P", "REMUX", "WEBDL", "DV", "HDR10+", "HDR10"}
        filtered_tokens = [t for t in tokens if t in allowed_tokens]

        if filtered_tokens:
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

    # --- Logo / fallback title ---
    if logo:
        composite_logo(
            image, logo,
            max_w_ratio=cfg.logo_max_w_ratio,
            max_h_ratio=cfg.logo_max_h_ratio,
            bottom_ratio=cfg.logo_bottom_ratio,
        )
    elif fallback_title:
        try:
            font_size = int(width * 0.1)
            font = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
        except IOError:
            font = ImageFont.load_default()

        title_cy = height - int(height * 0.3)
        max_width = int(width * 0.82)

        while True:
            bbox = draw.textbbox((0, 0), fallback_title, font=font)
            text_width = bbox[2] - bbox[0]
            if text_width <= max_width or font_size <= 24:  # type: ignore
                break
            font_size -= 2  # type: ignore
            try:
                font = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                break

        tx, ty = _text_center(draw, fallback_title, font, width / 2, title_cy)  # type: ignore
        shadow_offset = max(2, int(font_size * 0.04))  # type: ignore
        draw.text((tx + shadow_offset, ty + shadow_offset), fallback_title, font=font, fill=(0, 0, 0, 180))
        draw.text((tx, ty), fallback_title, font=font, fill=(255, 255, 255, 255))

    # --- Rating / genre label ---
    if cfg.rating_display_mode != 0:

        if cfg.rating_display_mode == 1:
            font_size = int(width * cfg.accent_bar_font_size_ratio)
            label = f"{genre} · {release_year}"
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
                glow_threshold=cfg.score_glow_threshold,
                glow_blur=cfg.score_glow_blur,
                glow_alpha=cfg.score_glow_alpha,
            )

        elif cfg.rating_display_mode == 2:
            font_size = int(width * cfg.numeric_score_font_size_ratio)
            label = f"{genre} ★ {score}"
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
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Ubuntu-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            y = round(height * cfg.minimalist_mode_font_y_offset)
            right_edge = width - int(width * cfg.minimalist_mode_font_x_offset)

            year_text  = str(release_year or "")
            genre_text = genre

            pip_gap = int(font_size * 0.55)
            pip_w   = max(4, int(font_size * 0.18))
            pip_h   = int(font_size * 1.4)

            genre_bb = draw.textbbox((0, 0), genre_text, font=font_meta)
            genre_w  = genre_bb[2] - genre_bb[0]

            if year_text:
                year_bb = draw.textbbox((0, 0), year_text, font=font_meta)
                year_w  = year_bb[2] - year_bb[0]
            else:
                year_w = 0

            pip_x  = right_edge - year_w - pip_gap - pip_w
            pip_cy = round(y + font_size * 0.60)

            genre_x = pip_x - pip_gap - genre_w
            draw.text((genre_x, y), genre_text, font=font_meta, fill=(235, 235, 235, 255))

            if year_text:
                year_x = pip_x + pip_w + pip_gap
                draw.text((year_x, y), year_text, font=font_meta, fill=(235, 235, 235, 255))

            if score not in ("N/A", None):
                draw_score_bar_vertical(
                    image,
                    score,
                    x=pip_x,
                    y_center=pip_cy,
                    height=pip_h,
                    width=pip_w,
                )

    # --- Discovery sash ---
    if cfg.show_award_sash and discovery_meta is not None:
        sash_result = pick_sash(discovery_meta, cfg.sash_priority)
        if sash_result is not None:
            label, sash_type = sash_result
            image = draw_award_sash(image, label, sash_type=sash_type)

    return image


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def _with_lease(lease_name: str, ttl_seconds: float) -> str | None:
    """Best-effort leader election helper. Returns a lease token if this
    replica/worker is currently the leader, None otherwise. Caller refreshes
    the lease before each iteration (or releases it on shutdown).

    With the in-process coordinator each uvicorn worker is its own leader
    (no cross-process state). With the Redis coordinator the lease is
    server-side and exactly one replica holds it at a time.
    """
    token = await coord.try_acquire_lease(lease_name, ttl_seconds)
    if token is not None:
        logger.info(f"Acquired lease {lease_name!r} (token={token})")
    return token


async def _cache_prune_loop() -> None:
    """Periodically prune expired rows from all cache tables. Leader-elected
    so multi-replica deployments only run one prune per cycle."""
    # Lease TTL covers worst-case: one iteration's sleep + run time, plus
    # a buffer so a slow SQLite VACUUM doesn't release the lease mid-run.
    lease_ttl = 8 * 3600.0
    lease_token: str | None = None

    # Wait a few minutes after startup before the first run so the service
    # is fully warmed before taking the SQLite write lock.
    await asyncio.sleep(300)
    try:
        while True:
            if lease_token is None:
                lease_token = await _with_lease(coord.LEASE_CACHE_PRUNE, lease_ttl)
            else:
                if not await coord.refresh_lease(coord.LEASE_CACHE_PRUNE, lease_token, lease_ttl):
                    # Lost the lease (e.g. Redis expired it while we were
                    # sleeping). Drop the token; another replica may pick it
                    # up before we try again.
                    logger.info(f"Lease {coord.LEASE_CACHE_PRUNE!r} lost")
                    lease_token = None

            if lease_token is not None:
                logger.info("Running scheduled cache prune (leader)")
                # Phase 10: prune_caches is async (blobstore deletes for
                # evicted composite rows). The DB-side work inside it still
                # runs sync, but a 6h-cadence call to a sync sqlite path
                # via the event loop is fine.
                await prune_caches()
                # Coordinator-managed state (rating backoff, bg-fetch claims).
                # On the in-process backend this evicts expired entries from
                # per-worker dicts; Redis backend is a no-op (server expires).
                await coord.prune_expired()
            else:
                logger.debug("Cache prune skipped — another replica holds the lease")

            await asyncio.sleep(6 * 3600)   # every 6 hours
    finally:
        if lease_token is not None:
            await coord.release_lease(coord.LEASE_CACHE_PRUNE, lease_token)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _HTTP_CLIENT, _configurator_html
    init_db()
    logger.info(f"Cache initialised (composite TTL {_cfg.COMPOSITE_CACHE_TTL}s / "
                f"{_cfg.COMPOSITE_CACHE_TTL / 86400:.1f}d)")
    await blobstore.init()
    await coord.init()
    # Label the backend selection so Prometheus dashboards can group by mode.
    import cache as _cache_mod
    _metrics.backend_info.labels(
        storage=_cache_mod.BACKEND_KIND,
        coordinator=coord.BACKEND_KIND,
        blobstore=blobstore.BACKEND_KIND,
    ).set(1)
    _HTTP_CLIENT = _make_http_client()
    logger.info("HTTP client initialised")
    _configurator_html = _load_configurator_html()
    prune_task   = asyncio.create_task(_cache_prune_loop())
    digital_task = asyncio.create_task(digital_release_poll_loop(_HTTP_CLIENT))
    yield
    # Cancel background tasks then await them so their finally blocks (which
    # release leases via the coordinator) run before we close the coord
    # client. Without this wait, a Redis-backed deployment would close the
    # connection mid-release and leave the lease keys until TTL expiry,
    # blocking other replicas from picking up the work for hours.
    prune_task.cancel()
    digital_task.cancel()
    for _t in (prune_task, digital_task):
        try:
            await _t
        except asyncio.CancelledError:
            pass
    await _HTTP_CLIENT.aclose()
    logger.info("HTTP client closed")
    await coord.close()
    logger.info("Coordinator closed")
    await blobstore.close()
    logger.info("Blob store closed")
    close_db()
    logger.info("Storage backend closed")


app = FastAPI(lifespan=lifespan)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_FONTS_DIR = os.path.join(BASE_DIR, "fonts")
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
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    return {
        "tmdb_key_set":          bool(_cfg.SERVER_TMDB_KEY),
        "mdblist_key_set":       bool(_cfg.SERVER_MDBLIST_KEY),
        "aiostreams_configured": bool(_cfg.AIOSTREAMS_URL and _cfg.AIOSTREAMS_AUTH),
    }


# ---------------------------------------------------------------------------
# Configurator HTML
# ---------------------------------------------------------------------------

_configurator_html: str | None = None


def _load_configurator_html() -> str:
    html_path = os.path.join(os.path.dirname(__file__), "configurator.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return "<h1>Configurator not found</h1><p>Place configurator.html alongside main.py</p>"


@app.get("/metrics")
async def metrics_endpoint(access_key: str = ""):
    """Prometheus exposition. Optional shared-secret guard via METRICS_ACCESS_KEY.

    Note: the binding doesn't separate ports — operators wanting strict
    metrics isolation should bind a sidecar at the ingress level. The
    shared-secret guard is a defence-in-depth when /metrics is publicly
    reachable.
    """
    if _cfg.METRICS_ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.METRICS_ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, CollectorRegistry
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        # Aggregate counters/histograms across all uvicorn worker processes
        # so a scrape always reflects the whole replica's traffic, not just
        # whichever worker happened to handle it.
        from prometheus_client import multiprocess
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        payload = generate_latest(registry)
    else:
        payload = generate_latest()
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
@app.get("/live")
async def liveness_probe():
    """Lightweight liveness probe — no auth required.

    /health is kept as an alias of /live for upstream compatibility (the
    Docker healthcheck in compose.yaml hits /health). Kubernetes deployments
    should use /live for liveness and /ready for readiness.
    """
    return {"status": "ok"}


@app.get("/ready")
async def readiness_probe():
    """Readiness probe — checks the backends this replica depends on.

    Used by Kubernetes / load balancers to remove a replica from rotation
    when one of its dependencies is unhealthy. Each check runs in parallel
    so a slow backend doesn't block the others.

    Returns 200 with a JSON breakdown when all backends are reachable, or
    503 with the same breakdown when at least one is not. Independent of
    /live so a transient backend hiccup pulls the replica out of rotation
    without killing the process.
    """
    import cache as _cache_mod
    import blobstore as _blob_mod

    # Run every probe concurrently. The sync ones (SQLite ping, FS dir
    # check, S3 head when not via boto3-async) go to the threadpool so a
    # stalled backend can't pin the event loop and starve /live or normal
    # request handlers.
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
    all_ok = db_ok and coord_ok and blob_ok
    if not all_ok:
        return Response(
            content=__import__("json").dumps({"status": "degraded", **body}),
            status_code=503,
            media_type="application/json",
        )
    return {"status": "ok", **body}


@app.get("/", response_class=HTMLResponse)
async def get_configurator(access_key: str = ""):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized. Provide ?access_key=<key>")
    return HTMLResponse(content=_configurator_html or _load_configurator_html())


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------

@app.get("/search")
async def search_proxy(
    q: str,
    tmdb_key: str = "",
    access_key: str = "",
):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")
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
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized")

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
):
    if _cfg.ACCESS_KEY and not hmac.compare_digest(access_key, _cfg.ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Unauthorized, your access key is not valid for this instance.")

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

    # Per-tenant rate limit. Tenant identity is derived from the user-supplied
    # API key when the user brings their own (so noisy tenants throttle
    # themselves without dragging other tenants' quotas down). Requests using
    # the operator's keys share the "operator" bucket — a noisy anonymous
    # caller can still saturate the operator's MDBList quota; the operator
    # should set RATE_LIMIT_RPS or restrict ACCESS_KEY.
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

    raw_params = {
        k: v for k, v in request.query_params.items()
        if k not in (
            "tmdb_id", "imdb_id", "mdblist_key", "tmdb_key", "type",
            "quality", "season", "episode", "access_key",
        )
    }
    rcfg = build_request_config(raw_params)

    # ------------------------------------------------------------------
    # Final poster cache — keyed on imdb_id, type, and a short hash of
    # all rendering parameters so different visual configs don't collide.
    # Skipped when an explicit quality= override is supplied (one-off).
    # ------------------------------------------------------------------
    if not quality:
        _params_hash = hashlib.md5(
            "&".join(f"{k}={v}" for k, v in sorted(raw_params.items())).encode()
        ).hexdigest()[:8]
        final_cache_key = f"{imdb_id}:{type}:{_params_hash}"
        # Phase 10: when the blobstore has a public CDN URL configured
        # (OBJECT_STORE_PUBLIC_URL), the freshness probe alone is enough
        # to authorise a 302 — we never pull the bytes back through the
        # app on a cache hit. With Cloudflare in front of a B2 bucket
        # that's free egress + zero app CPU per hit.
        cdn_url = get_cached_final_poster_url(final_cache_key)
        if cdn_url is not None:
            if await is_cached_final_poster_fresh(final_cache_key):
                logger.info(f"Final poster cache hit (CDN redirect) for {final_cache_key}")
                resp = Response(status_code=302)
                resp.headers["Location"] = cdn_url
                if _cfg.CDN_CACHE_TTL > 0:
                    resp.headers["Cache-Control"] = f"public, max-age={_cfg.CDN_CACHE_TTL}"
                return resp
        else:
            # Local / no-CDN deployment: fall back to inline serving.
            cached_jpeg = await get_cached_final_poster(final_cache_key)
            if cached_jpeg is not None:
                logger.info(f"Final poster cache hit (inline) for {final_cache_key}")
                return Response(content=cached_jpeg, media_type="image/jpeg")
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
                return Response(content=await _existing_fut, media_type="image/jpeg")
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
    # Goal: ensure at most one MDBlist call per imdb_id per worker at a
    # time, and suppress re-fetches for an hour after a confirmed failure.
    #
    # Back-off check: if a recent fetch returned FETCH_FAILED, skip the
    # API call entirely until the TTL expires.
    #
    # Coalescing: if another coroutine in this worker is already fetching
    # the same imdb_id, wait for its asyncio.Event, then re-read the DB.
    # If it succeeded we get the cached data for free; if it failed we
    # re-check the back-off (now set by the other coroutine) before
    # deciding whether to attempt our own call.
    # ------------------------------------------------------------------
    _rating_event_to_set: asyncio.Event | None = None

    if not rating_already_cached and effective_mdblist_key:
        if await coord.is_backoff_active(coord.NS_RATING_BACKOFF, imdb_id):
            logger.debug(f"Rating fetch for {imdb_id} skipped (MDBlist back-off active)")
            effective_mdblist_key = None   # treat as no-key; serve without rating

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
                if await coord.is_backoff_active(coord.NS_RATING_BACKOFF, imdb_id):
                    logger.debug(
                        f"Rating fetch for {imdb_id} suppressed after coalescence (back-off active)"
                    )
                    effective_mdblist_key = None
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

    quality_needs_fetch = (
        rcfg.badge_display_mode in (1, 2)
        and not quality
        and cached_tokens is None
    )

    # If quality needs fetching, fire it in the background and serve the poster
    # immediately without badges.  The cache will be warm on the next request.
    quality_pending = False
    if quality_needs_fetch:
        if await coord.claim_inflight(coord.NS_QUALITY_BG, imdb_id, ttl_seconds=300.0):
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
            f"No MDblist key for {imdb_id} and no cached rating — "
            "poster will be served without rating/award data."
        )

    effective_movie_weights = rcfg.movie_weights or _cfg.MOVIE_WEIGHTS
    effective_tv_weights    = rcfg.tv_weights    or _cfg.TV_WEIGHTS

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    client = _HTTP_CLIENT

    try:
        genre_ids, is_textless, logos, release_year, title, poster_path, tmdb_data = (
            await fetch_poster_metadata(client, tmdb_id, effective_tmdb_key, type)
        )

        if rating_already_cached or not effective_mdblist_key:
            rating_coro = _resolved(
                (cached_ratings_dict, cached_genre, cached_release_date, [], cached_age_rating)
            )
        else:
            rating_coro = _with_retry(
                fetch_rating,
                client, imdb_id, effective_mdblist_key, genre_ids, type,
                movie_weights=effective_movie_weights,
                tv_weights=effective_tv_weights,
            )

        # Quality is always fetched in the background (never inline); the 4th
        # gather slot was removed after quality_needs_fetch was made always-False.
        (
            image,
            logo,
            rating_result,
            trending_rank,
        ) = await asyncio.gather(
            fetch_poster_image(client, tmdb_id, type, poster_path),
            fetch_logo(client, logos, rcfg.logo_language) if is_textless else _resolved(None),
            rating_coro,
            fetch_trending_rank(client, tmdb_id, effective_tmdb_key, type),
        )

        # ------------------------------------------------------------------
        # Unpack results
        # ------------------------------------------------------------------
        rating_failed = (
            not rating_already_cached
            and effective_mdblist_key
            and (rating_result is FETCH_FAILED)
        )

        if rating_failed:
            logger.warning(f"Rating fetch failed for {imdb_id} after retry — skipping rating cache")
            await coord.set_backoff(coord.NS_RATING_BACKOFF, imdb_id, ttl_seconds=3600.0)
            logger.info(f"MDBlist back-off set for {imdb_id} (1 hour)")
            ratings_dict   = {}
            genre          = cached_genre or "Unknown"
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

            if isinstance(ratings_dict, dict):
                weights = (
                    effective_tv_weights
                    if type in ("tv", "series")
                    else effective_movie_weights
                )
                score = calculate_weighted_score(ratings_dict, weights)
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

        logger.info(f"Quality for {imdb_id}: tokens={quality_tokens} year={release_year}")

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
        )

        # Offload CPU-bound PIL compositing + JPEG encoding to the thread pool
        # so the event loop stays free for concurrent requests.
        _bp_args = dict(
            logo=logo if is_textless else None,
            fallback_title=title if is_textless and not logo else None,
            discovery_meta=discovery_meta,
            quality_tokens=quality_tokens,
            release_year=release_year,
            age_rating=age_rating,
        )

        def _composite_and_encode() -> bytes:
            result = build_poster(image, score, genre, rcfg, **_bp_args)
            buf = io.BytesIO()
            result.convert("RGB").save(buf, format="JPEG", quality=85)
            return buf.getvalue()

        # Acquire a render slot. If RENDER_QUEUE_TIMEOUT is configured and
        # exceeded, return 503 with Retry-After so the client backs off
        # instead of compounding the saturation. The semaphore is created
        # lazily inside the event loop the first time we get here.
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
        # Skipped when quality is still being fetched in the background — the
        # cached composite would be missing badges.  The next request will find
        # quality cached and store a proper composite then.
        if final_cache_key is not None and not quality_pending:
            await set_cached_final_poster(final_cache_key, img_bytes)
            logger.info(f"Final poster cached for {final_cache_key}")

        if _render_fut is not None:
            _render_fut.set_result(img_bytes)

        response = Response(content=img_bytes, media_type="image/jpeg")
        if _cfg.CDN_CACHE_TTL > 0:
            response.headers["Cache-Control"] = f"public, max-age={_cfg.CDN_CACHE_TTL}"
        return response

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
            # Invalidate the metadata cache so the next request re-fetches fresh data.
            _endpoint = "tv" if type in ("tv", "series") else "movie"
            delete_cached_tmdb_metadata(f"{_endpoint}_{tmdb_id}")
            logger.warning(
                f"TMDB image 404 for tmdb_id={tmdb_id} — metadata cache invalidated, "
                f"will self-heal on next request"
            )
            raise HTTPException(status_code=404, detail="Poster image not found on TMDB")
        logger.error(f"Upstream HTTP {status} for tmdb_id={tmdb_id}: {exc}")
        raise HTTPException(status_code=502, detail=f"Upstream error {status}")
    except HTTPException:
        # Preserve intentional HTTPExceptions raised inside the try block —
        # notably the 503/Retry-After from render-queue saturation — so the
        # generic Exception handler below doesn't downgrade them to 500.
        raise
    except Exception as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.exception(f"Error building poster for tmdb_id={tmdb_id}")
        raise HTTPException(status_code=500, detail="Failed to build poster")
    finally:
        # Always fire the rating event so any coalesced waiters unblock,
        # regardless of whether the fetch succeeded or failed.
        if _rating_event_to_set is not None:
            _rating_event_to_set.set()
            _rating_fetch_inflight.pop(imdb_id, None)
        if final_cache_key is not None:
            _render_inflight.pop(final_cache_key, None)