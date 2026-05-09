"""Find text rows in the chat screenshot via row brightness profile."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))
from segment import text_mask, find_rows  # noqa

im = np.array(Image.open(ROOT / "samples" / "chat_ascii_01.png").convert("RGB"))
H, W, _ = im.shape
print(f"image: {W}x{H}")

# Try the existing text_mask on the entire image
mask = text_mask(im, offset=60)
Image.fromarray(mask).save(ROOT / "scratch" / "chat_mask.png")

bands = find_rows(mask, y_abs_offset=0, min_count=8)
print(f"\ndetected {len(bands)} text bands:")
for i, (a, b) in enumerate(bands, 1):
    print(f"  band {i:2d}: y={a:3d}..{b:3d}  height={b-a+1}")
