"""High-level recognition API.

Top-level entry point: `recognize_raid_party(image_rgb, templates)` returns
a list of (name, confidence) for each row found in the raid party window.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re
import numpy as np
from PIL import Image

from segment import (segment_raid_party, find_chars, reconcile_boxes,
                     color_text_mask, text_mask, detect_baseline)
from match import match_row, Match
from templates import Template, load_templates
from detect import detect_raid_window, detect_raid_window_with_y_candidates, WindowDetection
from resources import resource_path


def ui_scale_setting_to_factor(value: float) -> float:
    """Convert DFO's UI Scale setting to actual render scale.

    Based on measured samples:
        UI Scale 0%   -> ~0.50x
        UI Scale 50%  -> ~0.6667x
        UI Scale 100% -> ~1.00x

    A quadratic through those points gives:
        actual_scale = 0.5 + x / 6 + x^2 / 3, where x = setting / 100

    `value` is the UI Scale setting shown in game, from 0 to 100.
    """
    if not 0.0 <= value <= 100.0:
        raise ValueError(f"UI Scale setting must be 0..100, got {value:g}")
    x = value / 100.0
    return 0.5 + (x / 6.0) + ((x * x) / 3.0)


def normalize_ocr_name(name: str) -> str:
    name = name.replace("|", "l")
    name = re.sub(r"^lz", "Iz", name)
    return re.sub(r"J(?=\d)", "1", name)


def normalize_ocr_matches(matches: list[Match]) -> str:
    chars = [m.char for m in matches]
    return normalize_ocr_name("".join(chars))


@dataclass
class RecognizedRow:
    y_abs: tuple[int, int]
    name: str
    char_matches: list[Match]

    @property
    def min_score(self) -> float:
        if not self.char_matches:
            return 0.0
        return min(m.score for m in self.char_matches)

    @property
    def mean_score(self) -> float:
        if not self.char_matches:
            return 0.0
        return float(np.mean([m.score for m in self.char_matches]))


@dataclass
class RecognizedFame:
    y_abs: tuple[int, int]
    fame: int | None
    text: str
    mean_score: float


# Reference Fame column inside the detected raid window. The left edge starts
# after the "Fame" header padding to avoid party icon artifacts.
REF_FAME_COL = (235, 78, 326, 363)


class RowCache:
    """Maps row-mask bytes -> (name, matches). Steady-state UI rows hash the
    same across frames, so recognition becomes a dict lookup."""
    def __init__(self, capacity: int = 256):
        self._cache: dict[bytes, tuple[str, list[Match]]] = {}
        self._capacity = capacity

    def get(self, row_mask: np.ndarray):
        return self._cache.get(row_mask.tobytes())

    def set(self, row_mask: np.ndarray, name: str, matches: list[Match]) -> None:
        if len(self._cache) >= self._capacity:
            self._cache.pop(next(iter(self._cache)))
        self._cache[row_mask.tobytes()] = (name, matches)


def _match_mask(mask: np.ndarray,
                baseline_y: int,
                templates: dict[str, list[Template]],
                *,
                expected: int | None = None) -> tuple[str, list[Match]]:
    raw_boxes = find_chars(mask)
    if expected is not None:
        boxes = reconcile_boxes(raw_boxes, mask, expected=expected)
    else:
        # Keep connected glyph clusters intact here. match_row has access to
        # templates and can split wide clusters more safely than width-only
        # projection logic.
        boxes = raw_boxes
    name, matches = match_row(mask, boxes, baseline_y, templates)
    return normalize_ocr_matches(matches), matches


def _mean_score(matches: list[Match]) -> float:
    if not matches:
        return 0.0
    return float(np.mean([m.score for m in matches]))


def _min_score(matches: list[Match]) -> float:
    if not matches:
        return 0.0
    return min(m.score for m in matches)


def recognize_raid_party(image_rgb: np.ndarray,
                         templates: dict[str, list[Template]],
                         *,
                         name_col_xywh: tuple[int, int, int, int] | None = None,
                         scale: float = 1.0,
                         y_scale: float | None = None,
                         expected_lengths: list[int] | None = None,
                         min_mean_score: float = 0.55,
                         drop_negative_min: bool = True,
                         row_cache: RowCache | None = None,
                         ) -> list[RecognizedRow]:
    """Run recognition on a screenshot. If `name_col_xywh` is provided
    (e.g. from `detect_raid_window`), segments inside that ROI; otherwise
    uses reference-scale defaults.

    Low-confidence rows (faint placeholders, partially-rendered text in
    empty slots) are filtered out. Tunable via `min_mean_score` and
    `drop_negative_min`.
    """
    if y_scale is None:
        y_scale = scale
    rows = segment_raid_party(image_rgb, name_col_xywh=name_col_xywh,
                              scale=scale, y_scale=y_scale)
    out: list[RecognizedRow] = []
    for i, row in enumerate(rows):
        if row_cache is not None:
            hit = row_cache.get(row.mask)
        else:
            hit = None
        if hit is not None:
            name, matches = hit
        else:
            expected = (expected_lengths[i] if expected_lengths is not None and
                        i < len(expected_lengths) else None)
            name, matches = _match_mask(row.mask, row.baseline_y, templates,
                                        expected=expected)
            base_mean = _mean_score(matches)
            base_min = _min_score(matches)
            should_try_color_mask = base_mean < 0.82 or base_min < 0.65
            alt_mask = color_text_mask(row.image) if should_try_color_mask else row.mask
            if should_try_color_mask and alt_mask.shape == row.mask.shape and not np.array_equal(alt_mask, row.mask):
                alt_name, alt_matches = _match_mask(alt_mask, row.baseline_y,
                                                    templates, expected=expected)
                alt_mean = _mean_score(alt_matches)
                # The alternative mask is allowed to win only when it clearly
                # improves confidence. This preserves the stable default path.
                if (alt_mean > base_mean + 0.06 and
                        _min_score(alt_matches) >= base_min - 0.03):
                    name, matches = alt_name, alt_matches
            if row_cache is not None:
                row_cache.set(row.mask, name, matches)
        rec = RecognizedRow(y_abs=row.y_abs, name=name, char_matches=matches)
        if drop_negative_min and rec.min_score < 0:
            continue
        if rec.mean_score < min_mean_score:
            continue
        out.append(rec)
    return out


def recognize_raid_fames(image_rgb: np.ndarray,
                         det: WindowDetection,
                         rows: list[RecognizedRow],
                         templates: dict[str, list[Template]],
                         *,
                         min_digits: int = 5,
                         min_mean_score: float = 0.45,
                         ) -> dict[tuple[int, int], RecognizedFame]:
    """Read the Fame column for already-segmented raid rows.

    The row y-ranges come from name segmentation, which is more reliable than
    trying to segment the Fame column independently. Fame text is numeric and
    much less ambiguous than character names, so it can be used to constrain
    remote lookup candidates.
    """
    wx, _wy, _ww, _wh = det.window_xywh
    s = det.scale
    x0 = wx + int(round(REF_FAME_COL[0] * s))
    x1 = wx + int(round(REF_FAME_COL[2] * s))
    x0 = max(0, min(image_rgb.shape[1], x0))
    x1 = max(0, min(image_rgb.shape[1], x1))
    if x1 <= x0:
        return {}

    digit_templates = {
        ch: variants for ch, variants in templates.items()
        if ch.isdigit() or ch == ","
    }
    out: dict[tuple[int, int], RecognizedFame] = {}
    for row in rows:
        y0, y1 = row.y_abs
        y0 = max(0, min(image_rgb.shape[0], y0))
        y1 = max(0, min(image_rgb.shape[0] - 1, y1))
        if y1 < y0:
            continue
        crop = image_rgb[y0:y1 + 1, x0:x1]
        mask = text_mask(crop, offset=35.0)
        boxes = find_chars(mask)
        if not boxes:
            continue
        _text, matches = match_row(mask, boxes, detect_baseline(mask),
                                   digit_templates, width_tol=2, height_tol=2)
        digit_matches = [m for m in matches if m.char.isdigit()]
        digits = "".join(m.char for m in digit_matches)
        mean = float(np.mean([m.score for m in digit_matches])) if digit_matches else 0.0
        fame = None
        if len(digits) >= min_digits and mean >= min_mean_score:
            try:
                value = int(digits)
            except ValueError:
                value = 0
            if 30_000 <= value <= 999_999:
                fame = value
        out[row.y_abs] = RecognizedFame(
            y_abs=row.y_abs,
            fame=fame,
            text=digits,
            mean_score=mean,
        )
    return out


def detect_and_recognize(image_rgb: np.ndarray,
                         templates: dict[str, list[Template]],
                         *,
                         hint: tuple[WindowDetection, float] | WindowDetection | None = None,
                         row_cache: RowCache | None = None,
                         template_scale: float = 1.0,
                         y_factor_filter: float | None = None,
                         near_scale: float | None = None,
                         ) -> tuple[WindowDetection, list[RecognizedRow]]:
    """End-to-end: find raid window in image, then recognize names within.

    `hint`: previous frame's detection. Speeds up steady-state detection.
    `row_cache`: shared cache so identical rows across frames skip matching.
    `y_factor_filter`: pinned Y-doubling factor (1.0 or 2.0). Once known,
    cold scans skip the irrelevant Y-candidates.
    `near_scale`: pinned UI scale; cold scans probe only a narrow band
    around it. Pass when the user's UI Scale setting is known. Leave None
    for ad-hoc screenshot tests where the sample's scale may not match
    the configured templates.

    `template_scale` is the UI Scale used to build the in-memory template
    library. When it matches the detected window scale, recognition runs on
    the native capture. Otherwise we keep the old 1x fallback for ad-hoc
    screenshot tests.
    """
    det, y_factor = detect_raid_window_with_y_candidates(
        image_rgb, hint=hint, y_factor_filter=y_factor_filter,
        near_scale=near_scale)
    setattr(det, "y_factor", y_factor)
    if not det.found:
        return det, []

    s = det.scale
    if y_factor != 1.0:
        x, y, w, h = det.name_col_xywh
        name_col = (x, int(round(y * y_factor)), w, int(round(h * y_factor)))
        rows = recognize_raid_party(image_rgb, templates,
                                    name_col_xywh=name_col,
                                    scale=s, y_scale=s * y_factor,
                                    row_cache=row_cache)
        return det, rows

    if abs(s - template_scale) < 0.08:
        rows = recognize_raid_party(image_rgb, templates,
                                    name_col_xywh=det.name_col_xywh, scale=s,
                                    row_cache=row_cache)
        return det, rows

    # Resize the cropped Name column to scale 1.0 for recognition.
    x, y, w, h = det.name_col_xywh
    crop = image_rgb[y:y + h, x:x + w]
    new_w = max(1, int(round(w / s)))
    new_h = max(1, int(round(h / s)))
    crop_1x = np.array(Image.fromarray(crop).resize((new_w, new_h), Image.LANCZOS))
    rows_1x = recognize_raid_party(crop_1x, templates,
                                   name_col_xywh=(0, 0, new_w, new_h),
                                   scale=1.0, row_cache=row_cache)
    # Map y_abs (which is in crop_1x coords) back to input coords.
    rows_mapped: list[RecognizedRow] = []
    for r in rows_1x:
        y0_1x, y1_1x = r.y_abs
        y0_in = y + int(round(y0_1x * s))
        y1_in = y + int(round(y1_1x * s))
        rows_mapped.append(RecognizedRow(
            y_abs=(y0_in, y1_in), name=r.name, char_matches=r.char_matches))
    return det, rows_mapped


def _scaled_template(tpl: Template, scale: float) -> Template:
    h, w = tpl.image.shape
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    img = Image.fromarray(tpl.image).resize((new_w, new_h), Image.LANCZOS)
    arr = np.array(img)
    binary = (arr > 100).astype(np.uint8) * 255
    rows_with = (binary > 0).any(axis=1)
    cols_with = (binary > 0).any(axis=0)
    if rows_with.any() and cols_with.any():
        top = int(np.argmax(rows_with))
        bot = len(rows_with) - 1 - int(np.argmax(rows_with[::-1]))
        left = int(np.argmax(cols_with))
        right = len(cols_with) - 1 - int(np.argmax(cols_with[::-1]))
        binary = binary[top:bot + 1, left:right + 1]
    return Template(char=tpl.char, image=binary,
                    bottom_to_baseline=int(round(tpl.bottom_to_baseline * scale)))


def scale_templates(templates: dict[str, list[Template]],
                    scale: float) -> dict[str, list[Template]]:
    """Return a de-duplicated template library scaled from 100% UI."""
    if abs(scale - 1.0) < 1e-3:
        return templates
    out: dict[str, list[Template]] = {}
    for ch, variants in templates.items():
        seen = set()
        scaled: list[Template] = []
        for tpl in variants:
            st = _scaled_template(tpl, scale)
            key = (st.image.shape, st.bottom_to_baseline, st.image.tobytes())
            if key in seen:
                continue
            seen.add(key)
            scaled.append(st)
        out[ch] = scaled
    return out


def load_default_templates(ui_scale: float = 1.0) -> dict[str, list[Template]]:
    """Load the production library and scale it for the user's UI Scale."""
    merged = resource_path("templates", "merged", "default")
    raid_dir = resource_path("templates", "raid_party", "default")
    chat_dir = resource_path("templates", "chat", "default")

    if abs(ui_scale - 1.0) < 1e-3 and (merged / "manifest.json").exists():
        return load_templates(merged)

    # Only the chat library is a 100% source library. The raid_party library
    # contains native captures from earlier non-100% samples, so keep those
    # variants unscaled as supplemental exact matches.
    templates = scale_templates(load_templates(chat_dir), ui_scale)
    if (raid_dir / "manifest.json").exists():
        raid = load_templates(raid_dir)
        for ch, variants in raid.items():
            if ch == "B":
                continue
            templates.setdefault(ch, []).extend(variants)
    return templates


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?", default=None)
    parser.add_argument("--ui-scale", type=float, default=100.0,
                        help="DFO UI Scale setting percent (default: 100)")
    args = parser.parse_args()

    path = (Path(args.image) if args.image else
            Path(__file__).resolve().parent.parent / "samples" / "raid_party_01.png")
    ui_scale = ui_scale_setting_to_factor(args.ui_scale)
    im = np.array(Image.open(path).convert("RGB"))
    tpls = load_default_templates(ui_scale=ui_scale)
    det, results = detect_and_recognize(im, tpls, template_scale=ui_scale)
    print(f"detected={det.found} score={det.score:.3f} "
          f"scale={det.scale:.2f} ui_scale={ui_scale:.2f} "
          f"name_col={det.name_col_xywh}")
    print(f"recognized {len(results)} rows from {path.name}:")
    for i, r in enumerate(results, 1):
        print(f"  {i:2d}. {r.name!r:20s}  "
              f"min={r.min_score:.2f}  mean={r.mean_score:.2f}")
