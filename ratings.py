#ratings.py
import logging
import httpx
import numpy as np

logger = logging.getLogger(__name__)
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from awards import FETCH_FAILED, _FetchFailed, _RateLimited
from config import (
    MOVIE_WEIGHTS,
    TV_WEIGHTS,
    GENRE_MAP,
    GENRE_PRIORITY,
    SCORE_NORMALISERS,
    SCORE_GLOW_THRESHOLD,
    SCORE_GLOW_BLUR,
    SCORE_GLOW_ALPHA,
)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

async def fetch_rating(
    client: httpx.AsyncClient,
    imdb_id: str,
    mdblist_key: str,
    genre_ids: list[int],
    media_type: str = "movie",
    *,
    movie_weights: dict | None = None,
    tv_weights: dict | None = None,
) -> "tuple[dict | str, str, str | None, list[dict], int | None] | _FetchFailed | _RateLimited":
    """
    Returns ``(ratings_dict, genre, release_date, keywords, age_rating)`` on
    success, ``FETCH_FAILED`` on a network / API / 5xx error, or
    ``_RateLimited(retry_after)`` on HTTP 429.
    """

    genre = "Unknown"
    for gid in GENRE_PRIORITY:
        if gid in genre_ids:
            genre = GENRE_MAP[gid]
            break

    mdb_type = "show" if media_type in ("tv", "series") else "movie"

    try:
        logger.info(f"External API Call: Requested ratings+keywords from MDBlist for {imdb_id}")
        import upstream
        resp = await upstream.request(
            client, "GET",
            f"https://api.mdblist.com/imdb/{mdb_type}/{imdb_id}",
            service=upstream.SVC_MDBLIST,
            raise_for_status=False,
            params={"apikey": mdblist_key, "append_to_response": "keyword"},
            timeout=5.0,
        )
    except upstream.CircuitOpenError:
        logger.warning(f"MDblist circuit open — skipping rating fetch for {imdb_id}")
        return FETCH_FAILED
    except Exception as exc:
        logger.error(f"MDblist request error for {imdb_id}: {type(exc).__name__}: {exc}")
        return FETCH_FAILED

    if resp.status_code == 429:
        retry_after: float | None = None
        raw = resp.headers.get("retry-after")
        if raw:
            try:
                retry_after = float(raw.strip())
            except ValueError:
                # MDBlist sends integer-seconds in practice; we don't decode
                # the HTTP-date form. Leave None and the caller falls back
                # to its 1h default.
                retry_after = None
        logger.warning(
            f"MDblist rate-limited for {imdb_id} (retry-after={retry_after})"
        )
        return _RateLimited(retry_after)

    if resp.status_code == 404:
        logger.info(f"MDblist 404 for {imdb_id} — title not found, returning empty result")
        return {}, genre, None, [], None

    if resp.status_code != 200:
        logger.warning(f"MDblist error {resp.status_code} for {imdb_id}")
        return FETCH_FAILED

    data         = resp.json()
    release_date = data.get("released")
    keywords: list[dict] = data.get("keywords") or []

    age_rating: int | None = data.get("age_rating") or None
    if age_rating is not None:
        try:
            age_rating = int(age_rating)
        except (ValueError, TypeError):
            age_rating = None

    ratings_dict: dict[str, float] = {}
    for r in data.get("ratings", []):
        source = (r.get("source") or "").lower()
        value  = r.get("value")
        if source in SCORE_NORMALISERS and value is not None:
            ratings_dict[source] = value

    return ratings_dict, genre, release_date, keywords, age_rating


# ---------------------------------------------------------------------------
# Score colour
# ---------------------------------------------------------------------------

def _score_color(score: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    if score < 50:
        return (255, 80, 80), (160, 40, 40)
    elif score < 70:
        return (255, 210, 90), (200, 150, 40)
    elif score < 85:
        return (120, 255, 160), (40, 170, 90)
    else:
        return (190, 140, 255), (186, 85, 211)


def _score_color_alt(score: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Six-band alternative: dark red → red → dark amber → yellow → dark green → bright green."""
    if score < 17:    # dark red
        return (180, 30,  30),  (120, 15,  15)
    elif score < 34:  # red
        return (255, 70,  70),  (200, 45,  45)
    elif score < 50:  # dark amber
        return (200, 130, 20),  (150, 90,  10)
    elif score < 67:  # yellow
        return (255, 215, 60),  (210, 165, 30)
    elif score < 84:  # dark green
        return (50,  160, 80),  (25,  110, 50)
    else:             # bright green
        return (110, 245, 150), (60,  190, 100)


def _score_color_metal(score: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Four-band metal palette mirroring the quality-tier badge colours: grey → bronze → silver → gold."""
    if score < 50:    # grey
        return (140, 140, 148), (90,  90,  98)
    elif score < 70:  # bronze
        return (210, 120,  50), (150, 80,  25)
    elif score < 85:  # silver
        return (218, 224, 240), (155, 165, 195)
    else:             # gold
        return (255, 210,  60), (200, 150,  25)


def _soften(rgb: tuple[int, int, int], amount: float = 0.9) -> tuple[int, int, int]:
    r, g, b = rgb
    return (
        int(r * amount + 255 * (1 - amount)),
        int(g * amount + 255 * (1 - amount)),
        int(b * amount + 255 * (1 - amount)),
    )


# ---------------------------------------------------------------------------
# Score bar  (horizontal)
# ---------------------------------------------------------------------------

def draw_score_bar(
    image: Image.Image,
    score: int | str,
    *,
    bottom_margin: int = 30,
    side_margin: int = 70,
    glow_threshold: int = SCORE_GLOW_THRESHOLD,
    glow_blur: int = SCORE_GLOW_BLUR,
    glow_alpha: int = SCORE_GLOW_ALPHA,
    color_mode: int = 0,
) -> None:
    if score is None:
        return
    if isinstance(score, str):
        try:
            score = int(score)
        except ValueError:
            return
    score = max(0, min(int(score), 100))
    W, H = image.size
    bar_h  = max(8, round(H * 0.012))
    x0, x1 = side_margin, W - side_margin
    y1, y0  = H - bottom_margin, H - bottom_margin - bar_h
    bar_w   = x1 - x0
    fill_w  = int(bar_w * (score / 100))
    radius = min(bar_h // 2, 8)

    # ── Track (background pill) ───────────────────────────────────────────
    # Drawn BEFORE the fill_w<=0 early-return so score=0 still shows an
    # empty track rather than no bar at all (which would otherwise be
    # visually indistinguishable from "no rating available").
    track = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(track).rounded_rectangle(
        [(x0, y0), (x1 - 1, y1 - 1)],
        radius=radius,
        fill=(255, 255, 255, 45),
    )
    image.alpha_composite(track)

    if fill_w <= 0:
        return
    _color_fn = {1: _score_color_alt, 2: _score_color_metal}.get(color_mode, _score_color)
    left_color, right_color = _color_fn(score)
    left_color  = _soften(left_color,  0.90)
    right_color = _soften(right_color, 0.90)

    # ── Filled segment — numpy gradient, no Python pixel loop ────────────
    # Build an (bar_h × fill_w) RGB array by interpolating left→right colour.
    t = np.linspace(0, 1, fill_w, dtype=np.float32)               # (fill_w,)
    r_ch = (left_color[0] * (1 - t) + right_color[0] * t).astype(np.uint8)
    g_ch = (left_color[1] * (1 - t) + right_color[1] * t).astype(np.uint8)
    b_ch = (left_color[2] * (1 - t) + right_color[2] * t).astype(np.uint8)
    a_ch = np.full(fill_w, 220, dtype=np.uint8)

    # Stack into RGBA (fill_w, 4), then broadcast to (bar_h, fill_w, 4)
    row  = np.stack([r_ch, g_ch, b_ch, a_ch], axis=1)             # (fill_w, 4)
    grad_arr = np.broadcast_to(row, (bar_h, fill_w, 4)).copy()    # (bar_h, fill_w, 4)
    grad = Image.fromarray(grad_arr, "RGBA")

    # Rounded left/right mask so filled segment respects pill shape
    # We build the mask at bar_h × (fill_w + radius) then crop to fill_w
    mask_w = fill_w + radius
    mask_img  = Image.new("L", (mask_w, bar_h), 0)
    mask_draw = ImageDraw.Draw(mask_img)
    if score >= 99:
        mask_draw.rounded_rectangle([(0, 0), (fill_w - 1, bar_h - 1)], radius=radius, fill=255)
    else:
        mask_draw.rounded_rectangle([(0, 0), (mask_w - 1, bar_h - 1)], radius=radius, fill=255)
    mask_img = mask_img.crop((0, 0, fill_w, bar_h))
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius=0.8))

    fill_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fill_layer.paste(grad, (x0, y0), mask_img)
    image.alpha_composite(fill_layer)

    # ── Highlight sliver ─────────────────────────────────────────────────
    hl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(hl).line(
        [(x0 + radius, y0 + 1), (x0 + fill_w - 1, y0 + 1)],
        fill=(255, 255, 255, 60),
        width=1,
    )
    image.alpha_composite(hl)

    # ── Glow ─────────────────────────────────────────────────────────────
    if score >= glow_threshold:
        expand = glow_blur * 2
        glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle(
            [(x0 - expand, y0 - expand), (x0 + fill_w + expand, y1 + expand)],
            radius=radius + expand,
            fill=(255, 255, 255, glow_alpha),
        )
        glow = glow.filter(ImageFilter.GaussianBlur(glow_blur))
        image.alpha_composite(glow)


# ---------------------------------------------------------------------------
# Score bar  (vertical pip)
# ---------------------------------------------------------------------------

def draw_score_bar_vertical(
    image: Image.Image,
    score: int | str,
    *,
    x: float,
    y_center: int,
    height: int = 36,
    width: int = 4,
    color_mode: int = 0,
) -> None:
    if score is None:
        return
    if isinstance(score, str):
        try:
            score = int(score)
        except ValueError:
            return

    score = max(0, min(int(score), 100))
    _color_fn = {1: _score_color_alt, 2: _score_color_metal}.get(color_mode, _score_color)
    left_color, right_color = _color_fn(score)
    draw = ImageDraw.Draw(image)
    y0 = int(y_center - height / 2)
    y1 = y0 + height
    radius = max(1, width // 2)

    draw.rounded_rectangle(
        [(x, y0), (x + width, y1)],
        radius=radius,
        fill=(*left_color, 255),
    )


# ---------------------------------------------------------------------------
# Weighted score
# ---------------------------------------------------------------------------

def calculate_weighted_score(
    ratings: dict,
    weights: dict,
) -> int | str:

    total_weight = 0.0
    weighted_sum = 0.0

    for source, value in ratings.items():
        if source not in weights:
            continue

        weight = weights[source]

        if weight == 0:
            continue

        normaliser = SCORE_NORMALISERS.get(source)
        if not normaliser:
            logger.warning(f"No normaliser for source '{source}' — skipping")
            continue

        weighted_sum += normaliser(value) * weight
        total_weight += weight

    if total_weight == 0:
        return "N/A"

    return round(weighted_sum / total_weight)