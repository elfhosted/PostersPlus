#tvdb.py
"""TheTVDB v4 as a *fallback* art source (logos, backdrops, optionally posters).

Design notes
------------
- Entirely opt-in: every entry point short-circuits when ``SERVER_TVDB_KEY`` is
  empty, so a server without a key behaves exactly as TMDB-only.
- TVDB v4 has no "api key per request" mode (unlike TMDB/MDBList). The key is
  exchanged once via ``POST /login`` for a JWT bearer token valid ~1 month; the
  token is cached (in the shared SQLite cache) and refreshed on expiry or 401,
  guarded by a single-flight lock so concurrent requests don't stampede login.
- Artwork entries only carry a numeric ``type`` id. The meaning of each id comes
  from ``GET /artwork/types``; we fetch that catalogue once (cached long) and
  classify by slug/name keyword ("clearlogo"/"poster"/"background") rather than
  hardcoding ids, so a future TVDB id reshuffle can't silently break us.
- Failures never propagate into a request: any error logs and yields None, which
  the caller treats identically to "TVDB has nothing for this title".

This module is the data layer only; wiring into the render fallback chains lives
in main.py and is added in later phases.
"""
import asyncio
import io
import logging

import httpx
from PIL import Image

logger = logging.getLogger(__name__)

from cache import (
    get_cached_tvdb_json,
    set_cached_tvdb_json,
    get_cached_tmdb_logo,
    set_cached_tmdb_logo,
    get_cached_tmdb_poster,
    set_cached_tmdb_poster,
)
from config import (
    TVDB_API_BASE,
    SERVER_TVDB_KEY,
    TVDB_SUBSCRIBER_PIN,
    TVDB_CONCURRENCY,
    TVDB_ARTWORK_CACHE_DURATION,
    TVDB_NEG_CACHE_DURATION,
    TVDB_TYPES_CACHE_DURATION,
    POSTER_WIDTH,
    POSTER_HEIGHT,
)

_API_BASE      = TVDB_API_BASE   # ElfHosted fork: configurable (emdb)
_ARTWORK_BASE  = "https://artworks.thetvdb.com"

# Refresh the ~1-month token comfortably before it expires.
_TOKEN_TTL_SECONDS = 25 * 86400
_TOKEN_CACHE_KEY   = "auth:token"
_TYPES_CACHE_KEY   = "artwork:types"

# Lazily-created asyncio primitives (bind to the running loop on first use).
_token_lock: "asyncio.Lock | None" = None
_semaphore:  "asyncio.Semaphore | None" = None
# In-process token cache so the common path needs neither DB nor login.
_token_mem: str | None = None


def tvdb_enabled() -> bool:
    """True when a TVDB API key is configured."""
    return bool(SERVER_TVDB_KEY)


def tvdb_status() -> str:
    """Compact runtime status for startup logging."""
    if not SERVER_TVDB_KEY:
        return "disabled (no TVDB_API_KEY)"
    return "enabled (token acquired lazily on first use)"


def _get_lock() -> "asyncio.Lock":
    global _token_lock
    if _token_lock is None:
        _token_lock = asyncio.Lock()
    return _token_lock


def _get_semaphore() -> "asyncio.Semaphore":
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(TVDB_CONCURRENCY)
    return _semaphore


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def _login(client: httpx.AsyncClient) -> str | None:
    """Exchange the API key for a bearer token. Returns None on failure."""
    payload: dict = {"apikey": SERVER_TVDB_KEY}
    if TVDB_SUBSCRIBER_PIN:
        payload["pin"] = TVDB_SUBSCRIBER_PIN
    try:
        logger.info("External API Call: TVDB login")
        resp = await client.post(f"{_API_BASE}/login", json=payload, timeout=15.0)
        resp.raise_for_status()
        token = ((resp.json() or {}).get("data") or {}).get("token")
        if not token:
            logger.warning("TVDB login returned no token")
            return None
        set_cached_tvdb_json(
            _TOKEN_CACHE_KEY, {"token": token}, _TOKEN_TTL_SECONDS
        )
        return token
    except Exception as exc:
        logger.warning(f"TVDB login failed: {exc}")
        return None


async def _get_token(client: httpx.AsyncClient, *, force: bool = False) -> str | None:
    """Return a valid bearer token, logging in once (single-flight) as needed."""
    global _token_mem
    if not force:
        if _token_mem:
            return _token_mem
        cached = get_cached_tvdb_json(_TOKEN_CACHE_KEY)
        if cached and cached.get("token"):
            _token_mem = cached["token"]
            return _token_mem
    async with _get_lock():
        # Another coroutine may have refreshed while we waited for the lock.
        if not force:
            if _token_mem:
                return _token_mem
            cached = get_cached_tvdb_json(_TOKEN_CACHE_KEY)
            if cached and cached.get("token"):
                _token_mem = cached["token"]
                return _token_mem
        _token_mem = await _login(client)
        return _token_mem


async def _authed_get(
    client: httpx.AsyncClient, path: str, params: dict | None = None
) -> dict | None:
    """GET a TVDB endpoint with the bearer token, retrying once on 401.
    Returns the parsed ``data`` payload, or None on any failure."""
    global _token_mem
    token = await _get_token(client)
    if not token:
        return None
    url = f"{_API_BASE}{path}"
    for attempt in (1, 2):
        try:
            resp = await client.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15.0,
            )
            if resp.status_code == 401 and attempt == 1:
                # Token expired/invalid — force one refresh and retry.
                _token_mem = None
                token = await _get_token(client, force=True)
                if not token:
                    return None
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return (resp.json() or {}).get("data")
        except Exception as exc:
            logger.warning(f"TVDB GET {path} failed: {exc}")
            return None
    return None


# ---------------------------------------------------------------------------
# Artwork-type catalogue  (id -> category, derived by slug/name keyword)
# ---------------------------------------------------------------------------

def _classify(slug: str, name: str) -> str | None:
    """Map a TVDB artwork type's slug/name to one of our categories."""
    text = f"{slug} {name}".lower()
    if "clearlogo" in text or text.strip().endswith("logo") or " logo" in text:
        return "logos"
    if "background" in text or "fanart" in text:
        return "backgrounds"
    if "poster" in text:
        return "posters"
    return None


async def _type_map(client: httpx.AsyncClient) -> dict[str, dict[int, str]]:
    """Return ``{record_type: {type_id: category}}`` for movie/series artworks.

    ``category`` is one of 'logos' | 'backgrounds' | 'posters'. Cached long since
    the catalogue is effectively static; classification is keyword-based so a TVDB
    id renumbering can't break us as long as the slug/name still describes the art.
    """
    cached = get_cached_tvdb_json(_TYPES_CACHE_KEY)
    if cached:
        # JSON keys are strings — restore the inner id keys to ints.
        return {
            rt: {int(k): v for k, v in inner.items()}
            for rt, inner in cached.items()
        }
    data = await _authed_get(client, "/artwork/types")
    out: dict[str, dict[int, str]] = {"movie": {}, "series": {}}
    if isinstance(data, list):
        for t in data:
            rt   = (t.get("recordType") or "").lower()
            cat  = _classify(t.get("slug") or "", t.get("name") or "")
            tid  = t.get("id")
            if rt in out and cat and isinstance(tid, int):
                out[rt][tid] = cat
    if out["movie"] or out["series"]:
        set_cached_tvdb_json(
            _TYPES_CACHE_KEY,
            {rt: {str(k): v for k, v in inner.items()} for rt, inner in out.items()},
            TVDB_TYPES_CACHE_DURATION * 86400,
        )
    return out


def _record_type(media_type: str) -> str:
    return "series" if media_type in ("tv", "series") else "movie"


# TVDB tags artwork with ISO 639-2/B (3-letter) codes; the rest of the app uses
# ISO 639-1 (2-letter).  Map the common ones so language-preferred selection
# works; unknown codes pass through unchanged (still matches the neutral/eng/best
# fallbacks in _select_by_language).
_LANG_2_TO_3 = {
    "en": "eng", "es": "spa", "fr": "fra", "de": "deu", "it": "ita",
    "pt": "por", "ja": "jpn", "ko": "kor", "zh": "zho", "ru": "rus",
    "nl": "nld", "pl": "pol", "sv": "swe", "da": "dan", "no": "nor",
    "fi": "fin", "tr": "tur", "ar": "ara", "hi": "hin", "cs": "ces",
    "hu": "hun", "el": "ell", "he": "heb", "th": "tha", "uk": "ukr",
    "ro": "ron",
}


def _to_tvdb_lang(code: str | None) -> str | None:
    """Map an app language/locale code to TVDB's 3-letter code.

    TVDB does not tag artwork by region, so region-qualified locales collapse to
    their base language (es-mx → spa): a Mexican-Spanish request still wants
    Spanish artwork, and the alternative is matching nothing at all. The strict
    region separation TMDB gives us simply isn't available from this provider.
    """
    if not code:
        return code
    c = code.strip().lower().replace("_", "-")
    base = c.split("-", 1)[0]
    return _LANG_2_TO_3.get(base, base)


# ---------------------------------------------------------------------------
# ID resolution
# ---------------------------------------------------------------------------

async def resolve_tvdb_id(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    tvdb_id_hint: int | str | None = None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
) -> int | None:
    """Resolve a TVDB numeric id for a title.

    Prefers an explicit hint (e.g. tvdb_id surfaced by TMDB external_ids), then
    falls back to ``/search/remoteid`` by IMDb id, then by TMDB id. Both positive
    and negative results are cached so repeat misses don't re-hit the API.
    """
    if not tvdb_enabled():
        return None
    if tvdb_id_hint:
        try:
            return int(tvdb_id_hint)
        except (TypeError, ValueError):
            pass

    want = _record_type(media_type)
    cache_key = f"id:{want}:{imdb_id or ''}:{tmdb_id or ''}"
    cached = get_cached_tvdb_json(cache_key)
    if cached is not None:
        return cached.get("tvdb_id")  # may be None (negative cache)

    resolved: int | None = None
    async with _get_semaphore():
        for remote in (imdb_id, tmdb_id):
            if not remote:
                continue
            data = await _authed_get(client, f"/search/remoteid/{remote}")
            if not isinstance(data, list):
                continue
            for item in data:
                rec = item.get(want) if isinstance(item, dict) else None
                if isinstance(rec, dict) and rec.get("id"):
                    try:
                        resolved = int(rec["id"])
                    except (TypeError, ValueError):
                        resolved = None
                    break
            if resolved is not None:
                break

    if resolved:
        logger.info(f"TVDB id resolved: {want} imdb={imdb_id} tmdb={tmdb_id} -> {resolved}")
    else:
        logger.info(f"TVDB no match for {want} imdb={imdb_id} tmdb={tmdb_id}")
    set_cached_tvdb_json(
        cache_key,
        {"tvdb_id": resolved},
        (TVDB_ARTWORK_CACHE_DURATION if resolved else TVDB_NEG_CACHE_DURATION) * 86400,
    )
    return resolved


# ---------------------------------------------------------------------------
# Artwork index
# ---------------------------------------------------------------------------

async def fetch_tvdb_artworks(
    client: httpx.AsyncClient, tvdb_id: int, media_type: str
) -> dict[str, list[dict]]:
    """Return ``{'logos': [...], 'backgrounds': [...], 'posters': [...]}``.

    Each entry is ``{'url': str, 'language': str|None, 'score': float}`` sorted by
    descending score. The artwork index is language-agnostic, so a single fetch
    per TVDB id serves every requested logo language. Results (including empty)
    are cached.
    """
    if not tvdb_enabled():
        return {"logos": [], "backgrounds": [], "posters": []}

    want = _record_type(media_type)
    cache_key = f"art:{want}:{tvdb_id}"
    cached = get_cached_tvdb_json(cache_key)
    if cached is not None:
        return cached

    out: dict[str, list[dict]] = {"logos": [], "backgrounds": [], "posters": []}
    async with _get_semaphore():
        type_map = await _type_map(client)
        endpoint = "series" if want == "series" else "movies"
        # short=false guarantees the artworks array is included (short=true drops it).
        data = await _authed_get(
            client, f"/{endpoint}/{tvdb_id}/extended", params={"short": "false"}
        )
    artworks = (data or {}).get("artworks") if isinstance(data, dict) else None
    if isinstance(artworks, list):
        id_to_cat = type_map.get(want, {})
        for art in artworks:
            cat = id_to_cat.get(art.get("type"))
            if not cat:
                continue
            image = art.get("image") or ""
            if not image:
                continue
            url = image if image.startswith("http") else f"{_ARTWORK_BASE}/{image.lstrip('/')}"
            out[cat].append({
                "url": url,
                "language": art.get("language"),
                "score": float(art.get("score") or 0),
            })
        for cat in out:
            out[cat].sort(key=lambda a: a["score"], reverse=True)

    _has_any = any(out[c] for c in out)
    logger.info(
        f"TVDB artworks for tvdb_id={tvdb_id}: "
        f"logos={len(out['logos'])} backgrounds={len(out['backgrounds'])} "
        f"posters={len(out['posters'])}"
    )
    set_cached_tvdb_json(
        cache_key,
        out,
        (TVDB_ARTWORK_CACHE_DURATION if _has_any else TVDB_NEG_CACHE_DURATION) * 86400,
    )
    return out


def _select_by_language(
    items: list[dict],
    languages: list[str] | None,
    *,
    strict: bool = False,
) -> dict | None:
    """Pick the best artwork by language preference. Tries each requested language
    in turn, then language-neutral, then English. Items are pre-sorted by score.

    When ``strict`` is False (backgrounds/posters), an unrelated foreign-language
    item is accepted as a last resort. When ``strict`` is True (logos), that
    catch-all is dropped and ``None`` is returned instead — so the caller's
    provider chain (TMDB/Metahub) is tried rather than serving, say, a French
    logo for an English title."""
    if not items:
        return None
    for language in (languages or ()):
        if not language:
            continue
        for it in items:
            if it.get("language") == language:
                return it
    for it in items:
        if it.get("language") in (None, ""):
            return it
    for it in items:
        if it.get("language") == "eng":
            return it
    return None if strict else items[0]


# ---------------------------------------------------------------------------
# Image fetchers
# ---------------------------------------------------------------------------

async def _download(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        async with _get_semaphore():
            resp = await client.get(url, follow_redirects=True, timeout=20.0)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        logger.warning(f"TVDB image download failed ({url}): {exc}")
        return None


def _cache_key_for(url: str, prefix: str) -> str:
    # artworks.thetvdb.com paths are stable and unique per image.
    tail = url.split("artworks.thetvdb.com/", 1)[-1]
    return f"tvdb_{prefix}_" + tail.strip("/").replace("/", "_")


def _logo_language_order(
    logo_language: str | None,
    original_language: str | None,
    logo_priority: str,
    secondary_language: str | None = None,
) -> list[str]:
    """Ordered list of TVDB (3-letter) language codes to prefer, derived from the
    same priority rules TMDB uses so both sources agree on which languages count
    as a match (and, crucially, which don't).

    Deduplicated because region collapsing can fold two distinct TMDB entries
    onto one TVDB code — es-mx before es both become spa."""
    from tmdb import image_language_order
    order = image_language_order(
        logo_language or "en", original_language, logo_priority, secondary_language
    )
    return list(dict.fromkeys(lang for lang in (_to_tvdb_lang(c) for c in order) if lang))


async def fetch_tvdb_logo(
    client: httpx.AsyncClient,
    artworks: dict[str, list[dict]],
    logo_language: str | None = None,
    original_language: str | None = None,
    logo_priority: str = "native_original",
    secondary_language: str | None = None,
) -> Image.Image | None:
    """Best TVDB clearlogo as an alpha-trimmed RGBA image, or None."""
    chosen = _select_by_language(
        artworks.get("logos", []),
        _logo_language_order(
            logo_language, original_language, logo_priority, secondary_language
        ),
        strict=True,
    )
    if not chosen:
        return None
    url = chosen["url"]
    cache_key = _cache_key_for(url, "logo")
    cached = get_cached_tmdb_logo(cache_key)
    if cached:
        logger.info("TVDB logo cache hit")
        return Image.open(io.BytesIO(cached)).convert("RGBA")

    raw = await _download(client, url)
    if raw is None:
        return None
    try:
        logo = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as exc:
        logger.warning(f"TVDB logo parse failed: {exc}")
        return None
    bbox = logo.getchannel("A").getbbox()
    if bbox:
        logo = logo.crop(bbox)
    buf = io.BytesIO()
    logo.save(buf, format="PNG")
    set_cached_tmdb_logo(cache_key, buf.getvalue())
    return logo


async def tvdb_logo(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    logo_language: str | None = None,
    original_language: str | None = None,
    logo_priority: str = "native_original",
    secondary_language: str | None = None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
    tvdb_id_hint: int | str | None = None,
) -> Image.Image | None:
    """One-call logo rescue: resolve the TVDB id, pull the artwork index, return
    the best clearlogo. Safe to call unconditionally — yields None when TVDB is
    disabled or has nothing. All sub-steps are cached, so calling this alongside
    the backdrop/poster helpers in the same request costs at most one API burst.
    """
    from config import TVDB_USE_LOGOS
    if not tvdb_enabled() or not TVDB_USE_LOGOS:
        return None
    try:
        tvdb_id = await resolve_tvdb_id(
            client, media_type=media_type, tvdb_id_hint=tvdb_id_hint,
            imdb_id=imdb_id, tmdb_id=tmdb_id,
        )
        if not tvdb_id:
            return None
        artworks = await fetch_tvdb_artworks(client, tvdb_id, media_type)
        logo = await fetch_tvdb_logo(
            client, artworks, logo_language,
            original_language=original_language, logo_priority=logo_priority,
            secondary_language=secondary_language,
        )
        if logo is not None:
            logger.info(f"TVDB logo rescue succeeded for tvdb_id={tvdb_id}")
        else:
            logger.info(f"TVDB logo rescue found no usable logo for tvdb_id={tvdb_id}")
        return logo
    except Exception as exc:
        logger.warning(f"TVDB logo rescue failed: {exc}")
        return None


async def fetch_tvdb_backdrop(
    client: httpx.AsyncClient,
    artworks: dict[str, list[dict]],
    tvdb_id: int,
    *,
    avoid_text: bool = False,
) -> Image.Image | None:
    """Best TVDB background, cropped to a portrait poster via the same crop logic
    as TMDB backdrops (face-aware → saliency, optional text avoidance)."""
    chosen = _select_by_language(artworks.get("backgrounds", []), None)
    if not chosen:
        return None
    url = chosen["url"]
    # Reuse TMDB's crop + cache-version scheme so behaviour and invalidation match.
    from tmdb import _crop_and_normalise_backdrop, normalise_poster, _CROP_VERSION
    cache_key = (
        _cache_key_for(url, "backdrop") + f"_{_CROP_VERSION}" + ("_ta" if avoid_text else "")
    )
    cached = get_cached_tmdb_poster(cache_key)
    if cached:
        logger.info(f"TVDB backdrop cache hit for {tvdb_id}")
        image = Image.open(io.BytesIO(cached)).convert("RGBA")
        if image.size != (POSTER_WIDTH, POSTER_HEIGHT):
            image = normalise_poster(image)
        return image

    raw = await _download(client, url)
    if raw is None:
        return None
    try:
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as exc:
        logger.warning(f"TVDB backdrop parse failed for {tvdb_id}: {exc}")
        return None
    image = await asyncio.get_running_loop().run_in_executor(
        None, _crop_and_normalise_backdrop, image, f"tvdb:{tvdb_id}", avoid_text
    )
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(cache_key, buf.getvalue())
    return image


async def tvdb_backdrop(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
    tvdb_id_hint: int | str | None = None,
    avoid_text: bool = False,
) -> tuple[Image.Image | None, int | None]:
    """One-call backdrop rescue: resolve id, pull artwork index, crop the best
    background to portrait. Returns ``(image, tvdb_id)`` — the id is handed back
    so the caller can build a stable text-detection cache key. ``(None, id)`` when
    there's no usable background; ``(None, None)`` when TVDB is disabled/unmatched.
    """
    from config import TVDB_USE_BACKDROPS
    if not tvdb_enabled() or not TVDB_USE_BACKDROPS:
        return None, None
    try:
        tvdb_id = await resolve_tvdb_id(
            client, media_type=media_type, tvdb_id_hint=tvdb_id_hint,
            imdb_id=imdb_id, tmdb_id=tmdb_id,
        )
        if not tvdb_id:
            return None, None
        artworks = await fetch_tvdb_artworks(client, tvdb_id, media_type)
        image = await fetch_tvdb_backdrop(client, artworks, tvdb_id, avoid_text=avoid_text)
        return image, tvdb_id
    except Exception as exc:
        logger.warning(f"TVDB backdrop rescue failed: {exc}")
        return None, None


async def fetch_tvdb_poster(
    client: httpx.AsyncClient,
    artworks: dict[str, list[dict]],
    tvdb_id: int,
    language: str | None = None,
) -> Image.Image | None:
    """Best TVDB poster, normalised to poster dimensions. NOTE: TVDB posters
    frequently carry burned-in title text — callers must vet with text detection
    before compositing a logo over one."""
    _lang = _to_tvdb_lang(language)
    chosen = _select_by_language(artworks.get("posters", []), [_lang] if _lang else None)
    if not chosen:
        return None
    url = chosen["url"]
    from tmdb import normalise_poster
    cache_key = _cache_key_for(url, "poster")
    cached = get_cached_tmdb_poster(cache_key)
    if cached:
        logger.info(f"TVDB poster cache hit for {tvdb_id}")
        image = Image.open(io.BytesIO(cached)).convert("RGBA")
        if image.size != (POSTER_WIDTH, POSTER_HEIGHT):
            image = normalise_poster(image)
        return image

    raw = await _download(client, url)
    if raw is None:
        return None
    try:
        image = Image.open(io.BytesIO(raw)).convert("RGBA")
    except Exception as exc:
        logger.warning(f"TVDB poster parse failed for {tvdb_id}: {exc}")
        return None
    image = normalise_poster(image)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(cache_key, buf.getvalue())
    return image


async def tvdb_poster(
    client: httpx.AsyncClient,
    *,
    media_type: str,
    language: str | None = None,
    imdb_id: str | None = None,
    tmdb_id: str | None = None,
    tvdb_id_hint: int | str | None = None,
) -> tuple[Image.Image | None, int | None]:
    """One-call poster rescue: resolve id, pull artwork index, return the best
    poster normalised to poster dimensions, plus the resolved id for the caller's
    text-detection key. TVDB posters usually carry burned-in title text, so the
    caller MUST vet the result before compositing a logo. Gated by TVDB_USE_POSTERS
    (default off)."""
    from config import TVDB_USE_POSTERS
    if not tvdb_enabled() or not TVDB_USE_POSTERS:
        return None, None
    try:
        tvdb_id = await resolve_tvdb_id(
            client, media_type=media_type, tvdb_id_hint=tvdb_id_hint,
            imdb_id=imdb_id, tmdb_id=tmdb_id,
        )
        if not tvdb_id:
            return None, None
        artworks = await fetch_tvdb_artworks(client, tvdb_id, media_type)
        image = await fetch_tvdb_poster(client, artworks, tvdb_id, language)
        return image, tvdb_id
    except Exception as exc:
        logger.warning(f"TVDB poster rescue failed: {exc}")
        return None, None
