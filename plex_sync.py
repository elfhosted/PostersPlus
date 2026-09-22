"""Sync PostersPlus-rendered posters into a Plex Media Server library.

STANDALONE COMPANION SCRIPT — not part of the FastAPI service, not imported
by main.py, and not wired into the Docker image. Run it separately (locally,
on a schedule, however fits your workflow) against a running PostersPlus
instance and a reachable Plex Media Server.

Unlike Stremio (which fetches poster URLs lazily whenever it renders a
catalog), Plex has no "arbitrary URL, fetched live on render" mechanism —
artwork has to be *pushed* into its library via the API. That's what this
script does: for each movie/show in the configured libraries, it
  1. reads the IMDb/TMDB IDs Plex already has from its <Guid> metadata,
  2. derives real quality-badge tokens from the file's own media info
     (resolution / HDR / audio — see extract_quality_tokens), and
  3. fetches the rendered poster from PostersPlus and uploads it to Plex.

Why "real" quality info instead of AIOStreams: on Stremio, quality badges
answer "what's available to stream right now" (scraped from addons, before
you've played anything). On Plex you already own the file — its actual
resolution/HDR/codec is sitting right there in the library, more accurate
than anything a scraper could guess. PostersPlus already supports this via
a plain pass-through `quality=` override (see quality.parse_quality), so no
changes to the rendering pipeline were needed — only this adapter.

CAVEATS — read before trusting this against a big library:
  * The quality-token mapping in extract_quality_tokens() was written from
    a single sample <Stream> block, not validated against a live server.
    Different rips/encoders label things inconsistently (HDR vs HDR10 vs
    HDR10+, DTS-HD MA vs DTS:X, etc). ALWAYS run with --dry-run first and
    sanity-check the derived tokens against a handful of titles you know
    well before turning it loose on a whole library.
  * TV shows derive their quality tokens from a single representative episode
    (S01E01 by convention — the same rule Stremio's own quality badges use,
    per the user's request) rather than the show as a whole — see
    representative_episode(). A show spans many episodes that may genuinely
    differ in quality (e.g. a "complete series" folder assembled from rips of
    different eras/sources), so this is necessarily an approximation, not a
    single technically "correct" answer — same trade-off Stremio makes.
  * When extract_quality_tokens() legitimately finds NOTHING (older/lower-
    quality rips — e.g. a 720p file with no HDR/DV/source signals we
    recognise), build_poster_request() sends `quality=NONE` rather than
    omitting the param. This is intentional, not a typo: PostersPlus's
    /poster endpoint treats a *missing or empty* `quality=` as "go fetch one
    yourself" and falls back to its own AIOStreams/scraper-based lookup —
    which answers "what release is available to stream right now" and can be
    wildly higher quality than the user's actual file (confirmed: a 720p rip
    rendering with a gold "4K REMUX DV" badge because some 4K release of that
    title exists online somewhere). "NONE" is a non-empty string that isn't a
    recognised label in config.QUALITY_LABELS, so the server takes the
    explicit-override branch (skipping AIOStreams) and quality.parse_quality()
    discards it, yielding the empty token list we actually derived — at the
    cost of one harmless "unknown quality token ignored" warning server-side.
  * Plex may re-fetch and overwrite custom posters when its metadata agent
    refreshes an item. `item.uploadPoster()` (what this script calls) is the
    same call Plex Web's own "Upload Image" button makes, and that action is
    documented to LOCK the poster field — a locked field is explicitly
    skipped by metadata-agent refreshes, so a normal "Refresh Metadata" or
    scheduled library scan should leave your synced poster alone. What WILL
    blow it away: re-matching the item to different metadata ("Fix Match" /
    "Match"), removing and re-adding the title, or wiping/recreating the
    library — any of those effectively creates a new item and the lock (and
    this script's sync-state fingerprint) doesn't carry over. This hasn't
    been soak-tested over weeks against a live instance, so still keep an
    eye out and report back if posters revert unexpectedly.

Quick start — two ways to configure:
  1. Environment variables (shown below), or
  2. Open this file and fill in the "EASY CONFIG" block near the top
     (PLEX_BASE_URL_DEFAULT, PLEX_TOKEN_DEFAULT, etc.) so you don't have
     to export anything each time — env vars still override those defaults
     if both are set.

    pip install -r requirements-plex.txt
    export PLEX_BASE_URL="http://192.168.1.50:32400"
    export PLEX_TOKEN="xxxxxxxxxxxxxxxxxxxx"      # see https://support.plex.tv/articles/204059436
    export POSTERSPLUS_URL="http://localhost:8000"
    python plex_sync.py --dry-run --limit 5

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
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

logger = logging.getLogger("plex_sync")

try:
    from plexapi.server import PlexServer
except ImportError:
    PlexServer = None  # reported at runtime so --help still works without the dep


# ---------------------------------------------------------------------------
# >>> EASY CONFIG — fill these in if you'd rather not export environment
# variables every time you run this script. Anything left blank here just
# falls back to the matching environment variable below (and an env var, if
# set, always WINS over whatever you put here — so scheduled tasks / CI can
# still override these without editing the file).
#
#   PLEX_BASE_URL_DEFAULT:     your server address, e.g. "http://192.168.1.50:32400"
#   PLEX_TOKEN_DEFAULT:        your Plex auth token (see the Quick start link below)
#   POSTERSPLUS_URL_DEFAULT:   bare server URL, OR a full "Copy URL" recipe URL
#
# SECURITY NOTE: PLEX_TOKEN_DEFAULT is a credential. If this file lives in a
# git checkout, filling it in here means it can end up committed by accident.
# Either keep using the environment variable for the token specifically, or
# make a personal copy of this script outside the repo (and/or add it to
# .gitignore) before pasting a real token into it.
# ---------------------------------------------------------------------------

PLEX_BASE_URL_DEFAULT = "http://localhost:32400"
PLEX_TOKEN_DEFAULT = ""
POSTERSPLUS_URL_DEFAULT = "http://localhost:8000"
POSTERSPLUS_ACCESS_KEY_DEFAULT = "" # only required if set on PostersPlus
POSTERSPLUS_TMDB_KEY_DEFAULT = "" # not required if set on PostersPlus server side
POSTERSPLUS_MDBLIST_KEY_DEFAULT = "" # not required if set on PostersPlus server side
PLEX_LIBRARY_SECTIONS_DEFAULT = ""   # comma-separated section titles, e.g. "Movies,4K Movies"
PLEX_SYNC_STATE_PATH_DEFAULT = "plex_sync_state.json"

# ---------------------------------------------------------------------------
# Configuration — env vars, mirroring PostersPlus's own config.py conventions
# (each one falls back to the EASY CONFIG default above if unset)
# ---------------------------------------------------------------------------

PLEX_BASE_URL = os.environ.get("PLEX_BASE_URL", PLEX_BASE_URL_DEFAULT).rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", PLEX_TOKEN_DEFAULT)


def _parse_postersplus_url(raw: str) -> tuple[str, dict]:
    """Split POSTERSPLUS_URL into (base_url, recipe_defaults).

    POSTERSPLUS_URL accepts two forms:
      * a bare server address — "http://127.0.0.1:8000" — in which case
        every poster is rendered with PostersPlus's own defaults, or
      * a full recipe URL copied straight from the configurator's
        "Copy URL" button — "http://127.0.0.1:8000/poster?bar_style=...
        &sash_badge_inset=0.000&...". Every query parameter on a recipe
        URL becomes a *default* applied to every synced item — gradients,
        badge styles, rating-bar layout, weighting profiles, all of it.

    Either way, the script's own per-item values (tmdb_id, imdb_id, type,
    quality, primary_client) always win over whatever the recipe says for
    those specific keys, since those five are inherently dynamic/per-item —
    a hardcoded tmdb_id in a copy-pasted URL would be wrong for every title
    except the one you copied it from.
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

# Comma-separated Plex library section titles to sync, e.g. "Movies,4K Movies".
# Empty = sync every movie/show section the server has.
LIBRARY_SECTIONS = [
    s.strip() for s in os.environ.get("PLEX_LIBRARY_SECTIONS", PLEX_LIBRARY_SECTIONS_DEFAULT).split(",") if s.strip()
]

# Where to remember what's already been synced, so re-runs only touch items
# whose derived poster would actually change. Delete this file (or pass
# --force) to re-sync everything from scratch.
STATE_PATH = Path(os.environ.get("PLEX_SYNC_STATE_PATH", PLEX_SYNC_STATE_PATH_DEFAULT))


# ---------------------------------------------------------------------------
# Recipe fingerprint — captures the *rendering* settings (gradients, badge
# styles, weighting profiles, everything POSTERSPLUS_URL contributes as a
# recipe default) so that changing your poster style invalidates the sync
# state and triggers a re-render/re-upload, instead of being silently
# skipped as "unchanged" forever (see the fingerprint comment in sync_item).
# ---------------------------------------------------------------------------
RECIPE_FINGERPRINT = "&".join(
    f"{k}={v}" for k, v in sorted(POSTERSPLUS_RECIPE_DEFAULTS.items())
)


# ---------------------------------------------------------------------------
# ID extraction — Plex already carries IMDb/TMDB IDs in its <Guid> metadata
# ---------------------------------------------------------------------------

_GUID_RE = re.compile(r"^(imdb|tmdb|tvdb)://(\w+)$")


def extract_ids(item) -> tuple[str | None, str | None]:
    """Pull (imdb_id, tmdb_id) out of a Plex item's cross-reference GUIDs.

    Plex's own `item.guid` (singular) is an opaque internal identifier —
    the useful cross-references live in `item.guids`, e.g.:
        <Guid id="imdb://tt8772262"/>
        <Guid id="tmdb://530385"/>

    Library-listing responses don't always include these (depends on PMS
    version / how the item was matched), so we reload the full metadata if
    the cheap lookup comes back empty.
    """
    def _scan(guids):
        found_imdb = found_tmdb = None
        for guid in guids or []:
            m = _GUID_RE.match(getattr(guid, "id", "") or "")
            if not m:
                continue
            scheme, value = m.groups()
            if scheme == "imdb":
                found_imdb = value
            elif scheme == "tmdb":
                found_tmdb = value
        return found_imdb, found_tmdb

    imdb_id, tmdb_id = _scan(getattr(item, "guids", None))
    if not (imdb_id and tmdb_id):
        try:
            item.reload()
        except Exception as exc:
            logger.debug(f"Reload failed for {getattr(item, 'title', '?')}: {exc}")
        else:
            imdb_id, tmdb_id = _scan(getattr(item, "guids", None))
    return imdb_id, tmdb_id


# ---------------------------------------------------------------------------
# Quality-token derivation — translate Plex's *real* file metadata into the
# same token vocabulary PostersPlus expects via its `quality=` override
# (see config.QUALITY_LABELS: 4K, 1080P, REMUX, WEBDL, DV, HDR10+, HDR10,
# ATMOS, DTSX).
# ---------------------------------------------------------------------------

# Plex doesn't expose "Remux" / "Web-DL" as structured media properties —
# they describe the *source* of a rip, not a technical property of the
# encoded stream — so those two are inferred from text instead: the on-disk
# filename (`part.file`) AND the video stream's displayTitle/
# extendedDisplayTitle. The latter matters because organizing tools (Sonarr/
# Radarr) and manual renames routinely strip source info from filenames, but
# Plex often keeps the original scene-release name in the stream's embedded
# title metadata — see extract_quality_tokens() for a confirmed real-world
# example (The Matrix) where this is the *only* place "BDRemux" appears.
#
# NOTE: deliberately no \b word boundaries around "remux" — real-world
# filenames/titles glue it onto a prefix with no separator (e.g.
# "...2160p.BDRemux...", confirmed against a real library), which a
# \bremux\b boundary check misses entirely. A bare substring search is
# plenty specific; nothing else plausible contains "remux".
_REMUX_RE = re.compile(r"remux", re.I)
_WEBDL_RE = re.compile(r"\bweb[-_. ]?dl\b", re.I)

# Ranks used to pick the "best" <Media> entry on multi-version items (e.g. a
# library that has both a 4K remux and a 1080p fallback copy of the same
# movie) — Plex *usually* orders item.media with the best version first, but
# that's an assumption rather than a guarantee, so rank explicitly instead.
_RESOLUTION_RANK = {"4k": 4, "2160": 4, "1080": 3, "1080p": 3, "720": 2, "720p": 2, "576": 1, "480": 1, "sd": 0}


def _best_media(item):
    media_list = getattr(item, "media", None) or []
    if not media_list:
        return None
    return max(
        media_list,
        key=lambda m: _RESOLUTION_RANK.get((getattr(m, "videoResolution", "") or "").lower(), -1),
    )


def _media_and_part(item, *, allow_reload: bool = True):
    """Return (media, part) for the best version of `item`, reloading once if
    the part's <Stream> children aren't loaded yet.

    Confirmed against a real library: Plex's library-listing ("lite") objects
    carry top-level <Media> attributes (resolution, codec) but NOT <Part>/
    <Stream> children — those only materialise after item.reload(). Since
    DOVI/HDR/Atmos/DTS:X detection all need stream-level data, a missing
    `streams` list is the signal to force a reload (mirrors the same lazy-load
    quirk extract_ids() already works around for `guids`).
    """
    media = _best_media(item)
    if media is None:
        return None, None
    try:
        part = media.parts[0]
    except (AttributeError, IndexError, TypeError):
        return media, None

    if allow_reload and not getattr(part, "streams", None):
        try:
            item.reload()
        except Exception as exc:
            logger.debug(f"Reload for stream info failed on {getattr(item, 'title', '?')}: {exc}")
        else:
            return _media_and_part(item, allow_reload=False)
    return media, part


def representative_episode(show):
    """Pick a stand-in episode to derive a TV show's quality tokens from.

    A show spans many episodes that may differ in quality (a "complete series"
    folder is often a grab-bag of rips from different eras/sources), so there's
    no single technically "correct" answer at the show level. Stremio's own
    quality badges face the same ambiguity and resolve it by keying off the
    first episode — the user explicitly asked to mirror that convention here
    rather than inventing a different rule, so: season 1, episode 1 if it
    exists, else the lowest (season, episode) pair actually present (skipping
    season 0 "Specials" when a real season exists, since specials are often
    odd one-off rips that aren't representative of the show as a whole).

    Returns None (-> no quality tokens, same as the old "shows skip quality"
    behaviour) if the show has no episodes or its episode list can't be loaded
    — e.g. a show that's still being scanned/matched.
    """
    try:
        return show.episode(season=1, episode=1)
    except Exception:
        pass
    try:
        episodes = show.episodes() or []
    except Exception as exc:
        logger.debug(f"Could not load episodes for {getattr(show, 'title', '?')}: {exc}")
        return None
    if not episodes:
        return None
    non_specials = [e for e in episodes if (getattr(e, "seasonNumber", None) or 0) > 0]
    candidates = non_specials or episodes
    return min(candidates, key=lambda e: (getattr(e, "seasonNumber", 0) or 0, getattr(e, "index", 0) or 0))


def extract_quality_tokens(item) -> list[str]:
    """Best-effort mapping from a single playable item's real Plex media info
    to PostersPlus quality tokens. Works on anything with `.media` — a Movie
    or an Episode (sync_item() passes a representative_episode() for shows,
    since quality is only meaningful at the single-file level; see the module
    docstring for why). Returns [] for Show/Season objects directly, or for
    items with no media analysis yet.

    NOTE: refined against a real library sample but still best-effort — see
    the CAVEATS in this module's docstring. Treat the output as a strong
    starting point, not gospel; keep an eye on titles you know well.
    """
    tokens: set[str] = set()
    media, part = _media_and_part(item)
    if media is None or part is None:
        return []

    res = (getattr(media, "videoResolution", "") or "").lower()
    if res in ("4k", "2160"):
        tokens.add("4K")
    elif res in ("1080", "1080p"):
        tokens.add("1080P")

    video_stream_titles: list[str] = []
    for stream in getattr(part, "streams", None) or []:
        stream_type = getattr(stream, "streamType", None)
        if stream_type == 1:  # video
            if getattr(stream, "DOVIPresent", False):
                tokens.add("DV")
            elif (getattr(stream, "colorTrc", "") or "") == "smpte2084":
                # Plex doesn't distinguish HDR10 vs HDR10+ at the stream
                # level — this will under-detect HDR10+ specifically.
                tokens.add("HDR10")
            # Stash these for the source-quality (REMUX/WEB-DL) sniff below —
            # see the comment there for why we look here too.
            video_stream_titles.append(getattr(stream, "displayTitle", "") or "")
            video_stream_titles.append(getattr(stream, "extendedDisplayTitle", "") or "")
        elif stream_type == 2:  # audio
            title = (
                f"{getattr(stream, 'displayTitle', '') or ''} "
                f"{getattr(stream, 'extendedDisplayTitle', '') or ''}"
            ).upper()
            if "ATMOS" in title:
                tokens.add("ATMOS")
            if "DTS:X" in title or "DTS-X" in title or "DTSX" in title:
                tokens.add("DTSX")

    file_path = getattr(part, "file", "") or ""
    # REMUX/WEB-DL detection is regex-on-text, and `part.file` alone is an
    # unreliable source of that text: organizing tools (Sonarr/Radarr) and
    # manual renames routinely strip source-quality info from the on-disk
    # filename. Plex, however, frequently preserves the *original* scene-
    # release name in the video stream's displayTitle/extendedDisplayTitle —
    # confirmed on a real library sample where The Matrix's on-disk file is
    # "The.Matrix.1999.mkv" (no "remux" anywhere in it) but its video stream's
    # extendedDisplayTitle is literally "The.Matrix.1999.UHD.2160P.BDRemux.
    # HDR10.TrueHD.Atmos.7.1.Dolby.Vision.HEVC-FZHD (4K DoVi/HDR10 HEVC Main
    # 10)". Checking `part.file` alone would misclassify that as a non-REMUX
    # 4K DV title (silver, 4 pts) when it's actually a 4K DV REMUX (gold,
    # 6 pts) — so we also sniff the video stream titles here.
    source_haystack = " ".join([file_path, *video_stream_titles])
    if _REMUX_RE.search(source_haystack):
        tokens.add("REMUX")
    elif _WEBDL_RE.search(source_haystack):
        tokens.add("WEBDL")

    return sorted(tokens)


def inspect_item(item) -> None:
    """Dump an item's raw Media/Part/Stream attributes for debugging.

    Purely read-only — never touches PostersPlus or Plex's write API. Used by
    --inspect when extract_quality_tokens() guesses wrong, so the *actual*
    field names/values/casing can be seen and the mapping corrected against
    real data instead of the single sample it was originally written from.
    """
    logger.info(f"--- {item.title!r} ({item.TYPE}, ratingKey={item.ratingKey}) ---")
    imdb_id, tmdb_id = extract_ids(item)
    logger.info(f"  ids: imdb={imdb_id!r} tmdb={tmdb_id!r}")
    if item.TYPE == "movie":
        derived = extract_quality_tokens(item)
        logger.info(f"  extract_quality_tokens() currently derives: {derived}")
    else:
        rep_episode = representative_episode(item)
        if rep_episode is None:
            logger.info("  representative_episode(): none found — would sync without quality")
        else:
            derived = extract_quality_tokens(rep_episode)
            logger.info(
                f"  representative_episode(): {rep_episode.title!r} "
                f"(S{getattr(rep_episode, 'seasonNumber', '?')}E{getattr(rep_episode, 'index', '?')}, "
                f"ratingKey={getattr(rep_episode, 'ratingKey', '?')})"
            )
            logger.info(f"  extract_quality_tokens() currently derives: {derived}")
            item = rep_episode  # fall through to dump this episode's media/part/stream info below

    try:
        media_list = item.media
    except AttributeError:
        media_list = []
    if not media_list:
        logger.info("  (no <Media> entries — nothing to inspect)")
        return

    for mi, media in enumerate(media_list):
        logger.info(
            f"  media[{mi}]: videoResolution={getattr(media, 'videoResolution', None)!r} "
            f"videoCodec={getattr(media, 'videoCodec', None)!r} "
            f"audioCodec={getattr(media, 'audioCodec', None)!r} "
            f"container={getattr(media, 'container', None)!r}"
        )
        for pi, part in enumerate(getattr(media, "parts", None) or []):
            logger.info(f"    part[{pi}].file = {getattr(part, 'file', None)!r}")
            for si, stream in enumerate(getattr(part, "streams", None) or []):
                logger.info(
                    f"    stream[{si}]: streamType={getattr(stream, 'streamType', None)!r} "
                    f"codec={getattr(stream, 'codec', None)!r} "
                    f"displayTitle={getattr(stream, 'displayTitle', None)!r} "
                    f"extendedDisplayTitle={getattr(stream, 'extendedDisplayTitle', None)!r}"
                )
                logger.info(
                    f"              DOVIPresent={getattr(stream, 'DOVIPresent', None)!r} "
                    f"DOVIProfile={getattr(stream, 'DOVIProfile', None)!r} "
                    f"colorTrc={getattr(stream, 'colorTrc', None)!r} "
                    f"colorSpace={getattr(stream, 'colorSpace', None)!r} "
                    f"hdr={getattr(stream, 'hdr', None)!r}"
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
        # writing) but forward-looking: a "plex" entry has been added to
        # _CLIENT_EDGE_INSETS (0/0 — Plex shows posters uncropped) so this
        # is meaningful as soon as that ships. Overrides whatever
        # primary_client a copy-pasted Stremio recipe URL might specify.
        "primary_client": "plex",
    })
    # Optional: Plex has no IMDb id for every item. tmdb_id identifies the
    # title on its own; sending imdb_id when we do have it keys the rating
    # cache by IMDb id, which shares the row with every other client.
    if imdb_id:
        params["imdb_id"] = imdb_id
    if quality_tokens:
        params["quality"] = ",".join(quality_tokens)
    else:
        # IMPORTANT: do NOT just omit `quality=` here (and don't send `quality=`
        # with an empty value either). PostersPlus's /poster handler does
        # `if quality: ...else: quality_tokens = get_cached_quality(...) or
        # fetch-from-AIOStreams`. It treats a *missing or empty-string* quality
        # param as "caller didn't specify one — go figure it out yourself",
        # and falls back to its own AIOStreams/scraper-based lookup. That
        # lookup answers "what release of this title is available to stream
        # right now on the internet", which can be wildly different from what
        # the user's actual file on disk has — e.g. a 720p rip can render with
        # a gold "4K REMUX DV" badge purely because some 4K release of that
        # title exists somewhere online. That defeats the entire point of this
        # script (badges that reflect the real local file).
        #
        # When extract_quality_tokens() legitimately finds nothing (older/
        # lower-quality rips with no resolution/HDR/source tokens we recognise),
        # we must still positively assert "I checked — there is nothing" in a
        # way the server can't mistake for "not specified". Since `if quality:`
        # treats "" the same as absent, an empty value can't do that. Instead
        # send a sentinel string that IS non-empty (so the server takes the
        # explicit-override branch and skips the AIOStreams fallback) but
        # ISN'T a recognised label in config.QUALITY_LABELS (so quality.
        # parse_quality() discards it — logging one harmless "unknown quality
        # token ignored" warning server-side — and yields the empty token list
        # we actually derived). Net effect: no quality badges, no AIOStreams
        # detour, exactly matching the real file.
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
# Sync
# ---------------------------------------------------------------------------

def iter_library_items(plex):
    sections = plex.library.sections()
    if LIBRARY_SECTIONS:
        wanted = set(LIBRARY_SECTIONS)
        sections = [s for s in sections if s.title in wanted]
        missing = wanted - {s.title for s in sections}
        for name in missing:
            logger.warning(f"Library section {name!r} not found on this server — skipping")
    for section in sections:
        if section.type not in ("movie", "show"):
            continue
        logger.info(f"Scanning library section {section.title!r} ({section.type})")
        for item in section.all():
            yield item


def sync_item(item, *, client: httpx.Client, state: dict, dry_run: bool) -> str:
    """Sync one item; returns a short status string used for the run summary."""
    imdb_id, tmdb_id = extract_ids(item)
    # tmdb_id alone is enough: it identifies the title to PostersPlus. imdb_id is
    # optional enrichment, and plenty of library items legitimately have no IMDb
    # guid — skipping them left those posters unsynced for no reason.
    if not tmdb_id:
        logger.info(f"Skipping {item.title!r} — no tmdb id in Plex metadata")
        return "skipped (no ids)"

    media_type = "movie" if item.TYPE == "movie" else "tv"
    # Quality is only meaningful at the single-file level, so for shows we
    # derive it from a representative episode (S01E01 by convention — same
    # rule Stremio's own badges use) rather than the show as a whole. See
    # representative_episode() for the fallback rules and the module docstring
    # CAVEATS for why this is necessarily a "good enough" approximation, not
    # a single technically "correct" answer for a multi-episode series.
    if media_type == "movie":
        quality_tokens = extract_quality_tokens(item)
    else:
        rep_episode = representative_episode(item)
        if rep_episode is None:
            logger.debug(f"No episodes found for {item.title!r} — syncing without quality")
            quality_tokens = []
        else:
            quality_tokens = extract_quality_tokens(rep_episode)
            logger.debug(
                f"{item.title!r}: deriving quality from {rep_episode.title!r} "
                f"(S{getattr(rep_episode, 'seasonNumber', '?')}E{getattr(rep_episode, 'index', '?')})"
            )

    # Includes RECIPE_FINGERPRINT so that changing your POSTERSPLUS_URL (e.g.
    # swapping bar_style=frosted for bar_style=gold, tweaking gradients, or
    # pointing at a different recipe entirely) invalidates every cached entry
    # and triggers a re-render/re-upload on the next run — rather than the
    # old behaviour of skipping every item forever because only the ids and
    # quality tokens were tracked, not the rendering recipe itself.
    # Same string as before for any item that has an IMDb id, so switching to
    # the optional form does not invalidate a single cached fingerprint.
    fingerprint = f"{imdb_id or f'tmdb:{tmdb_id}'}:{tmdb_id}:{','.join(quality_tokens)}:{RECIPE_FINGERPRINT}"
    state_key = str(item.ratingKey)
    if state.get(state_key) == fingerprint:
        return "unchanged"

    request = build_poster_request(
        imdb_id=imdb_id, tmdb_id=tmdb_id, media_type=media_type, quality_tokens=quality_tokens,
    )
    if dry_run:
        logger.info(f"[dry-run] {item.title!r}: quality={quality_tokens or '(none)'} -> {request.url}")
        return "would sync"

    try:
        image_bytes = fetch_poster_bytes(request, client)
    except httpx.HTTPError as exc:
        logger.warning(f"Poster fetch failed for {item.title!r}: {exc}")
        return "error (fetch)"

    try:
        item.uploadPoster(filepath=image_bytes)
    except Exception as exc:
        logger.warning(f"Poster upload failed for {item.title!r}: {exc}")
        return "error (upload)"

    state[state_key] = fingerprint
    logger.info(f"Synced poster for {item.title!r} (quality={quality_tokens or 'none'})")
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
        description="Sync PostersPlus posters into a Plex library.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log what would be fetched/uploaded for each item without contacting "
             "PostersPlus or Plex's write API. Start here — always.",
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
        help="Debug mode: dump each item's raw Media/Part/Stream attributes "
             "(resolution, codecs, DOVI/HDR flags, audio stream titles, file "
             "path) plus what extract_quality_tokens() currently derives from "
             "them, then exit. Purely read-only — never contacts PostersPlus "
             "or Plex's write API. Use this when the derived quality tokens "
             "look wrong, so the mapping can be corrected against real data.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if PlexServer is None:
        logger.error("plexapi is not installed — run: pip install -r requirements-plex.txt")
        return 1
    if not PLEX_BASE_URL or not PLEX_TOKEN:
        logger.error("Set PLEX_BASE_URL and PLEX_TOKEN environment variables first "
                     "(see https://support.plex.tv/articles/204059436 for the token).")
        return 1

    plex = PlexServer(PLEX_BASE_URL, PLEX_TOKEN)

    if args.inspect:
        for n, item in enumerate(iter_library_items(plex), start=1):
            if args.limit and n > args.limit:
                logger.info(f"Reached --limit {args.limit}; stopping")
                break
            inspect_item(item)
        return 0

    state = {} if args.force else load_state()

    counts: dict[str, int] = {}
    # Poster rendering can be slow (face/text detection, OCR, SVG logo
    # rasterisation) — give it generous headroom rather than the 5s default.
    with httpx.Client(timeout=120) as client:
        for n, item in enumerate(iter_library_items(plex), start=1):
            if args.limit and n > args.limit:
                logger.info(f"Reached --limit {args.limit}; stopping")
                break
            status = sync_item(item, client=client, state=state, dry_run=args.dry_run)
            counts[status] = counts.get(status, 0) + 1

    if not args.dry_run:
        save_state(state)

    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items())) or "nothing to do"
    logger.info(f"Done — {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
