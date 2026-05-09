"""Inspect 'l' candidate widths and shapes from rows that fail."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))
from segment import segment_raid_party, find_chars, reconcile_boxes  # noqa: E402
from templates import load_templates  # noqa: E402

GT = ["zerkaa", "Stupre", "Bright", "CuteAngel", "XanDeusVult", "Jieha",
      "KFusion", "Okuu", "Poisonuss", "LetMeShieldU", "Endeauvirre", "integrationA"]

im = np.array(Image.open(ROOT / "samples" / "raid_party_01.png").convert("RGB"))
templates = load_templates(ROOT / "templates" / "raid_party" / "default")
rows = segment_raid_party(im)

print(f"'l' template: {templates['l'].image.shape}")
print(f"'i' template: {templates['i'].image.shape}\n")

# Find every 'l' and 'i' instance and print its width
for ridx, name in enumerate(GT):
    row = rows[ridx]
    boxes = reconcile_boxes(find_chars(row.mask), row.mask, expected=len(name))
    for cidx, ch in enumerate(name):
        if ch in ('l', 'i'):
            l, r = boxes[cidx]
            glyph = row.mask[:, l:r + 1]
            cols_with = (glyph > 0).any(axis=0)
            rows_with = (glyph > 0).any(axis=1)
            if cols_with.any():
                left_t = int(np.argmax(cols_with))
                right_t = len(cols_with) - 1 - int(np.argmax(cols_with[::-1]))
                top = int(np.argmax(rows_with))
                bot = len(rows_with) - 1 - int(np.argmax(rows_with[::-1]))
                tight_w = right_t - left_t + 1
                tight_h = bot - top + 1
                print(f"row {ridx} '{name}' [{cidx}] = '{ch}': "
                      f"box={l}..{r} (w={r-l+1}) tight=({tight_h}, {tight_w})")
