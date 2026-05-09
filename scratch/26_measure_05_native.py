"""Measure header + Name column positions in raid_party_05_native.png (100% UI scale, de-doubled)."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))

im = np.array(Image.open(ROOT / "samples" / "raid_party_05_native.png").convert("RGB"))
H, W, _ = im.shape
print(f"raid_party_05_native: {W}x{H}")

# Header strip: gold text "Party Level Fame Name Class" on dark gradient
# Identify by finding rows with gold-colored pixels (R>180, G>140, B<120 approx)
gold_mask = (im[:, :, 0] > 180) & (im[:, :, 1] > 140) & (im[:, :, 2] < 130)
gold_per_row = gold_mask.sum(axis=1)
print("\nrows with gold pixel count > 30:")
in_band = False
band_start = -1
for y in range(H):
    if gold_per_row[y] > 30:
        if not in_band:
            band_start = y
            in_band = True
    elif in_band:
        print(f"  band y={band_start}..{y-1}  height={y-band_start}  max_count={gold_per_row[band_start:y].max()}")
        in_band = False

# Save crops for visual verification
for y0, y1 in [(75, 95), (78, 96), (80, 100)]:
    Image.fromarray(im[y0:y1]).save(ROOT / "scratch" / f"05native_y{y0}-{y1}.png")
