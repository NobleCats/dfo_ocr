"""Auto-detect the raid party window in a screenshot.

Uses multi-scale template matching of a fixed UI marker (the column header
strip "Party | Level | Fame | Name | Class") to find the window's position
and scale. From the marker hit, we compute the window bounding box and the
Name column ROI in the input image's coordinate system.

Reference geometry (from samples/raid_party_05_native.png at UI Scale 100%,
Y-collapsed):
    - window size:    898 x 386
    - marker offset within window: (30, 65)
    - marker size:    835 x 13
    - Name column relative to window: x=330..478, y=78..363

`detect_raid_window` returns `scale = 1.0` when the input is at 100% UI
Scale. A user playing at e.g. 69% UI Scale will be detected at scale=0.69.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import cv2
from PIL import Image

from resources import resource_path


@dataclass
class WindowDetection:
    found: bool
    score: float
    scale: float                       # marker scale (1.0 = reference)
    window_xywh: tuple[int, int, int, int]  # window bbox in input image
    name_col_xywh: tuple[int, int, int, int]  # Name column bbox
    name_col_data_y: tuple[int, int]   # data area y range relative to col


# Reference geometry (constants, derived from raid_party_05_native.png at
# UI Scale 100%, Y-collapsed).
REF_WINDOW_W = 898
REF_WINDOW_H = 386
REF_MARKER_OFFSET = (30, 65)   # (x, y) of marker top-left within window
REF_MARKER_SIZE = (835, 13)
REF_NAME_COL = (330, 78, 478, 363)  # (x0, y0, x1, y1) relative to window
REF_NAME_DATA_Y = (78, 363)


_DEFAULT_MARKER_PATH = (
    resource_path("markers", "raid_party", "column_header_100pct.png")
)


def _load_marker(path: Path | None = None) -> np.ndarray:
    p = path or _DEFAULT_MARKER_PATH
    return np.array(Image.open(p).convert("RGB"))


def _build_detection(scale: float, score: float, marker_xy: tuple[int, int],
                     score_threshold: float) -> WindowDetection:
    mx, my = marker_xy
    wx = mx - int(round(REF_MARKER_OFFSET[0] * scale))
    wy = my - int(round(REF_MARKER_OFFSET[1] * scale))
    ww = int(round(REF_WINDOW_W * scale))
    wh = int(round(REF_WINDOW_H * scale))
    ncx0 = wx + int(round(REF_NAME_COL[0] * scale))
    ncy0 = wy + int(round(REF_NAME_COL[1] * scale))
    ncx1 = wx + int(round(REF_NAME_COL[2] * scale))
    ncy1 = wy + int(round(REF_NAME_COL[3] * scale))
    return WindowDetection(
        found=score >= score_threshold,
        score=float(score),
        scale=float(scale),
        window_xywh=(wx, wy, ww, wh),
        name_col_xywh=(ncx0, ncy0, ncx1 - ncx0, ncy1 - ncy0),
        name_col_data_y=(ncy0, ncy1),
    )


def detect_raid_window(image_rgb: np.ndarray,
                       *,
                       min_scale: float = 0.5,
                       max_scale: float = 2.5,
                       scale_step: float = 0.02,
                       score_threshold: float = 0.7,
                       marker: np.ndarray | None = None,
                       hint: WindowDetection | None = None,
                       hint_search_radius: int = 24,
                       cold_fallback: bool = True,
                       near_scale: float | None = None,
                       near_scale_radius: float = 0.1,
                       ) -> WindowDetection:
    """Find the raid party window in `image_rgb`.

    `hint`: a previous frame's detection. When provided, we first try to
    re-confirm the window at the same scale within a small search box around
    the previous marker location. This makes steady-state detection ~10-50x
    faster than the full multi-scale scan.

    `cold_fallback`: when True (default) and a hint is supplied but fails,
    fall through to the full multi-scale scan. Set False when the caller
    plans to retry on a later frame — saves seconds of redundant work on
    high-resolution captures.

    `near_scale`: when set, restrict the multi-scale loop to
    [near_scale - near_scale_radius, near_scale + near_scale_radius]. Use
    this once the user's UI scale is known so cold scans don't probe the
    full 0.5–2.5 range.
    """
    if marker is None:
        marker = _load_marker()
    img_gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    marker_gray = cv2.cvtColor(marker, cv2.COLOR_RGB2GRAY)

    if hint is not None and hint.found:
        scale = hint.scale
        new_w = int(round(marker_gray.shape[1] * scale))
        new_h = int(round(marker_gray.shape[0] * scale))
        if 8 <= new_w <= img_gray.shape[1] and 4 <= new_h <= img_gray.shape[0]:
            wx, wy, _, _ = hint.window_xywh
            mx_prev = wx + int(round(REF_MARKER_OFFSET[0] * scale))
            my_prev = wy + int(round(REF_MARKER_OFFSET[1] * scale))
            x0 = max(0, mx_prev - hint_search_radius)
            y0 = max(0, my_prev - hint_search_radius)
            x1 = min(img_gray.shape[1], mx_prev + new_w + hint_search_radius)
            y1 = min(img_gray.shape[0], my_prev + new_h + hint_search_radius)
            if x1 - x0 >= new_w and y1 - y0 >= new_h:
                resized = cv2.resize(marker_gray, (new_w, new_h),
                                     interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
                roi = img_gray[y0:y1, x0:x1]
                result = cv2.matchTemplate(roi, resized, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                if max_val >= score_threshold:
                    return _build_detection(
                        scale, max_val, (max_loc[0] + x0, max_loc[1] + y0),
                        score_threshold)
        if not cold_fallback:
            return WindowDetection(False, 0.0, hint.scale,
                                   (0, 0, 0, 0), (0, 0, 0, 0), (0, 0))
        # Hint failed → fall through to full scan

    best = WindowDetection(False, 0.0, 1.0, (0, 0, 0, 0), (0, 0, 0, 0), (0, 0))
    # Always anchor to the canonical [min_scale, max_scale] grid so the
    # narrow scan probes the same scales the full scan would. Anchoring to
    # `near_scale` itself produces an off-by-fractional grid that can miss
    # the marker by ~1% (0.774 vs 0.76 in practice → score 0.87 → 0.61).
    full_scales = np.arange(min_scale, max_scale + 1e-6, scale_step)
    if near_scale is not None:
        lo = near_scale - near_scale_radius
        hi = near_scale + near_scale_radius
        scales = full_scales[(full_scales >= lo) & (full_scales <= hi)]
    else:
        scales = full_scales
    for scale in scales:
        new_w = int(round(marker_gray.shape[1] * scale))
        new_h = int(round(marker_gray.shape[0] * scale))
        if new_w < 8 or new_h < 4:
            continue
        if new_w > img_gray.shape[1] or new_h > img_gray.shape[0]:
            continue
        resized = cv2.resize(marker_gray, (new_w, new_h),
                             interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        result = cv2.matchTemplate(img_gray, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val > best.score:
            best = _build_detection(scale, max_val, max_loc, score_threshold)
    return best


def detect_raid_window_with_y_candidates(
    image_rgb: np.ndarray,
    *,
    hint: tuple[WindowDetection, float] | WindowDetection | None = None,
    y_factor_filter: float | None = None,
    near_scale: float | None = None,
) -> tuple[WindowDetection, float]:
    """Detect the raid window, trying Y-collapsed candidates as needed.

    Some captures contain a Y-doubled game viewport inside a larger monitor
    frame, so a global Y-doubling probe can miss it. We try the original
    frame plus both possible collapsed offsets and return the best match.
    The second return value maps detection Y coordinates back to the input
    image (`1.0` for original, `2.0` for collapsed candidates).

    `y_factor_filter`: when set, only Y-candidates matching this factor are
    scanned on the cold path. Once the user's display mode is known
    (1.0 = native, 2.0 = Y-doubled), filtering cuts cold-scan cost ~3x.

    `near_scale`: pinned UI scale (e.g. user's setting). Cold scans probe
    only a narrow band around this value instead of the full 0.5–2.5 range.
    """
    from capture import collapse_y_doubling

    hint_det = hint[0] if isinstance(hint, tuple) else hint
    hint_y_factor = hint[1] if isinstance(hint, tuple) else None

    if hint_det is not None and hint_det.found and hint_y_factor is not None:
        # Hint-only check on this frame: don't fall through to a full scan
        # when hint fails. The next frame, with hint cleared, will run the
        # cold path (throttled and y-filtered) instead — far cheaper than
        # paying full-scan cost on every hint miss.
        if hint_y_factor == 1.0:
            det = detect_raid_window(image_rgb, hint=hint_det,
                                     cold_fallback=False)
        elif hint_y_factor == 2.0:
            det = detect_raid_window(collapse_y_doubling(image_rgb, 0),
                                     hint=hint_det, cold_fallback=False)
            if not det.found:
                det = detect_raid_window(collapse_y_doubling(image_rgb, 1),
                                         hint=hint_det, cold_fallback=False)
        else:
            det = WindowDetection(False, 0.0, 1.0, (0, 0, 0, 0), (0, 0, 0, 0), (0, 0))
        if det.found:
            return det, hint_y_factor
        # Hint missed → bail out cheap. Caller will retry cold next frame.
        return WindowDetection(False, 0.0, 1.0, (0, 0, 0, 0), (0, 0, 0, 0), (0, 0)), hint_y_factor

    all_candidates = [
        (image_rgb, 1.0),
        (collapse_y_doubling(image_rgb, 0), 2.0),
        (collapse_y_doubling(image_rgb, 1), 2.0),
    ]
    if y_factor_filter is None:
        candidates = all_candidates
    else:
        candidates = [c for c in all_candidates if c[1] == y_factor_filter]
        if not candidates:
            candidates = all_candidates
    best_det: WindowDetection | None = None
    best_y_factor = 1.0
    for candidate, y_factor in candidates:
        det = detect_raid_window(candidate, hint=hint_det, near_scale=near_scale)
        if best_det is None or det.score > best_det.score:
            best_det = det
            best_y_factor = y_factor
    assert best_det is not None
    return best_det, best_y_factor


if __name__ == "__main__":
    import sys
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / "samples" / "raid_party_01.png"
    )
    im = np.array(Image.open(path).convert("RGB"))
    result, y_factor = detect_raid_window_with_y_candidates(im)
    print(f"input: {path.name}  size={im.shape[1]}x{im.shape[0]}")
    if y_factor != 1.0:
        print("y-doubled: detected via collapsed candidate")
    print(f"found: {result.found}")
    print(f"score: {result.score:.3f}")
    print(f"scale: {result.scale:.2f}")
    print(f"window xywh: {result.window_xywh}")
    print(f"name col xywh: {result.name_col_xywh}")
