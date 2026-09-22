"""Sync PostersPlus-rendered posters into a Jellyfin Media Server library.

STANDALONE COMPANION SCRIPT — not part of the FastAPI service, not imported
by main.py, and not wired into the Docker image. Run it separately (locally,
on a schedule, however fits your workflow) against a running PostersPlus
instance and a reachable Jellyfin server. This is the Jellyfin sibling of
plex_sync.py — same idea, same shape, ported to Jellyfin's REST API.

Unlike Stremio (which fetches poster URLs lazily whenever it renders a
catalog), Jellyfin has no "arbitrary URL, fetched live on render" mechanism —
artwork has to be *pushed* into its library via the API. That's what this
script does: for each movie/show in the configured libraries, it
  1. reads the IMDb/TMDB IDs Jellyfin already has from its ProviderIds,
  2. derives real quality-badge tokens from the file's own media info
     (resolution / HDR / audio — see extract_quality_tokens), and
  3. fetches the rendered poster from PostersPlus and POSTs it to Jellyfin's
     `/Items/{id}/Images/Primary` endpoint.

Why "real" quality info instead of AIOStreams: on Stremio, quality badges
answer "what's available to stream right now" (scraped from addons, before
you've played anything). On Jellyfin you already own the file — its actual
resolution/HDR/codec is sitting right there in the library, more accurate
than anything a scraper could guess. PostersPlus already supports this via
a plain pass-through `quality=` override (see quality.parse_quality), so no
changes to the rendering pipeline were needed — only this adapter.

No Jellyfin SDK dependency: this talks to Jellyfin's REST API directly with
plain httpx (the same library this script already uses to call PostersPlus,
and a core PostersPlus dependency already) — one less thing to install versus
plex_sync.py's `plexapi` requirement.

A structural advantage over the Plex version: Jellyfin's media-stream info
includes a `VideoRangeType` field that *directly* distinguishes HDR10 from
HDR10+ (and several Dolby-Vision-with-fallback combinations) — see
_VIDEO_RANGE_TO_TOKEN. Plex only exposes "this stream uses the PQ
(smpte2084) transfer function", which can't tell HDR10 and HDR10+ apart
(plex_sync.py documents that as a known under-detection risk). Jellyfin
doesn't have that particular blind spot.

CAVEATS — read before trusting this against a big library:
  * The quality-token mapping in extract_quality_tokens() was written from
    REST API documentation and source-code reading, NOT validated against a
    live server (the dev environment this was written in can't reach a real
    Jellyfin instance). Field names, casing, and exact VideoRangeType values
    may differ from what your server actually returns. ALWAYS run with
    --dry-run and --inspect first and sanity-check the derived tokens against
    a handful of titles you know well — exactly the same workflow that
    refined plex_sync.py's mapping against real Plex data. Report back what
    --inspect shows for a few titles and the mapping can be corrected the
    same way the Plex one was.
  * 4K/1080p detection is threshold-based on the primary video stream's
    Width/Height (Jellyfin doesn't expose a simple "2160"/"1080" resolution
    label the way Plex's `videoResolution` does) — see the constants near
    extract_quality_tokens(). Edge cases (anamorphic/odd aspect ratios,
    unusual encodes) may be misclassified; --inspect dumps the raw
    width/height so the thresholds can be tuned against real data.
  * REMUX/WEB-DL detection is the same best-effort text search plex_sync.py
    uses — regex over the file path AND the video stream's DisplayTitle/
    Title (organizing tools routinely strip source info from on-disk
    filenames, but Jellyfin — like Plex — often keeps the original
    scene-release name in the stream's embedded title metadata).
  * TV shows derive their quality tokens from a single representative episode
    (S01E01 by convention — same rule Stremio's own quality badges use, and
    the same rule plex_sync.py now follows) rather than the show as a whole.
    See representative_episode(). Necessarily an approximation — a show spans
    many episodes that may genuinely differ in quality.
  * When extract_quality_tokens() legitimately finds NOTHING, this sends
    `quality=NONE` to PostersPlus rather than omitting `quality=` — see the
    big comment in build_poster_request() for why (short version: PostersPlus
    treats a missing/empty `quality=` as "go fetch one from AIOStreams
    yourself", which answers "what's available to stream right now on the
    internet" and can produce wildly-too-generous badges for an old/low-
    quality local rip). This is a PostersPlus-side behaviour, not a
    Jellyfin-specific concern — the fix is identical to plex_sync.py's.
  * POSTER PERSISTENCE — this is the one place this script's behaviour is
    genuinely DIFFERENT from (and less certain than) plex_sync.py's, and is
    worth extra attention once you're testing against a live server:
    Plex's `uploadPoster()` is documented to LOCK the image field, so normal
    metadata refreshes leave it alone. Jellyfin's `POST /Items/{id}/Images/
    Primary` does NOT appear to carry the same guarantee — community reports
    describe custom images reverting after library/metadata refreshes unless
    the item's metadata is manually locked via the web UI (item → edit →
    padlock). This script deliberately does NOT try to lock anything via the
    API itself: Jellyfin's lock is a coarse, whole-item `LockData` flag (or a
    named `LockedFields` list) rather than a per-image lock, and flipping it
    automatically risks freezing fields you didn't mean to freeze (Overview,
    Genres, ProviderIds, ...) — too big a side effect for this script to take
    on your behalf. If you find posters reverting, lock them manually via the
    UI. Also note: this script's "unchanged, skip" state-fingerprint check has
    no way to detect a silent server-side revert — if Jellyfin quietly puts
    the old image back, the fingerprint still matches and the item is skipped.
    `--force` (or deleting the state file) is your escape hatch if that
    happens. Please report back what you see — this is the part of the
    Jellyfin port that most needs real-world validation.

Quick start — two ways to configure:
  1. Environment variables (shown below), or
  2. Open this file and fill in the "EASY CONFIG" block near the top
     (JELLYFIN_BASE_URL_DEFAULT, JELLYFIN_API_KEY_DEFAULT, etc.) so you don't
     have to export anything each time — env vars still override those
     defaults if both are set.

    pip install -r requirements-jellyfin.txt
    export JELLYFIN_BASE_URL="http://192.168.1.50:8096"
    export JELLYFIN_API_KEY="xxxxxxxxxxxxxxxxxxxx"   # Dashboard -> Advanced -> API Keys
    export POSTERSPLUS_URL="http://localhost:8000"
    python jellyfin_sync.py --dry-run --limit 5

POSTERSPLUS_URL also accepts a full recipe URL copied straight from the
configurator's "Copy URL" button — e.g.
    POSTERSPLUS_URL="http://localhost:8000/poster?bar_style=frosted&sash_badge_inset=0.000&..."
— in which case every one of those query parameters (gradients, bar/badge
styles, weighting profiles, everything) is applied as a default to every
synced poster. Only tmdb_id/type/quality/primary_client (and imdb_id, when the
item has one) are always
computed per-item and override whatever the recipe URL says for those keys.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

logger = logging.getLogger("jellyfin_sync")


# ---------------------------------------------------------------------------
# >>> EASY CONFIG — fill these in if you'd rather not export environment
# variables every time you run this script. Anything left blank here just
# falls back to the matching environment variable below (and an env var, if
# set, always WINS over whatever you put here — so scheduled tasks / CI can
# still override these without editing the file).
#
#   JELLYFIN_BASE_URL_DEFAULT: your server address, e.g. "http://192.168.1.50:8096"
#   JELLYFIN_API_KEY_DEFAULT:  an API key from Dashboard -> Advanced -> API Keys
#   POSTERSPLUS_URL_DEFAULT:   bare server URL, OR a full "Copy URL" recipe URL
#
# SECURITY NOTE: JELLYFIN_API_KEY_DEFAULT is a credential. If this file lives
# in a git checkout, filling it in here means it can end up committed by
# accident. Either keep using the environment variable for the key
# specifically, or make a personal copy of this script outside the repo
# (and/or add it to .gitignore) before pasting a real key into it.
# ---------------------------------------------------------------------------

JELLYFIN_BASE_URL_DEFAULT = "http://localhost:8096"
JELLYFIN_API_KEY_DEFAULT = ""
# Almost never needed — leave blank unless /Items comes back empty for you.
# Some older/edge-case server configurations scope item listing to a user;
# if that turns out to be true for your server, set this to a user's GUID
# (Dashboard -> Users -> click a user -> the Id is in the page URL) and it'll
# be added as `userId=` on every /Items and /Shows/.../Episodes call.
JELLYFIN_USER_ID_DEFAULT = ""
POSTERSPLUS_URL_DEFAULT = "http://localhost:8000"
POSTERSPLUS_ACCESS_KEY_DEFAULT = ""  # only required if set on PostersPlus
POSTERSPLUS_TMDB_KEY_DEFAULT = ""    # not required if set on PostersPlus server side
POSTERSPLUS_MDBLIST_KEY_DEFAULT = "" # not required if set on PostersPlus server side
JELLYFIN_LIBRARY_SECTIONS_DEFAULT = ""  # comma-separated library names, e.g. "Movies,4K Movies"
JELLYFIN_SYNC_STATE_PATH_DEFAULT = "jellyfin_sync_state.json"

# ---------------------------------------------------------------------------
# Configuration — env vars, mirroring PostersPlus's own config.py conventions
# (each one falls back to the EASY CONFIG default above if unset)
# ---------------------------------------------------------------------------

JELLYFIN_BASE_URL = os.environ.get("JELLYFIN_BASE_URL", JELLYFIN_BASE_URL_DEFAULT).rstrip("/")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", JELLYFIN_API_KEY_DEFAULT)
JELLYFIN_USER_ID = os.environ.get("JELLYFIN_USER_ID", JELLYFIN_USER_ID_DEFAULT)


def _parse_postersplus_url(raw: str) -> tuple[str, dict]:
    """Split POSTERSPLUS_URL into (base_url, recipe_defaults).

    Identical logic to plex_sync.py's helper of the same name — see there for
    the full rationale. Short version: POSTERSPLUS_URL accepts either a bare
    server address or a full "Copy URL" recipe URL; in the latter case every
    query parameter on it becomes a per-item rendering default, with this
    script's own dynamic values (tmdb_id, imdb_id, type, quality,
    primary_client) always winning since a hardcoded value in a copy-pasted
    URL would be wrong for every title except the one it was copied from.
    """
    parts = urlsplit(raw)
    recipe_defaults = dict(parse_qsl(parts.query, keep_blank_values=True))
    path = parts.path
    if path.endswith("/poster"):
        path = path[: -len("/poster")]
    base = urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", ""))
    return base or "http://localhost:8000", recipe_defaults


POSTERSPLUS_URL, POSTERSPLUS_RECIPE_DEFAULTS = _parse_postersplus_url(
    os.environ.get("POSTERSPLUS_URL", POSTERSPLUS_URL_DEFAULT)
)
# Only needed if your PostersPlus instance requires them (server-side keys
# configured / ACCESS_KEY set). Leave unset if your instance handles it.
POSTERSPLUS_ACCESS_KEY = os.environ.get("POSTERSPLUS_ACCESS_KEY", POSTERSPLUS_ACCESS_KEY_DEFAULT)
POSTERSPLUS_TMDB_KEY = os.environ.get("POSTERSPLUS_TMDB_KEY", POSTERSPLUS_TMDB_KEY_DEFAULT)
POSTERSPLUS_MDBLIST_KEY = os.environ.get("POSTERSPLUS_MDBLIST_KEY", POSTERSPLUS_MDBLIST_KEY_DEFAULT)

# Comma-separated Jellyfin library (collection) names to sync, e.g.
# "Movies,4K Movies". Empty = sync every movie/show library the server has.
LIBRARY_SECTIONS = [
    s.strip() for s in os.environ.get("JELLYFIN_LIBRARY_SECTIONS", JELLYFIN_LIBRARY_SECTIONS_DEFAULT).split(",")
    if s.strip()
]

# Where to remember what's already been synced, so re-runs only touch items
# whose derived poster would actually change. Delete this file (or pass
# --force) to re-sync everything from scratch.
STATE_PATH = Path(os.environ.get("JELLYFIN_SYNC_STATE_PATH", JELLYFIN_SYNC_STATE_PATH_DEFAULT))


# ---------------------------------------------------------------------------
# Recipe fingerprint — captures the *rendering* settings (gradients, badge
# styles, weighting profiles, everything POSTERSPLUS_URL contributes as a
# recipe default) so that changing your poster style invalidates the sync
# state and triggers a re-render/re-upload, instead of being silently skipped
# as "unchanged" forever (see the fingerprint comment in sync_item).
# ---------------------------------------------------------------------------
RECIPE_FINGERPRINT = "&".join(
    f"{k}={v}" for k, v in sorted(POSTERSPLUS_RECIPE_DEFAULTS.items())
)


# ---------------------------------------------------------------------------
# Jellyfin REST client — thin wrapper around plain httpx, no SDK dependency.
# ---------------------------------------------------------------------------
#
# Auth: Jellyfin (and Emby, which it forked from) accept a pre-generated API
# key via the `Authorization: MediaBrowser ...` header. The Client/Device/
# DeviceId/Version fields are mostly cosmetic for API-key auth (they identify
# "what app is this" in the server's session list) — any stable values work;
# what matters is the Token. The older `X-Emby-Token` header and `api_key`/
# `ApiKey` query-string forms also work today but are flagged for eventual
# removal, so this uses the documented forward-looking form.
_JELLYFIN_AUTH_HEADER = (
    'MediaBrowser Client="PostersPlus-sync", Device="script", '
    'DeviceId="postersplus-jellyfin-sync", Version="1.0", Token="{token}"'
)

# How many items to request per /Items / /Shows/.../Episodes page. Jellyfin
# (like most REST APIs) caps/paginates list results — without paging through
# `StartIndex`, large libraries would silently only yield their first page.
_PAGE_SIZE = 200


def _jf_params(**params):
    """Build query params for a Jellyfin API call, adding `userId` if the
    user configured JELLYFIN_USER_ID (see the EASY CONFIG comment for when
    that's actually needed — normally never), and dropping anything left as
    None so optional filters don't get serialised as the literal string 'None'.
    """
    if JELLYFIN_USER_ID:
        params["userId"] = JELLYFIN_USER_ID
    return {k: v for k, v in params.items() if v is not None}


def jf_get(client: httpx.Client, path: str, **params) -> dict:
    resp = client.get(path, params=_jf_params(**params))
    resp.raise_for_status()
    return resp.json()


def iter_libraries(client: httpx.Client) -> list[dict]:
    """Return the server's library ("virtual folder") definitions.

    `/Library/VirtualFolders` is an admin-level endpoint that returns each
    library's display Name, its CollectionType ("movies"/"tvshows"/"music"/
    "mixed"/None/...), and crucially its ItemId — the id to pass as `ParentId`
    when listing the library's contents via /Items. Returns a bare JSON array
    on the servers this was written against; defensively also handles a
    {"Items": [...]} envelope in case that differs across versions.
    """
    data = jf_get(client, "/Library/VirtualFolders")
    if isinstance(data, list):
        return data
    return data.get("Items") or []


# Maps a library's CollectionType to the Jellyfin BaseItem `Type` values we
# want out of it. Libraries with other/no CollectionType (music, books, mixed
# "movies and shows" libraries, photos, ...) are skipped entirely — mirrors
# plex_sync.py's `if section.type not in ("movie", "show"): continue`.
_LIBRARY_ITEM_TYPES = {
    "movies": ("Movie",),
    "tvshows": ("Series",),
}


def _paginated_items(client: httpx.Client, *, path: str, **params) -> "iter[dict]":
    """Yield every item from a paginated Jellyfin list endpoint, following
    `StartIndex`/`TotalRecordCount` until exhausted. Both /Items and
    /Shows/{id}/Episodes use this same {"Items": [...], "TotalRecordCount":
    N} envelope.
    """
    start = 0
    while True:
        data = jf_get(client, path, **params, StartIndex=start, Limit=_PAGE_SIZE)
        items = data.get("Items") or []
        if not items:
            return
        yield from items
        start += len(items)
        total = data.get("TotalRecordCount")
        if total is None or start >= total:
            return


def iter_library_items(client: httpx.Client):
    libraries = iter_libraries(client)
    if LIBRARY_SECTIONS:
        wanted = set(LIBRARY_SECTIONS)
        libraries = [lib for lib in libraries if lib.get("Name") in wanted]
        missing = wanted - {lib.get("Name") for lib in libraries}
        for name in missing:
            logger.warning(f"Library {name!r} not found on this server — skipping")

    for lib in libraries:
        collection_type = (lib.get("CollectionType") or "").lower()
        include_types = _LIBRARY_ITEM_TYPES.get(collection_type)
        name = lib.get("Name") or "?"
        if not include_types:
            continue
        item_id = lib.get("ItemId")
        if not item_id:
            logger.warning(f"Library {name!r} has no ItemId in /Library/VirtualFolders — skipping")
            continue
        logger.info(f"Scanning library {name!r} ({collection_type})")
        yield from _paginated_items(
            client, path="/Items",
            ParentId=item_id,
            Recursive="true",
            IncludeItemTypes=",".join(include_types),
            Fields="ProviderIds,MediaSources,MediaStreams,Path",
        )


# ---------------------------------------------------------------------------
# ID extraction — Jellyfin already carries IMDb/TMDB IDs in its ProviderIds
# ---------------------------------------------------------------------------

def extract_ids(item: dict) -> tuple[str | None, str | None]:
    """Pull (imdb_id, tmdb_id) out of a Jellyfin item's ProviderIds.

    Jellyfin's BaseItemDto carries these as a flat dict, e.g.:
        "ProviderIds": {"Imdb": "tt8772262", "Tmdb": "530385", "Tvdb": "..."}

    Casing of the keys is normally "Imdb"/"Tmdb"/"Tvdb" (matches the provider
    plugin names) but is lower-cased here defensively — third-party metadata
    plugins and older server versions have been known to vary.
    """
    provider_ids = item.get("ProviderIds") or {}
    lowered = {str(k).lower(): v for k, v in provider_ids.items() if v}
    return lowered.get("imdb"), lowered.get("tmdb")


# ---------------------------------------------------------------------------
# Quality-token derivation — translate Jellyfin's *real* file metadata into
# the same token vocabulary PostersPlus expects via its `quality=` override
# (see config.QUALITY_LABELS: 4K, 1080P, REMUX, WEBDL, DV, HDR10+, HDR10,
# ATMOS, DTSX).
# ---------------------------------------------------------------------------

# Same rationale as plex_sync.py's _REMUX_RE/_WEBDL_RE — Jellyfin doesn't
# expose "Remux"/"Web-DL" as structured properties either (they describe a
# rip's *source*, not a technical stream property), so they're inferred from
# text: the file path AND the video stream's DisplayTitle/Title, for the same
# "renamed on disk but the embedded release name survives" reason documented
# at length in plex_sync.py's extract_quality_tokens().
_REMUX_RE = re.compile(r"remux", re.I)
_WEBDL_RE = re.compile(r"\bweb[-_. ]?dl\b", re.I)

# Resolution thresholds for the primary video stream's pixel dimensions.
# Jellyfin doesn't hand back a ready-made "2160"/"1080" label the way Plex's
# `videoResolution` does — MediaStream only carries raw Width/Height — so 4K/
# 1080p are inferred from those instead. Generous-but-specific bands: real 4K
# UHD masters are 3840x2160 (or 4096-wide DCI masters); real 1080p is
# 1920x1080. The "OR height" arm catches anamorphic/letterboxed/anti-aliased
# encodes that keep one dimension at the canonical value but shrink the other
# (e.g. an ultra-widescreen crop stored at 3840x1606). Anything below 1080p
# (720p, SD, ...) legitimately yields no resolution token — same as Plex.
_4K_MIN_WIDTH = 3800
_4K_MIN_HEIGHT = 2000
_1080P_MIN_WIDTH = 1900
_1080P_MIN_HEIGHT = 1000

# Jellyfin's `VideoRangeType` is considerably more granular than what Plex
# exposes (Plex can only say "this stream's transfer function is PQ", which
# can't tell HDR10 and HDR10+ apart — see plex_sync.py's documented
# under-detection caveat). All the Dolby-Vision-with-fallback combinations
# collapse to plain "DV" here: PostersPlus's scoring caps the "visual"
# category at 2 points and DV alone is already worth the max, so which
# fallback layer (if any) rides along doesn't change the badge — what matters
# for scoring purposes is simply "this stream IS Dolby Vision". HLG has no
# corresponding PostersPlus token and is deliberately left unmapped (same
# as plain SDR).
_VIDEO_RANGE_TYPE_TO_TOKEN = {
    "dovi":                 "DV",
    "doviwithhdr10":        "DV",
    "doviwithhdr10plus":    "DV",
    "doviwithhlg":          "DV",
    "doviwithel":           "DV",
    "doviwithelhdr10plus":  "DV",
    "hdr10plus":            "HDR10+",
    "hdr10":                "HDR10",
}


def _best_media_source(item: dict) -> dict | None:
    """Return the highest-resolution MediaSource for `item` (handles
    multi-version items — e.g. a library that has both a 4K remux and a 1080p
    fallback copy of the same movie — the same way plex_sync.py's
    _best_media() ranks <Media> entries explicitly rather than trusting
    server ordering).

    Jellyfin's /Items response shape for media info has been observed to vary
    by which `Fields` were requested and by server version: usually a nested
    `MediaSources: [{Path, MediaStreams: [...]}, ...]`, but some responses
    instead (or additionally) attach a flat `MediaStreams` directly on the
    item. Both shapes are handled here so the rest of the pipeline only has
    to deal with one ({"Path": ..., "MediaStreams": [...]}).
    """
    sources = item.get("MediaSources") or []
    if not sources:
        streams = item.get("MediaStreams")
        if streams:
            return {"Path": item.get("Path") or "", "MediaStreams": streams}
        return None

    def _video_pixel_area(source: dict) -> int:
        for stream in source.get("MediaStreams") or []:
            if (stream.get("Type") or "").lower() == "video":
                return (stream.get("Width") or 0) * (stream.get("Height") or 0)
        return 0

    return max(sources, key=_video_pixel_area)


def representative_episode(client: httpx.Client, series: dict) -> dict | None:
    """Pick a stand-in episode to derive a TV show's quality tokens from.

    Identical convention to plex_sync.py's representative_episode() (S01E01,
    falling back to the lowest (season, episode) pair present, preferring
    real seasons over season-0 "Specials") — see that function's docstring
    for the full rationale (short version: Stremio keys its own quality
    badges off the first episode for the same "no single right answer at the
    show level" reason, and the user explicitly asked to mirror that rule
    rather than invent a different one).

    Returns None — meaning "sync without quality", same as a show with no
    loadable episodes — if the episode list can't be fetched (still being
    scanned/matched, transient API error, ...).
    """
    series_id = series.get("Id")
    if not series_id:
        return None
    try:
        episodes = list(_paginated_items(
            client, path=f"/Shows/{series_id}/Episodes",
            Fields="ProviderIds,MediaSources,MediaStreams,Path",
        ))
    except httpx.HTTPError as exc:
        logger.debug(f"Could not load episodes for {series.get('Name')!r}: {exc}")
        return None
    if not episodes:
        return None

    def _season_episode(ep: dict) -> tuple[int, int]:
        return (ep.get("ParentIndexNumber") or 0, ep.get("IndexNumber") or 0)

    s01e01 = next(
        (ep for ep in episodes if ep.get("ParentIndexNumber") == 1 and ep.get("IndexNumber") == 1),
        None,
    )
    if s01e01 is not None:
        return s01e01
    non_specials = [ep for ep in episodes if (ep.get("ParentIndexNumber") or 0) > 0]
    candidates = non_specials or episodes
    return min(candidates, key=_season_episode)


def extract_quality_tokens(item: dict) -> list[str]:
    """Best-effort mapping from a single playable item's real Jellyfin media
    info to PostersPlus quality tokens. Works on anything with media info — a
    Movie or an Episode (sync_item() passes a representative_episode() for
    shows; see the module docstring for why). Returns [] for Series/Season
    items directly, or for items with no media analysis yet.

    NOTE: written from API documentation and source-reading, NOT validated
    against a live server — see the CAVEATS in this module's docstring.
    Treat the output as a strong starting point, not gospel; keep an eye on
    titles you know well, exactly like plex_sync.py's mapping was refined.
    """
    tokens: set[str] = set()
    media_source = _best_media_source(item)
    if media_source is None:
        return []

    streams = media_source.get("MediaStreams") or []
    video_streams = [s for s in streams if (s.get("Type") or "").lower() == "video"]
    audio_streams = [s for s in streams if (s.get("Type") or "").lower() == "audio"]

    primary_video = video_streams[0] if video_streams else None
    if primary_video is not None:
        width = primary_video.get("Width") or 0
        height = primary_video.get("Height") or 0
        if width >= _4K_MIN_WIDTH or height >= _4K_MIN_HEIGHT:
            tokens.add("4K")
        elif width >= _1080P_MIN_WIDTH or height >= _1080P_MIN_HEIGHT:
            tokens.add("1080P")

        video_range_type = (primary_video.get("VideoRangeType") or "").strip().lower()
        mapped = _VIDEO_RANGE_TYPE_TO_TOKEN.get(video_range_type)
        if mapped:
            tokens.add(mapped)

    for stream in audio_streams:
        title = f"{stream.get('DisplayTitle') or ''} {stream.get('Title') or ''}".upper()
        if "ATMOS" in title:
            tokens.add("ATMOS")
        if "DTS:X" in title or "DTS-X" in title or "DTSX" in title:
            tokens.add("DTSX")

    file_path = media_source.get("Path") or ""
    video_titles = []
    for stream in video_streams:
        video_titles.append(stream.get("DisplayTitle") or "")
        video_titles.append(stream.get("Title") or "")
    source_haystack = " ".join([file_path, *video_titles])
    if _REMUX_RE.search(source_haystack):
        tokens.add("REMUX")
    elif _WEBDL_RE.search(source_haystack):
        tokens.add("WEBDL")

    return sorted(tokens)


def inspect_item(client: httpx.Client, item: dict) -> None:
    """Dump an item's raw MediaSource/MediaStream attributes for debugging.

    Purely read-only — never touches PostersPlus or Jellyfin's write API.
    Used by --inspect when extract_quality_tokens() guesses wrong, so the
    *actual* field names/values/casing can be seen and the mapping corrected
    against real data — exactly the workflow that refined plex_sync.py's
    mapping from "written against a single sample" to "validated against a
    real library".
    """
    name = item.get("Name") or "?"
    item_type = item.get("Type") or "?"
    item_id = item.get("Id") or "?"
    logger.info(f"--- {name!r} ({item_type}, id={item_id}) ---")
    imdb_id, tmdb_id = extract_ids(item)
    logger.info(f"  ids: imdb={imdb_id!r} tmdb={tmdb_id!r}")

    target = item
    if item_type == "Movie":
        derived = extract_quality_tokens(item)
        logger.info(f"  extract_quality_tokens() currently derives: {derived}")
    elif item_type == "Series":
        rep_episode = representative_episode(client, item)
        if rep_episode is None:
            logger.info("  representative_episode(): none found — would sync without quality")
            return
        derived = extract_quality_tokens(rep_episode)
        logger.info(
            f"  representative_episode(): {rep_episode.get('Name')!r} "
            f"(S{rep_episode.get('ParentIndexNumber', '?')}E{rep_episode.get('IndexNumber', '?')}, "
            f"id={rep_episode.get('Id', '?')})"
        )
        logger.info(f"  extract_quality_tokens() currently derives: {derived}")
        target = rep_episode
    else:
        logger.info(f"  (unhandled item type {item_type!r} — nothing to derive)")
        return

    media_source = _best_media_source(target)
    if media_source is None:
        logger.info("  (no MediaSources/MediaStreams — nothing to inspect)")
        return
    logger.info(f"  mediaSource.Path = {media_source.get('Path')!r}")
    for si, stream in enumerate(media_source.get("MediaStreams") or []):
        stream_type = stream.get("Type") or "?"
        logger.info(
            f"    stream[{si}]: type={stream_type!r} codec={stream.get('Codec')!r} "
            f"width={stream.get('Width')!r} height={stream.get('Height')!r} "
            f"displayTitle={stream.get('DisplayTitle')!r} title={stream.get('Title')!r}"
        )
        if stream_type.lower() == "video":
            logger.info(
                f"              videoRange={stream.get('VideoRange')!r} "
                f"videoRangeType={stream.get('VideoRangeType')!r} "
                f"colorSpace={stream.get('ColorSpace')!r} "
                f"colorTransfer={stream.get('ColorTransfer')!r} "
                f"colorPrimaries={stream.get('ColorPrimaries')!r}"
            )


# ---------------------------------------------------------------------------
# PostersPlus fetch
# ---------------------------------------------------------------------------

def build_poster_request(*, imdb_id: str | None, tmdb_id: str, media_type: str, quality_tokens: list[str]) -> httpx.Request:
    # Start from whatever recipe defaults came from POSTERSPLUS_URL (gradients,
    # bar/badge styles, weighting profiles, ...), then layer the per-item
    # dynamic values on top — those five always win, since a value baked into
    # a copy-pasted recipe URL (e.g. a literal tmdb_id, or a stale `quality`)
    # would be wrong for every title except the one the URL was copied from.
    params = dict(POSTERSPLUS_RECIPE_DEFAULTS)
    # A recipe url copied from the configurator may itself contain an
    # imdb_id. Drop it before layering ours on: for an item with no IMDb
    # id of its own it would otherwise survive and stamp one title's IMDb
    # id onto every TMDB-only item in the library.
    params.pop("imdb_id", None)
    params.update({
        "tmdb_id": tmdb_id,
        "type": media_type,
        # No-op today (only Stremio profiles exist server-side as of this
        # writing) but forward-looking: a "jellyfin" entry has been added to
        # _CLIENT_EDGE_INSETS (0/0 — Jellyfin shows posters uncropped, same
        # as Plex) so this is meaningful as soon as that ships. Overrides
        # whatever primary_client a copy-pasted Stremio recipe URL might
        # specify.
        "primary_client": "jellyfin",
    })
    # Optional: Jellyfin has no IMDb id for every item. tmdb_id identifies the
    # title on its own; sending imdb_id when we do have it keys the rating
    # cache by IMDb id, which shares the row with every other client.
    if imdb_id:
        params["imdb_id"] = imdb_id
    if quality_tokens:
        params["quality"] = ",".join(quality_tokens)
    else:
        # IMPORTANT: do NOT just omit `quality=` here (and don't send `quality=`
        # with an empty value either) — identical reasoning to plex_sync.py's
        # build_poster_request(), copied verbatim because the underlying
        # behaviour lives entirely in PostersPlus's /poster handler, not in
        # anything Plex- or Jellyfin-specific:
        #
        # PostersPlus's /poster handler does `if quality: ... else:
        # quality_tokens = get_cached_quality(...) or fetch-from-AIOStreams`.
        # It treats a *missing or empty-string* quality param as "caller
        # didn't specify one — go figure it out yourself", and falls back to
        # its own AIOStreams/scraper-based lookup. That lookup answers "what
        # release of this title is available to stream right now on the
        # internet", which can be wildly different from what the user's
        # actual file on disk has — e.g. a 720p rip can render with a gold
        # "4K REMUX DV" badge purely because some 4K release of that title
        # exists somewhere online. That defeats the entire point of this
        # script (badges that reflect the real local file).
        #
        # When extract_quality_tokens() legitimately finds nothing (older/
        # lower-quality rips with no resolution/HDR/source tokens we
        # recognise), we must still positively assert "I checked — there is
        # nothing" in a way the server can't mistake for "not specified".
        # Since `if quality:` treats "" the same as absent, an empty value
        # can't do that. Instead send a sentinel string that IS non-empty (so
        # the server takes the explicit-override branch and skips the
        # AIOStreams fallback) but ISN'T a recognised label in
        # config.QUALITY_LABELS (so quality.parse_quality() discards it —
        # logging one harmless "unknown quality token ignored" warning
        # server-side — and yields the empty token list we actually derived).
        # Net effect: no quality badges, no AIOStreams detour, exactly
        # matching the real file.
        params["quality"] = "NONE"
    if POSTERSPLUS_ACCESS_KEY:
        params["access_key"] = POSTERSPLUS_ACCESS_KEY
    if POSTERSPLUS_TMDB_KEY:
        params["tmdb_key"] = POSTERSPLUS_TMDB_KEY
    if POSTERSPLUS_MDBLIST_KEY:
        params["mdblist_key"] = POSTERSPLUS_MDBLIST_KEY
    return httpx.Request("GET", f"{POSTERSPLUS_URL}/poster", params=params)


def fetch_poster_bytes(request: httpx.Request, client: httpx.Client) -> bytes:
    # Per-request timeouts aren't a kwarg on Client.send() — the timeout has
    # to be configured on the Client itself (see main(), httpx.Client(timeout=...)).
    resp = client.send(request)
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# Jellyfin upload
# ---------------------------------------------------------------------------

def upload_poster(client: httpx.Client, item_id: str, image_bytes: bytes) -> None:
    """POST a rendered poster to Jellyfin as an item's primary image.

    `POST /Items/{itemId}/Images/{imageType}` (confirmed against Jellyfin's
    own ImageController source — see SetItemImage): the request body must be
    the image's raw bytes, BASE64-ENCODED (the server wraps the request body
    in a base64-decoding stream — sending raw binary will corrupt the image),
    and `Content-Type` must be a real image MIME type the server recognises
    (`image/jpeg` — NOT the common typo `image/jpg`, which is documented to
    trip up this exact endpoint). PostersPlus's /poster always serves
    `image/jpeg` (see main.py), so that's hardcoded here — it'll never be
    anything else coming out of fetch_poster_bytes().

    `imageType="Primary"` is Jellyfin's name for what Plex calls the "poster"
    / what TMDB calls the "poster_path" — the main cover art shown in grids
    and detail pages.
    """
    resp = client.post(
        f"/Items/{item_id}/Images/Primary",
        content=base64.b64encode(image_bytes),
        headers={"Content-Type": "image/jpeg"},
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync_item(item: dict, *, jf_client: httpx.Client, pp_client: httpx.Client, state: dict, dry_run: bool) -> str:
    """Sync one item; returns a short status string used for the run summary."""
    name = item.get("Name") or item.get("OriginalTitle") or "?"
    item_id = item.get("Id")
    item_type = item.get("Type")

    imdb_id, tmdb_id = extract_ids(item)
    # tmdb_id alone is enough: it identifies the title to PostersPlus. imdb_id is
    # optional enrichment, and plenty of library items legitimately have no IMDb
    # provider id — skipping them left those posters unsynced for no reason.
    if not tmdb_id:
        logger.info(f"Skipping {name!r} — no tmdb id in Jellyfin metadata (ProviderIds)")
        return "skipped (no ids)"
    if not item_id:
        logger.info(f"Skipping {name!r} — no item id")
        return "skipped (no item id)"

    media_type = "movie" if item_type == "Movie" else "tv"
    # Quality is only meaningful at the single-file level, so for shows we
    # derive it from a representative episode (S01E01 by convention — same
    # rule Stremio's own badges and plex_sync.py use) rather than the show
    # as a whole. See representative_episode() for the fallback rules and the
    # module docstring CAVEATS for why this is necessarily a "good enough"
    # approximation, not a single technically "correct" answer for a
    # multi-episode series.
    if media_type == "movie":
        quality_tokens = extract_quality_tokens(item)
    else:
        rep_episode = representative_episode(jf_client, item)
        if rep_episode is None:
            logger.debug(f"No episodes found for {name!r} — syncing without quality")
            quality_tokens = []
        else:
            quality_tokens = extract_quality_tokens(rep_episode)
            logger.debug(
                f"{name!r}: deriving quality from {rep_episode.get('Name')!r} "
                f"(S{rep_episode.get('ParentIndexNumber', '?')}E{rep_episode.get('IndexNumber', '?')})"
            )

    # Includes RECIPE_FINGERPRINT so that changing your POSTERSPLUS_URL (e.g.
    # swapping bar_style=frosted for bar_style=gold, tweaking gradients, or
    # pointing at a different recipe entirely) invalidates every cached entry
    # and triggers a re-render/re-upload on the next run, rather than skipping
    # every item forever because only the ids and quality tokens were tracked.
    # Same string as before for any item that has an IMDb id, so switching to
    # the optional form does not invalidate a single cached fingerprint.
    fingerprint = f"{imdb_id or f'tmdb:{tmdb_id}'}:{tmdb_id}:{','.join(quality_tokens)}:{RECIPE_FINGERPRINT}"
    state_key = str(item_id)
    if state.get(state_key) == fingerprint:
        return "unchanged"

    request = build_poster_request(
        imdb_id=imdb_id, tmdb_id=tmdb_id, media_type=media_type, quality_tokens=quality_tokens,
    )
    if dry_run:
        logger.info(f"[dry-run] {name!r}: quality={quality_tokens or '(none)'} -> {request.url}")
        return "would sync"

    try:
        image_bytes = fetch_poster_bytes(request, pp_client)
    except httpx.HTTPError as exc:
        logger.warning(f"Poster fetch failed for {name!r}: {exc}")
        return "error (fetch)"

    try:
        upload_poster(jf_client, item_id, image_bytes)
    except httpx.HTTPError as exc:
        logger.warning(f"Poster upload failed for {name!r}: {exc}")
        return "error (upload)"

    state[state_key] = fingerprint
    logger.info(f"Synced poster for {name!r} (quality={quality_tokens or 'none'})")
    return "synced"


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"Could not read state file {STATE_PATH} ({exc}) — starting fresh")
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Could not write state file {STATE_PATH}: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Sync PostersPlus posters into a Jellyfin library.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log what would be fetched/uploaded for each item without contacting "
             "PostersPlus or Jellyfin's write API. Start here — always.",
    )
    parser.add_argument(
        "--limit", type=int, default=0, metavar="N",
        help="Stop after N items (0 = no limit). Useful while validating the "
             "quality-token mapping against a few known titles first.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Ignore the local state file and re-sync every item regardless of "
             "whether its derived fingerprint has changed.",
    )
    parser.add_argument(
        "--inspect", action="store_true",
        help="Debug mode: dump each item's raw MediaSource/MediaStream attributes "
             "(resolution, codecs, VideoRangeType, audio stream titles, file path) "
             "plus what extract_quality_tokens() currently derives from them, then "
             "exit. Purely read-only — never contacts PostersPlus or Jellyfin's "
             "write API. Use this when the derived quality tokens look wrong (or "
             "before your first real run, since this mapping hasn't been validated "
             "against a live server yet — see the CAVEATS in the module docstring), "
             "so the mapping can be corrected against real data.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not JELLYFIN_BASE_URL or not JELLYFIN_API_KEY:
        logger.error("Set JELLYFIN_BASE_URL and JELLYFIN_API_KEY first (or fill in the "
                     "EASY CONFIG block) — an API key can be generated under "
                     "Dashboard -> Advanced -> API Keys in Jellyfin's web UI.")
        return 1

    # Two separate clients: one authenticated against Jellyfin (base_url +
    # Authorization header baked in, so every jf_get()/upload_poster() call
    # is just a relative path), one plain for PostersPlus (which builds full
    # URLs itself via build_poster_request — same as plex_sync.py's client).
    # Poster rendering can be slow (face/text detection, OCR, SVG logo
    # rasterisation) — give PostersPlus generous headroom rather than the 5s
    # default; Jellyfin's own API is local-network-fast so its default is fine.
    with httpx.Client(
        base_url=JELLYFIN_BASE_URL,
        headers={"Authorization": _JELLYFIN_AUTH_HEADER.format(token=JELLYFIN_API_KEY)},
        timeout=30,
    ) as jf_client, httpx.Client(timeout=120) as pp_client:

        try:
            jf_get(jf_client, "/System/Info")
        except httpx.HTTPError as exc:
            logger.error(f"Could not reach Jellyfin at {JELLYFIN_BASE_URL!r} with the "
                         f"configured API key — check JELLYFIN_BASE_URL/JELLYFIN_API_KEY: {exc}")
            return 1

        if args.inspect:
            for n, item in enumerate(iter_library_items(jf_client), start=1):
                if args.limit and n > args.limit:
                    logger.info(f"Reached --limit {args.limit}; stopping")
                    break
                inspect_item(jf_client, item)
            return 0

        state = {} if args.force else load_state()

        counts: dict[str, int] = {}
        for n, item in enumerate(iter_library_items(jf_client), start=1):
            if args.limit and n > args.limit:
                logger.info(f"Reached --limit {args.limit}; stopping")
                break
            status = sync_item(item, jf_client=jf_client, pp_client=pp_client, state=state, dry_run=args.dry_run)
            counts[status] = counts.get(status, 0) + 1

    if not args.dry_run:
        save_state(state)

    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items())) or "nothing to do"
    logger.info(f"Done — {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
