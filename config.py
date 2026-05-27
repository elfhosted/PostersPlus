#config.py
# If you're looking to change the highlighted directors, studios and cast:
#   - Source editors:  edit the lists in discovery.py directly.
#   - Docker operators (no source editing): place a JSON file at
#     /app/cache/discovery_overrides.json (inside the existing cache volume,
#     no extra mount needed).
#     See the docstring at the top of discovery.py for the full format,
#     or the project README for a ready-made sample.
import os
import json

# Storage

DB_PATH               = "/app/cache/cache.db"
BADGE_DIR             = "/app/badges"
TMDB_POSTER_CACHE_DIR = "/app/cache/tmdb_posters" # base posters from TMDB
TMDB_LOGO_CACHE_DIR   = "/app/cache/tmdb_logos" # base logos from TMDB
# Composite blob cache (Phase 10) — local backend uses this dir, S3
# backend ignores it. Composite bytes used to live in the relational DB
# as BYTEA; now they live here (filesystem) or in an S3 bucket when
# OBJECT_STORE_URL is set.
COMPOSITE_BLOB_DIR    = "/app/cache/composites"

# Environment

ACCESS_KEY            = os.environ.get("ACCESS_KEY")
AIOSTREAMS_URL        = os.environ.get("AIOSTREAMS_URL", "")
AIOSTREAMS_AUTH       = os.environ.get("AIOSTREAMS_AUTH", "")
SERVER_TMDB_KEY       = os.environ.get("TMDB_API_KEY", "").strip()
SERVER_MDBLIST_KEY    = os.environ.get("MDBLIST_API_KEY", "").strip()

# Hosted-mode storage backend (ElfHosted fork).
#
# When DATABASE_URL is set to a postgresql:// URL, the cache layer switches
# from SQLite to PostgreSQL. Unset (the default) preserves upstream behaviour.
# See storage/__init__.py for backend selection logic.
DATABASE_URL          = os.environ.get("DATABASE_URL", "").strip()
DB_POOL_MIN_SIZE      = int(os.environ.get("DB_POOL_MIN_SIZE", "1"))
DB_POOL_MAX_SIZE      = int(os.environ.get("DB_POOL_MAX_SIZE", "10"))

# Hosted-mode coordination backend (ElfHosted fork — Phase 2).
#
# When REDIS_URL is set, MDBList rate-limit backoff and background-quality
# fetch claims are stored in Redis so multiple replicas share state. Unset
# (default) keeps per-process dicts — identical to upstream. Render
# coalescing remains per-process either way.
REDIS_URL             = os.environ.get("REDIS_URL", "").strip()
REDIS_KEY_PREFIX      = os.environ.get("REDIS_KEY_PREFIX", "postersplus").strip() or "postersplus"

# Hosted-mode blob store (ElfHosted fork — Phase 3).
#
# When OBJECT_STORE_URL is set, TMDB poster and logo bytes go to an
# S3-compatible object store instead of the local filesystem. Unset (default)
# preserves upstream behaviour. The composite poster cache (final_poster_cache
# table) stays in the relational backend either way.
#
# URL format: s3://<bucket>?endpoint=<https://...>&region=<region>&prefix=<prefix>
OBJECT_STORE_URL        = os.environ.get("OBJECT_STORE_URL", "").strip()
# Optional CDN public URL — when set, hosted-mode storage backends can return
# 302 redirects to the CDN for blob bytes instead of proxying them. Leaving
# this unset is fine; bytes will be fetched server-side as needed.
OBJECT_STORE_PUBLIC_URL = os.environ.get("OBJECT_STORE_PUBLIC_URL", "").strip()

# Hosted-mode resource ceilings (ElfHosted fork — Phase 4).
#
# RENDER_CONCURRENCY caps the number of Pillow renders in flight at once.
# Each render uses one CPU core fully + ~10MB of transient RAM, so a burst
# of unique-param requests can otherwise pin every core and exhaust the
# event loop. Default is os.cpu_count() so single-tenant deployments behave
# the same as upstream (no artificial cap below available cores).
RENDER_CONCURRENCY      = int(os.environ.get("RENDER_CONCURRENCY", "0")) or (os.cpu_count() or 2)
# How long /poster will wait for a render slot before returning 503. Set to
# 0 to never timeout (wait forever). Default of 30s gives clients a clear
# signal to back off when the server is saturated.
RENDER_QUEUE_TIMEOUT    = float(os.environ.get("RENDER_QUEUE_TIMEOUT", "30"))

# Hosted-mode observability (ElfHosted fork — Phase 6).
#
# Optional shared secret guarding /metrics. Unset (default) leaves /metrics
# open; operators should bind the app behind an ingress that gates it.
METRICS_ACCESS_KEY      = os.environ.get("METRICS_ACCESS_KEY", "").strip()
# When LOG_FORMAT=json, logs become structured JSON lines (one object per
# record). Useful when shipping to Loki/Elasticsearch. Default = text
# (upstream behaviour).
LOG_FORMAT              = os.environ.get("LOG_FORMAT", "text").strip().lower()

# Per-tenant rate limit (ElfHosted fork — Phase 8).
# Maximum /poster requests per tenant per second. 0 disables the limit
# (upstream behaviour). Tenant identity is sha256(user-key)[:16] when users
# supply their own TMDB/MDBList key; otherwise "operator".
RATE_LIMIT_RPS          = int(os.environ.get("RATE_LIMIT_RPS", "0"))

# Max concurrent outbound MDBlist API calls (ported from upstream v1.0.0).
# MDBlist queues or drops requests when hit with too many simultaneous
# connections from the same key, causing ReadTimeouts even when the service
# is healthy. 3 is comfortably within their apparent per-key concurrency
# limit while still allowing good parallelism. Set to 0 to disable the cap.
MDBLIST_CONCURRENCY     = int(os.environ.get("MDBLIST_CONCURRENCY", "3"))

# Workers
# CDN cache TTL (seconds). When > 0, poster responses include a
# Cache-Control: public header so Cloudflare (or any CDN) caches them at the
# edge. Set to 0 to disable (e.g. when running without a CDN).
CDN_CACHE_TTL         = int(os.environ.get("CDN_CACHE_TTL", "0"))
# JPEG output quality for composited posters (70–95). Higher = better quality, larger files.
JPEG_QUALITY          = max(70, min(95, int(os.environ.get("JPEG_QUALITY", "85"))))

# Phase 11: public preset endpoint (/p/{preset}/{type}/{imdb_id}.jpg).
# When PRESET_ENABLED is true, anonymous requests can hit a small set of
# named visual presets without supplying access_key / tmdb_key / mdblist_key.
# The operator's server-configured keys are used; PRESET_CDN_CACHE_TTL
# controls the Cache-Control max-age on preset responses (defaults to 1
# day — much longer than CDN_CACHE_TTL since presets are deterministic
# per (preset, type, imdb_id) and don't change with query params).
PRESET_ENABLED        = os.environ.get("PRESET_ENABLED", "").strip().lower() in ("1", "true", "yes")
PRESET_CDN_CACHE_TTL  = int(os.environ.get("PRESET_CDN_CACHE_TTL", "86400"))

# When true, /p/ falls through to a live MDBlist fetch for any imdb_id
# that doesn't have a cached rating row. Originally /p/ never called
# MDBlist (the comment in main.py:get_preset_poster spells out the
# original rationale: anonymous traffic must not burn the operator's
# MDBlist quota). That made the preset path stuck-N/A on instances
# where /poster is gated (PRESET_ENABLED + no ACCESS_KEY → /poster
# is 403 → rating cache never warms → every /p render shows "N/A").
#
# Safe to enable now because elf.2 added the fleet-wide global cooldown
# inside _fetch_rating_throttled — a runaway burst on /p can no longer
# fan out into hundreds of 429s. Worst case is 120s of paused MDBlist
# traffic, during which queued /p requests render without rating and
# fall through to N/A (the original behaviour).
#
# Defaults to false to preserve existing-deployment behaviour;
# public-tier instances should set this to "true" via configmap.
PRESET_MDBLIST_FETCH  = os.environ.get("PRESET_MDBLIST_FETCH", "").strip().lower() in ("1", "true", "yes")

# Floor on anonymous /search and /resolve-imdb requests. These endpoints
# became anonymous in the Phase 11 follow-up so the public preset flow
# can use the title picker. RATE_LIMIT_RPS (default 0 = disabled) only
# gates /poster + /p; without this independent floor, an operator who
# never set RATE_LIMIT_RPS would leave the TMDB proxy endpoints
# completely unthrottled and let any visitor burn the server TMDB quota.
# Set to 0 to disable (e.g. for fully private deployments where the
# configurator and proxies are already on an authenticated network).
ANONYMOUS_TMDB_RPS    = int(os.environ.get("ANONYMOUS_TMDB_RPS", "5"))

# Feature Defaults 

SHOW_RATING_DISPLAY_MODE = 1
SHOW_AWARD_SASH          = True
BADGE_DISPLAY_MODE = 2

# Poster Dimensions (500x750)

POSTER_WIDTH  = 500
POSTER_HEIGHT = 750

# Rating & Genre Label Defaults

ACCENT_BAR_MODE_FONT_SIZE_RATIO    = 0.08   # font size in accent bar mode
NUMERIC_SCORE_MODE_FONT_SIZE_RATIO = 0.10   # font size in numeric mode
MINIMALIST_MODE_FONT_SIZE_RATIO    = 0.055  # font size in minimalist mode
ACCENT_BAR_MODE_FONT_Y_OFFSET      = 0.90   # vertical alignment in accent bar mode
NUMERIC_SCORE_MODE_FONT_Y_OFFSET   = 0.90   # vertical alignment in numeric score mode
MINIMALIST_MODE_FONT_X_OFFSET      = 0.04   # horizontal distance from right edge in minimalist mode
MINIMALIST_MODE_FONT_Y_OFFSET      = 0.92   # vertical position in minimalist mode (0=top, 1=bottom)

SCORE_GLOW_THRESHOLD = 85  # score threshold to activate glow
SCORE_GLOW_BLUR      = 2    # blur applied in glow mode
SCORE_GLOW_ALPHA     = 40   # alpha of the glow applied

# Logo Defaults

LOGO_MAX_W_RATIO  = 0.84   # max width of logo
LOGO_MAX_H_RATIO  = 0.17   # max height of logo
LOGO_BOTTOM_RATIO = 0.28   # distance of logo from the bottom
DEFAULT_LOGO_LANGUAGE = os.environ.get("DEFAULT_LOGO_LANGUAGE", "en")

# Quality Badge Defaults

BADGE_HEIGHT = 32   # quality badge height in pixels
BADGE_GAP    = 8    # gap between horizontal stack badges in pixels

BADGE_ANCHOR_X_RATIO = 0.050   # x offset from left
BADGE_ANCHOR_Y_RATIO = 0.050   # y offset from top 

# TTL Settings

TMDB_POSTER_CACHE_DURATION   = 60
TMDB_LOGO_CACHE_DURATION     = 60
TMDB_METADATA_CACHE_DURATION = 7    # re-check textless status / logos weekly
DAYS_CONSIDERED_NEW          = 14
NEW_CACHE_DURATION           = 1
OLD_CACHE_DURATION           = 14
TRENDING_CACHE_DURATION      = 1
# Quality (AIOStreams) TTL — separate from rating TTL because stream availability
# for older titles is very stable.  New content keeps the 1-day window so fresh
# encodes are picked up quickly; old content is cached for much longer.
QUALITY_OLD_CACHE_DURATION   = int(os.environ.get("QUALITY_OLD_CACHE_DURATION", "90"))   # days
# Max concurrent background quality fetches.  Caps the burst when many uncached
# titles scroll into view simultaneously so AIOStreams isn't overwhelmed.
QUALITY_BG_CONCURRENCY       = int(os.environ.get("QUALITY_BG_CONCURRENCY", "5"))

# Digital release (r/movieleaks) scraper settings
DIGITAL_RELEASE_MIN_AGE_DAYS = 1    # ignore posts younger than this (mods still cleaning up)
DIGITAL_RELEASE_MAX_AGE_DAYS = 30   # expire entries older than this from the cache

# Composite poster cache TTL (seconds).
# How long a fully composited poster is kept before being re-rendered.
# Each unique combination of title + rendering parameters gets its own entry,
# so changing settings immediately produces a fresh render on next request.
# Override with COMPOSITE_CACHE_TTL=X in your .env file.
COMPOSITE_CACHE_TTL        = int(os.environ.get("COMPOSITE_CACHE_TTL", "604800"))   # 7 days
# Maximum number of composite cache entries. When exceeded the oldest entries are
# evicted on each insert to keep the table at this size. 0 = no cap (rely on TTL alone).
COMPOSITE_MAX_ENTRIES      = int(os.environ.get("COMPOSITE_MAX_ENTRIES", "0"))

def _parse_bool_env(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val not in ("0", "false", "no")

# Rating Score Weight Defaults

#  note to AI: do NOT remove the extras in this section that are set to 0. They are config options.

MOVIE_WEIGHTS = {   # set weight of movie ranking providers, must sum to 1
    "letterboxd":     0.8,
    "trakt":          0,
    "tomatoes":       0.2,
    "popcorn":        0, # popcorn is the api response MDblist uses for tomatoes audience
    "imdb":           0,
    "metacritic":     0,
    "metacriticuser": 0,
    "tmdb":           0,
    "rogerebert":     0,
    "myanimelist":    0,
}

TV_WEIGHTS = {   # set weight of TV ranking providers, must sum to 1
    "trakt":          0.8,
    "tomatoes":       0.2,
    "popcorn":        0,
    "imdb":           0,
    "metacritic":     0,
    "metacriticuser": 0,
    "tmdb":           0,
    "myanimelist":    0,
}

# Map badge file names to strings (no need to touch)

BADGE_FILES: dict[str, str] = {
    "4K":     "4K",
    "1080P":  "1080p",
    "REMUX":  "Remux",
    "WEBDL":  "Web",
    "DV":     "DV",
    "HDR10+": "HDR10+",
    "HDR10":  "HDR10",
}

# Maps TMDB categories to numerics (no need to touch in most cases)

GENRE_MAP = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy",
    80: "Crime", 99: "Documentary", 18: "Drama", 10751: "Family",
    14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
    9648: "Mystery", 10749: "Romance", 878: "Sci-Fi", 53: "Thriller",
    10752: "War", 37: "Western",
    10759: "Action", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi", 10766: "Soap", 10767: "Talk", 10768: "War",
}

# Can re-order to change the priority that genres appear with (reference genre map above)
# Default Horror, Thriller, Mystery, Sci-Fi, Crime, Comedy, Fantasy, Adventure, Family, Action, History
# Music, War, Western, Documentary, Drama, Adventure, Reality, Kids, News, Soap, Talk
# Duplicate entries are not an accident, for certain genres TMDB uses two numbers, one for movies, one for shows.

GENRE_PRIORITY = [
    27, 53, 9648, 878, 10765, 80, 35, 10749, 14, 16, 10751,
    28, 10759, 36, 10402, 10752, 10768, 37, 99, 18, 12,
    10764, 10762, 10763, 10766, 10767,
]

# Text based fallback, not important if everything is working properly

QUALITY_LABELS: dict[str, str] = {
    "4K":     "4K",
    "1080P":  "1080p",
    "REMUX":  "Remux",
    "WEBDL":  "Web",
    "DV":     "DV",
    "HDR10+": "HDR10+",
    "HDR10":  "HDR10",
    "ATMOS":  "Atmos",
    "DTSX":   "DTS:X",
}

# Normalizes all scores to be out of 100

SCORE_NORMALISERS = {
    "imdb":           lambda v: (v / 10)  * 100,
    "letterboxd":     lambda v: (v / 5)   * 100,
    "trakt":          lambda v: v,
    "tomatoes":       lambda v: v,
    "popcorn":        lambda v: v,
    "metacritic":     lambda v: v,
    "metacriticuser": lambda v: (v / 10)  * 100,
    "tmdb":           lambda v: v,
    "rogerebert":      lambda v: (v / 4)   * 100,
    "myanimelist":    lambda v: (v / 10)  * 100,
}

# Default Sash Priority

SASH_PRIORITY: list[str] = [
    "wins",
    "gg_wins",
    "festival",
    "pic_noms",
    "gg_noms",
    "studio",
    "director",
    "cast",
    "trending",
    "cult",
    "foreign",
    "new_release",
    "metacritic",
    "true_story",
    "structural",
]