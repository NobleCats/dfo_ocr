"""Extract column-header marker and reference geometry from raid_party_04
at UI Scale 100%."""
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
SAMPLE = ROOT / "samples" / "raid_party_04.png"

im = np.array(Image.open(SAMPLE).convert("RGB"))
H, W, _ = im.shape
print(f"raid_party_04: {W}x{H}")

# Find the column header strip by looking for the dark blue band with
# "Party Level Fame Name Class" labels. Use brightness profile to locate.
gray = im.mean(axis=2)
# Sum brightness across each row
row_avg = gray.mean(axis=1)
# Print profile around expected y for header (top ~10-20% of window)
print("\ny brightness samples (looking for header strip):")
for y in range(0, min(200, H), 5):
    bar = "#" * int(row_avg[y] / 2)
    print(f"  y={y:3d}  avg={row_avg[y]:5.1f}  {bar}")
