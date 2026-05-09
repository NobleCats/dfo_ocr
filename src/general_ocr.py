"""General-purpose OCR helpers for fame numbers, class names, and UI text.

Engine selection:
- Primary: PaddleOCR PP-OCRv5 when available.
- Fallback: EasyOCR.

The module is lazy-initialized so GUI startup is not blocked. Public helpers:
read_fame(), read_class(), read_text_boxes().
"""
from __future__ import annotations

import logging
import os

import re
import sys as _sys
import threading
from dataclasses import dataclass

# Disable paddle's OneDNN backend BEFORE any paddle/paddleocr import.
os.environ.setdefault("FLAGS_use_mkldnn", "false")

# Stub modelscope so paddleocr 3.x's logger init doesn't transitively import
# torch, whose Windows DLL load can race with paddle's CUDA libraries.
for _name in ("modelscope", "modelscope.utils", "modelscope.utils.logger"):
    _sys.modules.setdefault(_name, type(_sys)(_name))

import cv2
import numpy as np

_logger = logging.getLogger("dfogang.general_ocr")

_reader = None
_engine: str = ""  # "paddle" or "easyocr"
_reader_lock = threading.Lock()
_reader_failed = False

# OCR profile:
# - default/server (default): PaddleOCR PP-OCRv5 server recognizer — higher
#   accuracy for mixed-case Korean-transliterated names like "NeNeSan".
# - mobile: lighter English/numeric recognizer; opt-in via DFO_OCR_PROFILE=mobile.
# Override exact model with DFO_OCR_RECOGNITION_MODEL env var.
_OCR_PROFILE = os.environ.get("DFO_OCR_PROFILE", "").strip().lower()
_OCR_RECOGNITION_MODEL = os.environ.get("DFO_OCR_RECOGNITION_MODEL", "").strip()

_CACHE_CAP = 256
_fame_cache: dict[bytes, tuple[int | None, str, float]] = {}
_class_cache: dict[bytes, tuple[str, float]] = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class OCRTextBox:
    text: str
    confidence: float
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 in input-image coords

    @property
    def x0(self) -> int:
        return self.bbox[0]

    @property
    def y0(self) -> int:
        return self.bbox[1]

    @property
    def x1(self) -> int:
        return self.bbox[2]

    @property
    def y1(self) -> int:
        return self.bbox[3]

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0


def _cache_get(cache: dict, key: bytes):
    with _cache_lock:
        return cache.get(key)


def _cache_set(cache: dict, key: bytes, value) -> None:
    with _cache_lock:
        if len(cache) >= _CACHE_CAP:
            cache.pop(next(iter(cache)))
        cache[key] = value


def _try_paddle():
    try:
        import paddle
        paddle.set_flags({"FLAGS_use_mkldnn": False})
        from paddleocr import PaddleOCR
    except Exception as exc:
        _logger.warning("PaddleOCR import failed: %s", exc)
        return None, ""
    base_kwargs = dict(
        lang="en",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False,
    )

    # Explicit model override > mobile profile opt-in > server default.
    model_name = _OCR_RECOGNITION_MODEL
    if not model_name and _OCR_PROFILE == "mobile":
        model_name = "en_PP-OCRv5_mobile_rec"

    attempts: list[tuple[str, dict]] = []
    if model_name:
        kwargs = dict(base_kwargs)
        kwargs["text_recognition_model_name"] = model_name
        attempts.append((model_name, kwargs))
    attempts.append(("default", base_kwargs))

    active_profile = _OCR_PROFILE or "server"

    for label, kwargs in attempts:
        try:
            reader = PaddleOCR(**kwargs)
            if label == "default":
                _logger.info(
                    "PaddleOCR reader initialized (PP-OCRv5 profile=%s rec_model=server_default)",
                    active_profile)
            else:
                _logger.info(
                    "PaddleOCR reader initialized (PP-OCRv5 profile=%s rec_model=%s)",
                    active_profile, label)
            return reader, "paddle"
        except Exception as exc:
            _logger.warning("PaddleOCR init failed for %s: %s", label, exc)
            continue
    return None, ""


def _try_easyocr():
    try:
        import easyocr
    except Exception as exc:
        _logger.warning("EasyOCR import failed: %s", exc)
        return None, ""
    try:
        reader = easyocr.Reader(["en"], gpu=False, verbose=False)
        _logger.info("EasyOCR reader initialized")
        return reader, "easyocr"
    except Exception as exc:
        _logger.warning("EasyOCR init failed: %s", exc)
        return None, ""


def _get_reader():
    """Lazy init. Tries PaddleOCR first, then EasyOCR."""
    global _reader, _engine, _reader_failed
    if _reader is not None:
        return _reader
    if _reader_failed:
        return None
    with _reader_lock:
        if _reader is not None:
            return _reader
        if _reader_failed:
            return None
        for picker in (_try_paddle, _try_easyocr):
            reader, engine = picker()
            if reader is not None:
                _reader = reader
                _engine = engine
                return _reader
        _reader_failed = True
        return None


def prewarm_in_background() -> None:
    """Kick off OCR model load in a daemon thread."""
    if _reader is not None or _reader_failed:
        return
    threading.Thread(target=_get_reader, daemon=True, name="ocr-prewarm").start()


def _prep(crop_rgb: np.ndarray) -> np.ndarray:
    """Pass raw colour pixels through. PP-OCRv5 handles small anti-aliased text best."""
    return crop_rgb if crop_rgb.ndim == 3 else cv2.cvtColor(crop_rgb, cv2.COLOR_GRAY2RGB)


def _has_text(crop_rgb: np.ndarray, min_pixels: int = 12) -> bool:
    if crop_rgb.size == 0:
        return False
    gray = crop_rgb.max(axis=2) if crop_rgb.ndim == 3 else crop_rgb
    return int((gray > 100).sum()) >= min_pixels


def _filter_text(text: str, allowlist: str | None) -> str:
    if allowlist is None:
        return text.strip()
    return "".join(c for c in text if c in allowlist).strip()


def read_text_boxes(image_rgb: np.ndarray, allowlist: str | None = None) -> list[OCRTextBox]:
    """Return OCR text with bounding boxes in input-image coordinates.

    This is used for template-free UI/window detection. For PaddleOCR, allowlist
    is applied after recognition because the pipeline API does not accept a
    per-call allowlist. For EasyOCR, allowlist is passed into readtext and also
    applied post-hoc for consistency.
    """
    if image_rgb.size == 0 or not _has_text(image_rgb, min_pixels=20):
        return []
    reader = _get_reader()
    if reader is None:
        return []
    img = _prep(image_rgb)

    boxes: list[OCRTextBox] = []
    if _engine == "paddle":
        try:
            result = reader.predict(img)
        except Exception as exc:
            _logger.debug("paddle ocr boxes call failed: %s", exc)
            return []
        if not result:
            return []
        for r in result:
            texts = r.get("rec_texts", []) or []
            scores = r.get("rec_scores", []) or []
            polys = r.get("dt_polys", []) or []
            for text, score, poly in zip(texts, scores, polys):
                if poly is None or len(poly) == 0:
                    continue
                xs = [float(p[0]) for p in poly]
                ys = [float(p[1]) for p in poly]
                filtered = _filter_text(str(text), allowlist)
                if not filtered:
                    continue
                boxes.append(OCRTextBox(
                    text=filtered,
                    confidence=float(score),
                    bbox=(int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))),
                ))
    else:
        try:
            kwargs = {"detail": 1}
            if allowlist is not None:
                kwargs["allowlist"] = allowlist
            result = reader.readtext(img, **kwargs)
        except Exception as exc:
            _logger.debug("easyocr boxes call failed: %s", exc)
            return []
        for poly, text, score in result:
            xs = [float(p[0]) for p in poly]
            ys = [float(p[1]) for p in poly]
            filtered = _filter_text(str(text), allowlist)
            if not filtered:
                continue
            boxes.append(OCRTextBox(
                text=filtered,
                confidence=float(score),
                bbox=(int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))),
            ))

    boxes.sort(key=lambda b: (b.y0, b.x0))
    return boxes


def _readtext(reader, image: np.ndarray, allowlist: str) -> list[tuple[str, float]]:
    """Engine-neutral OCR call. Returns (text, confidence), sorted left to right."""
    boxes = read_text_boxes(image, allowlist=allowlist)
    boxes.sort(key=lambda b: b.x0)
    return [(b.text, b.confidence) for b in boxes]


def read_fame(crop_rgb: np.ndarray) -> tuple[int | None, str, float]:
    """Read a fame number. Returns (value, raw_text, mean_confidence)."""
    if crop_rgb.size == 0 or not _has_text(crop_rgb):
        return None, "", 0.0
    reader = _get_reader()
    if reader is None:
        return None, "", 0.0
    big = _prep(crop_rgb)
    cache_key = big.tobytes()
    cached = _cache_get(_fame_cache, cache_key)
    if cached is not None:
        return cached
    fragments = _readtext(reader, big, "0123456789,")
    if not fragments:
        out = (None, "", 0.0)
        _cache_set(_fame_cache, cache_key, out)
        return out
    text = "".join(t for t, _ in fragments)
    confs = [c for _, c in fragments]
    avg_conf = float(np.mean(confs)) if confs else 0.0
    digits = re.sub(r"[^0-9]", "", text)
    if not digits:
        out = (None, text, avg_conf)
        _cache_set(_fame_cache, cache_key, out)
        return out
    value = _parse_with_trim(digits)
    out = (value, text, avg_conf) if value is not None else (None, text, avg_conf)
    _cache_set(_fame_cache, cache_key, out)
    return out


_FAME_MIN, _FAME_MAX = 10_000, 999_999


def _parse_with_trim(digits: str) -> int | None:
    candidates = {digits}
    if len(digits) > 1:
        candidates.add(digits[1:])
        candidates.add(digits[:-1])
    if len(digits) > 2:
        candidates.add(digits[1:-1])
    best = None
    for cand in candidates:
        if not cand:
            continue
        try:
            v = int(cand)
        except ValueError:
            continue
        if _FAME_MIN <= v <= _FAME_MAX:
            if best is None or len(cand) > len(str(best)):
                best = v
    return best


def read_class(crop_rgb: np.ndarray) -> tuple[str, float]:
    """Read a class / awakening label, e.g. 'Neo: Paramedic'."""
    if crop_rgb.size == 0 or not _has_text(crop_rgb):
        return "", 0.0
    reader = _get_reader()
    if reader is None:
        return "", 0.0
    big = _prep(crop_rgb)
    cache_key = big.tobytes()
    cached = _cache_get(_class_cache, cache_key)
    if cached is not None:
        return cached
    allowlist = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ:' "
    fragments = _readtext(reader, big, allowlist)
    if not fragments:
        out = ("", 0.0)
        _cache_set(_class_cache, cache_key, out)
        return out
    text = " ".join(t.strip() for t, _ in fragments if t.strip())
    avg_conf = float(np.mean([c for _, c in fragments])) if fragments else 0.0
    out = (text, avg_conf)
    _cache_set(_class_cache, cache_key, out)
    return out
