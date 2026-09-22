"""
Landscape (16:9) poster rendering.

Deliberately a separate renderer rather than a mode inside ``build_poster``.
Almost every anchor in the portrait layout is keyed to *width* — the diagonal
sash, the badge row, the rating bar, the logo box — and on a canvas that is
twice as wide and 40% shorter every one of them lands wrong.  The portrait
vocabulary does not survive the aspect change, so this file owns its own.

Layout, all fractions of the canvas:

    +--------------------------------------------------+
    |  [badge]                              [badge]    |   top_left / top_right
    |                                                  |
    |                                                  |
    |......................vignette....................|   band, 0.40 h
    |  LOGO  (or title)              Genre | Yr | 87   |
    +--------------------------------------------------+

Three rules govern the whole thing:

  * **Sizes key off height, positions off both.**  A width-derived font on a
    1000x563 canvas is nearly three times its optical size on 500x750.
  * **Both top corners stay clear of anything load-bearing.**  Stremio draws its
    watched check and hover-dismiss top-left, Nuvio draws its watched badge
    top-right; each takes roughly 11% of width by 20% of height.  The badge is
    placed inside that zone only because the user asked for it — it is a glass
    pill, so a small circle overlapping its leading corner stays readable.
  * **Baselines sit above 0.85 h**, clearing Stremio's continue-watching
    progress bar.

The tinted vignette is the one part of the portrait system that transfers
unchanged, and improves: its colour ramp already runs left-to-right across the
band, so twice the width gives it twice the runway.  Its helpers live in
main.py and are imported at call time — the same late-import idiom tvdb.py uses
for tmdb internals — to keep this module free of a circular import.
"""
from __future__ import annotations

import colorsys
import os

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from i18n import translate_genre, translate_sash

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# --- Layout constants (fractions of the canvas) ------------------------------

_BAND_RATIO      = 0.40   # bottom vignette height
_BAND_ALPHA      = 212    # peak alpha at the very bottom row
_BAND_CURVE      = 1.5    # easing exponent, shared with the portrait band

# Absolute cell counts the tint sampler works in.  The portrait defaults (64/24)
# describe a 500px-wide band; at 1000px each cell would cover twice the content,
# so the local-colour end of the blur slider would go coarse exactly where it
# wants to be sharper.
_TINT_COLUMNS    = 96
_RAMP_COLUMNS    = 36

_SIDE_PAD        = 0.055  # left inset for the logo / badge
_RIGHT_PAD       = 0.045  # right inset for the info strip
# Shared bottom baseline for the logo and the info strip — the logo's ink
# bottom and the text baseline, which is where the two align optically.
#
# Anchored low on purpose.  The band's alpha ramps to full at the very bottom
# row, so anything sitting high in it is being asked to read against the weakest
# part of the only thing put there to support it.  This leaves a ~6% margin
# below the text, which is about where the ink stops once descenders are drawn.
_BASELINE        = 0.925
_BAND_CLEAR      = 0.02   # keep the logo this far inside the band's top edge

_LOGO_MAX_W      = 0.42   # keeps the logo out of the info strip's half
_LOGO_MAX_H      = 0.30

_BADGE_TOP       = 0.075
_BADGE_FONT      = 0.042
_BADGE_PAD_X     = 22
_BADGE_PAD_Y     = 11

_INFO_FONT       = 0.058  # "Genre • Year • Score" strip

# Fallback title, used when a title has no logo.  A range rather than a size:
# it is set as large as fits and stepped down before anything is cut, because a
# title is content and losing it should be the last resort.  Two lines are
# allowed for the same reason — the logo box is 0.30 h and one line of text uses
# about a third of that, so the second line is free and lands the text nearer the
# optical weight of the logos it shares a row with.
_TITLE_FONT_MAX  = 0.085
_TITLE_FONT_MIN  = 0.050
_TITLE_FONT_STEP = 0.005
_TITLE_LINE      = 1.12   # line height as a multiple of font size
_TITLE_MAX_LINES = 2

_MUTED           = (255, 255, 255, 170)
_SEPARATOR       = (255, 255, 255, 90)

# Black, not a colour of its own.  The panel already carries the poster's hue,
# and any tinted border competes with it — a gold one disappeared outright on
# posters whose dominant colour was itself gold.  Black reads as an edge against
# every panel the art can produce.
_BORDER_RGB      = (0, 0, 0)
_BORDER_RATIO    = 0.045  # hairline width as a fraction of pill height
_BORDER_ALPHA    = 200
_BORDER          = False  # borderless: the lift below is what separates it

# Borderless lift.  The panel takes the colour of the art directly under it and
# raises its Value, so it reads as a lit surface sitting above that art rather
# than a hole cut into it.  Whichever of the two lifts is larger wins: the
# multiplier carries mid-tones, the addend rescues near-black backings that a
# multiplier would leave black.  Saturation eases off slightly — a lit surface
# scatters, so holding full chroma reads as paint rather than glass.
_LIFT_MUL        = 1.85
_LIFT_ADD        = 0.34
_LIFT_SAT        = 0.82
_LIFT_OPACITY    = 0.86  # frost layer alpha; higher than the bordered pill used
# Minimum luma the panel has to stand off its backing by, 0-255.  Lifting alone
# cannot always reach it: a backing that is already bright has no headroom left,
# and the panel lands on the same tone it is sitting on.  Where that happens the
# same colour is taken downward instead — still the art's own hue, still no
# border, just separated in the direction that had room.
_MIN_SEPARATION  = 30.0
_DROP_MUL        = 0.45
_DROP_SUB        = 0.28

# How the badge takes the poster's colour — see _glass_pill.  "match" holds the
# art's own lightness, so a dark poster keeps a dark panel; True is the frosted
# notch's reference mode, which lifts Value and always lands light.
_LANDSCAPE_FROST_MODE: bool | str = "match"


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(os.path.join(_FONTS_DIR, name), max(1, size))


def _draw_vignette(image: Image.Image, art: Image.Image, cfg) -> None:
    """Paint the bottom band, tinted from the art when the user asked for it.

    ``art`` is the pre-vignette snapshot: sampling ``image`` would just return
    the darkness a previous pass painted.
    """
    from main import (
        _vignette_dominant_rgb, _vignette_secondary_rgb, _vignette_tint_band,
        _vignette_frost_band, _vignette_level_band, _VIGNETTE_SAT_FULL,
    )

    width, height = image.size
    band_h = max(1, int(height * _BAND_RATIO))
    band_y = height - band_h

    t = np.linspace(0, 1, band_h, dtype=np.float32)
    eased = ((1 - (1 - t) ** _BAND_CURVE) * _BAND_ALPHA).astype(np.uint8)
    ramp = Image.fromarray(
        np.broadcast_to(eased[:, np.newaxis], (band_h, width)).copy(), mode="L"
    )

    box = (0, band_y, width, height)
    tinted = None
    if cfg.vignette_poster_color_bottom:
        _strict, tint, conf = _vignette_dominant_rgb(art)
        if tint is not None:
            second = (
                _vignette_secondary_rgb(art, tint)
                if cfg.vignette_color_ramp and conf > 0 else None
            )
            # Same derivation the portrait bands use: levelling follows
            # whichever of saturation / blur is asking for more of it.
            slider = min(1.0, max(0.0, cfg.vignette_color_saturation) / _VIGNETTE_SAT_FULL)
            level = max(slider, min(1.0, max(0.0, cfg.vignette_color_blur)))
            _vignette_frost_band(image, box, ramp, cfg.vignette_color_blur)
            _vignette_level_band(image, box, ramp, level)
            tinted = _vignette_tint_band(
                art, box, tint, conf,
                cfg.vignette_color_saturation, cfg.vignette_color_blur,
                second, cfg.vignette_color_lightness,
            ).convert("RGBA")

    if tinted is None:
        tinted = Image.new("RGBA", (width, band_h), (0, 0, 0, 0))
    tinted.putalpha(ramp)
    image.paste(tinted, (0, band_y), mask=tinted)


def _luma(rgb) -> float:
    """Rec. 709 relative luminance, 0-255."""
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _lift(rgb: tuple[float, float, float], backing: float) -> tuple[int, int, int]:
    """Move a colour off its backing in Value, keeping its hue.

    Up by preference — a lit surface above the art is the effect being aimed at.
    But a bright backing leaves nowhere to go: on a stadium crowd at luma 126 the
    lifted panel measured 128, a separation of 2, which the eye reads as a hole
    rather than a surface.  When the lift cannot clear ``_MIN_SEPARATION`` the
    same hue is taken down instead, which always has room because the floor is
    black.
    """
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb))
    s *= _LIFT_SAT

    up = colorsys.hsv_to_rgb(h, s, min(1.0, max(v * _LIFT_MUL, v + _LIFT_ADD)))
    up = tuple(c * 255 for c in up)
    if _luma(up) - backing >= _MIN_SEPARATION:
        return tuple(round(c) for c in up)

    down = colorsys.hsv_to_rgb(h, s, max(0.0, min(v * _DROP_MUL, v - _DROP_SUB)))
    return tuple(round(c * 255) for c in down)


def _glass_pill(image: Image.Image, box: tuple[int, int, int, int],
                art: Image.Image, cfg) -> tuple[int, int, int]:
    """Frosted pill carrying the poster's own colour.  Returns its ink colour.

    Same construction as the portrait frosted notch, and deliberately the same
    helpers: a blurred crop of what the pill sits on, under a tint layer whose
    colour comes from the art rather than from the crop.  Sampling the whole
    frame rather than the region under the pill is what keeps it agreeing with
    the vignette — a local sample would put a different colour under a top-left
    badge than under a bottom-left one on the same poster.

    ``_LANDSCAPE_FROST_MODE`` picks how that colour is used:
      "match" — the colour as it came, lightness included, floored short of
                black.  Keeps a dark poster dark, so the pill still reads as
                smoked glass rather than becoming a bright chip.
      True    — reference: the poster's true hue and saturation, lifted to a
                legibility floor.  Light panel, dark ink.
    """
    from awards import dominant_frost_rgb, _frosted_tint, _frost_ink
    from ratings import _cairo_pill_mask

    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return (255, 255, 255)

    blurred = (image.crop(box).convert("RGB")
               .filter(ImageFilter.GaussianBlur(max(4, int(h * 0.35))))
               .convert("RGBA"))

    if _BORDER:
        tint = _frosted_tint(*dominant_frost_rgb(art),
                             cfg.sash_badge_frost_saturation, _LANDSCAPE_FROST_MODE)
        opacity = cfg.sash_badge_frost_opacity
    else:
        # Borderless: the colour comes from the art *under the pill* rather than
        # from the whole frame, because separation is a local judgement — what
        # matters is the panel standing off the pixels it actually covers.
        # dominant_frost_rgb's fallback handles the case where those pixels are
        # too dark or too washed to carry a hue, borrowing the frame's instead.
        backing = np.asarray(blurred.convert("RGB"), dtype=np.float32)
        tint = _lift(dominant_frost_rgb(image.crop(box), fallback=art),
                     _luma(backing.reshape(-1, 3).mean(axis=0)))
        opacity = _LIFT_OPACITY

    # Cairo rasterises at ANTIALIAS_BEST; PIL's rounded_rectangle has no
    # antialiasing at all, which on a hairline border is the difference between
    # an edge and a staircase.  The border is the difference of two masks rather
    # than a stroked outline, so both of its edges are smooth — stroking would
    # only smooth the outer one.
    mask = _cairo_pill_mask(w, h, h // 2)
    blurred.putalpha(mask)
    frost = Image.new("RGBA", (w, h), (*tint, 0))
    frost.putalpha(mask.point(lambda a: int(a * opacity)))
    image.alpha_composite(Image.alpha_composite(blurred, frost), (x0, y0))

    if _BORDER:
        bw = max(1, round(h * _BORDER_RATIO))
        iw, ih = max(1, w - 2 * bw), max(1, h - 2 * bw)
        inner = Image.new("L", (w, h), 0)
        inner.paste(_cairo_pill_mask(iw, ih, ih // 2), (bw, bw))
        ring = ImageChops.subtract(mask, inner)

        border = Image.new("RGBA", (w, h), (*_BORDER_RGB, 255))
        border.putalpha(ring.point(lambda a: a * _BORDER_ALPHA // 255))
        image.alpha_composite(border, (x0, y0))

    return _frost_ink(*tint)


def _draw_badge(image: Image.Image, text: str, position: str, art: Image.Image,
                cfg, logo_height: int = 0, plain: bool = False) -> None:
    width, height = image.size
    draw = ImageDraw.Draw(image)

    if plain:
        # The stacked slot sits inside the band, so the glass would be a second
        # surface doing a job the vignette has already done.  Set at the info
        # strip's size and on its baseline, so the two read as one bottom row
        # rather than as a label that happens to be near some metadata.
        _plain_font = _font("Inter-Bold.ttf", int(height * _INFO_FONT))
        draw.text((int(width * _SIDE_PAD), int(height * _BASELINE)), text,
                  font=_plain_font, fill=(255, 255, 255, 242), anchor="ls")
        return

    font = _font("Inter-Bold.ttf", int(height * _BADGE_FONT))
    tw = draw.textlength(text, font=font)
    th = int(height * _BADGE_FONT)
    bw, bh = int(tw + _BADGE_PAD_X * 2), int(th + _BADGE_PAD_Y * 2)

    if position == "top_right":
        x, y = width - int(width * _RIGHT_PAD) - bw, int(height * _BADGE_TOP)
    elif position == "logo":
        # Stacked above the logo, sharing its left edge.  With no logo drawn —
        # original art, which carries its own title treatment — there is nothing
        # to stack on, so the badge takes the bottom-left slot itself.
        x = int(width * _SIDE_PAD)
        y = int(height * _BASELINE) - bh
        if logo_height:
            y -= logo_height + int(height * 0.045)
    else:  # top_left
        x, y = int(width * _SIDE_PAD), int(height * _BADGE_TOP)

    ink = _glass_pill(image, (x, y, x + bw, y + bh), art, cfg)
    draw.text((x + _BADGE_PAD_X, y + _BADGE_PAD_Y - 2), text, font=font,
              fill=(*ink, 245))


def _draw_logo(image: Image.Image, logo: Image.Image) -> int:
    """Left-aligned, bottom-anchored. Returns the drawn height."""
    width, height = image.size

    alpha = logo.getchannel("A")
    bbox = alpha.point(lambda a: 255 if a > 32 else 0).getbbox() or alpha.getbbox()
    if bbox:
        logo = logo.crop(bbox)
    if logo.width <= 0 or logo.height <= 0:
        return 0

    # Height is capped by the ratio AND by the band itself: a tall logo scaled
    # only by _LOGO_MAX_H can top out above the vignette, leaving its upper half
    # sitting on bare art with nothing behind it.
    band_top = height * (1 - _BAND_RATIO) + height * _BAND_CLEAR
    max_h = min(int(height * _LOGO_MAX_H), int(height * _BASELINE - band_top))
    scale = min(int(width * _LOGO_MAX_W) / logo.width, max(1, max_h) / logo.height)
    drawn = logo.resize((max(1, round(logo.width * scale)),
                         max(1, round(logo.height * scale))), Image.Resampling.LANCZOS)

    x = int(width * _SIDE_PAD)
    y = int(height * _BASELINE) - drawn.height

    # Soft drop shadow so a white wordmark survives a light patch in the band.
    shadow = Image.new("RGBA", drawn.size, (0, 0, 0, 0))
    shadow.putalpha(drawn.getchannel("A"))
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(7)), (x + 3, y + 5))
    image.alpha_composite(drawn, (x, y))
    return drawn.height


def _wrap(draw, text: str, font, max_w: float, max_lines: int) -> list[str] | None:
    """Greedy word wrap.  None when it will not fit in ``max_lines`` — including
    the case of a single word too long for one line, which no wrap can help."""
    words = text.split()
    if not words:
        return None
    lines, current = [], words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_w:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines:
                return None
    lines.append(current)
    if any(draw.textlength(line, font=font) > max_w for line in lines):
        return None
    return lines


def _ellipsize(draw, text: str, font, max_w: float) -> str:
    """Trim to fit, measuring *with* the ellipsis so the result is inside max_w.

    Whole words go first — "Marvelous…" reads as a title cut short, where the
    character-wise version, "Marvelous Mornin…", reads as a bug.  Characters are
    only cut when a single word is itself too long.
    """
    if draw.textlength(text, font=font) <= max_w:
        return text
    words = text.split()
    while len(words) > 1:
        words.pop()
        candidate = " ".join(words) + "…"
        if draw.textlength(candidate, font=font) <= max_w:
            return candidate
    stem = words[0] if words else text
    while stem and draw.textlength(stem + "…", font=font) > max_w:
        stem = stem[:-1]
    return f"{stem}…" if stem else ""


def _draw_title(image: Image.Image, title: str) -> int:
    """Left-aligned, bottom-anchored text stand-in for a missing logo.

    Shares the logo's box, and returns the drawn height the same way, so a badge
    stacked above it clears the text rather than landing on it.
    """
    width, height = image.size
    draw = ImageDraw.Draw(image)

    max_w = int(width * _LOGO_MAX_W)
    band_top = height * (1 - _BAND_RATIO) + height * _BAND_CLEAR
    max_h = min(int(height * _LOGO_MAX_H), int(height * _BASELINE - band_top))

    # Largest size that fits, one line preferred over two at every size — a
    # single line beside a logo reads better than a wrapped one a size larger.
    chosen: tuple[object, list[str], int] | None = None
    ratio = _TITLE_FONT_MAX
    while ratio >= _TITLE_FONT_MIN - 1e-9 and chosen is None:
        size = max(1, int(height * ratio))
        font = _font("Inter-Bold.ttf", size)
        line_h = round(size * _TITLE_LINE)
        for count in range(1, _TITLE_MAX_LINES + 1):
            if line_h * count > max_h:
                break
            lines = _wrap(draw, title, font, max_w, count)
            if lines is not None and len(lines) == count:
                chosen = (font, lines, line_h)
                break
        ratio -= _TITLE_FONT_STEP

    if chosen is None:
        # Nothing fits whole: set at the smallest size and cut the last line.
        size = max(1, int(height * _TITLE_FONT_MIN))
        font = _font("Inter-Bold.ttf", size)
        line_h = round(size * _TITLE_LINE)
        count = max(1, min(_TITLE_MAX_LINES, int(max_h // line_h) or 1))
        lines = _wrap(draw, title, font, max_w, count) or []
        if len(lines) < count:
            # Rebuild greedily, keeping whatever fits, then trim the tail.
            words, lines, current = title.split(), [], ""
            for word in words:
                trial = f"{current} {word}".strip()
                if current and draw.textlength(trial, font=font) > max_w:
                    lines.append(current)
                    current = word
                    if len(lines) == count:
                        break
                else:
                    current = trial
            if len(lines) < count and current:
                lines.append(current)
        lines = lines[:count]
        if lines:
            consumed = len(" ".join(lines))
            remainder = title[consumed:].strip()
            if remainder:
                lines[-1] = f"{lines[-1]} {remainder}"
            # Every line, not only the last.  The greedy pass above appends a
            # word that is itself wider than the box untouched, and that word
            # can land on any line — trimming the tail alone left the overlong
            # one running off the canvas.  _ellipsize is a no-op on a line that
            # already fits, so the lines that were fine stay untouched.
            lines = [_ellipsize(draw, line, font, max_w) for line in lines]
        chosen = (font, lines or [_ellipsize(draw, title, font, max_w)], line_h)

    font, lines, line_h = chosen
    baseline = int(height * _BASELINE)
    for i, line in enumerate(reversed(lines)):
        draw.text((int(width * _SIDE_PAD), baseline - i * line_h), line,
                  font=font, fill=(255, 255, 255, 245), anchor="ls")

    ascent = font.getmetrics()[0]
    return line_h * (len(lines) - 1) + ascent


def _draw_info_strip(image: Image.Image, genre_label: str,
                     release_year: str | None, score) -> None:
    """`Genre • Year • 87`, right-aligned on the shared baseline.

    Drawn right-to-left so the score stays pinned to the right edge whatever the
    genre string does, and the whole strip is measured before anything is drawn
    so a long genre can be dropped rather than colliding with the logo.
    """
    width, height = image.size
    font = _font("Inter-Bold.ttf", int(height * _INFO_FONT))
    draw = ImageDraw.Draw(image)

    # No rating is not a rating of nothing: a title MDBList has no score for
    # drops out of the row entirely, taking its separator with it, rather than
    # printing a placeholder that reads as a value.
    if isinstance(score, bool):
        score_text = None
    elif isinstance(score, int):
        score_text = str(score)
    elif isinstance(score, str) and score.strip().isdigit():
        score_text = score.strip()
    else:
        score_text = None

    # The score takes the same weight as the genre and the year rather than a
    # score-banded colour.  Here the three are one line of metadata, and one
    # member of it changing hue per title breaks the row instead of ranking it.
    parts: list[tuple[str, tuple[int, int, int, int]]] = []
    if genre_label:
        parts.append((genre_label, _MUTED))
    if release_year:
        parts.append((str(release_year), _MUTED))
    if score_text:
        parts.append((score_text, _MUTED))
    if not parts:
        return

    sep = "  •  "
    sep_w = draw.textlength(sep, font=font)

    def total(items) -> float:
        return (sum(draw.textlength(t, font=font) for t, _ in items)
                + sep_w * max(0, len(items) - 1))

    # Everything left of the info strip belongs to the logo; if the two would
    # meet, shed the genre first, then the year, before shrinking any type.
    limit = width * (1 - _RIGHT_PAD) - width * (_SIDE_PAD + _LOGO_MAX_W) - width * 0.03
    while len(parts) > 1 and total(parts) > limit:
        parts.pop(0)

    x = width - int(width * _RIGHT_PAD)
    baseline = int(height * _BASELINE)
    for i, (text, fill) in enumerate(reversed(parts)):
        tw = draw.textlength(text, font=font)
        draw.text((x - tw, baseline), text, font=font, fill=fill, anchor="ls")
        x -= tw
        if i < len(parts) - 1:
            x -= sep_w
            draw.text((x, baseline), sep, font=font, fill=_SEPARATOR, anchor="ls")


def build_landscape(
    image: Image.Image,
    score: int | str,
    genre: str,
    cfg,
    logo: Image.Image | None = None,
    fallback_title: str | None = None,
    discovery_meta=None,
    release_year: str | None = None,
    **_ignored,
) -> Image.Image:
    """Render the landscape poster.  Mirrors ``build_poster``'s call shape so the
    request pipeline can swap one for the other; extra kwargs it does not use
    (quality tokens, age rating) are accepted and dropped."""
    from main import pick_sash

    image = image.convert("RGBA")
    art = image.copy()          # pre-vignette snapshot for tint sampling

    _draw_vignette(image, art, cfg)

    # What belongs in the logo slot was decided upstream, where the art actually
    # got picked: a logo, or a title to stand in for one, or neither when the
    # chosen art already carries its own title treatment.  Re-deriving that from
    # cfg.landscape_art is what this used to do, and it was wrong in exactly the
    # cases that matter — `original` falling back to the neutral backdrop or to
    # the genre canvas passes a title precisely because that art has none, and
    # suppressing it produced a completely untitled render.
    logo_height = 0
    if logo is not None:
        logo_height = _draw_logo(image, logo)
    elif fallback_title:
        # Height comes back for the same reason it does from the logo: a
        # badge stacked above needs something to clear.
        logo_height = _draw_title(image, fallback_title)

    _draw_info_strip(image,
                     "" if cfg.hide_genre else (translate_genre(genre, cfg.logo_language) or genre),
                     release_year, score)

    if cfg.sash_mode != "hidden" and discovery_meta is not None:
        sash_result = pick_sash(discovery_meta, cfg.sash_priority)
        if sash_result is not None:
            label, _sash_type = sash_result
            position = getattr(cfg, "landscape_badge_pos", "top_left")
            _draw_badge(image, translate_sash(label, cfg.logo_language).upper(),
                        position, art, cfg, logo_height=logo_height,
                        # Only a stacked badge with an empty logo slot lands
                        # inside the band.  Stacked over a logo or a title it
                        # often clears the band's top edge, and the top corners
                        # are bare art, so those keep the glass that makes them
                        # readable.  Keyed on what was drawn, not on the mode
                        # that was asked for, for the same reason as above.
                        plain=(logo_height == 0 and position == "logo"))

    return image
