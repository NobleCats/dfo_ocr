"""Test if a lower threshold fixes 'h' segmentation in chat font."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))
from segment import find_rows, detect_baseline, find_chars  # noqa


def text_mask_with_offset(roi_rgb, offset):
    gray = roi_rgb.mean(axis=2).astype(np.float32)
    row_med = np.median(gray, axis=1, keepdims=True)
    return (gray > (row_med + offset)).astype(np.uint8) * 255


im = np.array(Image.open(ROOT / "samples" / "chat_ascii_02.png").convert("RGB"))

# Test different thresholds on line 0
print("Testing thresholds on lowercase line 0 ('abcdefghijklmnopqrstuv'):\n")
for offset in [20, 30, 40, 50, 60]:
    mask_all = text_mask_with_offset(im, offset)
    bands = find_rows(mask_all, y_abs_offset=0, min_count=3)
    if not bands:
        continue
    band_top, band_bot = bands[0]
    band_local = mask_all[band_top:band_bot + 1]
    baseline_local = detect_baseline(band_local)
    baseline_abs = band_top + baseline_local
    win_top = max(0, baseline_abs - 18 + 1)
    win_bot = min(im.shape[0], baseline_abs + 6 + 1)
    row_rgb = im[win_top:win_bot]
    row_mask = text_mask_with_offset(row_rgb, offset)
    boxes = find_chars(row_mask)
    print(f"offset={offset:2d}: {len(boxes)} boxes  widths={[r-l+1 for l,r in boxes]}")

# Also dump the actual gray values at col 89-94 (suspected 'h' arch gap)
print("\nGray values at row=10 (near baseline), cols 75-100:")
gray = im.mean(axis=2)
print("col:", "  ".join(f"{c:3d}" for c in range(75, 101)))
for y in [4, 6, 8, 10, 12, 14, 16, 18]:
    print(f"y={y:2d}:", " ".join(f"{int(gray[y, c]):3d}" for c in range(75, 101)))
