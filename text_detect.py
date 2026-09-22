"""PP-OCRv5 burned-in text detection for posters and backdrop crops."""
from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
from contextlib import contextmanager
from difflib import SequenceMatcher
import logging
from queue import LifoQueue
import os
import re
import threading
import unicodedata
import urllib.request

import numpy as np

from config import EFFECTIVE_CPUS, TEXTLESS_DETECTION_CONCURRENCY as _CONCURRENCY

logger = logging.getLogger(__name__)

try:
    from rapidocr import RapidOCR
    _HAS_RAPIDOCR = True
    _RAPIDOCR_IMPORT_ERROR = None
except Exception as exc:
    RapidOCR = None
    _HAS_RAPIDOCR = False
    _RAPIDOCR_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

_MODEL_URL = os.environ.get(
    "PPOCR_MODEL_URL",
    "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.8.0/"
    "onnx/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx",
)
_MODEL_SHA256 = os.environ.get(
    "PPOCR_MODEL_SHA256",
    "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
)
_BAKED_MODEL = "/app/models/ch_PP-OCRv5_det_mobile.onnx"
_MODEL_PATH = os.environ.get("PPOCR_MODEL_PATH") or (
    _BAKED_MODEL if os.path.exists(_BAKED_MODEL)
    else "/app/cache/ch_PP-OCRv5_det_mobile.onnx"
)
_RAPIDOCR_MODELS = importlib.resources.files("rapidocr").joinpath("models") if _HAS_RAPIDOCR else None


def _find_bundled_model(models_path, keyword: str) -> str:
    """Find a bundled rapidocr .onnx model by keyword, tolerating version renames."""
    if models_path is None:
        return ""
    import pathlib
    d = pathlib.Path(str(models_path))
    if not d.exists():
        return ""
    try:
        matches = sorted(
            str(f) for f in d.iterdir()
            if f.suffix == ".onnx" and keyword in f.stem.lower()
        )
        if not matches:
            logger.warning(
                f"No bundled rapidocr model found for keyword '{keyword}' in {d}; "
                f"available: {[f.name for f in d.iterdir() if f.suffix == '.onnx']}"
            )
        return matches[0] if matches else ""
    except Exception as exc:
        logger.warning(f"Could not scan rapidocr models dir {d}: {exc}")
        return ""


_CLS_MODEL_PATH = _find_bundled_model(_RAPIDOCR_MODELS, "cls")
_REC_MODEL_PATH = _find_bundled_model(_RAPIDOCR_MODELS, "rec")

try:
    _BOX_THRESHOLD = float(os.environ.get("PPOCR_BOX_THRESHOLD", "0.70"))
except (TypeError, ValueError):
    _BOX_THRESHOLD = 0.70
_BOX_THRESHOLD = max(0.0, min(1.0, _BOX_THRESHOLD))

try:
    _WIDE_BOX_THRESHOLD = float(
        os.environ.get("PPOCR_WIDE_BOX_THRESHOLD", "0.30")
    )
except (TypeError, ValueError):
    _WIDE_BOX_THRESHOLD = 0.30
_WIDE_BOX_THRESHOLD = max(0.0, min(_BOX_THRESHOLD, _WIDE_BOX_THRESHOLD))

try:
    _WIDE_MIN_ASPECT = float(os.environ.get("PPOCR_WIDE_MIN_ASPECT", "3.0"))
except (TypeError, ValueError):
    _WIDE_MIN_ASPECT = 3.0
_WIDE_MIN_ASPECT = max(1.0, _WIDE_MIN_ASPECT)

try:
    _WIDE_MIN_AREA = float(os.environ.get("PPOCR_WIDE_MIN_AREA", "0.01"))
except (TypeError, ValueError):
    _WIDE_MIN_AREA = 0.01
_WIDE_MIN_AREA = max(0.0, min(1.0, _WIDE_MIN_AREA))

try:
    _WIDE_MIN_Y = float(os.environ.get("PPOCR_WIDE_MIN_Y", "0.55"))
except (TypeError, ValueError):
    _WIDE_MIN_Y = 0.55
_WIDE_MIN_Y = max(0.0, min(1.0, _WIDE_MIN_Y))

try:
    _SCAN_TOP = float(os.environ.get("TEXTLESS_SCAN_TOP", "0.08"))
except (TypeError, ValueError):
    _SCAN_TOP = 0.08
_SCAN_TOP = max(0.0, min(0.9, _SCAN_TOP))

# NOTE: RapidOCR does not actually honour this — the detector receives the poster
# at its native size rounded up to multiples of 32 (a 500x750 poster arrives as
# 736x512, ~377 kpx, not the 352x512 / ~180 kpx this value implies).  Leave it.
# Forcing the configured limit through a pre-resize is ~1.37x faster but loses
# 7 of 26 detections on known-positive posters, and recall is the whole point of
# the feature.  It is kept here because DETECT_RES_SIG embeds it, so cached
# verdicts stay tied to the value that was in force when they were written.
_LIMIT_SIDE_LEN = 512

# Sizing is driven by the cores this process may actually use, not the host's
# core count — see config.effective_cpus().  Sessions divide the thread budget
# between them, and ONNX throughput degrades sharply once the total exceeds the
# real budget, so the split has to come out of a correct figure.
_MODEL_SESSIONS = max(1, min(EFFECTIVE_CPUS, _CONCURRENCY))
# No arbitrary ceiling: scaling was still near-linear at 4 threads on 4 cores
# (3.2x vs one thread) with no sign of saturation, so a 16-core host should get
# 16.  The floor of 1 matters more than any cap — oversubscription is far more
# expensive than undersubscription (on a 2-CPU budget: 135 ms at 2 threads,
# 299 ms at 6, 409 ms at 8).
_ORT_THREADS = max(1, EFFECTIVE_CPUS // _MODEL_SESSIONS)
DETECT_RES_SIG = (
    f"ppocrv5m-r7-s{_LIMIT_SIDE_LEN}-c{int(round(_BOX_THRESHOLD * 100))}"
    f"-wc{int(round(_WIDE_BOX_THRESHOLD * 100))}"
    f"-wa{int(round(_WIDE_MIN_ASPECT * 10))}"
    f"-wr{int(round(_WIDE_MIN_AREA * 10000))}"
    f"-wy{int(round(_WIDE_MIN_Y * 100))}"
    f"-t{int(round(_SCAN_TOP * 100))}"
)

_ocr_pool = None
_ocr_sessions = []
_model_lock = threading.Lock()
_load_failed = False
_load_error = None


def text_detection_available() -> bool:
    """True when the PP-OCR runtime is importable."""
    return _HAS_RAPIDOCR


def text_detection_status() -> str:
    """Compact runtime status suitable for startup and request logs."""
    if not _HAS_RAPIDOCR:
        return f"RapidOCR import failed ({_RAPIDOCR_IMPORT_ERROR})"
    if _ocr_pool is not None:
        return (
            f"ready ({DETECT_RES_SIG}, model={_MODEL_PATH}, "
            f"sessions={_MODEL_SESSIONS}, ort_threads={_ORT_THREADS})"
        )
    if _load_failed:
        return f"model load failed ({_load_error})"
    return f"not loaded (model={_MODEL_PATH})"


def _valid_model(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) < 1_000_000:
        return False
    if os.environ.get("PPOCR_SKIP_MODEL_HASH", "").lower() in ("1", "true", "yes"):
        return True
    digest = hashlib.sha256()
    with open(path, "rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == _MODEL_SHA256


def _new_ocr_session():
    params = {
        "Global.use_cls": False,
        "Global.use_rec": False,
        "Global.log_level": "error",
        "Det.model_path": _MODEL_PATH,
        "Det.limit_side_len": _LIMIT_SIDE_LEN,
        "Det.limit_type": "max",
        "Det.box_thresh": 0.3,
        "EngineConfig.onnxruntime.intra_op_num_threads": _ORT_THREADS,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        # Disable the ORT CPU memory arena so freed tensor allocations are
        # returned to the OS rather than held at the inference high-water mark
        # indefinitely.  Slightly slower first inference; no steady-state cost.
        "EngineConfig.onnxruntime.enable_cpu_mem_arena": False,
    }
    # Point RapidOCR at the bundled read-only models so it doesn't try to write
    # to a writable alias path.  Omit the key entirely when the model wasn't
    # found (e.g. rapidocr renamed it in a minor release) — RapidOCR will use
    # its own default, which is safer than a guaranteed FileNotFoundError.
    if _CLS_MODEL_PATH:
        params["Cls.model_path"] = _CLS_MODEL_PATH
    if _REC_MODEL_PATH:
        params["Rec.model_path"] = _REC_MODEL_PATH
    return RapidOCR(params=params)


def _ensure_model():
    """Download and load the bounded PP-OCR session pool once."""
    global _ocr_pool, _ocr_sessions, _load_failed, _load_error
    if not _HAS_RAPIDOCR:
        if not _load_failed:
            _load_error = _RAPIDOCR_IMPORT_ERROR
            _load_failed = True
            logger.warning(f"PP-OCR runtime unavailable: {_RAPIDOCR_IMPORT_ERROR}")
        return None
    if _ocr_pool is not None or _load_failed:
        return _ocr_pool
    with _model_lock:
        if _ocr_pool is not None or _load_failed:
            return _ocr_pool
        try:
            if not _valid_model(_MODEL_PATH):
                logger.info(
                    "Downloading PP-OCRv5 Mobile model (one-time) "
                    f"to {_MODEL_PATH}"
                )
                os.makedirs(os.path.dirname(_MODEL_PATH) or ".", exist_ok=True)
                tmp = _MODEL_PATH + ".part"
                urllib.request.urlretrieve(_MODEL_URL, tmp)
                if not _valid_model(tmp):
                    raise ValueError("downloaded model failed SHA-256 validation")
                os.replace(tmp, _MODEL_PATH)

            sessions = [_new_ocr_session() for _ in range(_MODEL_SESSIONS)]
            pool = LifoQueue(maxsize=_MODEL_SESSIONS)
            for session in sessions:
                pool.put(session)
            _ocr_sessions = sessions
            _ocr_pool = pool
            logger.info(
                "PP-OCRv5 Mobile text detector ready: "
                f"signature={DETECT_RES_SIG}, model={_MODEL_PATH}, "
                f"rapidocr={importlib.metadata.version('rapidocr')}, "
                f"sessions={_MODEL_SESSIONS}, ort_threads={_ORT_THREADS}, "
                f"threshold={_BOX_THRESHOLD:.2f}, "
                f"wide_threshold={_WIDE_BOX_THRESHOLD:.2f}, "
                f"wide_aspect={_WIDE_MIN_ASPECT:.2f}, "
                f"wide_area={_WIDE_MIN_AREA:.4f}"
            )
        except Exception as exc:
            _load_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "PP-OCR model unavailable; text detection disabled: "
                f"{_load_error}"
            )
            _load_failed = True
    return _ocr_pool



@contextmanager
def _borrow_ocr():
    pool = _ensure_model()
    if pool is None:
        yield None
        return
    session = pool.get()
    try:
        yield session
    finally:
        pool.put(session)


def warm_model() -> bool:
    return _ensure_model() is not None


def _detect(image):
    if _ensure_model() is None:
        return None, None, 0, 0
    pil_image = image.convert("RGB")
    width, height = pil_image.size
    if not height or not width:
        pil_image.close()
        return None, None, width, height
    try:
        with _borrow_ocr() as ocr:
            result = ocr(pil_image, use_det=True, use_cls=False, use_rec=False)
        boxes  = [] if result.boxes is None else list(result.boxes)
        scores = [] if result.scores is None else list(result.scores)
        del result
    finally:
        pil_image.close()
    return boxes, scores, width, height


def _normalise_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _title_terms(value: str) -> list[str]:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return [
        term for term in re.findall(r"[a-z0-9]+", value.lower())
        if len(term) >= 4
    ]


def _text_matches_title(candidate: str, title: str) -> bool:
    candidate = _normalise_text(candidate)
    expected = _normalise_text(title)
    if len(candidate) < 4 or len(expected) < 4:
        return False
    if candidate in expected or expected in candidate:
        return True
    if len(candidate) < 6:
        return False
    if len(expected) >= 6 and SequenceMatcher(None, candidate, expected).ratio() >= 0.82:
        return True
    return any(
        SequenceMatcher(None, candidate, term).ratio() >= 0.82
        for term in _title_terms(title)
        if len(term) >= 6
    )


def _recognised_alpha_lengths(texts: list[str]) -> list[int]:
    return [
        len(re.sub(r"[^a-z]", "", text.lower()))
        for text in texts
    ]


def _recognised_title_match(
    image,
    title: str | list[str] | tuple[str, ...],
    boxes,
    scores,
) -> tuple[bool, list[str], int]:
    titles = [title] if isinstance(title, str) else list(title)
    titles = [value for value in titles if value]
    expected_titles = [_normalise_text(value) for value in titles]
    if not any(len(value) >= 4 for value in expected_titles):
        return False, [], 0

    source = image.convert("RGB")
    try:
        width, height = source.size
        image_area = max(1, width * height)
        texts = []
        centred_lines = 0
        for box, score in zip(boxes, scores):
            box = np.asarray(box, dtype=np.float32)
            box_width = float(box[:, 0].max() - box[:, 0].min())
            box_height = float(box[:, 1].max() - box[:, 1].min())
            aspect = box_width / max(1.0, box_height)
            area_ratio = (box_width * box_height) / image_area
            centre_x = float(box[:, 0].mean()) / max(1, width)
            title_candidate = aspect >= 1.5 and area_ratio >= _WIDE_MIN_AREA
            centred_candidate = (
                aspect >= _WIDE_MIN_ASPECT
                and area_ratio >= 0.0015
                and 0.25 <= centre_x <= 0.75
            )
            if (
                float(score) < _WIDE_BOX_THRESHOLD
                or not (title_candidate or centred_candidate)
            ):
                continue

            pad = max(3, int(box_height * 0.2))
            left   = max(0,     int(box[:, 0].min()) - pad)
            top    = max(0,     int(box[:, 1].min()) - pad)
            right  = min(width, int(box[:, 0].max()) + pad)
            bottom = min(height, int(box[:, 1].max()) + pad)
            pil_crop = source.crop((left, top, right, bottom))
            try:
                crop = np.asarray(pil_crop)
                with _borrow_ocr() as ocr:
                    result = ocr(crop, use_det=False, use_cls=False, use_rec=True)
            finally:
                pil_crop.close()
                del crop
            recognised = [] if result.txts is None else [
                str(text) for text in result.txts
            ]
            del result
            texts.extend(recognised)
            if centred_candidate and any(
                len(re.sub(r"[^a-z]", "", text.lower())) >= 5
                for text in recognised
            ):
                centred_lines += 1
            if title_candidate and any(
                _text_matches_title(text, alias)
                for text in recognised
                for alias in titles
            ):
                return True, texts, centred_lines
            if float(score) >= 0.80 and area_ratio >= 0.10:
                for text in recognised:
                    candidate = _normalise_text(text)
                    if any(
                        len(candidate) >= 6
                        and len(expected) >= 6
                        and SequenceMatcher(None, candidate, expected).ratio() >= 0.70
                        for expected in expected_titles
                    ):
                        return True, texts, centred_lines
        return False, texts, centred_lines
    finally:
        source.close()


def _qualifying_boxes(
    boxes,
    scores,
    width: int,
    height: int,
    conf: float,
    scan_top: float,
):
    cutoff = height * scan_top
    image_area = max(1, width * height)
    hits = []
    for box, score in zip(boxes, scores):
        box = np.asarray(box, dtype=np.float32)
        score = float(score)
        center_y = float(box[:, 1].mean())
        if center_y < cutoff:
            continue

        box_width = float(box[:, 0].max() - box[:, 0].min())
        box_height = float(box[:, 1].max() - box[:, 1].min())
        aspect = box_width / max(1.0, box_height)
        area_ratio = (box_width * box_height) / image_area
        is_wide_title = (
            score >= _WIDE_BOX_THRESHOLD
            and aspect >= _WIDE_MIN_ASPECT
            and area_ratio >= _WIDE_MIN_AREA
            and center_y / max(1, height) >= _WIDE_MIN_Y
        )
        if is_wide_title:
            hits.append((box, score, is_wide_title, aspect, area_ratio))
    return hits


def poster_has_burned_in_text(
    image,
    *,
    conf: float = _BOX_THRESHOLD,
    lower_region: float = _SCAN_TOP,
    title: str | list[str] | tuple[str, ...] | None = None,
    source: str = "poster",
    debug: bool = False,
) -> bool | None:
    """Return True/False for a completed scan, or None when unavailable."""
    try:
        if source not in ("poster", "backdrop"):
            raise ValueError(f"unknown text-detection source: {source}")
        boxes, scores, width, height = _detect(image)
        if boxes is None:
            return None
        hits = _qualifying_boxes(boxes, scores, width, height, conf, lower_region)
        recognised = []
        should_recognise = bool(title) and any(
            float(score) >= _WIDE_BOX_THRESHOLD
            for score in scores
        )
        detected = False
        centred_lines = 0
        if should_recognise:
            detected, recognised, centred_lines = _recognised_title_match(
                image, title, boxes, scores
            )
        alpha_lengths = _recognised_alpha_lengths(recognised)
        # Two centred lines are enough when OCR also sees substantial copy;
        # short two-line logos remain below these character thresholds.
        if (
            not detected
            and source == "poster"
            and centred_lines >= 2
            and sum(alpha_lengths) >= 30
            and max(alpha_lengths, default=0) >= 16
        ):
            detected = True
        # Recognition is primary because PP-OCR can confidently box broad scene
        # textures. Preserve a narrow escape hatch for unreadable poster titles.
        if not detected and source == "poster":
            has_readable_text = max(alpha_lengths, default=0) >= 6
            for box, score, _is_wide, aspect, area_ratio in hits:
                box = np.asarray(box, dtype=np.float32)
                left_margin = float(box[:, 0].min()) / max(1, width)
                right_margin = 1.0 - float(box[:, 0].max()) / max(1, width)
                centre_x = float(box[:, 0].mean()) / max(1, width)
                full_width_title = (
                    has_readable_text
                    and aspect >= 3.0
                    and area_ratio >= 0.10
                    and 0.25 <= centre_x <= 0.75
                )
                if (
                    score >= conf
                    and area_ratio >= 0.03
                    and (
                        (
                            left_margin >= 0.05
                            and right_margin >= 0.05
                        )
                        or full_width_title
                    )
                ):
                    detected = True
                    break
        if debug:
            candidates = []
            image_area = max(1, width * height)
            for box, score in zip(boxes, scores):
                box = np.asarray(box, dtype=np.float32)
                box_width = float(box[:, 0].max() - box[:, 0].min())
                box_height = float(box[:, 1].max() - box[:, 1].min())
                candidates.append(
                    f"{float(score):.3f}/a{box_width / max(1.0, box_height):.2f}"
                    f"/r{(box_width * box_height) / image_area:.4f}"
                    f"/y{float(box[:, 1].mean()) / max(1, height):.2f}"
                )
            best = max((score for _box, score, *_rest in hits), default=0.0)
            logger.info(
                f"text_detect (PP-OCRv5 Mobile): boxes={len(hits)}, "
                f"best={best:.3f}, threshold={conf:.3f}, "
                f"source={source}, centred_lines={centred_lines}, "
                f"candidates=[{', '.join(candidates[:20])}], "
                f"recognised={recognised[:10]} -> {'TEXT' if detected else 'clear'}"
            )
        return detected
    except Exception as exc:
        logger.warning(f"text_detect error; scan unavailable: {exc}")
        return None


def text_column_profile(image, conf: float = _BOX_THRESHOLD):
    """Return a normalised horizontal text-density profile, or None."""
    try:
        boxes, scores, width, height = _detect(image)
        if boxes is None or width <= 0:
            return None
        profile = np.zeros(width, dtype=np.float32)
        hits = _qualifying_boxes(
            boxes, scores, width, height, conf, _SCAN_TOP
        )
        for box, score, _is_wide, _aspect, _area_ratio in hits:
            left = max(0, min(width - 1, int(np.floor(box[:, 0].min()))))
            right = max(left + 1, min(width, int(np.ceil(box[:, 0].max()))))
            profile[left:right] += score
        maximum = float(profile.max())
        if maximum > 0:
            profile /= maximum
        return profile
    except Exception as exc:
        logger.warning(f"text_column_profile error: {exc}")
        return None


if __name__ == "__main__":
    import sys
    from PIL import Image

    logging.basicConfig(level=logging.INFO)
    for path in sys.argv[1:]:
        try:
            result = poster_has_burned_in_text(Image.open(path), debug=True)
            print(f"{path}: {'HAS TEXT' if result else 'clear'}")
        except Exception as exc:
            print(f"{path}: error {exc}")
