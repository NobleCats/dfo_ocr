"""Party Apply window detection + per-row recognition.

This module detects the party-apply / raid-request list by matching small header
anchors, then OCRs only the small row crops. Do not use full-screen OCR here:
cold detection must stay fast enough to recover when the window opens, closes, or
moves.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from neople import name_similarity  # kept for compatibility with older callers/imports
from resources import resource_path

try:
    from general_ocr import (
        read_fame as _ocr_fame,
        read_class as _ocr_class,
        read_text_boxes as _ocr_text_boxes,
    )
except ImportError:
    _ocr_fame = None
    _ocr_class = None
    _ocr_text_boxes = None


def _otsu_mask(crop_rgb: np.ndarray) -> np.ndarray:
    """Global Otsu binarization for small high-contrast text crops."""
    gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return mask


def _detect_top_text_y(
    image_rgb: np.ndarray,
    row_top: int,
    row_bot: int,
    scale: float,
) -> int | None:
    """Return absolute y of the topmost bright text row inside a data row."""
    H, W = image_rgb.shape[:2]
    row_top = max(0, row_top)
    row_bot = min(H - 1, row_bot)
    if row_bot - row_top < 8:
        return None

    x0 = max(0, int(round(180 * scale)))
    x1 = W
    strip = image_rgb[row_top:row_bot + 1, x0:x1].max(axis=2)
    bg_level = float(np.percentile(strip, 10))
    bright = strip > bg_level + 35
    counts = bright.sum(axis=1)
    if counts.size == 0 or counts.max() < 6:
        return None

    threshold = max(3, int(counts.max()) // 5)
    for y, c in enumerate(counts):
        if c >= threshold:
            return row_top + y
    return None


_logger = logging.getLogger("dfogang.party_apply")

_DEBUG_CROP_LIMIT = int(os.environ.get("DFO_DEBUG_CROP_LIMIT", "0"))
_DEBUG_CROP_COUNT = 0
_DEBUG_SESSION_LOGGED = False

# Cold party-apply detection must be cheap. Native marker probes cover the
# available marker captures. All column_header_*pct.png files are loaded
# dynamically so marker updates do not require code changes.
PA_COLD_CANDIDATES_PER_FRAME = 6
# Width-ladder fallback: instead of knowing the exact DFO UI Scale %, use
# the real 0% and 100% header marker widths as min/max and resize the
# 100% marker across that interval. The order is midpoint-first so unknown
# intermediate scales are reached quickly without a blocking full sweep.
PA_WIDTH_LADDER_STEP_PX = 6
PA_WIDTH_LADDER_CANDIDATES_PER_FRAME = 10
PA_ADAPTIVE_HOT_SCALE_MAX = 8
PA_ADAPTIVE_HOT_SCALE_EPS = 0.015

# Geometry measured from the UI Scale 100% reference capture.
REF_MARKER_SIZE = (1050, 26)  # marker (column header strip) WxH
REF_MARKER_LEFT_IN_WINDOW = 16

REF_FIRST_ROW_TOP_DY = 36
REF_ROW_PITCH = 74
REF_ROW_TOP_LINE_DY = 0
REF_ROW_TOP_LINE_H = 25
REF_ROW_BOT_LINE_DY = 28
REF_ROW_BOT_LINE_H = 26

REF_ADVENTURE_X = (40, 245)
REF_FAME_X = (265, 382)
REF_NAME_X = (390, 620)
REF_CLASS_X = (390, 620)
REF_STATUS_X = (655, 870)

REF_FIXED_PREFIX_NAME = "Lv. 115 "
REF_FIXED_PREFIX_CLASS_REGEX = re.compile(r"^[A-Za-z]?Neo[: ]\s*")

# These pads keep the digit crop generous across UI scales.
FAME_STAR_ICON_RIGHT_PAD = 22
CLASS_BADGE_RIGHT_PAD = 30
FAME_DIGIT_LEFT_BREATHING = 6
FAME_DIGIT_RIGHT_BREATHING = 42

ROW_MIN_TEXT_PIXELS = 12

# Fast row-gate constants. These prevent non-table/game-scene rows from entering
# PaddleOCR when the detector has row positions that extend beyond the request list.
ROW_GATE_MIN_BRIGHT_PIXELS = 20
ROW_GATE_MIN_TRANSITIONS = 400
# Additional cheap column gates. A real applicant row should have visible fame
# digits and name/class text in their expected columns. These reject background
# UI/chat text that happens to have many transitions in the combined row band.
ROW_GATE_MIN_FAME_BRIGHT_PIXELS = 10
ROW_GATE_MIN_NAME_BRIGHT_PIXELS = 18
ROW_GATE_MIN_NAME_TRANSITIONS = 45
# Applicant rows fill from the top of the request table. If the top rows are
# empty, do not keep scanning down into unrelated game UI/background.
ROW_GATE_MAX_INITIAL_EMPTY_ROWS = 4
MAX_OCR_PER_FRAME = 12
ROW_GATE_MIN_ACTION_PIXELS = 14
ROW_OCR_CACHE_CAP = 128

_DEFAULT_MARKER_PATH = resource_path("markers", "party_apply", "column_header_69pct.png")
_OPTIONAL_MARKER_PATHS = (
    resource_path("markers", "party_apply", "column_header_0pct.png"),
    resource_path("markers", "party_apply", "column_header_50pct.png"),
    resource_path("markers", "party_apply", "column_header_80pct.png"),
    resource_path("markers", "party_apply", "column_header_100pct.png"),
)


def _debug_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "DFOGANG_RaidHelper" / "debug_crops"


def _debug_crop_stats(crop_rgb: np.ndarray) -> str:
    if crop_rgb.size == 0:
        return "empty"
    gray = crop_rgb.max(axis=2) if crop_rgb.ndim == 3 else crop_rgb
    return (
        f"shape={tuple(crop_rgb.shape)} min={int(gray.min())} "
        f"p25={float(np.percentile(gray, 25)):.1f} "
        f"p50={float(np.percentile(gray, 50)):.1f} "
        f"p90={float(np.percentile(gray, 90)):.1f} max={int(gray.max())}"
    )


def _save_debug_crop(label: str, crop_rgb: np.ndarray) -> None:
    global _DEBUG_CROP_COUNT
    if _DEBUG_CROP_LIMIT <= 0 or _DEBUG_CROP_COUNT >= _DEBUG_CROP_LIMIT:
        return
    if crop_rgb.size == 0:
        return
    try:
        d = _debug_dir()
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_")[:80]
        name = f"{_DEBUG_CROP_COUNT:03d}_{time.strftime('%H%M%S')}_{safe}.png"
        Image.fromarray(crop_rgb).save(d / name)
        _DEBUG_CROP_COUNT += 1
    except Exception as exc:
        _logger.debug("debug crop save failed label=%r: %s", label, exc)


@dataclass
class PartyApplyDetection:
    found: bool
    score: float
    scale: float  # 1.0 = 69% reference capture
    marker_xywh: tuple[int, int, int, int]
    rows_top_y: list[int]
    is_manual: bool = False  # True when built from user-calibrated AREA guide


@dataclass
class PartyApplyRow:
    index: int
    y_abs: tuple[int, int]
    fame: int | None
    fame_text: str
    fame_score: float
    name: str
    name_raw: str
    name_score: float
    class_name: str
    class_raw: str
    class_score: float
    adventure: str
    adventure_raw: str
    adventure_score: float
    # Partial fame prefix range: set when OCR captured only leading digits of a
    # 5-6 digit fame number (e.g. '7850' → range_min=78500, range_max=78509).
    # When set, Neople search uses [range_min..range_max] instead of fame ± 100.
    fame_range_min: int | None = None
    fame_range_max: int | None = None

    @property
    def is_empty(self) -> bool:
        """True when this row does not look like an applicant row."""
        if self.fame is not None and self.fame_score >= 0.7:
            return False

        # The 'Lv' marker is reliable even when the digits after it are garbled.
        head = self.name_raw[:14].lower()
        if any(s in head for s in ("lv", "iv", "lu", "iu")):
            return False

        return True



def _partial_fame_prefix(raw_digits: str) -> tuple[int, int] | None:
    """Return (range_min, range_max) when raw_digits is a plausible partial
    fame prefix (e.g. '7850' → (78500, 78509) for missing_digits=1).

    Endgame fame is typically 5 digits (10000..99999). If OCR captured 4
    digits that don't form a valid fame value, they may be the leading 4 of
    5. 3-digit prefixes (100-wide ranges) are also supported. missing_digits
    >= 3 would make the range too broad and is rejected.
    """
    if not raw_digits or not raw_digits.isdigit():
        return None
    n = len(raw_digits)
    for expected_len in (5, 6):
        missing = expected_len - n
        if missing <= 0 or missing >= 3:
            continue
        prefix_val = int(raw_digits)
        mul = 10 ** missing
        range_min = prefix_val * mul
        range_max = range_min + mul - 1
        if 10_000 <= range_min and range_max <= 999_999:
            return range_min, range_max
    return None


@dataclass
class _PendingOCRRow:
    index: int
    y_abs: tuple[int, int]
    row_sig: bytes
    fame_x: tuple[int, int]
    fame_y: tuple[int, int]
    name_x: tuple[int, int]
    name_y: tuple[int, int]
    class_x: tuple[int, int]
    class_y: tuple[int, int]
    adv_x: tuple[int, int]
    adv_y: tuple[int, int]
    scale: float


@dataclass
class _CompositeField:
    row_pos: int
    field: str
    rect: tuple[int, int, int, int]


@dataclass
class _CompositeOCRResult:
    fame_text: str = ""
    fame_score: float = 0.0
    name_raw: str = ""
    name_score: float = 0.0
    class_raw: str = ""
    class_score: float = 0.0


_ROW_OCR_CACHE: dict[bytes, PartyApplyRow] = {}


def _cache_party_apply_row(sig: bytes, row: PartyApplyRow) -> None:
    return


def _row_mask_signature(image_rgb: np.ndarray, rects: list[tuple[int, int, int, int]]) -> bytes:
    """Stable-ish row signature based on text/button masks, not raw pixels.

    The request window is translucent, so raw crop bytes change as the animated
    game scene moves behind it. Thresholded/resized masks are stable enough to
    cache repeated OCR of the same applicant row while still changing when the
    row contents change.
    """
    H, W = image_rgb.shape[:2]
    parts: list[bytes] = []
    for x0, y0, x1, y1 in rects:
        x0, x1 = max(0, x0), min(W, x1)
        y0, y1 = max(0, y0), min(H, y1)
        if x1 - x0 < 4 or y1 - y0 < 4:
            parts.append(b"")
            continue
        crop = image_rgb[y0:y1, x0:x1]
        gray = crop.max(axis=2)
        bg = float(np.percentile(gray, 25))
        mask = (gray > bg + 55).astype(np.uint8) * 255
        small = cv2.resize(mask, (64, 16), interpolation=cv2.INTER_AREA)
        parts.append(small.tobytes())
    return b"|".join(parts)


def _has_pending_action_button(image_rgb: np.ndarray, x_range: tuple[int, int], y_range: tuple[int, int], scale: float) -> bool:
    """Cheap check for the Accept/Decline button area.

    Empty slots such as "Click a slot to remove" have text in the row and can
    pass the generic glyph-transition gate, but they do not have the blue/brown
    action buttons that pending applicants have.
    """
    H, W = image_rgb.shape[:2]
    x0, x1 = max(0, x_range[0]), min(W, x_range[1])
    y0, y1 = max(0, y_range[0]), min(H, y_range[1])
    if x1 - x0 < 8 or y1 - y0 < 8:
        return False
    crop = image_rgb[y0:y1, x0:x1]
    r = crop[:, :, 0].astype(np.int16)
    g = crop[:, :, 1].astype(np.int16)
    b = crop[:, :, 2].astype(np.int16)

    # Blue Accept button and orange/brown Decline button in RGB space.
    blue = (b > 80) & (g > 35) & (r < 90) & ((b - r) > 35)
    orange = (r > 90) & (g > 45) & (b < 90) & ((r - b) > 35)
    min_pixels = max(6, int(round(ROW_GATE_MIN_ACTION_PIXELS * max(0.45, scale) * max(0.45, scale))))
    return int(blue.sum()) >= min_pixels or int(orange.sum()) >= min_pixels

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _load_marker(path: Path | None = None) -> np.ndarray:
    p = path or _DEFAULT_MARKER_PATH
    return np.array(Image.open(p).convert("RGB"))


def _marker_base_scale(marker_gray: np.ndarray) -> float:
    """Marker capture scale relative to the 69% reference geometry."""
    try:
        return max(0.35, float(marker_gray.shape[1]) / float(REF_MARKER_SIZE[0]))
    except Exception:
        return 1.0


def _marker_sort_key(path: Path) -> tuple[int, str]:
    m = re.search(r"column_header_(\d+)pct\.png$", path.name)
    if m:
        return (int(m.group(1)), path.name)
    return (10_000, path.name)


def _available_marker_paths() -> list[Path]:
    """Return all checked-in party-apply header marker captures.

    The marker set is data-driven: any resources/markers/party_apply/
    column_header_*pct.png file is loaded automatically. This lets us update or
    add real crop images without changing this module every time.
    """
    marker_dir = _DEFAULT_MARKER_PATH.parent
    seen: set[Path] = set()
    paths: list[Path] = []

    if _DEFAULT_MARKER_PATH.exists():
        paths.append(_DEFAULT_MARKER_PATH)
        seen.add(_DEFAULT_MARKER_PATH)

    for p in sorted(marker_dir.glob("column_header_*pct.png"), key=_marker_sort_key):
        if p not in seen:
            paths.append(p)
            seen.add(p)

    # Backward compatibility for older installs where glob may not include
    # optional markers for any reason.
    for p in _OPTIONAL_MARKER_PATHS:
        if p.exists() and p not in seen:
            paths.append(p)
            seen.add(p)

    return paths


def _load_markers() -> list[np.ndarray]:
    """Load every available party-apply header marker."""
    paths = _available_marker_paths()
    if not paths:
        raise FileNotFoundError(
            f"No party-apply markers found under {_DEFAULT_MARKER_PATH.parent}"
        )
    return [_load_marker(p) for p in paths]


def _grid_support_score(
    img_gray: np.ndarray,
    marker_xy: tuple[int, int],
    marker_w: int,
    marker_h: int,
    scale: float,
    max_rows: int,
) -> float:
    """Cheap structural validation for a candidate request-list header."""
    H, W = img_gray.shape[:2]
    mx, my = marker_xy
    pitch = max(8, int(round(REF_ROW_PITCH * scale)))

    y0 = max(0, my + marker_h)
    y1 = min(H, y0 + pitch * min(max_rows, 6))
    x0 = max(0, mx)
    x1 = min(W, mx + marker_w)

    if y1 - y0 < pitch or x1 - x0 < 80:
        return 0.0

    roi = img_gray[y0:y1, x0:x1]
    dy = np.abs(np.diff(roi.astype(np.int16), axis=0)).mean(axis=1)
    if dy.size == 0:
        return 0.0

    bg = float(np.median(dy)) + 1e-6
    supports: list[float] = []
    for i in range(min(max_rows, 5)):
        expected = int(round((REF_FIRST_ROW_TOP_DY + i * REF_ROW_PITCH) * scale)) - marker_h
        lo = max(0, expected - max(3, pitch // 5))
        hi = min(len(dy), expected + max(4, pitch // 5))
        if hi <= lo:
            continue
        supports.append(float(dy[lo:hi].max() / bg))

    if not supports:
        return 0.0
    return float(np.mean(sorted(supports, reverse=True)[:3]))


def _candidate_found(
    marker_score: float,
    grid_score: float,
    score_threshold: float,
    effective_scale: float,
) -> bool:
    """Decide whether a marker candidate is usable.

    Intermediate DFO UI Scale values can render differently from a simple resize
    of the 0/50/69/100 marker captures, so a lower marker score may still be
    valid. However, v8l showed that low-score promotion at tiny effective scales
    (notably ~0.36) creates stable false positives while the request list is
    closed. Only promote low-score candidates in the effective-scale band where
    real request-list windows have been observed, and require stronger grid
    support than v8l.
    """
    if marker_score >= score_threshold:
        return True

    # Do not grid-promote implausibly small/large candidates. These were the
    # source of closed-window false positives such as score≈0.39, scale≈0.36.
    if effective_scale < 0.55 or effective_scale > 1.45:
        return False

    if marker_score >= 0.44 and grid_score >= 1.80:
        return True
    if marker_score >= 0.38 and grid_score >= 3.00:
        return True
    return False


def detect_party_apply(
    image_rgb: np.ndarray,
    *,
    min_scale: float = 0.45,
    max_scale: float = 2.0,
    scale_step: float = 0.02,
    coarse_step: float = 0.1,
    score_threshold: float = 0.54,
    marker: np.ndarray | None = None,
    hint: PartyApplyDetection | None = None,
    hint_search_radius: int = 200,
    near_scale: float | None = None,
    near_scale_radius: float = 0.1,
    max_rows: int = 12,
) -> PartyApplyDetection:
    """Locate the party-apply window in ``image_rgb``.

    v8r keeps v8m/v8n's false-positive controls and adaptive hot-scale caching.
    For arbitrary 1%-step UI Scale values, it does not try to map percent to
    pixels. It uses real 0% and 100% marker widths as bounds, then resizes
    the 100% marker across that width interval in a rotating midpoint-first
    ladder.
    """
    img_gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    marker_grays = (
        [cv2.cvtColor(marker, cv2.COLOR_RGB2GRAY)]
        if marker is not None
        else [cv2.cvtColor(m, cv2.COLOR_RGB2GRAY) for m in _load_markers()]
    )
    H, W = img_gray.shape

    def _remember_success_scale(scale: float) -> None:
        scale = float(scale)
        if not (0.25 <= scale <= 2.20):
            return
        hot = list(getattr(detect_party_apply, "_adaptive_hot_scales", []))
        if any(abs(scale - s) <= PA_ADAPTIVE_HOT_SCALE_EPS for s in hot):
            return
        hot.insert(0, scale)
        del hot[PA_ADAPTIVE_HOT_SCALE_MAX:]
        setattr(detect_party_apply, "_adaptive_hot_scales", hot)

    def _return(cand: PartyApplyDetection) -> PartyApplyDetection:
        if cand.found:
            _remember_success_scale(cand.scale)
        return cand

    if hint is not None and hint.found:
        best_hint: PartyApplyDetection | None = None
        for marker_gray in marker_grays:
            cand = _hint_lookup(
                img_gray,
                marker_gray,
                hint,
                hint_search_radius,
                score_threshold,
                max_rows,
                H,
            )
            if cand is not None and (best_hint is None or cand.score > best_hint.score):
                best_hint = cand
        if best_hint is not None:
            return _return(best_hint)

    def _scan_pair(marker_gray: np.ndarray, resize_scale: float) -> PartyApplyDetection:
        return _scan_scales([float(resize_scale)], img_gray, marker_gray, score_threshold, max_rows, H, W)

    def _best_marker_for_effective_scale(effective_scale: float) -> np.ndarray:
        return min(marker_grays, key=lambda m: abs(_marker_base_scale(m) - effective_scale))

    def _resize_for_effective_scale(marker_gray: np.ndarray, effective_scale: float) -> float:
        base = _marker_base_scale(marker_gray)
        if base <= 0:
            return 1.0
        return float(effective_scale) / base

    def _better(a: PartyApplyDetection, b: PartyApplyDetection) -> PartyApplyDetection:
        if b.found and not a.found:
            return b
        if a.found and not b.found:
            return a
        return b if b.score > a.score else a

    def _make_priority_effective_scales() -> tuple[float, ...]:
        """Effective-scale ladder derived only from actual 0% and 100% widths.

        We do not need the exact UI Scale % -> pixel formula. Any intermediate
        UI Scale must have a header width between the real 0% and 100% marker
        widths, so scan that width interval by resizing the 100% marker.
        """
        p0 = _DEFAULT_MARKER_PATH.parent / "column_header_0pct.png"
        p100 = _DEFAULT_MARKER_PATH.parent / "column_header_100pct.png"

        widths: list[int] = []
        if p0.exists() and p100.exists():
            try:
                w0 = int(Image.open(p0).size[0])
                w100 = int(Image.open(p100).size[0])
                lo, hi = sorted((w0, w100))
                step = max(2, int(PA_WIDTH_LADDER_STEP_PX))
                widths = list(range(lo, hi + 1, step))
                if widths[-1] != hi:
                    widths.append(hi)
            except Exception:
                widths = []

        if not widths:
            # Fallback to the older broad grid when marker assets are missing.
            widths = [int(round(REF_MARKER_SIZE[0] * x * 0.02)) for x in range(15, 101)]

        def midpoint_order(vals: list[int]) -> list[int]:
            out: list[int] = []
            def rec(lo_i: int, hi_i: int) -> None:
                if lo_i > hi_i:
                    return
                mid = (lo_i + hi_i) // 2
                out.append(vals[mid])
                rec(lo_i, mid - 1)
                rec(mid + 1, hi_i)
            rec(0, len(vals) - 1)
            return out

        ordered_widths = midpoint_order(widths)
        anchors = [
            # Exact/common captures first. Native marker scan handles them too,
            # but keeping them here helps after stale near_scale changes.
            0.66, 1.00, 1.30, 0.88, 0.46, 0.57, 0.75, 1.10,
        ]

        seen: set[float] = set()
        out: list[float] = []
        for v in anchors + [w / float(REF_MARKER_SIZE[0]) for w in ordered_widths]:
            v = round(float(v), 4)
            if min_scale * 0.65 <= v <= max_scale * 1.25 and v not in seen:
                seen.add(v)
                out.append(v)
        return tuple(out)

    def _scan_effective(target: float) -> PartyApplyDetection:
        marker_gray = _best_marker_for_effective_scale(float(target))
        resize_scale = _resize_for_effective_scale(marker_gray, float(target))
        return _scan_pair(marker_gray, resize_scale)

    def _scan_effective_from_100(target: float) -> PartyApplyDetection:
        """Scan by resizing the actual 100% marker to the requested width scale."""
        p100 = _DEFAULT_MARKER_PATH.parent / "column_header_100pct.png"
        if p100.exists():
            marker100 = cv2.cvtColor(_load_marker(p100), cv2.COLOR_RGB2GRAY)
        else:
            marker100 = _best_marker_for_effective_scale(float(target))
        resize_scale = _resize_for_effective_scale(marker100, float(target))
        return _scan_pair(marker100, resize_scale)

    # Always probe every available marker at its native size before using a
    # remembered scale. This keeps explicit 0/50/100 marker hits instant and
    # prevents stale near_scale from blocking a UI Scale change during testing.
    native_best = PartyApplyDetection(False, 0.0, 1.0, (0, 0, 0, 0), [])
    for marker_gray in marker_grays:
        cand = _scan_pair(marker_gray, 1.0)
        native_best = _better(native_best, cand)
        if cand.found and cand.score >= 0.90:
            return _return(cand)

    best = native_best

    # Adaptive hot scales: if an intermediate UI scale succeeded once in this
    # process, probe it before stale near_scale and before the rotating fallback.
    # This is the normal-use optimization: UI Scale is set once and rarely
    # changes, so reopen/move recovery becomes similar to native marker hits.
    hot_scales = list(getattr(detect_party_apply, "_adaptive_hot_scales", []))
    for target in hot_scales[:PA_COLD_CANDIDATES_PER_FRAME]:
        cand = _scan_effective(float(target))
        best = _better(best, cand)
        if cand.found:
            return _return(cand)

    # If app.py has a remembered scale, probe it next, but do not stop there on
    # failure. UI Scale can still be changed in edge-case tests.
    if near_scale is not None:
        offsets = (0.0, -0.03, 0.03, -0.06, 0.06, -near_scale_radius, near_scale_radius)
        idx = int(getattr(detect_party_apply, "_near_scale_probe_idx", 0)) % len(offsets)
        setattr(detect_party_apply, "_near_scale_probe_idx", (idx + 1) % len(offsets))

        targets = [float(near_scale), max(min_scale, min(max_scale, float(near_scale) + offsets[idx]))]
        for target in targets:
            cand = _scan_effective(target)
            best = _better(best, cand)
            if cand.found and cand.score >= 0.90:
                return _return(cand)

    # Cold/changed-scale path: probe a rotating slice of a continuous
    # effective-scale grid. This keeps per-frame cost bounded while covering
    # UI Scale 0..100 in 1% increments over several frames.
    priority_effective_scales = _make_priority_effective_scales()
    idx = int(getattr(detect_party_apply, "_cold_probe_idx", 0)) % len(priority_effective_scales)
    setattr(
        detect_party_apply,
        "_cold_probe_idx",
        (idx + max(PA_COLD_CANDIDATES_PER_FRAME, PA_WIDTH_LADDER_CANDIDATES_PER_FRAME)) % len(priority_effective_scales),
    )

    cold_count = max(PA_COLD_CANDIDATES_PER_FRAME, PA_WIDTH_LADDER_CANDIDATES_PER_FRAME)
    for j in range(cold_count):
        target = float(priority_effective_scales[(idx + j) % len(priority_effective_scales)])
        # Width-ladder fallback intentionally uses the 100% marker resized down
        # through the 0%..100% width range. This avoids depending on a possibly
        # wrong UI Scale percent formula.
        cand = _scan_effective_from_100(target)
        best = _better(best, cand)
        if cand.found and cand.score >= 0.90:
            return _return(cand)

    return _return(best)

def _scan_scales(
    scales,
    img_gray: np.ndarray,
    marker_gray: np.ndarray,
    score_threshold: float,
    max_rows: int,
    H: int,
    W: int,
) -> PartyApplyDetection:
    best = PartyApplyDetection(False, 0.0, 1.0, (0, 0, 0, 0), [])
    base_scale = _marker_base_scale(marker_gray)

    for scale in scales:
        new_w = int(round(marker_gray.shape[1] * scale))
        new_h = int(round(marker_gray.shape[0] * scale))
        if new_w < 16 or new_h < 4:
            continue
        if new_w > W or new_h > H:
            continue

        resized = cv2.resize(
            marker_gray,
            (new_w, new_h),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
        )
        result = cv2.matchTemplate(img_gray, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        effective_scale = float(scale) * base_scale
        grid_score = _grid_support_score(img_gray, max_loc, new_w, new_h, effective_scale, max_rows)
        cand = _build_detection(
            effective_scale,
            float(max_val),
            max_loc,
            score_threshold,
            new_w,
            new_h,
            max_rows,
            H,
            grid_score,
        )
        if (
            (cand.found and not best.found)
            or (cand.found == best.found and cand.score > best.score)
        ):
            best = cand

    return best


def _hint_lookup(
    img_gray: np.ndarray,
    marker_gray: np.ndarray,
    hint: PartyApplyDetection,
    radius: int,
    threshold: float,
    max_rows: int,
    H: int,
):
    effective_scale = hint.scale
    base_scale = _marker_base_scale(marker_gray)
    scale = effective_scale / base_scale

    new_w = int(round(marker_gray.shape[1] * scale))
    new_h = int(round(marker_gray.shape[0] * scale))
    if new_w < 16 or new_h < 4:
        return None

    mx_prev, my_prev, _, _ = hint.marker_xywh
    x0 = max(0, mx_prev - radius)
    y0 = max(0, my_prev - radius)
    x1 = min(img_gray.shape[1], mx_prev + new_w + radius)
    y1 = min(img_gray.shape[0], my_prev + new_h + radius)

    if x1 - x0 < new_w or y1 - y0 < new_h:
        return None

    resized = cv2.resize(
        marker_gray,
        (new_w, new_h),
        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC,
    )
    roi = img_gray[y0:y1, x0:x1]
    result = cv2.matchTemplate(roi, resized, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)

    abs_loc = (max_loc[0] + x0, max_loc[1] + y0)
    grid_score = _grid_support_score(img_gray, abs_loc, new_w, new_h, effective_scale, max_rows)
    if not _candidate_found(float(max_val), grid_score, threshold, effective_scale):
        return None

    return _build_detection(
        effective_scale,
        float(max_val),
        abs_loc,
        threshold,
        new_w,
        new_h,
        max_rows,
        H,
        grid_score,
    )


def _build_detection(
    scale: float,
    score: float,
    marker_xy: tuple[int, int],
    threshold: float,
    marker_w: int,
    marker_h: int,
    max_rows: int,
    image_h: int,
    grid_score: float = 0.0,
) -> PartyApplyDetection:
    mx, my = marker_xy
    rows_top_y: list[int] = []
    for i in range(max_rows):
        y = my + int(round((REF_FIRST_ROW_TOP_DY + i * REF_ROW_PITCH) * scale))
        if y + int(round(REF_ROW_PITCH * scale)) > image_h:
            break
        rows_top_y.append(y)

    return PartyApplyDetection(
        found=_candidate_found(float(score), float(grid_score), threshold, float(scale)),
        score=float(score),
        scale=float(scale),
        marker_xywh=(mx, my, marker_w, marker_h),
        rows_top_y=rows_top_y,
    )


def build_manual_party_apply_detection(
    marker_xy: tuple[int, int],
    scale: float,
    image_shape: tuple[int, int] | tuple[int, int, int],
    max_rows: int = MAX_OCR_PER_FRAME,
) -> PartyApplyDetection:
    """Build a detection from a user-calibrated column-header position.

    The manual guide stores the column-header marker's top-left and scale.
    Recognition can then reuse the same row/crop geometry without running the
    template scanner every frame.
    """
    image_h = int(image_shape[0])
    scale = max(0.2, float(scale))
    marker_w = int(round(REF_MARKER_SIZE[0] * scale))
    marker_h = int(round(REF_MARKER_SIZE[1] * scale))
    det = _build_detection(
        scale=scale,
        score=1.0,
        marker_xy=(int(round(marker_xy[0])), int(round(marker_xy[1]))),
        threshold=0.0,
        marker_w=marker_w,
        marker_h=marker_h,
        max_rows=max_rows,
        image_h=image_h,
        grid_score=1.0,
    )
    det.is_manual = True
    return det


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------

def recognize_party_apply(
    image_rgb: np.ndarray,
    det: PartyApplyDetection,
) -> list[PartyApplyRow]:
    """Read fame, name, class, and adventure for each detected row."""
    global _DEBUG_SESSION_LOGGED
    if not det.found:
        return []

    mx, my, _, _ = det.marker_xywh
    s = det.scale
    H, W = image_rgb.shape[:2]

    if not _DEBUG_SESSION_LOGGED:
        _logger.info(
            "party_apply diagnostics frozen=%s image=%sx%s marker=%s scale=%.4f rows=%d crop_dir=%s crop_limit=%d",
            bool(getattr(sys, "frozen", False)), W, H, det.marker_xywh, s,
            len(det.rows_top_y), _debug_dir(), _DEBUG_CROP_LIMIT,
        )
        _DEBUG_SESSION_LOGGED = True

    out_by_index: dict[int, PartyApplyRow] = {}
    pending_rows: list[_PendingOCRRow] = []
    empties_since_real = 0
    pitch = int(round(REF_ROW_PITCH * s))
    name_line_h = max(10, int(round(REF_ROW_TOP_LINE_H * s)))

    def _col(ref_x: int) -> int:
        return mx + int(round((ref_x - REF_MARKER_LEFT_IN_WINDOW) * s))

    fame_left_for_check = _col(REF_FAME_X[0])
    name_right_for_check = _col(REF_NAME_X[1])
    status_x = (_col(REF_STATUS_X[0]), _col(REF_STATUS_X[1]))

    ocr_rows_used = 0

    for i, row_top in enumerate(det.rows_top_y):
        row_bot = row_top + pitch - 1
        if row_top >= H:
            break
        row_bot = min(row_bot, H - 1)

        # Fast empty-row gate. Real applicant rows have many small glyph-edge
        # transitions. Scene/UI false positives tend to be large bright blobs.
        check_x0 = max(0, fame_left_for_check)
        check_x1 = min(W, name_right_for_check)
        check_y1 = min(H, row_top + pitch)

        # Manual mode: skip completely-dark rows (no text possible).
        # This prevents sending empty rows below the applicant list to OCR.
        if det.is_manual and check_x1 - check_x0 >= 10 and check_y1 - row_top >= 5:
            if int(image_rgb[row_top:check_y1, check_x0:check_x1].max()) < 15:
                empties_since_real += 1
                if empties_since_real >= 3:
                    break
                continue

        if not det.is_manual and check_x1 - check_x0 >= 10 and check_y1 - row_top >= 5:
            row_strip = image_rgb[row_top:check_y1, check_x0:check_x1]
            gray_max = row_strip.max(axis=2)
            bg = float(np.percentile(gray_max, 25))
            bright = gray_max > bg + 60
            bright_count = int(bright.sum())

            def _mark_empty() -> bool:
                nonlocal empties_since_real
                empties_since_real += 1
                if out_by_index and empties_since_real >= 3:
                    return True
                if not out_by_index and empties_since_real >= ROW_GATE_MAX_INITIAL_EMPTY_ROWS:
                    return True
                return False

            if bright_count < ROW_GATE_MIN_BRIGHT_PIXELS:
                if _mark_empty():
                    break
                continue

            transitions = int((bright[:, 1:] != bright[:, :-1]).sum())
            if transitions < ROW_GATE_MIN_TRANSITIONS:
                if _mark_empty():
                    break
                continue

            # Column-level sanity check. This is intentionally cheap and avoids
            # PaddleOCR calls on scene/chat/UI text that overlaps lower computed
            # row slots after the request window has been dragged.
            fame_rx0 = max(0, _col(REF_FAME_X[0]) + int(round((FAME_STAR_ICON_RIGHT_PAD - FAME_DIGIT_LEFT_BREATHING) * s)))
            fame_rx1 = min(W, _col(REF_FAME_X[1]) + int(round(FAME_DIGIT_RIGHT_BREATHING * s)))
            name_rx0 = max(0, _col(REF_NAME_X[0]))
            name_rx1 = min(W, _col(REF_NAME_X[1]))

            def _col_stats(x0: int, x1: int) -> tuple[int, int]:
                if x1 - x0 < 4:
                    return 0, 0
                sub = image_rgb[row_top:check_y1, x0:x1].max(axis=2)
                sub_bg = float(np.percentile(sub, 25))
                sub_bright = sub > sub_bg + 60
                sub_count = int(sub_bright.sum())
                sub_trans = int((sub_bright[:, 1:] != sub_bright[:, :-1]).sum()) if sub_bright.shape[1] > 1 else 0
                return sub_count, sub_trans

            fame_bright, fame_trans = _col_stats(fame_rx0, fame_rx1)
            name_bright, name_trans = _col_stats(name_rx0, name_rx1)
            if fame_bright < ROW_GATE_MIN_FAME_BRIGHT_PIXELS or name_bright < ROW_GATE_MIN_NAME_BRIGHT_PIXELS or name_trans < ROW_GATE_MIN_NAME_TRANSITIONS:
                _logger.debug(
                    "row %d gate REJECT columns bright=%d trans=%d fame_bright=%d fame_trans=%d name_bright=%d name_trans=%d",
                    i, bright_count, transitions, fame_bright, fame_trans, name_bright, name_trans,
                )
                if _mark_empty():
                    break
                continue

            if not det.is_manual and not _has_pending_action_button(image_rgb, status_x, (row_top, check_y1), s):
                _logger.debug(
                    "row %d gate REJECT no-action bright=%d transitions=%d fame_bright=%d fame_trans=%d name_bright=%d name_trans=%d",
                    i, bright_count, transitions, fame_bright, fame_trans, name_bright, name_trans,
                )
                if _mark_empty():
                    break
                continue

            _logger.debug(
                "row %d gate PASS bright=%d transitions=%d fame_bright=%d fame_trans=%d name_bright=%d name_trans=%d",
                i, bright_count, transitions, fame_bright, fame_trans, name_bright, name_trans,
            )

        if ocr_rows_used >= MAX_OCR_PER_FRAME:
            empties_since_real += 1
            if out_by_index and empties_since_real >= 3:
                break
            if not out_by_index and empties_since_real >= ROW_GATE_MAX_INITIAL_EMPTY_ROWS:
                break
            continue
        ocr_rows_used += 1

        top_y0 = row_top
        top_y1 = min(H, row_top + (pitch * 3) // 5)
        bot_y0 = max(0, row_top + (pitch * 2) // 5)
        bot_y1 = min(H, row_top + pitch)

        name_top = _detect_top_text_y(image_rgb, row_top, row_bot, s)
        if name_top is None:
            name_top = row_top
        name_y0 = max(0, name_top - 1)
        name_y1 = min(H, name_top + name_line_h + 2)

        def col(ref_x: int) -> int:
            return mx + int(round((ref_x - REF_MARKER_LEFT_IN_WINDOW) * s))

        adv_x = (col(REF_ADVENTURE_X[0]), col(REF_ADVENTURE_X[1]))
        fame_x = (col(REF_FAME_X[0]), col(REF_FAME_X[1]))
        name_x = (col(REF_NAME_X[0]), col(REF_NAME_X[1]))
        class_x = (col(REF_CLASS_X[0]), col(REF_CLASS_X[1]))

        fame_x_dig = (
            fame_x[0] + int(round((FAME_STAR_ICON_RIGHT_PAD - FAME_DIGIT_LEFT_BREATHING) * s)),
            fame_x[1] + int(round(FAME_DIGIT_RIGHT_BREATHING * s)),
        )

        row_crop = image_rgb[max(0, row_top):min(H, bot_y1), max(0, fame_x[0]):min(W, status_x[1])]
        fame_crop = image_rgb[max(0, top_y0):min(H, top_y1), max(0, fame_x_dig[0]):min(W, fame_x_dig[1])]
        name_crop = image_rgb[max(0, name_y0):min(H, name_y1), max(0, name_x[0]):min(W, name_x[1])]
        class_crop = image_rgb[max(0, bot_y0):min(H, bot_y1), max(0, class_x[0]):min(W, class_x[1])]
        _logger.debug(
            "row %d crop rects row=(%d,%d,%d,%d) fame=(%d,%d,%d,%d) name=(%d,%d,%d,%d) class=(%d,%d,%d,%d) row_stats=%s fame_stats=%s",
            i,
            max(0, fame_x[0]), max(0, row_top), min(W, status_x[1]), min(H, bot_y1),
            max(0, fame_x_dig[0]), max(0, top_y0), min(W, fame_x_dig[1]), min(H, top_y1),
            max(0, name_x[0]), max(0, name_y0), min(W, name_x[1]), min(H, name_y1),
            max(0, class_x[0]), max(0, bot_y0), min(W, class_x[1]), min(H, bot_y1),
            _debug_crop_stats(row_crop), _debug_crop_stats(fame_crop),
        )
        _save_debug_crop(f"row{i}_all_s{s:.3f}", row_crop)
        _save_debug_crop(f"row{i}_fame_s{s:.3f}", fame_crop)
        _save_debug_crop(f"row{i}_name_s{s:.3f}", name_crop)
        _save_debug_crop(f"row{i}_class_s{s:.3f}", class_crop)

        row_sig = _row_mask_signature(
            image_rgb,
            [
                (fame_x_dig[0], top_y0, fame_x_dig[1], top_y1),
                (name_x[0], name_y0, name_x[1], name_y1),
                (class_x[0], bot_y0, class_x[1], bot_y1),
                (status_x[0], row_top, status_x[1], bot_y1),
            ],
        )
        class_x_text = (class_x[0] + int(round(CLASS_BADGE_RIGHT_PAD * s)), class_x[1])
        pending_rows.append(_PendingOCRRow(
            index=i,
            y_abs=(top_y0, bot_y1),
            row_sig=row_sig,
            fame_x=fame_x_dig,
            fame_y=(top_y0, top_y1),
            name_x=name_x,
            name_y=(name_y0, name_y1),
            class_x=class_x_text,
            class_y=(bot_y0, bot_y1),
            adv_x=adv_x,
            adv_y=(top_y0, top_y1),
            scale=s,
        ))
        empties_since_real = 0
        continue

        fame_value, fame_text, fame_score = _read_fame(
            image_rgb,
            fame_x_dig,
            (top_y0, top_y1),
            s,
        )

        # Partial fame prefix: if OCR got only leading digits (e.g. '7850'
        # when the full fame is 78500), compute an exact search range so
        # the Neople API can still find the character.
        fame_range_min: int | None = None
        fame_range_max: int | None = None
        if fame_value is None and fame_text:
            _raw_digits = re.sub(r"[^0-9]", "", fame_text)
            _partial = _partial_fame_prefix(_raw_digits)
            if _partial is not None:
                fame_range_min, fame_range_max = _partial
                _logger.debug(
                    "row %d partial fame prefix %r → [%d..%d]",
                    i, _raw_digits, fame_range_min, fame_range_max)

        name_raw, name_score = _read_text(image_rgb, name_x, (name_y0, name_y1))
        name = _strip_lv_prefix(name_raw)

        class_x_text = (class_x[0] + int(round(CLASS_BADGE_RIGHT_PAD * s)), class_x[1])
        class_raw, class_score = _read_class(image_rgb, class_x_text, (bot_y0, bot_y1))
        class_name = REF_FIXED_PREFIX_CLASS_REGEX.sub("", class_raw).strip()

        adv_raw, adv_score = _read_text(image_rgb, adv_x, (top_y0, top_y1))
        adventure = adv_raw.strip("<>")

        row = PartyApplyRow(
            index=i,
            y_abs=(top_y0, bot_y1),
            fame=fame_value,
            fame_text=fame_text,
            fame_score=fame_score,
            name=name,
            name_raw=name_raw,
            name_score=name_score,
            class_name=class_name,
            class_raw=class_raw,
            class_score=class_score,
            adventure=adventure,
            adventure_raw=adv_raw,
            adventure_score=adv_score,
            fame_range_min=fame_range_min,
            fame_range_max=fame_range_max,
        )

        if row.is_empty:
            empties_since_real += 1
            if out and empties_since_real >= 3:
                break
            if not out and empties_since_real >= ROW_GATE_MAX_INITIAL_EMPTY_ROWS:
                break
            continue

        empties_since_real = 0
        _cache_party_apply_row(row_sig, row)
        out.append(row)

    if pending_rows:
        composite_results = _recognize_rows_composite(image_rgb, pending_rows)
        for pos, pending in enumerate(pending_rows):
            row = _build_row_from_ocr_result(
                image_rgb,
                pending,
                composite_results.get(pos, _CompositeOCRResult()),
            )
            if row.is_empty:
                continue
            out_by_index[pending.index] = row

    return [out_by_index[i] for i in sorted(out_by_index)]


def _clip_rect(
    image_rgb: np.ndarray,
    x_range: tuple[int, int],
    y_range: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    H, W = image_rgb.shape[:2]
    x0, x1 = max(0, x_range[0]), min(W, x_range[1])
    y0, y1 = max(0, y_range[0]), min(H, y_range[1])
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return x0, y0, x1, y1


def _recognize_rows_composite(
    image_rgb: np.ndarray,
    rows: list[_PendingOCRRow],
) -> dict[int, _CompositeOCRResult]:
    if _ocr_text_boxes is None:
        return {}

    pad = 10
    gap_x = 24
    gap_y = 14
    line_gap = 6
    field_entries: list[tuple[int, str, np.ndarray]] = []
    max_fame_w = 1
    max_text_w = 1

    for pos, row in enumerate(rows):
        specs = (
            ("fame", row.fame_x, row.fame_y),
            ("name", row.name_x, row.name_y),
            ("class", row.class_x, row.class_y),
        )
        for field, x_range, y_range in specs:
            rect = _clip_rect(image_rgb, x_range, y_range)
            if rect is None:
                continue
            x0, y0, x1, y1 = rect
            crop = image_rgb[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            field_entries.append((pos, field, crop))
            if field == "fame":
                max_fame_w = max(max_fame_w, crop.shape[1])
            else:
                max_text_w = max(max_text_w, crop.shape[1])

    if not field_entries:
        return {}

    row_heights: list[int] = []
    for pos in range(len(rows)):
        top_h = 1
        class_h = 1
        for entry_pos, field, crop in field_entries:
            if entry_pos != pos:
                continue
            if field in ("fame", "name"):
                top_h = max(top_h, crop.shape[0])
            elif field == "class":
                class_h = max(class_h, crop.shape[0])
        row_heights.append(top_h + line_gap + class_h + gap_y)

    canvas_w = pad * 2 + max_fame_w + gap_x + max_text_w
    canvas_h = pad + sum(row_heights) + pad
    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    field_rects: list[_CompositeField] = []
    row_y = pad

    for pos, row_h in enumerate(row_heights):
        top_h = max(
            [crop.shape[0] for entry_pos, field, crop in field_entries
             if entry_pos == pos and field in ("fame", "name")] or [1]
        )
        for entry_pos, field, crop in field_entries:
            if entry_pos != pos:
                continue
            if field == "fame":
                x = pad
                y = row_y
            elif field == "name":
                x = pad + max_fame_w + gap_x
                y = row_y
            else:
                x = pad + max_fame_w + gap_x
                y = row_y + top_h + line_gap
            h, w = crop.shape[:2]
            canvas[y:y + h, x:x + w] = crop
            field_rects.append(_CompositeField(pos, field, (x, y, x + w, y + h)))
        row_y += row_h

    _save_debug_crop("composite_ocr", canvas)
    boxes = _ocr_text_boxes(canvas)
    results = {pos: _CompositeOCRResult() for pos in range(len(rows))}
    assigned: dict[tuple[int, str], list] = {}
    margin = 3
    for box in boxes:
        cx = box.cx
        cy = box.cy
        for field_rect in field_rects:
            x0, y0, x1, y1 = field_rect.rect
            if x0 - margin <= cx <= x1 + margin and y0 - margin <= cy <= y1 + margin:
                assigned.setdefault((field_rect.row_pos, field_rect.field), []).append(box)
                break

    for (pos, field), field_boxes in assigned.items():
        field_boxes.sort(key=lambda b: b.x0)
        text = " ".join(b.text.strip() for b in field_boxes if b.text.strip()).strip()
        score = float(np.mean([b.confidence for b in field_boxes])) if field_boxes else 0.0
        if field == "fame":
            results[pos].fame_text = text
            results[pos].fame_score = score
        elif field == "name":
            results[pos].name_raw = text
            results[pos].name_score = score
        elif field == "class":
            results[pos].class_raw = text
            results[pos].class_score = score

    _logger.debug(
        "composite OCR rows=%d image=%s boxes=%d assigned=%d",
        len(rows), tuple(canvas.shape), len(boxes), sum(len(v) for v in assigned.values()),
    )
    return results


def _parse_fame_value(text: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", text)
    if not digits:
        return None
    candidates = {digits}
    if len(digits) > 1:
        candidates.add(digits[1:])
        candidates.add(digits[:-1])
    if len(digits) > 2:
        candidates.add(digits[1:-1])
    best: int | None = None
    for cand in candidates:
        if not cand:
            continue
        try:
            value = int(cand)
        except ValueError:
            continue
        if 10_000 <= value <= 999_999 and (best is None or len(cand) > len(str(best))):
            best = value
    return best


def _build_row_from_ocr_result(
    image_rgb: np.ndarray,
    row: _PendingOCRRow,
    result: _CompositeOCRResult,
) -> PartyApplyRow:
    fame_text = result.fame_text
    fame_score = result.fame_score
    fame_value = _parse_fame_value(fame_text)
    if fame_value is None or not fame_text:
        fame_value, fame_text, fame_score = _read_fame(
            image_rgb, row.fame_x, row.fame_y, row.scale)

    fame_range_min: int | None = None
    fame_range_max: int | None = None
    if fame_value is None and fame_text:
        raw_digits = re.sub(r"[^0-9]", "", fame_text)
        partial = _partial_fame_prefix(raw_digits)
        if partial is not None:
            fame_range_min, fame_range_max = partial
            _logger.debug(
                "row %d partial fame prefix %r -> [%d..%d]",
                row.index, raw_digits, fame_range_min, fame_range_max)

    name_raw = result.name_raw
    name_score = result.name_score
    if not name_raw:
        name_raw, name_score = _read_text(image_rgb, row.name_x, row.name_y)
    name = _strip_lv_prefix(name_raw)

    class_raw = result.class_raw
    class_score = result.class_score
    if not class_raw:
        class_raw, class_score = _read_class(image_rgb, row.class_x, row.class_y)
    class_name = REF_FIXED_PREFIX_CLASS_REGEX.sub("", class_raw).strip()

    return PartyApplyRow(
        index=row.index,
        y_abs=row.y_abs,
        fame=fame_value,
        fame_text=fame_text,
        fame_score=fame_score,
        name=name,
        name_raw=name_raw,
        name_score=name_score,
        class_name=class_name,
        class_raw=class_raw,
        class_score=class_score,
        adventure="",
        adventure_raw="",
        adventure_score=0.0,
        fame_range_min=fame_range_min,
        fame_range_max=fame_range_max,
    )


def _read_text(
    image_rgb: np.ndarray,
    x_range: tuple[int, int],
    y_range: tuple[int, int],
) -> tuple[str, float]:
    H, W = image_rgb.shape[:2]
    x0, x1 = max(0, x_range[0]), min(W, x_range[1])
    y0, y1 = max(0, y_range[0]), min(H, y_range[1])
    if x1 - x0 < 4 or y1 - y0 < 4:
        return "", 0.0
    if _ocr_text_boxes is None:
        return "", 0.0

    crop = image_rgb[y0:y1, x0:x1]
    if crop.size == 0:
        return "", 0.0
    boxes = _ocr_text_boxes(crop)
    if not boxes:
        return "", 0.0
    boxes.sort(key=lambda b: b.x0)
    text = " ".join(b.text.strip() for b in boxes if b.text.strip()).strip()
    mean = float(np.mean([b.confidence for b in boxes])) if boxes else 0.0
    return text, mean


def _read_fame(
    image_rgb: np.ndarray,
    x_range: tuple[int, int],
    y_range: tuple[int, int],
    scale: float,
) -> tuple[int | None, str, float]:
    """General OCR only. Returns (value, text, conf)."""
    H, W = image_rgb.shape[:2]

    if _ocr_fame is not None:
        y_pad = max(2, int(round(5 * scale)))
        y0 = max(0, y_range[0] - y_pad)
        y1 = min(H, y_range[1] + y_pad)
        x0 = max(0, x_range[0])
        x1 = min(W, x_range[1])

        if x1 - x0 >= 4 and y1 - y0 >= 4:
            crop = image_rgb[y0:y1, x0:x1]
            value, text, conf = _ocr_fame(crop)
            if value is not None:
                return value, text, conf
            if text:
                return None, text, conf

    return None, "", 0.0


def _read_class(
    image_rgb: np.ndarray,
    x_range: tuple[int, int],
    y_range: tuple[int, int],
) -> tuple[str, float]:
    """General OCR only."""
    H, W = image_rgb.shape[:2]

    if _ocr_class is not None:
        x0 = max(0, x_range[0])
        x1 = min(W, x_range[1])
        y0 = max(0, y_range[0])
        y1 = min(H, y_range[1])

        if x1 - x0 >= 4 and y1 - y0 >= 4:
            crop = image_rgb[y0:y1, x0:x1]
            text, conf = _ocr_class(crop)
            if text:
                return text, conf

    return "", 0.0


def _strip_lv_prefix(text: str) -> str:
    """Drop the leading 'Lv. 115 ' prefix, tolerating OCR garbling.

    OCR often misreads 'Lv.' as a short token such as 'u', 'iv', or '1v',
    producing output like 'u 115 ikura' instead of 'Lv. 115 ikura'.
    Three patterns are tried in order:
      1. Recognisable 'Lv/LV' variants.
      2. Any short OCR token (1-5 non-space chars) + space + plausible level
         number (1-125) + space — catches garbled prefixes.
      3. Purely digit prefix when the 'Lv.' text was entirely dropped.
    """
    s = text.lstrip()
    digit_class = r"[\dIlLiSsOoBb]"

    # Pattern 1: recognisable Lv/LV variants.
    m = re.search(r"[Ll][vVuUyY][.,;:]?\s*" + digit_class + r"{1,4}\s*", s[:16])
    if m:
        return s[m.end():]

    # Pattern 2: garbled Lv prefix — short token + whitespace + level digits +
    # whitespace.  Handles "u 115 name", "iv 115 name", "1v 115 name".
    m = re.match(r"^\S{1,5}\s+(\d{1,3})\s+", s)
    if m:
        try:
            if 1 <= int(m.group(1)) <= 125:
                return s[m.end():]
        except ValueError:
            pass

    # Pattern 3: purely digit prefix when Lv. was entirely dropped by OCR.
    m = re.match(r"^[\W_]*" + digit_class + r"{1,3}\s*", s)
    if m:
        return s[m.end():]

    return s


# ---------------------------------------------------------------------------
# CLI for quick verification
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))

    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument(
        "--ui-scale",
        type=float,
        default=69.0,
        help="DFO UI Scale percent (default 69 — sample's scale)",
    )
    args = parser.parse_args()

    REF_UI_SCALE_PCT = 69.0
    actual = max(0.35, 1.0 + (float(args.ui_scale) - 69.0) / 100.0)
    ref_factor = max(0.35, 1.0 + (REF_UI_SCALE_PCT - 69.0) / 100.0)
    near = actual / ref_factor

    img = np.array(Image.open(args.image).convert("RGB"))
    det = detect_party_apply(img, near_scale=near)

    print(
        f"detected={det.found} score={det.score:.3f} scale={det.scale:.3f} "
        f"marker={det.marker_xywh} rows={len(det.rows_top_y)}"
    )

    if not det.found:
        return_code = 1
    else:
        rows = recognize_party_apply(img, det)

        for r in rows:
            print(
                f" row {r.index}: fame={r.fame!s:>7} "
                f"name={r.name!r:20s} class={r.class_name!r:14s} "
                f"adv={r.adventure!r:14s}"
            )
        return_code = 0

    sys.exit(return_code)
