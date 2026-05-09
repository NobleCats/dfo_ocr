"""Extract the new column-header marker from raid_party_04 (100% UI scale).

Also finds Name column ROI relative to window.
"""
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
im = np.array(Image.open(ROOT / "samples" / "raid_party_04.png").convert("RGB"))
H, W, _ = im.shape

# Header location: visually identified at y=140..165, full content from x~40 to x~800
# Try different x ranges and visualize
for (x0, x1) in [(40, 720), (40, 730), (45, 715), (40, 700)]:
    crop = im[140:165, x0:x1]
    Image.fromarray(crop).save(ROOT / "scratch" / f"hdr_x{x0}-{x1}.png")
    print(f"saved hdr crop x={x0}..{x1}  shape={crop.shape}")

# Find precise window bbox: look at the dark frame + title bar
# Window appears to span almost the whole image. Let's check if there's
# a top title bar (like "25NightCord") and where it starts.
# Title bar: dark blue top strip
print("\nFirst 30 rows mean brightness:")
for y in range(30):
    avg = im[y].mean()
    print(f"  y={y:2d} avg={avg:5.1f}")
