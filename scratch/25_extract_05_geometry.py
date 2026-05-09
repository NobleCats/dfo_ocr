"""Find the column-header strip and Name column ROI in raid_party_05 (100% UI scale)."""
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
im = np.array(Image.open(ROOT / "samples" / "raid_party_05.png").convert("RGB"))
H, W, _ = im.shape
print(f"raid_party_05: {W}x{H}")

# Find header strip y-range. The header has a distinctive pattern: gold text
# on dark background. We look for a row band where many "white-ish" pixels
# appear in a horizontal strip.
gray = im.mean(axis=2)
text_mask = gray > 130   # gold text passes 130
strip_count = text_mask.sum(axis=1)

# Print y rows with significant content (likely header/data rows)
print("\ny rows with strong text content (count > 30):")
for y in range(H):
    if strip_count[y] > 30:
        print(f"  y={y:3d}  count={strip_count[y]}")
