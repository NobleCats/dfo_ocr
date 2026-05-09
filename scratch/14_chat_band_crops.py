"""Crop and visualize each detected band from chat_ascii_01.png."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))

im = np.array(Image.open(ROOT / "samples" / "chat_ascii_01.png").convert("RGB"))

bands = [(13, 34), (43, 54), (79, 94), (103, 118), (127, 146),
         (163, 175), (187, 208), (225, 232)]
out = ROOT / "scratch"
for i, (a, b) in enumerate(bands, 1):
    crop = im[a:b + 1]
    big = np.kron(crop, np.ones((3, 3, 1), dtype=np.uint8))
    Image.fromarray(big).save(out / f"chat_band_{i:02d}.png")
    print(f"saved chat_band_{i:02d}.png  y={a}..{b}")
