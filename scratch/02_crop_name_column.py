"""Crop the Name column and identify row centers."""
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
SAMPLE = ROOT / "samples" / "raid_party_01.png"
OUT = ROOT / "scratch"

im = np.array(Image.open(SAMPLE).convert("RGB"))
H, W, _ = im.shape

# First crop a wide horizontal slice over the data area
NAME_X = (260, 365)
DATA_Y = (120, 510)

name_col = im[DATA_Y[0]:DATA_Y[1], NAME_X[0]:NAME_X[1]]
Image.fromarray(name_col).save(OUT / "name_column.png")
print(f"saved name_column.png  shape={name_col.shape}")

# Project bright pixels per row (within name column only)
gray = name_col.mean(axis=2)
text_mask = gray > 180
row_text = text_mask.sum(axis=1)

# Find contiguous text bands
bands = []
in_band = False
start = 0
for y, c in enumerate(row_text):
    if c >= 3:
        if not in_band:
            start = y
            in_band = True
    else:
        if in_band:
            bands.append((start + DATA_Y[0], y - 1 + DATA_Y[0]))
            in_band = False
if in_band:
    bands.append((start + DATA_Y[0], len(row_text) - 1 + DATA_Y[0]))

print(f"\nfound {len(bands)} text bands in name column:")
for i, (a, b) in enumerate(bands, 1):
    print(f"  row {i:2d}: y={a}..{b}  height={b-a+1}")

# Also crop and save first row for closer inspection
if bands:
    a, b = bands[0]
    # add 2px padding above/below
    pad = 2
    row0 = im[max(0, a-pad):b+pad+1, NAME_X[0]:NAME_X[1]]
    Image.fromarray(row0).save(OUT / "row01.png")
    print(f"\nsaved row01.png  shape={row0.shape}  (zerkaa)")
