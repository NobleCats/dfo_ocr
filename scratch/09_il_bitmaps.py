"""Dump actual bitmaps for failing 'i'/'l' cases."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))
from segment import segment_raid_party, find_chars, reconcile_boxes, tight_crop  # noqa
from templates import load_templates  # noqa

GT = ["zerkaa", "Stupre", "Bright", "CuteAngel", "XanDeusVult", "Jieha",
      "KFusion", "Okuu", "Poisonuss", "LetMeShieldU", "Endeauvirre", "integrationA"]

im = np.array(Image.open(ROOT / "samples" / "raid_party_01.png").convert("RGB"))
templates = load_templates(ROOT / "templates" / "raid_party" / "default")
rows = segment_raid_party(im)


def dump(label, glyph):
    print(f"--- {label}: shape={glyph.shape} ---")
    for row in glyph:
        print("".join("#" if v > 0 else "." for v in row))
    print()


dump("template 'l'", templates['l'].image)
dump("template 'i'", templates['i'].image)

# Row 4 'XanDeusVult' [9] = 'l' (the failing one)
row = rows[4]
boxes = reconcile_boxes(find_chars(row.mask), row.mask, expected=11)
l, r = boxes[9]
glyph = tight_crop(row.mask[:, l:r + 1])
dump("row 4 'l' candidate (Vult)", glyph)

# Row 8 'Poisonuss' [2] = 'i' (the failing one)
row = rows[8]
boxes = reconcile_boxes(find_chars(row.mask), row.mask, expected=9)
l, r = boxes[2]
glyph = tight_crop(row.mask[:, l:r + 1])
dump("row 8 'i' candidate (Poison)", glyph)
