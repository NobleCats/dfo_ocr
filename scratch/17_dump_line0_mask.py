"""Dump line 0's mask to see why over-segmentation."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))
from segment import text_mask, find_rows, detect_baseline, find_chars  # noqa

im = np.array(Image.open(ROOT / "samples" / "chat_ascii_02.png").convert("RGB"))
mask_all = text_mask(im, offset=60)
bands = find_rows(mask_all, y_abs_offset=0, min_count=3)

# Line 0
band_top, band_bot = bands[0]
band_local = mask_all[band_top:band_bot + 1]
baseline_local = detect_baseline(band_local)
baseline_abs = band_top + baseline_local

ROW_ABOVE = 18; ROW_BELOW = 6
win_top = max(0, baseline_abs - ROW_ABOVE + 1)
win_bot = min(im.shape[0], baseline_abs + ROW_BELOW + 1)
row_rgb = im[win_top:win_bot]
row_mask = text_mask(row_rgb)

print(f"line 0 window: y={win_top}..{win_bot-1} shape={row_mask.shape}")

# Save mask large
big = np.kron(row_mask, np.ones((4, 4), dtype=np.uint8))
Image.fromarray(big).save(ROOT / "scratch" / "line0_mask.png")

# Print column-wise content
boxes = find_chars(row_mask)
print(f"\nfound {len(boxes)} boxes:")
for i, (l, r) in enumerate(boxes):
    glyph = row_mask[:, l:r + 1]
    print(f"  box {i:2d}: x={l:3d}..{r:3d} (w={r-l+1})  pixels={int((glyph>0).sum())}")

# Also: find columns where col_has changes
col_has = (row_mask > 0).any(axis=0)
print("\ncol_has profile (T/F):")
for x in range(row_mask.shape[1]):
    print("T" if col_has[x] else ".", end="")
print()

# Verify col 89 specifically: print all rows
print(f"\nrow_mask col 89, all rows (window y={win_top}..{win_bot-1}):")
for r in range(row_mask.shape[0]):
    print(f"  row {r:2d} (y={win_top+r:3d}): mask={int(row_mask[r, 89]):3d}  gray={im[win_top+r, 89].mean():.0f}")
print(f"\nrow_mask col 89 sum: {(row_mask[:, 89] > 0).sum()}")
print(f"row median (used for threshold) at row 7: {np.median(im[win_top + 7].mean(axis=-1)):.1f}")
