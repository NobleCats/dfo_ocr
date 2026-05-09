"""Find the Name column x-range and per-row y-ranges in the raid party UI."""
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
SAMPLE = ROOT / "samples" / "raid_party_01.png"
OUT = ROOT / "scratch"

im = np.array(Image.open(SAMPLE).convert("RGB"))
H, W, _ = im.shape
print(f"image: {W}x{H}")

gray = im.mean(axis=2)

# Strategy: the data rows have a dark-but-not-black background and are repeated.
# Find rows of text by detecting bright pixels (text is light on dark bg).
# Threshold high to isolate text glyphs.
text_mask = gray > 180

# Sum bright pixels per row → text rows show clear bumps
row_sum = text_mask.sum(axis=1)
col_sum = text_mask.sum(axis=0)

# Print row activity to identify the data rows
print("\n--- bright-pixel count per row (showing y where count > 5) ---")
last_active = -10
for y, c in enumerate(row_sum):
    if c > 5:
        if y - last_active > 3:
            print()
        print(f"y={y:3d}  count={c}")
        last_active = y

# Save column activity
print("\n--- bright-pixel count per column (every 5 cols, count > 3) ---")
for x in range(0, W, 5):
    if col_sum[x] > 3:
        print(f"x={x:3d}  count={col_sum[x]}")
