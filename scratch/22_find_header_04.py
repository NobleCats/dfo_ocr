"""Crop raid_party_04 at multiple y-ranges to find the column header."""
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
SAMPLE = ROOT / "samples" / "raid_party_04.png"
im = np.array(Image.open(SAMPLE).convert("RGB"))

# Crop the top half so we can locate the header visually
top = im[0:280]
Image.fromarray(top).save(ROOT / "scratch" / "raid04_top280.png")
print(f"saved top half ({top.shape[1]}x{top.shape[0]})")

# Also crop a candidate header area
for y0, y1 in [(125, 150), (130, 155), (135, 160), (140, 165), (150, 175)]:
    crop = im[y0:y1]
    Image.fromarray(crop).save(ROOT / "scratch" / f"raid04_y{y0}-{y1}.png")
    print(f"saved y={y0}..{y1}")
