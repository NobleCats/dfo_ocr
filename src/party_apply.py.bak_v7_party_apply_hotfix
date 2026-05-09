"""Party Apply window detection + per-row recognition.

The party-apply (a.k.a. party support / 'Party/Raid Group Request List') UI
is laid out very differently from the raid party window:

    +--------------------+--------------------------+----------------+--------+
    | Explorer Club Name | Fame/Online Character    | Application    | History|
    +--------------------+--------------------------+----------------+--------+
    | <Adventure>        |  ★ 77,802  Lv. 115 Name  | Accept Decline |        |
    |                    |  [B]Neo: <Class>         |                |        |
    +--------------------+--------------------------+----------------+--------+
    ...

Each data row is two lines tall: the top line carries `Lv. 115 <CharacterName>`
and the bottom line carries `[Awak]Neo: <ClassName>`. The Adventure column
sits to the left and is the same data on both sub-lines.

Reference geometry was measured from samples/party_apply_03.png at the user's
69% UI Scale setting. We treat *that capture* as scale=1.0 internally.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from match import match_row
from neople import name_similarity
from resources import resource_path
from segment import (color_text_mask, detect_baseline, find_chars,
                     reconcile_boxes, text_mask)
from templates import Template

try:
    from general_ocr import read_fame as _ocr_fame, read_class as _ocr_class
except ImportError:  # graceful fallback if EasyOCR / cv2 missing
    _ocr_fame = None
    _ocr_class = None


def _otsu_mask(crop_rgb: np.ndarray) -> np.ndarray:
    """Global Otsu binarization. Works well on small per-column crops where
    text vs background is bimodal regardless of stroke colour."""
    gray = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2GRAY)
    _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return mask


def _detect_top_text_y(image_rgb: np.ndarray, row_top: int, row_bot: int,
                       scale: float) -> int | None:
    """Return the absolute y of the topmost bright text row inside a data
    row's vertical span. Used to anchor a tight Y crop for the name OCR
    so the row-below class line doesn't bleed into the name template
    matching when name_x and class_x overlap.
    """
    H, W = image_rgb.shape[:2]
    row_top = max(0, row_top)
    row_bot = min(H - 1, row_bot)
    if row_bot - row_top < 8:
        return None
    # Look in the right two-thirds of the row where 'Lv. 115 Name' renders
    # — avoids the leftmost adventure-name (yellow) glyphs that otherwise
    # bias the projection upward.
    x0 = max(0, int(round(180 * scale)))
    x1 = W
    strip = image_rgb[row_top:row_bot + 1, x0:x1].max(axis=2)
    bg_level = float(np.percentile(strip, 10))
    bright = (strip > bg_level + 35)
    counts = bright.sum(axis=1)
    if counts.max() < 6:
        return None
    threshold = max(3, counts.max() // 5)
    for y, c in enumerate(counts):
        if c >= threshold:
            return row_top + y
    return None


_logger = logging.getLogger("dfogang.party_apply")


# Geometry (party_apply_03.png @ UI 69%, treated internally as scale=1.0).
REF_MARKER_SIZE = (734, 16)         # marker (column header strip) WxH
REF_MARKER_LEFT_IN_WINDOW = 38      # marker x within full window image
# Per-row offsets relative to MARKER top-left:
REF_FIRST_ROW_TOP_DY = 36           # row 1 top = marker top + 36
REF_ROW_PITCH = 56                  # successive rows

REF_ROW_TOP_LINE_DY = 0             # 'Lv. 115 Name' starts at row top
REF_ROW_TOP_LINE_H = 15
REF_ROW_BOT_LINE_DY = 14            # 'Neo: Class' offset from row top
REF_ROW_BOT_LINE_H = 18

# Column X relative to MARKER top-left (subtract 38 from image-x to get this):
REF_ADVENTURE_X = (42, 162)         # <Adventure>
REF_FAME_X = (200, 295)             # ★ 77,802
REF_NAME_X = (282, 442)             # Lv. 115 Name
REF_CLASS_X = (282, 442)            # [B]Neo: Class

REF_FIXED_PREFIX_NAME = "Lv. 115 "  # we strip this when reading the name
REF_FIXED_PREFIX_CLASS_REGEX = re.compile(r"^[A-Za-z]?Neo[: ]\s*")

# Party-apply text is multi-coloured (yellow adventure, green fame, light-blue
# name, green class). Per-row local thresholding (the raid-party path) loses
# stroke pixels on the dim coloured glyphs, so we use Otsu binarization on
# each per-region crop instead — high-contrast text vs window background
# binarises cleanly to 2 levels. Empirically this lifts the name line from
# garbled to mostly-readable at ui_scale ~0.7.
TEMPLATE_SCALE_FOR_PARTY_APPLY = 0.7
TEMPLATE_SCALE_FOR_PARTY_APPLY_DIGITS = 0.5

# X offset (relative to the FAME column left edge) past which the leading star
# icon ends and the actual digit text begins. The icon is anti-aliased into
# the digit area; cropping too tight bleeds the icon's right wing into the
# first OCR'd digit (we saw a phantom leading '8' in samples).
FAME_STAR_ICON_RIGHT_PAD = 22

# Same for the B/D awakening-tier badge sitting before "Neo:" on the class
# line.
CLASS_BADGE_RIGHT_PAD = 30

# OCR fame crops need extra horizontal breathing room. Template-based digit
# matching was too brittle for party-apply fame, so fame stays OCR-first; these
# pads avoid clipping the leftmost digit at 100% UI scale while giving OCR
# enough right-side context for comma-separated values.
FAME_DIGIT_LEFT_BREATHING = 6
FAME_DIGIT_RIGHT_BREATHING = 42


# Threshold for considering a row "real" (drops empty placeholder rows).
ROW_MIN_TEXT_PIXELS = 12


_DEFAULT_MARKER_PATH = (
    resource_path("markers", "party_apply", "column_header_69pct.png")
)
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
    scale: float                              # 1.0 = ref capture (UI 69%)
    marker_xywh: tuple[int, int, int, int]    # marker bbox in input image
    rows_top_y: list[int]                     # absolute y of each row top


@dataclass
class PartyApplyRow:
    index: int
    y_abs: tuple[int, int]   # full row (top-line top .. bot-line bot)
    fame: int | None
    fame_text: str
    fame_score: float
    name: str                # character name with 'Lv. 115 ' stripped
    name_raw: str            # full OCR before stripping
    name_score: float
    class_name: str          # with 'Neo:' / awakening prefix stripped
    class_raw: str
    class_score: float
    adventure: str           # adventure / explorer club name (without <>)
    adventure_raw: str
    adventure_score: float

    @property
    def is_empty(self) -> bool:
        """A row is "real" only when at least one anchor signal is present:
        a parseable fame value, OR an OCR'd name with the leading 'Lv'
        level marker. Class-only evidence is rejected: stray UI banners
        (e.g. 'Click a slot to remove' in sample 01) OCR enough alpha
        characters to look like a class but never include 'Lv' or a fame
        number.
        """
        # General-OCR confidence on a real fame number is consistently
        # 0.95+; conf in the 0.3-0.6 band almost always means we OCR'd
        # banner/icon noise (e.g. 'Click a slot to remove' read as
        # 'rckaslottoremove' with a phantom 5-digit fame at conf 0.44).
        # Raise the floor so banners no longer slip through is_empty.
        if self.fame is not None and self.fame_score >= 0.7:
            return False
        # Look for the 'Lv' / 'lv' marker in the name OCR — even when '115'
        # is mangled (ILS / IIS / 11S), the 'Lv' itself is reliable since it
        # has only two glyphs and is not a tier-badge artefact.
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
    """Return marker capture scale relative to the 69% reference geometry.

    Optional marker images are captured at their native UI scale, so the
    resize factor used by matchTemplate is not the same as the party-window
    geometry scale. Converting through marker width keeps row/column crops
    aligned for 0%, 50%, 100% UI-scale markers.
    """
    try:
        return max(0.35, float(marker_gray.shape[1]) / float(REF_MARKER_SIZE[0]))
    except Exception:
        return 1.0


def _load_markers() -> list[np.ndarray]:
    """Load every available party-apply header marker.

    The 69% reference marker is always used. Optional marker captures for
    other UI scales can be dropped into resources/markers/party_apply/ as
    column_header_80pct.png or column_header_100pct.png without changing code.
    """
    paths = [_DEFAULT_MARKER_PATH]
    paths.extend(p for p in _OPTIONAL_MARKER_PATHS if p.exists())
    return [_load_marker(p) for p in paths]



def _grid_support_score(img_gray: np.ndarray, marker_xy: tuple[int, int],
                        marker_w: int, marker_h: int, scale: float,
                        max_rows: int) -> float:
    """Cheap structural validation for a candidate request-list header.

    We deliberately avoid OCR here. A real party-apply table has repeated
    horizontal row separators immediately below the column header. False
    template hits on other UI panels can score moderately well, but they do
    not usually have the same dense grid pattern at the expected pitch.
    """
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
    # Horizontal line/edge energy, normalized against local texture.
    dy = np.abs(np.diff(roi.astype(np.int16), axis=0)).mean(axis=1)
    if dy.size == 0:
        return 0.0
    bg = float(np.median(dy)) + 1e-6
    # Count strong edges near expected row boundaries and return the strongest
    # normalized support. This is intentionally permissive; marker score still
    # remains the primary signal.
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


def _candidate_found(marker_score: float, grid_score: float,
                     score_threshold: float) -> bool:
    """Decide whether a marker candidate is usable.

    High marker scores pass directly. Lower scores are accepted only when the
    table grid below the header looks like the party-apply request list. This
    lets 0% UI scale recover with a lower template score without accepting
    random header-like text elsewhere on the screen.
    """
    if marker_score >= score_threshold:
        return True
    return marker_score >= 0.50 and grid_score >= 2.0

def detect_party_apply(image_rgb: np.ndarray,
                       *,
                       min_scale: float = 0.45,
                       max_scale: float = 2.0,
                       scale_step: float = 0.02,
                       coarse_step: float = 0.1,
                       score_threshold: float = 0.54,
                       marker: np.ndarray | None = None,
                       hint: PartyApplyDetection | None = None,
                       # Wide enough to absorb a one-frame in-game window
                       # drag of ~200px without falling back to the slow
                       # full multi-scale scan. Still much faster than full
                       # scan because we hold scale fixed and only sweep XY.
                       hint_search_radius: int = 200,
                       near_scale: float | None = None,
                       near_scale_radius: float = 0.1,
                       max_rows: int = 12,
                       ) -> PartyApplyDetection:
    """Locate the party-apply window in `image_rgb`.

    Row contents are OCR/template-read later; detection only needs to find the
    request-list column header so the downstream crop coordinates can be
    anchored. We therefore match a small set of header anchors captured at
    common DFO UI-scale settings (0%, 50%, 69%, 100%) instead of relying on a
    single 69% reference. This keeps the detector resolution-independent while
    avoiding broad full-window templates.

    Returns marker bbox plus precomputed row tops. The caller can then read
    each row at the per-column offsets defined above.
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
            cand = _hint_lookup(img_gray, marker_gray, hint,
                                hint_search_radius, score_threshold,
                                max_rows, H)
            if cand is not None and (best_hint is None or
                                     cand.score > best_hint.score):
                best_hint = cand
        if best_hint is not None:
            return best_hint

    full_scales = np.arange(min_scale, max_scale + 1e-6, scale_step)
    if near_scale is not None:
        # Restart after a window-close: we know the UI scale already, so
        # pick the marker whose native base scale matches it and probe
        # the marker's NATIVE size first (s=1.0). When it matches — the
        # common case for a same-game-session restart — that single
        # matchTemplate replaces the whole near-band sweep.
        best_marker = min(
            marker_grays,
            key=lambda m: abs(_marker_base_scale(m) - near_scale))
        native = _scan_scales([1.0], img_gray, best_marker,
                              score_threshold, max_rows, H, W)
        if native.found and native.score >= 0.9:
            return native
        base = _marker_base_scale(best_marker)
        lo = max(0.2, (near_scale - near_scale_radius) / base)
        hi = max(lo + scale_step, (near_scale + near_scale_radius) / base)
        scales = np.arange(lo, hi + 1e-6, scale_step)
        wide = _scan_scales(scales, img_gray, best_marker,
                            score_threshold, max_rows, H, W)
        return wide if wide.score > native.score else native

    # Two-stage scan: coarse (large step) over full range to find the right
    # neighbourhood, then fine refinement around the best candidate. This
    # cuts cold-scan latency on a 2K-wide capture from ~5s to <1s without
    # sacrificing score quality.
    #
    # We *always* include s=1.0 so a marker captured at its native UI scale
    # gets a perfect match against frames at the same scale — the marker
    # template is sharp enough that even a 5% resize (s=0.95 / 1.05) drops
    # the score below the threshold. Without the explicit s=1.0 probe, the
    # 100pct marker on a 100% UI frame coarse-picks s=0.45 score 0.28 and
    # never recovers in fine refinement.
    coarse = np.unique(np.concatenate([
        np.arange(min_scale, max_scale + 1e-6, coarse_step),
        np.array([1.0]),
    ]))
    coarse_best = PartyApplyDetection(False, 0.0, 1.0, (0, 0, 0, 0), [])
    coarse_best_marker: np.ndarray | None = None
    for marker_gray in marker_grays:
        cand = _scan_scales(coarse, img_gray, marker_gray, score_threshold,
                            max_rows, H, W)
        if ((cand.found and not coarse_best.found) or
                (cand.found == coarse_best.found and cand.score > coarse_best.score)):
            coarse_best = cand
            coarse_best_marker = marker_gray
        # A near-perfect coarse hit (e.g. native-scale marker) is already
        # the answer — fine refinement can't beat it. Bail early to keep
        # cold-scan latency well under a second.
        if cand.score >= 0.97:
            break
    if coarse_best.score == 0 or coarse_best_marker is None:
        return coarse_best
    if coarse_best.score >= 0.97:
        return coarse_best
    # Fine refinement only on the winning coarse marker. Running it across
    # all markers was 4x more matchTemplate calls for no quality benefit —
    # the wrong-base markers can never beat the winner here.
    fine_lo = max(min_scale, coarse_best.scale - coarse_step)
    fine_hi = min(max_scale, coarse_best.scale + coarse_step)
    fine = np.arange(fine_lo, fine_hi + 1e-6, scale_step)
    fine_best = _scan_scales(fine, img_gray, coarse_best_marker,
                             score_threshold, max_rows, H, W)
    if fine_best.found and not coarse_best.found:
        return fine_best
    if coarse_best.found and not fine_best.found:
        return coarse_best
    return fine_best if fine_best.score >= coarse_best.score else coarse_best


def _scan_scales(scales, img_gray, marker_gray, score_threshold,
                 max_rows, H, W) -> PartyApplyDetection:
    best = PartyApplyDetection(False, 0.0, 1.0, (0, 0, 0, 0), [])
    base_scale = _marker_base_scale(marker_gray)
    for scale in scales:
        new_w = int(round(marker_gray.shape[1] * scale))
        new_h = int(round(marker_gray.shape[0] * scale))
        if new_w < 16 or new_h < 4:
            continue
        if new_w > W or new_h > H:
            continue
        resized = cv2.resize(marker_gray, (new_w, new_h),
                             interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        result = cv2.matchTemplate(img_gray, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        effective_scale = float(scale) * base_scale
        grid_score = _grid_support_score(img_gray, max_loc, new_w, new_h,
                                         effective_scale, max_rows)
        cand = _build_detection(effective_scale, max_val, max_loc,
                               score_threshold, new_w, new_h, max_rows,
                               H, grid_score)
        if ((cand.found and not best.found) or
                (cand.found == best.found and cand.score > best.score)):
            best = cand
    return best


def _hint_lookup(img_gray, marker_gray, hint, radius, threshold, max_rows, H):
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
    resized = cv2.resize(marker_gray, (new_w, new_h),
                         interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
    roi = img_gray[y0:y1, x0:x1]
    result = cv2.matchTemplate(roi, resized, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    abs_loc = (max_loc[0] + x0, max_loc[1] + y0)
    grid_score = _grid_support_score(img_gray, abs_loc, new_w, new_h,
                                     effective_scale, max_rows)
    if not _candidate_found(float(max_val), grid_score, threshold):
        return None
    return _build_detection(
        effective_scale, max_val, abs_loc, threshold,
        new_w, new_h, max_rows, H, grid_score)


def _build_detection(scale: float, score: float, marker_xy: tuple[int, int],
                     threshold: float, marker_w: int, marker_h: int,
                     max_rows: int, image_h: int,
                     grid_score: float = 0.0) -> PartyApplyDetection:
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

def recognize_party_apply(image_rgb: np.ndarray,
                          det: PartyApplyDetection,
                          templates: dict[str, list[Template]],
                          digit_templates: dict[str, list[Template]] | None = None,
                          ) -> list[PartyApplyRow]:
    """Read fame, name, class, adventure for each row in `det`.

    `templates` is the alpha-character library used for names / classes /
    adventure. `digit_templates`, if provided, is a separate library tuned
    for the smaller fame digit text. When omitted we slice digits out of
    `templates` (works but matches less accurately at the same scale)."""
    if not det.found:
        return []
    mx, my, _, _ = det.marker_xywh
    s = det.scale
    H, W = image_rgb.shape[:2]
    if digit_templates is None:
        digit_templates = {ch: v for ch, v in templates.items()
                           if ch.isdigit() or ch == ","}

    out: list[PartyApplyRow] = []
    empties_since_real = 0
    pitch = int(round(REF_ROW_PITCH * s))
    name_line_h = max(10, int(round(REF_ROW_TOP_LINE_H * s)))
    # Build the column-X helpers ONCE per call. They do not depend on row.
    def _col(ref_x: int) -> int:
        return mx + int(round((ref_x - REF_MARKER_LEFT_IN_WINDOW) * s))

    fame_left_for_check = _col(REF_FAME_X[0])
    name_right_for_check = _col(REF_NAME_X[1])
    # Hard worst-case bound: at most this many rows go through the full
    # OCR pipeline per frame. Prevents the 1.5-2s blowup when the gate
    # leaks game-scene rows through (each PaddleOCR call is ~150ms; 5
    # leaks already double the frame budget).
    MAX_OCR_PER_FRAME = 8
    ocr_rows_used = 0
    for i, row_top in enumerate(det.rows_top_y):
        row_bot = row_top + pitch - 1
        if row_top >= H:
            break
        row_bot = min(row_bot, H - 1)

        # Fast empty-row gate: a real text row has many small horizontal
        # bright→dark transitions (one per glyph edge). Game content
        # outside the party_apply window — skill bars, character icons,
        # tile lighting — has equally bright pixels but only a handful
        # of large blobs, so its transition count is low. Counting
        # transitions instead of just bright pixels keeps random rows
        # below the visible window from falling into a full OCR pass
        # (each costs ~150ms and added up to 1.5s/frame at low UI
        # scales where rows_top_y extends past the window bottom).
        check_x0 = max(0, fame_left_for_check)
        check_x1 = min(W, name_right_for_check)
        check_y1 = min(H, row_top + pitch)
        if check_x1 - check_x0 >= 10 and check_y1 - row_top >= 5:
            row_strip = image_rgb[row_top:check_y1, check_x0:check_x1]
            gray_max = row_strip.max(axis=2)
            bg = float(np.percentile(gray_max, 25))
            bright = gray_max > bg + 60
            bright_count = int(bright.sum())
            if bright_count < 20:
                empties_since_real += 1
                if out and empties_since_real >= 3:
                    break
                continue
            transitions = int((bright[:, 1:] != bright[:, :-1]).sum())
            if transitions < 30:
                empties_since_real += 1
                if out and empties_since_real >= 3:
                    break
                continue
            # Diagnostic: log every row that passes the gate. Lets us see
            # in user logs whether real text or game-scene leakage is
            # what's burning OCR cycles.
            _logger.debug(
                "row %d gate PASS  bright=%d  transitions=%d", i,
                bright_count, transitions)
        # Hard cap the OCR-attempted row count so a leaky gate cannot
        # snowball into a 2s frame.
        if ocr_rows_used >= MAX_OCR_PER_FRAME:
            empties_since_real += 1
            if out and empties_since_real >= 3:
                break
            continue
        ocr_rows_used += 1

        # Fame and class X ranges are isolated from any other text, so we
        # can safely use a generous Y window that absorbs the +/-15px row
        # drift seen between samples (banner presence + table-bottom AA
        # shifts the data row).
        top_y0 = row_top
        top_y1 = min(H, row_top + (pitch * 3) // 5)
        bot_y0 = max(0, row_top + (pitch * 2) // 5)
        bot_y1 = min(H, row_top + pitch)

        # Name X overlaps with class X (both span the right two-thirds of
        # the column), so a wide Y here would feed 'Neo: Class' glyphs
        # into the name template matcher. Anchor name_y to the actual
        # fame/name line by detecting the topmost bright row inside the
        # row's vertical span.
        name_top = _detect_top_text_y(image_rgb, row_top, row_bot, s)
        if name_top is None:
            name_top = row_top
        name_y0 = max(0, name_top - 1)
        name_y1 = min(H, name_top + name_line_h + 2)

        # X ranges
        adv_x0 = mx + int(round((REF_ADVENTURE_X[0] - REF_MARKER_LEFT_IN_WINDOW + REF_MARKER_LEFT_IN_WINDOW) * s))
        # The marker_x corresponds to column-header text x=38 in ref. Ref column
        # X values were measured against ref-image x. To translate:
        #   image_x_at_scale_s = mx - 38*s + ref_x*s
        # but 38 is REF_MARKER_LEFT_IN_WINDOW. Simplify:
        def col(ref_x: int) -> int:
            return mx + int(round((ref_x - REF_MARKER_LEFT_IN_WINDOW) * s))

        adv_x = (col(REF_ADVENTURE_X[0]), col(REF_ADVENTURE_X[1]))
        fame_x = (col(REF_FAME_X[0]), col(REF_FAME_X[1]))
        name_x = (col(REF_NAME_X[0]), col(REF_NAME_X[1]))
        class_x = (col(REF_CLASS_X[0]), col(REF_CLASS_X[1]))

        # Fame: digits only, skip past the leading star icon.
        fame_x_dig = (
            fame_x[0] + int(round((FAME_STAR_ICON_RIGHT_PAD - FAME_DIGIT_LEFT_BREATHING) * s)),
            fame_x[1] + int(round(FAME_DIGIT_RIGHT_BREATHING * s)),
        )
        fame_value, fame_text, fame_score = _read_fame(
            image_rgb, fame_x_dig, (top_y0, top_y1), digit_templates, s)

        # Character name: top line, after 'Lv. 115 ' prefix. Templates only —
        # names are arbitrary character strings (e.g. '(-_-)') where general
        # OCR has no language prior to anchor on. Tight Y so the row-below
        # 'Neo: Class' glyphs don't bleed into the name OCR.
        name_raw, name_score = _read_text(
            image_rgb, name_x, (name_y0, name_y1), templates)
        name = _strip_lv_prefix(name_raw)

        # Class: bot line, skip past the awakening-tier badge ([B], [D]...).
        class_x_text = (class_x[0] + int(round(CLASS_BADGE_RIGHT_PAD * s)),
                        class_x[1])
        class_raw, class_score = _read_class(
            image_rgb, class_x_text, (bot_y0, bot_y1), templates)
        class_name = REF_FIXED_PREFIX_CLASS_REGEX.sub("", class_raw).strip()

        # Adventure: spans the full row vertically (it's one line, vertically
        # centered in the full row band). Use top-line y range; that's where
        # the adventure name renders.
        adv_raw, adv_score = _read_text(
            image_rgb, adv_x, (top_y0, top_y1), templates)
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
            # Skip empty/banner rows (e.g. sample 01's "Click a slot to
            # remove" tip is positioned between header and the first real
            # row). Stop only after we've seen a few consecutive empties
            # past the last real row — that's a strong signal we've left
            # the data area.
            empties_since_real += 1
            if out and empties_since_real >= 3:
                break
            continue
        empties_since_real = 0
        out.append(row)
    return out


def _read_text(image_rgb: np.ndarray,
               x_range: tuple[int, int],
               y_range: tuple[int, int],
               templates: dict[str, list[Template]]) -> tuple[str, float]:
    H, W = image_rgb.shape[:2]
    x0, x1 = max(0, x_range[0]), min(W, x_range[1])
    y0, y1 = max(0, y_range[0]), min(H, y_range[1])
    if x1 - x0 < 4 or y1 - y0 < 4:
        return "", 0.0
    crop = image_rgb[y0:y1, x0:x1]
    mask = _otsu_mask(crop)
    if (mask > 0).sum() < ROW_MIN_TEXT_PIXELS:
        # Otsu can degenerate when the crop is nearly uniform background
        # (empty row, sub-line above/below text). Fall through to the
        # raid-party masks so we don't drop a borderline-real row.
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


def _read_fame(image_rgb: np.ndarray,
               x_range: tuple[int, int],
               y_range: tuple[int, int],
               digit_templates: dict[str, list[Template]],
               scale: float,
               ) -> tuple[int | None, str, float]:
    """General OCR first, template fallback. Returns (value, text, conf)."""
    H, W = image_rgb.shape[:2]
    if _ocr_fame is not None:
        # EasyOCR works best with a few pixels of vertical breathing room
        # around the digit row.
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
            # Keep the template fallback only if EasyOCR returned nothing
            # at all — preferring a wrong-but-plausible EasyOCR digit string
            # over an even-worse template guess.
            if text:
                return None, text, conf
    text, score, value = _read_digits_via_templates(
        image_rgb, x_range, y_range, digit_templates)
    return value, text, score


def _read_class(image_rgb: np.ndarray,
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


def _read_digits_via_templates(image_rgb: np.ndarray,
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
    # Higher tolerance: party_apply digits sit on a slightly different
    # baseline and can render thinner than raid-party digits.
    text, matches = match_row(mask, boxes, baseline, digit_templates,
                              width_tol=4, height_tol=4)
    digits = "".join(m.char for m in matches if m.char.isdigit())
    if not digits:
        return text, 0.0, None
    mean = float(np.mean([m.score for m in matches if m.char.isdigit()]))
    try:
        value = int(digits)
    except ValueError:
        return digits, mean, None
    if not (10_000 <= value <= 999_999):
        return digits, mean, None
    return digits, mean, value


def _strip_lv_prefix(text: str) -> str:
    """Drop the leading 'Lv. 115 ' prefix; OCR may garble it slightly.

    Tolerant matching: the OCR layer can prepend stray noise glyphs
    (row-edge artifacts) and confuse 1↔I↔l, 5↔S, 0↔O, 8↔B, etc. We accept
    those near-look-alikes as the level digits and drop everything up to
    the end of the prefix.
    """
    s = text.lstrip()
    # The "115" portion can render as "115" / "I15" / "ILS" / "11S" / "tIS" ...
    # Up to 4 characters from a permissive look-alike set after 'Lv'.
    digit_class = r"[\dIlLiSsOoBb]"
    m = re.search(r"[Ll][vVuUyY][.,;:]?\s*" + digit_class + r"{1,4}\s*",
                  s[:14])
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
    parser.add_argument("--ui-scale", type=float, default=69.0,
                        help="DFO UI Scale percent (default 69 — sample's scale)")
    args = parser.parse_args()

    REF_UI_SCALE_PCT = 69.0
    actual = ui_scale_setting_to_factor(args.ui_scale)
    ref_factor = ui_scale_setting_to_factor(REF_UI_SCALE_PCT)
    near = actual / ref_factor

    img = np.array(Image.open(args.image).convert("RGB"))
    det = detect_party_apply(img, near_scale=near)
    print(f"detected={det.found} score={det.score:.3f} scale={det.scale:.3f} "
          f"marker={det.marker_xywh} rows={len(det.rows_top_y)}")
    if not det.found:
        return_code = 1
    else:
        templates = load_default_templates(
            ui_scale=TEMPLATE_SCALE_FOR_PARTY_APPLY * near)
        digit_lib = load_default_templates(
            ui_scale=TEMPLATE_SCALE_FOR_PARTY_APPLY_DIGITS * near)
        digit_templates = {ch: v for ch, v in digit_lib.items()
                           if ch.isdigit() or ch == ","}
        rows = recognize_party_apply(img, det, templates, digit_templates)
        for r in rows:
            print(f"  row {r.index}: fame={r.fame!s:>7} "
                  f"name={r.name!r:20s} class={r.class_name!r:14s} "
                  f"adv={r.adventure!r:14s}")
        return_code = 0
    sys.exit(return_code)
