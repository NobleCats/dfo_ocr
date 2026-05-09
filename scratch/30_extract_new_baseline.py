"""Extract templates from samples/new_chat_ascii_baseline.png.

The new baseline crams the full ASCII set into 4 rows at a smaller font size
than chat_ascii_baseline.png — closer to party_apply UI text. Each row begins
with an explicit '_' anchor so the segmenter has a known leading glyph.

Lines (top to bottom):
    _0123456789
    _abcdefghijklmnopqrstuvwxyz
    _ABCDEFGHIJKLMNOPQRSTUVWXYZ
    _~`!@#$^&*()-_+=|{}[]:;,./

Output: templates/chat_small/default/
"""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from segment import (detect_baseline, find_chars, reconcile_boxes,
                     crop_char_with_offset)  # noqa: E402
from templates import Template, save_templates  # noqa: E402


GT_LINES = [
    "_0123456789",
    "_abcdefghijklmnopqrstuvwxyz",
    "_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "_~`!@#$^&*()-_+=|{}[]:;,./",
]

# Tight band y-ranges measured by row-projection (see analysis above).
LINE_BANDS = [
    (3, 16),    # digits + leading underscore
    (20, 35),   # lowercase
    (39, 53),   # uppercase
    (56, 71),   # specials
]

ROW_ABOVE_BASELINE = 14
ROW_BELOW_BASELINE = 4


def chat_mask(image_rgb: np.ndarray, threshold: int = 160) -> np.ndarray:
    """Higher threshold than the legacy chat_ascii_02 extraction: the new
    baseline is anti-aliased tighter, so glyphs bleed into one another below
    ~140. Pick the lowest threshold that still produces clean per-char gaps."""
    gray = image_rgb.mean(axis=2)
    return (gray > threshold).astype(np.uint8) * 255


def main() -> None:
    src = ROOT / "samples" / "new_chat_ascii_baseline.png"
    img = np.array(Image.open(src).convert("RGB"))
    H, W, _ = img.shape
    print(f"image: {W}x{H}")

    full_mask = chat_mask(img)
    templates: dict[str, list[Template]] = {}
    seen_shapes: dict[str, set] = {}

    for line_idx, ((band_top, band_bot), gt) in enumerate(zip(LINE_BANDS, GT_LINES)):
        band_local = full_mask[band_top:band_bot + 1]
        baseline_local = detect_baseline(band_local)
        baseline_abs = band_top + baseline_local

        win_top = max(0, baseline_abs - ROW_ABOVE_BASELINE + 1)
        win_bot = min(H, baseline_abs + ROW_BELOW_BASELINE + 1)
        row_mask = full_mask[win_top:win_bot]
        baseline_in_window = baseline_abs - win_top

        raw_boxes = find_chars(row_mask)
        boxes = reconcile_boxes(raw_boxes, row_mask, expected=len(gt))
        if len(boxes) != len(gt):
            print(f"!! line {line_idx} ({gt!r}): "
                  f"got {len(boxes)} boxes, expected {len(gt)}")
            print(f"   raw widths: {[r-l+1 for l, r in raw_boxes]}")
            print(f"   reconciled widths: {[r-l+1 for l, r in boxes]}")
            return

        for ch, box in zip(gt, boxes):
            glyph, b2b = crop_char_with_offset(row_mask, box, baseline_in_window)
            shape_key = (glyph.shape, b2b, glyph.tobytes())
            if ch in seen_shapes and shape_key in seen_shapes[ch]:
                continue
            seen_shapes.setdefault(ch, set()).add(shape_key)
            templates.setdefault(ch, []).append(
                Template(char=ch, image=glyph, bottom_to_baseline=b2b))

    total = sum(len(v) for v in templates.values())
    print(f"extracted {len(templates)} unique chars, {total} templates total")

    out_dir = ROOT / "templates" / "chat_small" / "default"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.png"):
        f.unlink()
    if (out_dir / "manifest.json").exists():
        (out_dir / "manifest.json").unlink()
    save_templates(out_dir, templates)
    print(f"saved to {out_dir.relative_to(ROOT)}")

    print("\nper-character variants:")
    for ch in sorted(templates.keys(), key=lambda c: (c.isalpha(), c.isupper(), c)):
        v = templates[ch]
        shapes = [f"{t.width}x{t.height}" for t in v]
        print(f"  {ch!r:6s}: {len(v)} variant(s) {shapes}")


if __name__ == "__main__":
    main()
