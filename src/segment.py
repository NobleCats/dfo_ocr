"""Segmentation primitives: ROI → rows → characters.

Designed for the raid party UI initially but the row/char splitters are
generic over any cropped Name-column image.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np


# Raid party UI ROI (default = raid_party_05 at UI Scale 100%).
RAID_PARTY_NAME_X = (330, 478)
RAID_PARTY_DATA_Y = (78, 363)


@dataclass
class Row:
    y_abs: tuple[int, int]   # (top, bottom) in original image coords
    image: np.ndarray        # cropped RGB image of this row
    mask: np.ndarray         # binary text mask (uint8 0/255)
    baseline_y: int = -1     # baseline y within `mask` rows; -1 = unset


@dataclass
class CharBox:
    x: tuple[int, int]       # (left, right) within the row image
    image: np.ndarray        # cropped binary mask of this char (uint8 0/255)


def text_mask(roi_rgb: np.ndarray, offset: float = 60.0) -> np.ndarray:
    """Per-row local threshold: pixel is text if its value exceeds the
    row's background level by `offset`.

    Background level = 25th percentile per row. Median fails when text
    density per row exceeds 50% (e.g. on the cap line of a dense text line),
    where median flips to the foreground value and the threshold rejects
    real text pixels.
    """
    value = roi_rgb.max(axis=2).astype(np.float32)
    row_bg = np.percentile(value, 25, axis=1, keepdims=True)
    return (value > (row_bg + offset)).astype(np.uint8) * 255


def color_text_mask(roi_rgb: np.ndarray, offset: float = 60.0) -> np.ndarray:
    """Alternative mask for dim colored text.

    This is intentionally not the default. It is used as a recognition retry
    candidate and only accepted when template confidence improves.
    """
    rgb = roi_rgb.astype(np.float32)
    value = rgb.max(axis=2)
    chroma = value - rgb.min(axis=2)

    row_bg = np.percentile(value, 25, axis=1, keepdims=True)
    row_hi = np.percentile(value, 90, axis=1, keepdims=True)
    adaptive_offset = np.clip((row_hi - row_bg) * 0.50, 34.0, offset)
    bright = value > (row_bg + adaptive_offset)

    chroma_bg = np.percentile(chroma, 50, axis=1, keepdims=True)
    chroma_hi = np.percentile(chroma, 92, axis=1, keepdims=True)
    chroma_offset = np.clip((chroma_hi - chroma_bg) * 0.40, 22.0, 46.0)
    colored = (chroma > (chroma_bg + chroma_offset)) & (value > (row_bg + 24.0))

    return (bright | colored).astype(np.uint8) * 255


def find_rows(mask: np.ndarray, y_abs_offset: int,
              min_count: int = 6, min_gap: int = 4,
              min_height: int = 6) -> list[tuple[int, int]]:
    """Return contiguous y-bands where mask has enough bright pixels.
    Returns list of (y_top_abs, y_bottom_abs), inclusive bounds."""
    row_text = (mask > 0).sum(axis=1)
    bands: list[tuple[int, int]] = []
    in_band = False
    start = 0
    gap = 0
    for y, c in enumerate(row_text):
        if c >= min_count:
            if not in_band:
                start = y
                in_band = True
            gap = 0
        else:
            if in_band:
                gap += 1
                if gap >= min_gap:
                    end = y - gap
                    if end - start + 1 >= min_height:
                        bands.append((start + y_abs_offset, end + y_abs_offset))
                    in_band = False
                    gap = 0
    if in_band:
        end = len(row_text) - 1 - gap
        if end - start + 1 >= min_height:
            bands.append((start + y_abs_offset, end + y_abs_offset))
    return bands


def find_chars(row_mask: np.ndarray,
               min_gap: int = 1,
               min_width: int = 1) -> list[tuple[int, int]]:
    """Vertical-projection segmentation: find columns containing any text
    pixel, group consecutive columns separated by gaps."""
    col_has = (row_mask > 0).any(axis=0)
    boxes: list[tuple[int, int]] = []
    in_box = False
    start = 0
    gap = 0
    for x, present in enumerate(col_has):
        if present:
            if not in_box:
                start = x
                in_box = True
            gap = 0
        else:
            if in_box:
                gap += 1
                if gap >= min_gap:
                    end = x - gap
                    if end - start + 1 >= min_width:
                        boxes.append((start, end))
                    in_box = False
                    gap = 0
    if in_box:
        end = len(col_has) - 1 - gap
        if end - start + 1 >= min_width:
            boxes.append((start, end))
    return boxes


def auto_split_boxes(boxes: list[tuple[int, int]],
                     mask: np.ndarray,
                     max_char_width: int = 12,
                     min_pixel_count: int = 3) -> list[tuple[int, int]]:
    """Width-based segmentation cleanup for inference (no GT available).

    - Drops boxes with too few text pixels (segmentation noise).
    - Splits boxes wider than `max_char_width` at the local-minimum-density
      column, recursing until all boxes fit.

    A pixel-count filter (rather than min width) preserves narrow legitimate
    glyphs like 'l' (width 1 but ~10+ pixels tall) while dropping isolated
    1-2 pixel noise blobs.
    """
    out: list[tuple[int, int]] = []
    for l, r in boxes:
        glyph = mask[:, l:r + 1]
        if (glyph > 0).sum() < min_pixel_count:
            continue
        if r - l + 1 <= max_char_width:
            out.append((l, r))
            continue
        # Split this box at min-density column, recursively.
        col_density = (mask[:, l:r + 1] > 0).sum(axis=0).astype(np.int32)
        margin = max(2, (r - l + 1) // 5)
        interior = col_density[margin:-margin]
        if interior.size == 0:
            out.append((l, r))
            continue
        split_local = margin + int(np.argmin(interior))
        split_x = l + split_local
        left_box = (l, split_x - 1)
        right_box = (split_x + 1, r)
        if left_box[1] < left_box[0] or right_box[1] < right_box[0]:
            out.append((l, r))
            continue
        # Recurse on the halves
        out.extend(auto_split_boxes([left_box, right_box], mask,
                                    max_char_width, min_pixel_count))
    return out


def reconcile_boxes(boxes: list[tuple[int, int]],
                    mask: np.ndarray,
                    expected: int,
                    min_width: int = 2) -> list[tuple[int, int]]:
    """Adjust box list to match an expected character count using GT knowledge.

    Strategy:
    - If too many boxes: drop the narrowest ones (likely noise).
    - If too few: split the widest box at its minimum-density interior column.

    Used during template extraction where we know the target name. At inference
    time we'll use a template-driven matcher instead.
    """
    boxes = list(boxes)

    while len(boxes) > expected:
        # Drop narrowest box
        widths = [r - l + 1 for l, r in boxes]
        i_min = int(np.argmin(widths))
        if widths[i_min] >= min_width and len(boxes) > expected:
            # If the smallest is still wide, try merging two closest neighbors
            gaps = [boxes[k + 1][0] - boxes[k][1] - 1 for k in range(len(boxes) - 1)]
            i_gap = int(np.argmin(gaps))
            boxes[i_gap] = (boxes[i_gap][0], boxes[i_gap + 1][1])
            del boxes[i_gap + 1]
        else:
            del boxes[i_min]

    while len(boxes) < expected:
        widths = [r - l + 1 for l, r in boxes]
        i_max = int(np.argmax(widths))
        l, r = boxes[i_max]
        if r - l + 1 < 4:
            break  # cannot split further
        col_density = (mask[:, l:r + 1] > 0).sum(axis=0).astype(np.int32)
        # Search interior, not at edges
        margin = max(2, (r - l + 1) // 5)
        interior = col_density[margin:-margin]
        if interior.size == 0:
            break
        split_local = margin + int(np.argmin(interior))
        split_x = l + split_local
        # Insert a 1-column gap at split_x (drop that column from both halves)
        left_box = (l, split_x - 1)
        right_box = (split_x + 1, r)
        if left_box[1] - left_box[0] + 1 < 1 or right_box[1] - right_box[0] + 1 < 1:
            break
        boxes[i_max] = left_box
        boxes.insert(i_max + 1, right_box)

    return boxes


def crop_char(row_mask: np.ndarray, box: tuple[int, int]) -> np.ndarray:
    """Extract a character image tightly cropped to its content bounding box
    in BOTH axes, plus the y-offset of the top of the content within the
    original row mask. Returns just the cropped image (no offset)."""
    left, right = box
    glyph = row_mask[:, left:right + 1]
    return tight_crop(glyph)


def tight_crop(glyph: np.ndarray) -> np.ndarray:
    """Crop empty rows and columns around a glyph mask."""
    rows_with = (glyph > 0).any(axis=1)
    cols_with = (glyph > 0).any(axis=0)
    if not rows_with.any() or not cols_with.any():
        return glyph
    top = int(np.argmax(rows_with))
    bot = len(rows_with) - 1 - int(np.argmax(rows_with[::-1]))
    left = int(np.argmax(cols_with))
    right = len(cols_with) - 1 - int(np.argmax(cols_with[::-1]))
    return glyph[top:bot + 1, left:right + 1]


def crop_char_with_offset(row_mask: np.ndarray, box: tuple[int, int],
                          baseline_y: int) -> tuple[np.ndarray, int]:
    """Tight-crop a glyph and also report its bottom y relative to the row
    baseline. Lets the matcher align glyphs by baseline rather than top."""
    left, right = box
    glyph = row_mask[:, left:right + 1]
    rows_with = (glyph > 0).any(axis=1)
    cols_with = (glyph > 0).any(axis=0)
    if not rows_with.any() or not cols_with.any():
        return glyph, 0
    top = int(np.argmax(rows_with))
    bot = len(rows_with) - 1 - int(np.argmax(rows_with[::-1]))
    left_t = int(np.argmax(cols_with))
    right_t = len(cols_with) - 1 - int(np.argmax(cols_with[::-1]))
    cropped = glyph[top:bot + 1, left_t:right_t + 1]
    # Distance from glyph bottom to baseline. Negative means glyph descends
    # below baseline (descender); positive means glyph ends above baseline.
    bottom_to_baseline = baseline_y - bot
    return cropped, bottom_to_baseline


# Fixed-grid window dimensions for raid party UI (default scale).
# Each row's window is sized to capture the full font extent and is anchored
# at the BASELINE so that templates from different rows align regardless of
# whether the row contains descenders.
RAID_PARTY_ROW_ABOVE_BASELINE = 12   # cap-height + small margin
RAID_PARTY_ROW_BELOW_BASELINE = 3    # max descender depth + margin


def detect_baseline(band_mask: np.ndarray) -> int:
    """Return the y index (within the band mask) of the baseline.

    Baseline = lowest row whose horizontal projection is >= 50% of the peak.
    Descender rows (e.g., for 'g', 'p') have sparse pixels and fall below
    the threshold."""
    proj = (band_mask > 0).sum(axis=1).astype(np.float32)
    p_max = float(proj.max())
    if p_max == 0:
        return band_mask.shape[0] - 1
    above = proj >= (0.5 * p_max)
    indices = np.where(above)[0]
    return int(indices[-1])


def segment_raid_party(image_rgb: np.ndarray,
                       name_col_xywh: tuple[int, int, int, int] | None = None,
                       scale: float = 1.0,
                       y_scale: float | None = None) -> list[Row]:
    """Find data rows in the raid party Name column.

    `name_col_xywh`: (x, y, w, h) in input image coordinates. If None,
    falls back to the reference-scale defaults from raid_party_01.png.
    `scale`: ratio versus reference. `y_scale` can override the vertical
    ratio for captures that are stretched only on the Y axis.
    """
    if y_scale is None:
        y_scale = scale
    if name_col_xywh is None:
        x0, x1 = RAID_PARTY_NAME_X
        y0, y1 = RAID_PARTY_DATA_Y
    else:
        x, y, w, h = name_col_xywh
        x0, x1 = x, x + w
        y0, y1 = y, y + h
    above = max(1, int(round(RAID_PARTY_ROW_ABOVE_BASELINE * y_scale)))
    below = max(0, int(round(RAID_PARTY_ROW_BELOW_BASELINE * y_scale)))

    name_col = image_rgb[y0:y1, x0:x1]
    mask = text_mask(name_col)
    # Scale-adjust band detection thresholds for larger fonts.
    min_count = max(3, int(round(6 * scale)))
    bands = find_rows(mask, y_abs_offset=y0, min_count=min_count)
    rows: list[Row] = []
    for top, bot in bands:
        band_local = mask[top - y0:bot - y0 + 1]
        baseline_local = detect_baseline(band_local)
        baseline_abs = top + baseline_local

        win_top = baseline_abs - above + 1
        win_bot = baseline_abs + below + 1  # exclusive
        win_top = max(y0, win_top)
        win_bot = min(y1, win_bot)
        row_rgb = image_rgb[win_top:win_bot, x0:x1]
        row_mask = text_mask(row_rgb)
        baseline_in_window = baseline_abs - win_top
        rows.append(Row(y_abs=(win_top, win_bot - 1),
                        image=row_rgb, mask=row_mask,
                        baseline_y=baseline_in_window))
    return rows
