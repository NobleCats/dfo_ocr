"""Sanity-check segmentation: verify we get 12 rows and the right number
of characters per row matching the ground-truth names."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))
from segment import segment_raid_party, find_chars, crop_char, reconcile_boxes  # noqa: E402

GROUND_TRUTH = [
    "zerkaa", "Stupre", "Bright", "CuteAngel",
    "XanDeusVult", "Jieha", "KFusion", "Okuu",
    "Poisonuss", "LetMeShieldU", "Endeauvirre", "integrationA",
]

im = np.array(Image.open(ROOT / "samples" / "raid_party_01.png").convert("RGB"))
rows = segment_raid_party(im)

print(f"detected {len(rows)} rows (expected {len(GROUND_TRUTH)})\n")
all_ok = True
for i, (row, gt) in enumerate(zip(rows, GROUND_TRUTH), 1):
    raw_boxes = find_chars(row.mask)
    boxes = reconcile_boxes(raw_boxes, row.mask, expected=len(gt))
    n = len(boxes)
    expected = len(gt)
    status = "OK" if n == expected else "MISMATCH"
    if n != expected:
        all_ok = False
    print(f"row {i:2d}  y={row.y_abs[0]}..{row.y_abs[1]}  "
          f"chars={n:2d}/{expected:2d} ({status})  "
          f"raw={len(raw_boxes)}  gt={gt!r}")
    if n != expected:
        widths = [b[1] - b[0] + 1 for b in boxes]
        print(f"          widths={widths}")

print()
print("all rows segmented correctly:" if all_ok else "MISMATCHES present")

# Save first row's mask + char boxes for visual inspection
out = ROOT / "scratch"
row0 = rows[0]
Image.fromarray(row0.mask).save(out / "row0_mask.png")
boxes = find_chars(row0.mask)
print(f"\nrow0 ('{GROUND_TRUTH[0]}') boxes: {boxes}")
for j, b in enumerate(boxes):
    glyph = crop_char(row0.mask, b)
    Image.fromarray(glyph).save(out / f"row0_char{j}.png")
print(f"saved {len(boxes)} char images")
