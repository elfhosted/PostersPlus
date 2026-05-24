#tmdb.py
import asyncio
import io
import logging
import os
import httpx
import numpy as np
import re

logger = logging.getLogger(__name__)
from PIL import Image, ImageDraw

from cache import (
    get_cached_trending_snapshot,
    set_cached_trending_snapshot,
    get_cached_tmdb_poster,
    set_cached_tmdb_poster,
    get_cached_tmdb_logo,
    set_cached_tmdb_logo,
    get_cached_tmdb_metadata,
    set_cached_tmdb_metadata,
)

from config import (
    POSTER_WIDTH,
    POSTER_HEIGHT,
    LOGO_MAX_W_RATIO,
    LOGO_MAX_H_RATIO,
    LOGO_BOTTOM_RATIO,
)


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def normalise_poster(image: Image.Image) -> Image.Image:
    target_w, target_h = POSTER_WIDTH, POSTER_HEIGHT
    src_w, src_h = image.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w = round(src_w * scale)
    new_h = round(src_h * scale)
    image = image.resize((new_w, new_h), Image.LANCZOS)
    left = round((new_w - target_w) / 2)
    top  = round((new_h - target_h) / 2)
    return image.crop((left, top, left + target_w, top + target_h))


def ensure_light_logo(logo: Image.Image, threshold: float = 0.2) -> Image.Image:
    """
    If the visible pixels of *logo* are too dark, force them all to white.
    Uses numpy for vectorised luminance calculation — avoids materialising
    a Python list of per-pixel tuples.
    """
    rgba = np.array(logo.convert("RGBA"), dtype=np.float32)   # H×W×4
    alpha = rgba[:, :, 3]
    visible_mask = alpha > 30                                  # boolean H×W

    if not visible_mask.any():
        return logo

    r = rgba[:, :, 0][visible_mask]
    g = rgba[:, :, 1][visible_mask]
    b = rgba[:, :, 2][visible_mask]
    avg_lum = (0.2126 * r + 0.7152 * g + 0.0722 * b).mean() / 255.0

    if avg_lum > threshold:
        return logo

    # Force visible pixels to white, preserve alpha channel
    out = rgba.copy()
    out[:, :, 0][visible_mask] = 255
    out[:, :, 1][visible_mask] = 255
    out[:, :, 2][visible_mask] = 255
    return Image.fromarray(out.astype(np.uint8), "RGBA")


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

async def fetch_poster_metadata(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str = "movie",
) -> tuple[list[int], bool, list[dict], str | None, str, str, dict]:
    """
    Fetch (or return cached) TMDB metadata, including credits,
    production_companies, and original_language for discovery sash logic.

    Returns:
        (genre_ids, is_textless, logos, release_year, title, poster_path, tmdb_data)
    """
    endpoint = "tv" if media_type in ("tv", "series") else "movie"
    metadata_cache_key = f"{endpoint}_{tmdb_id}"

    meta = get_cached_tmdb_metadata(metadata_cache_key)

    if meta:
        logger.info(f"TMDB metadata cache hit for {tmdb_id}")
        tmdb_data = {
            "credits":               meta.get("credits", {}),
            "production_companies":  meta.get("production_companies", []),
            "original_language":     meta.get("original_language"),
            "runtime":               meta.get("runtime"),
            "number_of_seasons":     meta.get("number_of_seasons"),
            "number_of_episodes":    meta.get("number_of_episodes"),
        }
        return (
            meta["genre_ids"],
            meta["is_textless"],
            meta["logos"],
            meta["release_year"],
            meta["title"],
            meta["poster_path"],
            tmdb_data,
        )

    logger.info(f"External API Call: Requested meta from TMDB for {tmdb_id}")
    import upstream
    resp = await upstream.request(
        client, "GET",
        f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}",
        service=upstream.SVC_TMDB,
        params={
            "api_key": tmdb_key,
            "append_to_response": "images,credits",
        },
    )
    data = resp.json()

    title = (
        data.get("title")
        or data.get("name")
        or data.get("original_title")
        or data.get("original_name")
        or "Unknown Title"
    )

    raw_date = data.get("release_date") or data.get("first_air_date") or ""
    release_year: str | None = raw_date[:4] if len(raw_date) >= 4 else None

    images  = data.get("images", {})
    posters = images.get("posters", [])
    logos   = images.get("logos", [])

    textless = [p for p in posters if p.get("iso_639_1") is None]

    if textless:
        best = max(textless, key=lambda x: x.get("vote_average", 0))
        poster_path = best["file_path"]
        is_textless = True
    else:
        poster_path = data.get("poster_path")
        is_textless = False

    if not poster_path:
        raise ValueError("No poster available")

    genre_ids            = [g["id"] for g in data.get("genres", [])]
    credits              = data.get("credits", {})
    production_companies = data.get("production_companies", [])
    original_language    = data.get("original_language")
    runtime              = data.get("runtime")
    number_of_seasons    = data.get("number_of_seasons")
    number_of_episodes   = data.get("number_of_episodes")

    set_cached_tmdb_metadata(
        metadata_cache_key,
        title,
        release_year,
        genre_ids,
        is_textless,
        poster_path,
        logos,
        credits=credits,
        production_companies=production_companies,
        original_language=original_language,
        runtime=runtime,
        number_of_seasons=number_of_seasons,
        number_of_episodes=number_of_episodes,
    )

    tmdb_data = {
        "credits":              credits,
        "production_companies": production_companies,
        "original_language":    original_language,
        "runtime":              runtime,
        "number_of_seasons":    number_of_seasons,
        "number_of_episodes":   number_of_episodes,
    }

    return genre_ids, is_textless, logos, release_year, title, poster_path, tmdb_data


async def fetch_poster_image(
    client: httpx.AsyncClient,
    tmdb_id: str,
    media_type: str,
    poster_path: str,
) -> Image.Image:
    """
    Fetch and cache the base poster image.

    Disk cache format is JPEG (q=92 RGB) rather than PNG:
      - ~4-5x faster decode on cache hit
      - ~5x smaller on disk
      - Imperceptible quality difference for photographic poster art
    The image is returned as RGBA so the compositing pipeline can use
    alpha_composite throughout without mode-checking.
    """
    poster_cache_key = f"{media_type}_{tmdb_id}_{poster_path.strip('/')}"
    cached_bytes = get_cached_tmdb_poster(poster_cache_key)

    if cached_bytes:
        logger.info(f"TMDB poster cache hit for {tmdb_id}")
        # Stored as JPEG RGB — convert to RGBA for the compositing pipeline
        image = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        if image.size != (POSTER_WIDTH, POSTER_HEIGHT):
            image = normalise_poster(image)
        return image

    logger.info(f"External API Call: Requested poster from TMDB for {tmdb_id}")
    img_resp = await client.get(f"https://image.tmdb.org/t/p/w500{poster_path}")
    img_resp.raise_for_status()
    image = Image.open(io.BytesIO(img_resp.content)).convert("RGBA")
    image = normalise_poster(image)

    # Save as JPEG RGB (no alpha needed for base poster; restoring alpha on load is free)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=92)
    set_cached_tmdb_poster(poster_cache_key, buf.getvalue())

    return image


async def fetch_logo(
    client: httpx.AsyncClient,
    logos: list[dict],
    logo_language: str = "en",
) -> Image.Image | None:

    preferred = [
        lg for lg in logos
        if lg["file_path"].endswith(".png")
        and lg.get("iso_639_1") == logo_language
    ]

    english = [
        lg for lg in logos
        if lg["file_path"].endswith(".png")
        and lg.get("iso_639_1") == "en"
    ]

    neutral = [
        lg for lg in logos
        if lg["file_path"].endswith(".png")
        and lg.get("iso_639_1") is None
    ]

    candidates = preferred or neutral or english

    candidates = sorted(
        candidates,
        key=lambda x: x.get("vote_average", 0),
        reverse=True,
    )

    if not candidates:
        return None

    logo_path = candidates[0]["file_path"]

    logo_cache_key = logo_path.strip('/').replace('/', '_')
    cached_bytes = get_cached_tmdb_logo(logo_cache_key)

    if cached_bytes:
        logger.info("TMDB logo cache hit")
        logo = Image.open(io.BytesIO(cached_bytes)).convert("RGBA")
        return logo

    resp = await client.get(f"https://image.tmdb.org/t/p/w500{logo_path}")
    logger.info(f"External API Call: Requested logo from TMDB")
    resp.raise_for_status()

    logo = Image.open(io.BytesIO(resp.content)).convert("RGBA")

    bbox = logo.getchannel("A").getbbox()
    if bbox:
        logo = logo.crop(bbox)

    logo = ensure_light_logo(logo)

    buf = io.BytesIO()
    logo.save(buf, format="PNG")
    set_cached_tmdb_logo(logo_cache_key, buf.getvalue())

    return logo


async def fetch_trending_rank(
    client: httpx.AsyncClient,
    tmdb_id: str,
    tmdb_key: str,
    media_type: str = "movie",
) -> int | None:

    endpoint = "tv" if media_type in ("tv", "series") else "movie"

    snapshot = get_cached_trending_snapshot(endpoint)

    if snapshot is None:
        logger.info("External API Call: Refreshing TMDB trending snapshot (pages 1+2 concurrent)")

        async def _fetch_page(page: int) -> list[dict]:
            resp = await client.get(
                f"https://api.themoviedb.org/3/trending/{endpoint}/day",
                params={"api_key": tmdb_key, "page": page},
            )
            resp.raise_for_status()
            return resp.json().get("results", [])

        try:
            page1_results, page2_results = await asyncio.gather(
                _fetch_page(1),
                _fetch_page(2),
            )
        except Exception as exc:
            logger.error(f"TMDB trending fetch error: {exc}")
            return None

        rankings: dict[str, int] = {}
        for i, item in enumerate(page1_results, start=1):
            rankings[str(item["id"])] = i
        for i, item in enumerate(page2_results, start=len(page1_results) + 1):
            rankings[str(item["id"])] = i

        set_cached_trending_snapshot(endpoint, rankings)
        snapshot = rankings

    rank = snapshot.get(str(tmdb_id))

    if rank:
        logger.info(f"Trending rank for {tmdb_id}: #{rank}")

    return rank


# ---------------------------------------------------------------------------
# Logo rendering (onto poster)
# ---------------------------------------------------------------------------

def composite_logo(
    image: Image.Image,
    logo: Image.Image,
    *,
    max_w_ratio: float = LOGO_MAX_W_RATIO,
    max_h_ratio: float = LOGO_MAX_H_RATIO,
    bottom_ratio: float = LOGO_BOTTOM_RATIO,
) -> None:
    width, height = image.size

    max_w = int(width  * max_w_ratio)
    max_h = int(height * max_h_ratio)

    logo.thumbnail((max_w, max_h), Image.LANCZOS)

    alpha_bbox = logo.getchannel("A").getbbox()
    if alpha_bbox:
        logo = logo.crop(alpha_bbox)

    logo_x = round((width - logo.width) / 2)
    logo_y = height - int(height * bottom_ratio) - logo.height

    image.paste(logo, (logo_x, logo_y), logo)