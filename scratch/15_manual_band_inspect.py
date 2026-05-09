"""Manually inspect specific y-ranges to find each text line accurately."""
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
im = np.array(Image.open(ROOT / "samples" / "chat_ascii_01.png").convert("RGB"))
out = ROOT / "scratch"

# Crop the entire image at 3x scale and save with y-grid for inspection
big = np.kron(im, np.ones((3, 3, 1), dtype=np.uint8))
# Add y-axis ruler markers every 10 px (in original coords)
H, W, _ = big.shape
for y_orig in range(0, im.shape[0], 10):
    y_big = y_orig * 3
    if y_big < H:
        big[y_big:y_big + 1, :30] = [255, 0, 0]  # red marker bar at left edge
Image.fromarray(big).save(out / "chat_with_ruler.png")
print(f"saved chat_with_ruler.png  ({W}x{H})")

# Also dump rows 120-160 to find the digits
crop = im[120:165]
Image.fromarray(np.kron(crop, np.ones((3, 3, 1), dtype=np.uint8))).save(out / "chat_y120-165.png")
print(f"saved chat_y120-165.png")

# Dump rows 170-240 to see both special-char lines
crop = im[170:240]
Image.fromarray(np.kron(crop, np.ones((3, 3, 1), dtype=np.uint8))).save(out / "chat_y170-240.png")
print(f"saved chat_y170-240.png")

# Profile brightness per row to identify text rows precisely
gray = im.mean(axis=2)
H = im.shape[0]
print("\nrow brightness profile:")
for y in range(H):
    p = gray[y].max() - gray[y].min()  # contrast within row
    if p > 100:  # rows with high contrast are text rows
        print(f"  y={y:3d}  contrast={p:.0f}  mean={gray[y].mean():.0f}")
