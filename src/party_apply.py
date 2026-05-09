"""Party Apply window detection + per-row recognition.

This module detects the party-apply / raid-request list by matching small header
anchors, then OCRs only the small row crops. Do not use full-screen OCR here:
cold detection must stay fast enough to recover when the window opens, closes, or
moves.

Fame remains OCR-first because template digit matching proved brittle for this
window. Template digit matching is kept only as a fallback when OCR returns no
text.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from match import match_row
from neople import name_similarity  # kept for compatibility with older callers/imports
from resources import resource_path
from segment import (
    color_text_mask,
    detect_baseline,
    find_chars,
    reconcile_boxes,
    text_mask,
)
from templates import Template

try:
    from general_ocr import read_fame as _ocr_fame, read_class as _ocr_class
except ImportError:
    _ocr_fame = None
    _ocr_class = None


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

# Cold party-apply detection must be cheap. Native marker probes cover the
# supported UI scales (0/50/69/100). The expensive all-scale sweep is fallback
# only and is throttled while the window is closed.
PA_COLD_CANDIDATES_PER_FRAME = 6

# Geometry measured from samples/party_apply_03.png at UI Scale 69%.
REF_MARKER_SIZE = (734, 16)  # marker (column header strip) WxH
REF_MARKER_LEFT_IN_WINDOW = 38

REF_FIRST_ROW_TOP_DY = 36
REF_ROW_PITCH = 56
REF_ROW_TOP_LINE_DY = 0
REF_ROW_TOP_LINE_H = 15
REF_ROW_BOT_LINE_DY = 14
REF_ROW_BOT_LINE_H = 18

REF_ADVENTURE_X = (42, 162)
REF_FAME_X = (200, 295)
REF_NAME_X = (282, 442)
REF_CLASS_X = (282, 442)

REF_FIXED_PREFIX_NAME = "Lv. 115 "
REF_FIXED_PREFIX_CLASS_REGEX = re.compile(r"^[A-Za-z]?Neo[: ]\s*")

TEMPLATE_SCALE_FOR_PARTY_APPLY = 0.7
TEMPLATE_SCALE_FOR_PARTY_APPLY_DIGITS = 0.5

# Fame is OCR-first. These pads keep the digit crop generous across UI scales.
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
MAX_OCR_PER_FRAME = 3

_DEFAULT_MARKER_PATH = resource_path("markers", "party_apply", "column_header_69pct.png")
_OPTIONAL_MARKER_PATHS = (
    resource_path("markers", "party_apply", "column_header_0pct.png"),
    resource_path("markers", "party_apply", "column_header_50pct.png"),
    resource_path("markers", "party_apply", "column_header_80pct.png"),
    resource_path("markers", "party_apply", "column_header_100pct.png"),
)


@dataclass
class PartyApplyDetection:
    found: bool
    score: float
    scale: float  # 1.0 = 69% reference capture
    marker_xywh: tuple[int, int, int, int]
    rows_top_y: list[int]


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


def _load_markers() -> list[np.ndarray]:
    """Load every available party-apply header marker."""
    paths = [_DEFAULT_MARKER_PATH]
    paths.extend(p for p in _OPTIONAL_MARKER_PATHS if p.exists())
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


def _candidate_found(marker_score: float, grid_score: float, score_threshold: float) -> bool:
    """Decide whether a marker candidate is usable.

    Intermediate DFO UI Scale values do not render as a simple resize of the
    0/50/69/100 marker captures. Their header-template score can be only
    ~0.32-0.45 even when the request list is present. Promote these candidates
    only when the table structure under the header is strong enough.
    """
    if marker_score >= score_threshold:
        return True
    if marker_score >= 0.40 and grid_score >= 1.35:
        return True
    if marker_score >= 0.32 and grid_score >= 2.15:
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

    This version keeps cold detection bounded. It never performs an entire
    marker x scale sweep in one frame. Instead it probes a small number of
    high-priority marker/scale candidates per call and advances a cursor.

    Fast path order:
    1. previous hint local lookup,
    2. known-scale probes when a previous scale exists,
    3. rotating cold candidate probes for supported/common UI scales.
    """
    img_gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    marker_grays = (
        [cv2.cvtColor(marker, cv2.COLOR_RGB2GRAY)]
        if marker is not None
        else [cv2.cvtColor(m, cv2.COLOR_RGB2GRAY) for m in _load_markers()]
    )
    H, W = img_gray.shape

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
            return best_hint

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
        # Hand-tuned anchors first, then a dense continuous grid. This supports
        # every DFO UI Scale value without needing a marker file for every 1%.
        anchors = [
            0.66, 1.00, 0.46, 0.57, 0.75, 0.88, 1.10, 1.28,
            1.54, 1.65, 0.30, 0.36, 0.43, 0.49, 0.59, 0.61,
            0.84, 1.40, 1.80, 2.00,
        ]
        dense = [round(x * 0.02, 2) for x in range(15, 101)]  # 0.30..2.00
        seen: set[float] = set()
        out: list[float] = []
        for v in anchors + dense:
            if min_scale * 0.65 <= v <= max_scale * 1.25 and v not in seen:
                seen.add(v)
                out.append(v)
        return tuple(out)

    # Always probe every available marker at its native size before using a
    # remembered near_scale. This is important when the user changes DFO UI
    # Scale during the same app session: app.py may still pass near_scale=0.66
    # from a prior 0% detection, but the new 50%/100% marker can match
    # perfectly at native size.
    native_best = PartyApplyDetection(False, 0.0, 1.0, (0, 0, 0, 0), [])
    for marker_gray in marker_grays:
        cand = _scan_pair(marker_gray, 1.0)
        if (
            (cand.found and not native_best.found)
            or (cand.found == native_best.found and cand.score > native_best.score)
        ):
            native_best = cand
        if cand.found and cand.score >= 0.90:
            return cand

    # If app.py has a remembered scale, probe it first, but do not stop there
    # on failure. DFO UI Scale can be changed inside the same app session, so a
    # stale 0% near_scale must not prevent 50%/100% or intermediate scales from
    # being probed.
    best = native_best
    if near_scale is not None:
        best_marker = _best_marker_for_effective_scale(float(near_scale))
        offsets = (-near_scale_radius, -0.06, -0.03, 0.0, 0.03, 0.06, near_scale_radius)
        idx = int(getattr(detect_party_apply, "_near_scale_probe_idx", 0)) % len(offsets)
        setattr(detect_party_apply, "_near_scale_probe_idx", (idx + 1) % len(offsets))

        targets = [float(near_scale), max(min_scale, min(max_scale, float(near_scale) + offsets[idx]))]
        for target in targets:
            scale = _resize_for_effective_scale(best_marker, target)
            cand = _scan_pair(best_marker, scale)
            best = _better(best, cand)
            if cand.found and cand.score >= 0.90:
                return cand

    # Cold/changed-scale path: probe a small, rotating slice of a continuous
    # effective-scale grid. This avoids the v8 full sweep stalls while covering
    # UI Scale 0..100 in 1% increments over several frames.
    priority_effective_scales = _make_priority_effective_scales()
    idx = int(getattr(detect_party_apply, "_cold_probe_idx", 0)) % len(priority_effective_scales)
    setattr(
        detect_party_apply,
        "_cold_probe_idx",
        (idx + PA_COLD_CANDIDATES_PER_FRAME) % len(priority_effective_scales),
    )

    for j in range(PA_COLD_CANDIDATES_PER_FRAME):
        target = float(priority_effective_scales[(idx + j) % len(priority_effective_scales)])
        marker_gray = _best_marker_for_effective_scale(target)
        scale = _resize_for_effective_scale(marker_gray, target)
        cand = _scan_pair(marker_gray, scale)
        best = _better(best, cand)
        if cand.found and cand.score >= 0.90:
            return cand

    return best

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
    if not _candidate_found(float(max_val), grid_score, threshold):
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
        found=_candidate_found(float(score), float(grid_score), threshold),
        score=float(score),
        scale=float(scale),
        marker_xywh=(mx, my, marker_w, marker_h),
        rows_top_y=rows_top_y,
    )


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------

def recognize_party_apply(
    image_rgb: np.ndarray,
    det: PartyApplyDetection,
    templates: dict[str, list[Template]],
    digit_templates: dict[str, list[Template]] | None = None,
) -> list[PartyApplyRow]:
    """Read fame, name, class, and adventure for each detected row."""
    if not det.found:
        return []

    mx, my, _, _ = det.marker_xywh
    s = det.scale
    H, W = image_rgb.shape[:2]

    if digit_templates is None:
        digit_templates = {ch: v for ch, v in templates.items() if ch.isdigit() or ch == ","}

    out: list[PartyApplyRow] = []
    empties_since_real = 0
    pitch = int(round(REF_ROW_PITCH * s))
    name_line_h = max(10, int(round(REF_ROW_TOP_LINE_H * s)))

    def _col(ref_x: int) -> int:
        return mx + int(round((ref_x - REF_MARKER_LEFT_IN_WINDOW) * s))

    fame_left_for_check = _col(REF_FAME_X[0])
    name_right_for_check = _col(REF_NAME_X[1])

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

        if check_x1 - check_x0 >= 10 and check_y1 - row_top >= 5:
            row_strip = image_rgb[row_top:check_y1, check_x0:check_x1]
            gray_max = row_strip.max(axis=2)
            bg = float(np.percentile(gray_max, 25))
            bright = gray_max > bg + 60
            bright_count = int(bright.sum())

            def _mark_empty() -> bool:
                nonlocal empties_since_real
                empties_since_real += 1
                if out and empties_since_real >= 3:
                    return True
                if not out and empties_since_real >= ROW_GATE_MAX_INITIAL_EMPTY_ROWS:
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

            _logger.debug(
                "row %d gate PASS bright=%d transitions=%d fame_bright=%d fame_trans=%d name_bright=%d name_trans=%d",
                i, bright_count, transitions, fame_bright, fame_trans, name_bright, name_trans,
            )

        if ocr_rows_used >= MAX_OCR_PER_FRAME:
            empties_since_real += 1
            if out and empties_since_real >= 3:
                break
            if not out and empties_since_real >= ROW_GATE_MAX_INITIAL_EMPTY_ROWS:
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
        fame_value, fame_text, fame_score = _read_fame(
            image_rgb,
            fame_x_dig,
            (top_y0, top_y1),
            digit_templates,
            s,
        )

        name_raw, name_score = _read_text(image_rgb, name_x, (name_y0, name_y1), templates)
        name = _strip_lv_prefix(name_raw)

        class_x_text = (class_x[0] + int(round(CLASS_BADGE_RIGHT_PAD * s)), class_x[1])
        class_raw, class_score = _read_class(image_rgb, class_x_text, (bot_y0, bot_y1), templates)
        class_name = REF_FIXED_PREFIX_CLASS_REGEX.sub("", class_raw).strip()

        adv_raw, adv_score = _read_text(image_rgb, adv_x, (top_y0, top_y1), templates)
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
        )

        if row.is_empty:
            empties_since_real += 1
            if out and empties_since_real >= 3:
                break
            if not out and empties_since_real >= ROW_GATE_MAX_INITIAL_EMPTY_ROWS:
                break
            continue

        empties_since_real = 0
        out.append(row)

    return out


def _read_text(
    image_rgb: np.ndarray,
    x_range: tuple[int, int],
    y_range: tuple[int, int],
    templates: dict[str, list[Template]],
) -> tuple[str, float]:
    H, W = image_rgb.shape[:2]
    x0, x1 = max(0, x_range[0]), min(W, x_range[1])
    y0, y1 = max(0, y_range[0]), min(H, y_range[1])
    if x1 - x0 < 4 or y1 - y0 < 4:
        return "", 0.0

    crop = image_rgb[y0:y1, x0:x1]
    mask = _otsu_mask(crop)

    if (mask > 0).sum() < ROW_MIN_TEXT_PIXELS:
        mask = text_mask(crop)
    if (mask > 0).sum() < ROW_MIN_TEXT_PIXELS:
        mask = color_text_mask(crop)
    if (mask > 0).sum() < ROW_MIN_TEXT_PIXELS:
        return "", 0.0

    boxes = find_chars(mask)
    if not boxes:
        return "", 0.0

    baseline = detect_baseline(mask)
    text, matches = match_row(mask, boxes, baseline, templates)
    if not matches:
        return text, 0.0

    mean = float(np.mean([m.score for m in matches]))
    return text, mean


def _read_fame(
    image_rgb: np.ndarray,
    x_range: tuple[int, int],
    y_range: tuple[int, int],
    digit_templates: dict[str, list[Template]],
    scale: float,
) -> tuple[int | None, str, float]:
    """General OCR first, template fallback. Returns (value, text, conf)."""
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

    text, score, value = _read_digits_via_templates(
        image_rgb,
        x_range,
        y_range,
        digit_templates,
    )
    return value, text, score


def _read_class(
    image_rgb: np.ndarray,
    x_range: tuple[int, int],
    y_range: tuple[int, int],
    templates: dict[str, list[Template]],
) -> tuple[str, float]:
    """General OCR first, template fallback."""
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

    return _read_text(image_rgb, x_range, y_range, templates)


def _read_digits_via_templates(
    image_rgb: np.ndarray,
    x_range: tuple[int, int],
    y_range: tuple[int, int],
    digit_templates: dict[str, list[Template]],
) -> tuple[str, float, int | None]:
    H, W = image_rgb.shape[:2]
    x0, x1 = max(0, x_range[0]), min(W, x_range[1])
    y0, y1 = max(0, y_range[0]), min(H, y_range[1])
    if x1 - x0 < 4 or y1 - y0 < 4:
        return "", 0.0, None

    crop = image_rgb[y0:y1, x0:x1]
    mask = _otsu_mask(crop)
    if (mask > 0).sum() < 6:
        mask = text_mask(crop, offset=35.0)

    boxes = find_chars(mask)
    if not boxes:
        return "", 0.0, None

    baseline = detect_baseline(mask)
    text, matches = match_row(mask, boxes, baseline, digit_templates, width_tol=4, height_tol=4)
    digits = "".join(m.char for m in matches if m.char.isdigit())
    if not digits:
        return text, 0.0, None

    digit_scores = [m.score for m in matches if m.char.isdigit()]
    mean = float(np.mean(digit_scores)) if digit_scores else 0.0

    try:
        value = int(digits)
    except ValueError:
        return digits, mean, None

    if not (10_000 <= value <= 999_999):
        return digits, mean, None

    return digits, mean, value


def _strip_lv_prefix(text: str) -> str:
    """Drop the leading 'Lv. 115 ' prefix, tolerating common OCR variants."""
    s = text.lstrip()
    digit_class = r"[\dIlLiSsOoBb]"

    m = re.search(r"[Ll][vVuUyY][.,;:]?\s*" + digit_class + r"{1,4}\s*", s[:14])
    if m:
        return s[m.end():]

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

    from recognize import load_default_templates, ui_scale_setting_to_factor

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
    actual = ui_scale_setting_to_factor(args.ui_scale)
    ref_factor = ui_scale_setting_to_factor(REF_UI_SCALE_PCT)
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
        templates = load_default_templates(ui_scale=TEMPLATE_SCALE_FOR_PARTY_APPLY * near)
        digit_lib = load_default_templates(ui_scale=TEMPLATE_SCALE_FOR_PARTY_APPLY_DIGITS * near)
        digit_templates = {ch: v for ch, v in digit_lib.items() if ch.isdigit() or ch == ","}
        rows = recognize_party_apply(img, det, templates, digit_templates)

        for r in rows:
            print(
                f" row {r.index}: fame={r.fame!s:>7} "
                f"name={r.name!r:20s} class={r.class_name!r:14s} "
                f"adv={r.adventure!r:14s}"
            )
        return_code = 0

    sys.exit(return_code)
