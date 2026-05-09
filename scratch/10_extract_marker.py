"""Extract a fixed-UI marker (Join Raid button) for window detection."""
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
SAMPLE = ROOT / "samples" / "raid_party_01.png"
im = np.array(Image.open(SAMPLE).convert("RGB"))
print(f"image: {im.shape}")

# The Join Raid button is at the bottom-center of the window. From the
# screenshot at 692x579, visual inspection puts it around y=535-560, x=245-450.
# Crop a generous region first, save it, then refine.
button = im[525:560, 230:465]
header = im[95:120, 25:530]
Image.fromarray(header).save(ROOT / "scratch" / "header_candidate.png")
print(f"saved header candidate: {header.shape}")
Image.fromarray(button).save(ROOT / "scratch" / "join_raid_candidate.png")
print(f"saved candidate crop: {button.shape}")
