"""Widen the Name column ROI and use a color-distance mask to capture all text colors."""
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
SAMPLE = ROOT / "samples" / "raid_party_01.png"
OUT = ROOT / "scratch"

im = np.array(Image.open(SAMPLE).convert("RGB"))
H, W, _ = im.shape

# Try a wider name column — center is around x=310, width ~140
NAME_X = (245, 380)
DATA_Y = (120, 510)

name_col = im[DATA_Y[0]:DATA_Y[1], NAME_X[0]:NAME_X[1]]
Image.fromarray(name_col).save(OUT / "name_column_wide.png")

# Build a mask of "text-like" pixels: anything notably brighter than the local row mean.
# Convert to gray and look for bright pixels.
gray = name_col.mean(axis=2).astype(np.float32)

# For each row in the cropped image, compute a local threshold = row median + offset
# But median works only if rows have mostly background, which is true here.
row_med = np.median(gray, axis=1, keepdims=True)
mask = gray > (row_med + 60)  # 60 is arbitrary, anything well above background

# Save mask visualization
mask_img = (mask * 255).astype(np.uint8)
Image.fromarray(mask_img).save(OUT / "name_column_mask.png")

# Find row bands with this looser mask
row_text = mask.sum(axis=1)
print("row_text profile:")
for y, c in enumerate(row_text):
    if c > 0:
        print(f"  y_local={y:3d}  y_abs={y+DATA_Y[0]:3d}  count={int(c)}")
