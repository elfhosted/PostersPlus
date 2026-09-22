# i18n.py
#
# Poster-output translation with per-key English fallback.
#
# Each languages/<code>.json supplies genreLabels / sashLabels maps keyed by the
# CANONICAL ENGLISH strings the renderer produces (see languages/en.json for the
# reference vocabulary).  Translation is display-only: every internal decision
# (award-star matching, sash priority, font/colour lookups) stays in English, so
# a missing key, a malformed file, or a language with no JSON at all simply falls
# back to the English canonical string.  Nothing breaks if a translation is
# absent — it just renders in English.
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "languages")
_LANGS: dict[str, dict] = {}

# Trending labels are produced as "#<rank> Today"; translated via the
# "trendingToday" template key (e.g. "#{rank} Aujourd'hui") so the rank stays.
_TRENDING_RE = re.compile(r"^#(\d+)\s+Today$")

# Composite nominee labels are joined with this separator in discovery.pick_sash.
_NOM_SEP = " • "

# Dated release-status labels come out of discovery.release_date_label as
# "Oct 16 Cinema" or "Dec 2027 Cinema".  They translate through the
# "releaseDay" / "releaseMonth" templates ({month}, {day}, {year}, {window})
# so a language can reorder the parts; the window is the plain status label
# ("Cinema" / "Streaming" / "Physical") and translates through its own entry,
# and the month name comes from the top-level "monthsShort" list (twelve
# entries, January first).
_RELEASE_DATE_RE = re.compile(
    r"^([A-Z][a-z]{2}) (\d{1,2}|\d{4}) (Cinema|Streaming|Physical)$"
)
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def load_languages() -> None:
    """Load every languages/*.json into memory once (call at startup)."""
    _LANGS.clear()
    if not os.path.isdir(_LANG_DIR):
        return
    for fn in os.listdir(_LANG_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(_LANG_DIR, fn), encoding="utf-8") as f:
                data = json.load(f)
            code = (data.get("code") or os.path.splitext(fn)[0]).strip().lower()
            if code:
                _LANGS[code] = data
        except Exception as e:  # malformed file → skip, English fallback stands
            logger.warning(f"i18n: could not load language file {fn!r}: {e}")
    if _LANGS:
        logger.info(f"i18n: loaded languages {sorted(_LANGS)}")


def _lang_candidates(lang: str | None) -> list[str]:
    code = (lang or "").strip().lower().replace("_", "-")
    if not code:
        return []
    base = code.split("-", 1)[0]
    return list(dict.fromkeys([code, base]))


def has_language(lang: str | None) -> bool:
    return any(code in _LANGS for code in _lang_candidates(lang))


def _table(lang: str | None, key: str) -> dict:
    for code in _lang_candidates(lang):
        table = _LANGS.get(code, {}).get(key, {}) or {}
        if table:
            return table
    return {}


def _months_short(lang: str | None) -> tuple[str, ...]:
    for code in _lang_candidates(lang):
        months = _LANGS.get(code, {}).get("monthsShort")
        if isinstance(months, list) and len(months) == 12:
            return tuple(str(m) for m in months)
    return _MONTHS_EN


def _translate_release_date(match: "re.Match[str]", sl: dict, lang: str | None) -> str:
    month_en, rest, window_en = match.group(1), match.group(2), match.group(3)
    if month_en not in _MONTHS_EN:
        return match.group(0)
    month = _months_short(lang)[_MONTHS_EN.index(month_en)]
    window = sl.get(window_en, window_en)
    if len(rest) == 4:
        tmpl = sl.get("releaseMonth")
        return (tmpl.replace("{month}", month).replace("{year}", rest).replace("{window}", window)
                if tmpl else match.group(0))
    tmpl = sl.get("releaseDay")
    return (tmpl.replace("{month}", month).replace("{day}", rest).replace("{window}", window)
            if tmpl else match.group(0))


def translate_genre(name: str | None, lang: str | None) -> str:
    """Canonical English genre name → localized, or unchanged if no translation."""
    if not name:
        return name or ""
    return _table(lang, "genreLabels").get(name, name)


def translate_sash(label: str | None, lang: str | None) -> str:
    """Canonical English sash label → localized, or unchanged if no translation.

    Handles two special shapes: the "#<rank> Today" trending template and the
    " • "-joined composite nominee label (each part translated independently).
    Proper nouns and operator-defined labels (studio / director / cast) usually
    aren't in the JSON, so they pass straight through.
    """
    if not label:
        return label or ""
    sl = _table(lang, "sashLabels")
    if not sl:
        return label

    m = _TRENDING_RE.match(label)
    if m:
        tmpl = sl.get("trendingToday")
        return tmpl.replace("{rank}", m.group(1)) if tmpl else label

    m = _RELEASE_DATE_RE.match(label)
    if m:
        return _translate_release_date(m, sl, lang)

    if _NOM_SEP in label:
        return _NOM_SEP.join(sl.get(part, part) for part in label.split(_NOM_SEP))

    return sl.get(label, label)
