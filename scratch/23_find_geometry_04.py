"""Find precise window/header/Name-column geometry in raid_party_04."""
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
im = np.array(Image.open(ROOT / "samples" / "raid_party_04.png").convert("RGB"))
H, W, _ = im.shape

# Find left/right window edges by looking at the header row's bright edges.
# The header strip has a distinct horizontal pattern (Party Level Fame Name Class).
# Sample row y=150 (mid-header).
row = im[150].astype(np.float32).mean(axis=1)
print(f"Row 150 brightness range: {row.min():.0f} .. {row.max():.0f}")

# Find first and last "non-edge" columns (high contrast vs neighbors)
# Use a brightness threshold to find content vs frame
thresh = 30
content_cols = np.where(row > thresh)[0]
if len(content_cols):
    print(f"y=150 content x range: {content_cols.min()} .. {content_cols.max()}")

# Look at first data row (y around 175-220) to find Name column position
# Crop wider for visibility
crop = im[140:220, 0:W]
Image.fromarray(crop).save(ROOT / "scratch" / "raid04_header_data.png")
print(f"saved header+first-row crop")

# Also, find the Join Raid button at bottom
print("\nLooking for join raid button at bottom:")
for y in range(H - 100, H - 10, 2):
    row = im[y].mean(axis=1)
    if row.max() > 100:
        bright_cols = np.where(row > 100)[0]
        if len(bright_cols) > 50:
            print(f"  y={y}: bright x range {bright_cols.min()}..{bright_cols.max()}")
