"""Visualize and compare an 'a' candidate from row 11 vs the 'a' template
extracted from row 1, to see why matching fails."""
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

# Row 1 'zerkaa' has 'a' at index 4
# Row 4 'CuteAngel' has 'a' indirectly (via 'A'), but actual 'a' is index 7? no
# Row 11 'Endeauvirre' has 'a' at index 4
# Row 12 'integrationA' has 'a' at index 6

def get_glyph(row_idx: int, char_idx: int) -> np.ndarray:
    row = rows[row_idx]
    boxes = reconcile_boxes(find_chars(row.mask), row.mask, expected=len(GT[row_idx]))
    l, r = boxes[char_idx]
    return row.mask[:, l:r + 1]

# 'a' source = row 0 (zerkaa, idx 4), 'a' candidates = row 10 idx 4, row 11 idx 6
out = ROOT / "scratch"
for label, ridx, cidx in [("template_zerkaa_a", 0, 4),
                          ("cand_End_a", 10, 4),
                          ("cand_int_a", 11, 6)]:
    g = get_glyph(ridx, cidx)
    big = np.kron(g, np.ones((6, 6), dtype=np.uint8))
    Image.fromarray(big).save(out / f"glyph_{label}.png")
    print(f"{label}: shape={g.shape}")
    print(g.tolist())
    print()

# Print pixel-level diff for tuple (template_a, cand_End_a)
tpl_a = templates['a'].image
cand = get_glyph(10, 4)
print(f"\n'a' template shape: {tpl_a.shape}")
print(f"'a' candidate (row 11) shape: {cand.shape}")
# Pad to common width
max_w = max(tpl_a.shape[1], cand.shape[1])
def pad_w(a, w):
    if a.shape[1] == w: return a
    return np.hstack([a, np.zeros((a.shape[0], w - a.shape[1]), dtype=a.dtype)])
a, b = pad_w(tpl_a, max_w), pad_w(cand, max_w)
print(f"\ntemplate vs candidate pixel agreement: {(a > 0) == (b > 0)}")
