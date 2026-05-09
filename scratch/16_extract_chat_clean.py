"""Extract templates from clean-background chat screenshot.

Lines (ground truth, top to bottom):
    abcdefghijklmnopqrstuv
    wxyz
    ABCDEFGHIJKLMNOPQR
    STUVWXYZ
    0123456789
    ~`!@#$^&*()-_+=|{}[]
    :;,./

Total: 26 lower + 26 upper + 10 digit + 25 special = 87 chars.
"""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))
from segment import (detect_baseline, find_chars, reconcile_boxes,
                     crop_char_with_offset)  # noqa
from templates import Template  # noqa

GT_LINES = [
    "abcdefghijklmnopqrstuv",
    "wxyz",
    "ABCDEFGHIJKLMNOPQR",
    "STUVWXYZ",
    "0123456789",
    "~`!@#$^&*()-_+=|{}[]",
    ":;,./",
]

# Chat font is larger than raid-party Name column. Use a generous window
# anchored at the detected baseline so ascenders/descenders aren't clipped.
ROW_ABOVE_BASELINE = 18
ROW_BELOW_BASELINE = 6


def chat_mask(image_rgb: np.ndarray, threshold: int = 80) -> np.ndarray:
    """Mask the clean chat sample: white text on a black background."""
    gray = image_rgb.mean(axis=2)
    return (gray > threshold).astype(np.uint8) * 255


def find_chat_bands(mask: np.ndarray) -> list[tuple[int, int]]:
    """Find the seven text rows in chat_ascii_02.png.

    The source capture is not aligned, and sparse punctuation can split into
    multiple horizontal bands. Keep the logic explicit so a bad extraction
    fails loudly instead of silently creating poor templates.
    """
    row_counts = (mask > 0).sum(axis=1)
    bands: list[tuple[int, int]] = []
    in_band = False
    start = 0
    for y, count in enumerate(row_counts):
        if count >= 3 and not in_band:
            start = y
            in_band = True
        elif in_band and count < 3:
            bands.append((start, y - 1))
            in_band = False
    if in_band:
        bands.append((start, len(row_counts) - 1))

    # Merge tiny same-line fragments. In this sample, Q's descender creates a
    # nearby fragment in the A-R line.
    merged: list[tuple[int, int]] = []
    for band in bands:
        if merged and band[0] - merged[-1][1] - 1 <= 3:
            merged[-1] = (merged[-1][0], band[1])
        else:
            merged.append(band)

    # The final punctuation row is sparse enough to split around the comma and
    # slash. Merge only this trailing pair; the STUVWXYZ row above is close but
    # intentionally separate.
    if len(merged) == len(GT_LINES) + 1:
        merged = merged[:-2] + [(merged[-2][0], merged[-1][1])]
    return merged


SPECIAL_LINE_BOXES = [
    (8, 21),    # ~
    (38, 39),   # `
    (44, 45),   # !
    (46, 61),   # @
    (64, 77),   # #
    (80, 89),   # $
    (92, 97),   # ^
    (100, 117), # &
    (120, 131), # *
    (134, 139), # (
    (142, 147), # )
    (150, 155), # -
    (158, 175), # _
    (176, 183), # +
    (186, 199), # =
    (204, 205), # |
    (210, 217), # {
    (220, 227), # }
    (230, 235), # [
    (238, 243), # ]
]


def main():
    im = np.array(Image.open(ROOT / "samples" / "chat_ascii_02.png").convert("RGB"))
    H, W, _ = im.shape
    print(f"image: {W}x{H}")

    mask_all = chat_mask(im)
    bands = find_chat_bands(mask_all)
    print(f"detected {len(bands)} bands (expected {len(GT_LINES)}):")
    for i, (a, b) in enumerate(bands, 1):
        print(f"  band {i:2d}: y={a:3d}..{b:3d}  height={b-a+1}")

    if len(bands) != len(GT_LINES):
        print("\n!! band count mismatch -- investigate before extracting")
        return

    templates: dict[str, list[Template]] = {}
    seen_shapes: dict[str, set] = {}

    for line_idx, ((band_top, band_bot), gt) in enumerate(zip(bands, GT_LINES)):
        # Detect baseline within the band, then build a fixed-height window.
        band_local = mask_all[band_top:band_bot + 1]
        baseline_local = detect_baseline(band_local)
        baseline_abs = band_top + baseline_local

        win_top = max(0, baseline_abs - ROW_ABOVE_BASELINE + 1)
        win_bot = min(H, baseline_abs + ROW_BELOW_BASELINE + 1)
        row_mask = mask_all[win_top:win_bot]
        baseline_in_window = baseline_abs - win_top

        if gt == "~`!@#$^&*()-_+=|{}[]":
            raw_boxes = SPECIAL_LINE_BOXES
            boxes = raw_boxes
        else:
            raw_boxes = find_chars(row_mask)
            boxes = reconcile_boxes(raw_boxes, row_mask, expected=len(gt))
        if len(boxes) != len(gt):
            print(f"\n!! line {line_idx} ({gt!r}): got {len(boxes)} boxes, "
                  f"expected {len(gt)}")
            print(f"   raw_box widths: {[r-l+1 for l,r in raw_boxes]}")
            print(f"   reconciled widths: {[r-l+1 for l,r in boxes]}")
            return

        # Extract each character
        for ch, box in zip(gt, boxes):
            glyph, b2b = crop_char_with_offset(row_mask, box, baseline_in_window)
            shape_key = (glyph.shape, b2b, glyph.tobytes())
            if ch in seen_shapes and shape_key in seen_shapes[ch]:
                continue
            seen_shapes.setdefault(ch, set()).add(shape_key)
            templates.setdefault(ch, []).append(
                Template(char=ch, image=glyph, bottom_to_baseline=b2b))

    total = sum(len(v) for v in templates.values())
    print(f"\nextracted {len(templates)} unique chars, {total} templates total")

    # Save to a separate library so we don't clobber raid_party
    out_dir = ROOT / "templates" / "chat" / "default"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clear existing files
    for f in out_dir.glob("*.png"):
        f.unlink()
    if (out_dir / "manifest.json").exists():
        (out_dir / "manifest.json").unlink()

    from templates import save_templates  # noqa
    save_templates(out_dir, templates)
    print(f"saved to {out_dir}")

    # Print summary
    print("\nper-character variants:")
    for ch in sorted(templates.keys(), key=lambda c: (c.isalpha(), c.isupper(), c)):
        v = templates[ch]
        shapes = [f"{t.width}x{t.height}" for t in v]
        print(f"  {ch!r:6s}: {len(v)} variant(s) {shapes}")


if __name__ == "__main__":
    main()
