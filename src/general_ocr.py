"""General-purpose OCR for fame numbers and class names.

Templates work well for character names (random letter sequences like
'(-_-)' or 'lI|llL' where dictionary OCR has nothing to anchor on), but
they're a poor fit for fame digits and class labels — both are short
fixed-vocabulary strings where a general OCR model with the right
preprocessing easily beats template fidelity.

Engine selection:
- Primary: PaddleOCR PP-OCRv5 (paddleocr 3.x). Substantially more
  accurate than v4 on small/anti-aliased game text.
- Fallback: EasyOCR if PaddleOCR fails to load.

Init quirks:
- paddleocr 3.x pulls in modelscope which transitively imports torch.
  When torch and paddle's CUDA libs collide, torch fails to load and
  paddleocr import dies. We stub modelscope at module-import time to
  break the chain.
- paddle 3.x's OneDNN (MKL-DNN) backend has a bug
  (`ConvertPirAttribute2RuntimeAttribute not support`) on inference;
  we disable it via env flags before paddle imports.

Both engines are init-on-first-use so the GUI launch isn't blocked.
"""
from __future__ import annotations

import logging
import os
import re
import sys as _sys
import threading

# Disable paddle's OneDNN backend BEFORE any paddle/paddleocr import.
os.environ.setdefault("FLAGS_use_mkldnn", "false")

# Stub modelscope so paddleocr 3.x's logger init doesn't transitively
# import torch (whose Windows DLL load can race with paddle's CUDA libs).
for _name in ("modelscope", "modelscope.utils", "modelscope.utils.logger"):
    _sys.modules.setdefault(_name, type(_sys)(_name))

import cv2
import numpy as np


_logger = logging.getLogger("dfogang.general_ocr")

# Single shared reader, lazily resolved. Order of preference:
#   1. PaddleOCR (better small-text accuracy)
#   2. EasyOCR (broader install footprint already in the env)
# Once an engine succeeds, we lock it in for the lifetime of the process.
_reader = None
_engine: str = ""               # "paddle" or "easyocr"
_reader_lock = threading.Lock()
_reader_failed = False

# Per-process result cache. EasyOCR inference is ~150ms per call (CPU);
# the capture loop can revisit the same row on every frame, so we hash the
# binarized crop bytes and reuse the answer whenever the row hasn't changed.
_CACHE_CAP = 256
_fame_cache: dict[bytes, tuple[int | None, str, float]] = {}
_class_cache: dict[bytes, tuple[str, float]] = {}
_cache_lock = threading.Lock()


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
    try:
        # paddleocr 3.x default model series is PP-OCRv5. The constructor
        # signature changed: pre-3.x used `use_angle_cls` and `show_log`;
        # 3.x splits into the three pipeline-stage flags below and silently
        # ignores unknown kwargs.
        reader = PaddleOCR(
            lang="en",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            enable_mkldnn=False,
        )
        _logger.info("PaddleOCR reader initialized (PP-OCRv5)")
        return reader, "paddle"
    except Exception as exc:
        _logger.warning("PaddleOCR init failed: %s", exc)
        return None, ""


def _try_easyocr():
    try:
        import easyocr
    except Exception as exc:
        _logger.warning("EasyOCR import failed: %s", exc)
        return None, ""
    try:
        reader = easyocr.Reader(['en'], gpu=False, verbose=False)
        _logger.info("EasyOCR reader initialized")
        return reader, "easyocr"
    except Exception as exc:
        _logger.warning("EasyOCR init failed: %s", exc)
        return None, ""


def _get_reader():
    """Lazy init. Tries PaddleOCR first, falls back to EasyOCR. Caches the
    failure so we don't retry the heavy import on every frame."""
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
    """Kick off OCR model load in a daemon thread so the first capture frame
    doesn't block on cold init. Safe to call repeatedly."""
    if _reader is not None or _reader_failed:
        return
    threading.Thread(target=_get_reader, daemon=True,
                     name="ocr-prewarm").start()


def _readtext(reader, image: np.ndarray, allowlist: str) -> list:
    """Engine-neutral OCR call. Returns list of (text, confidence) sorted by
    bounding-box left-x. Allowlist hints aren't supported by PaddleOCR's
    pipeline so we filter post-hoc."""
    if _engine == "paddle":
        rgb = image if image.ndim == 3 else cv2.cvtColor(image, cv2.COLOR_GRAY2RGB)
        try:
            result = reader.predict(rgb)
        except Exception as exc:
            _logger.debug("paddle ocr call failed: %s", exc)
            return []
        if not result:
            return []
        # paddleocr 3.x returns one OcrResult per page (we send single-page).
        # Each OcrResult dict-like provides rec_texts, rec_scores, dt_polys
        # (4-point detection polygons).
        boxes = []
        for r in result:
            texts = r.get("rec_texts", []) or []
            scores = r.get("rec_scores", []) or []
            polys = r.get("dt_polys", []) or []
            for text, score, poly in zip(texts, scores, polys):
                x_left = float(min(p[0] for p in poly)) if len(poly) else 0.0
                text = "".join(c for c in text if c in allowlist)
                if text:
                    boxes.append((x_left, text, float(score)))
        boxes.sort(key=lambda b: b[0])
        return [(t, c) for _, t, c in boxes]
    # EasyOCR fallback
    try:
        result = reader.readtext(image, allowlist=allowlist, detail=1)
    except Exception as exc:
        _logger.debug("easyocr call failed: %s", exc)
        return []
    if not result:
        return []
    result_sorted = sorted(result, key=lambda r: r[0][0][0])
    return [(t, float(c)) for _, t, c in result_sorted]


def _prep(crop_rgb: np.ndarray) -> np.ndarray:
    """Pass-through. PP-OCRv5 reads raw colour pixels accurately and is
    actively *hurt* by Otsu binarization + Lanczos upscale (those destroy
    the anti-aliasing that distinguishes 6 from 5 in the small fame font).

    The crop is returned as-is so callers don't need to know whether
    pre-processing is happening or not.
    """
    return crop_rgb if crop_rgb.ndim == 3 else cv2.cvtColor(
        crop_rgb, cv2.COLOR_GRAY2RGB)


def _has_text(crop_rgb: np.ndarray, min_pixels: int = 12) -> bool:
    """Cheap brightness pre-check before invoking PaddleOCR. An empty row's
    crop has only a few stray bright pixels; running the heavy detection +
    recognition pass on every empty row turns a 'window detected, no rows
    yet' frame into a 5-second wait. This filters those out in <1ms.
    Per-frame caching can't help: the game scene behind the (semi-
    transparent) party_apply window animates, so empty crops have unique
    pixel bytes every frame.
    """
    if crop_rgb.size == 0:
        return False
    gray = crop_rgb.max(axis=2) if crop_rgb.ndim == 3 else crop_rgb
    return int((gray > 100).sum()) >= min_pixels


def read_fame(crop_rgb: np.ndarray) -> tuple[int | None, str, float]:
    """Read a fame number. Returns (value, raw_text, mean_confidence).

    The raw text strips commas and other non-digit characters before parsing.
    A value is only returned if it's in the plausible range 10_000..100_000.
    """
    # Cheap pre-check first so empty crops don't even trigger the
    # ~3s PaddleOCR reader initialization on the first frame.
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


_FAME_MIN, _FAME_MAX = 10_000, 100_000


def _parse_with_trim(digits: str) -> int | None:
    candidates = {digits}
    if len(digits) > 1:
        candidates.add(digits[1:])      # drop leading icon-edge digit
        candidates.add(digits[:-1])     # drop trailing artefact
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
            # Prefer the longest candidate that fits — that's most likely
            # to be the genuine reading, not a coincidence.
            if best is None or len(cand) > len(str(best)):
                best = v
    return best


def read_class(crop_rgb: np.ndarray) -> tuple[str, float]:
    """Read a class / awakening label, e.g. 'Neo: Paramedic'. Returns
    (raw_text, mean_confidence). Caller maps to a canonical class via the
    jobs API."""
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
    allowlist = ("abcdefghijklmnopqrstuvwxyz"
                 "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                 ":' ")
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
