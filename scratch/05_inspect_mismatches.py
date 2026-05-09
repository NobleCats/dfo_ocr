"""Save the masks of the 3 problem rows enlarged for visual inspection."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))
from segment import segment_raid_party, find_chars  # noqa: E402

GT = ["zerkaa", "Stupre", "Bright", "CuteAngel", "XanDeusVult", "Jieha",
      "KFusion", "Okuu", "Poisonuss", "LetMeShieldU", "Endeauvirre", "integrationA"]

im = np.array(Image.open(ROOT / "samples" / "raid_party_01.png").convert("RGB"))
rows = segment_raid_party(im)
out = ROOT / "scratch"

# Save problem rows: 0 (zerkaa), 7 (Okuu), 10 (Endeauvirre)
for idx in (0, 7, 10):
    row = rows[idx]
    name = GT[idx]
    # Enlarge 4x for visibility
    h, w = row.mask.shape
    big = np.kron(row.mask, np.ones((4, 4), dtype=np.uint8))
    Image.fromarray(big).save(out / f"problem_{idx:02d}_{name}_mask.png")

    boxes = find_chars(row.mask)
    print(f"\nrow {idx} '{name}' - {len(boxes)} boxes (expected {len(name)})")
    print(f"  shape: {row.mask.shape}")
    for j, (l, r) in enumerate(boxes):
        # column projection within the box
        col_density = (row.mask[:, l:r+1] > 0).sum(axis=0)
        print(f"  box {j}: x={l}..{r} (w={r-l+1})  col_density={col_density.tolist()}")
