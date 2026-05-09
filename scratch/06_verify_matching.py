"""End-to-end check: segment + match the source screenshot with the templates
extracted from itself. Should reproduce the ground truth perfectly."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))

from segment import segment_raid_party, find_chars, reconcile_boxes  # noqa: E402
from templates import load_templates  # noqa: E402
from match import match_row  # noqa: E402

GT = ["zerkaa", "Stupre", "Bright", "CuteAngel", "XanDeusVult", "Jieha",
      "KFusion", "Okuu", "Poisonuss", "LetMeShieldU", "Endeauvirre", "integrationA"]

im = np.array(Image.open(ROOT / "samples" / "raid_party_01.png").convert("RGB"))
templates = load_templates(ROOT / "templates" / "raid_party" / "default")
print(f"loaded {len(templates)} templates\n")

rows = segment_raid_party(im)
all_correct = True
for i, (row, gt) in enumerate(zip(rows, GT), 1):
    boxes = reconcile_boxes(find_chars(row.mask), row.mask, expected=len(gt))
    name, matches = match_row(row.mask, boxes, row.baseline_y, templates)
    ok = name == gt
    if not ok:
        all_correct = False
    print(f"row {i:2d} {'OK' if ok else 'FAIL':4s}  pred={name!r}  gt={gt!r}")
    if not ok:
        for j, (m, expected_ch) in enumerate(zip(matches, gt)):
            mark = "ok" if m.char == expected_ch else f"!! expected {expected_ch!r}"
            print(f"        char {j}: {m.char!r} (score={m.score:.3f}) "
                  f"runner-up={m.runner_up!r}({m.runner_up_score:.3f})  {mark}")

print()
print("ALL CORRECT" if all_correct else "MISMATCHES present")
