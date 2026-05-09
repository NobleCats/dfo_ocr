"""Extract per-character templates from a screenshot + ground truth names.

Templates are tight-cropped binary masks. Each template also records its
bottom-to-baseline offset so the matcher can align glyphs at the baseline.
"""
from __future__ import annotations
from pathlib import Path
import sys
import numpy as np
from PIL import Image

from segment import segment_raid_party, find_chars, reconcile_boxes, crop_char_with_offset
from templates import Template


def extract_from_screenshot(image_rgb: np.ndarray,
                            ground_truth: list[str]) -> dict[str, Template]:
    rows = segment_raid_party(image_rgb)
    if len(rows) != len(ground_truth):
        raise ValueError(f"row count mismatch: detected {len(rows)} rows but "
                         f"ground truth has {len(ground_truth)} names")

    templates: dict[str, list[Template]] = {}
    seen_shapes: dict[str, set] = {}
    for row, name in zip(rows, ground_truth):
        raw_boxes = find_chars(row.mask)
        boxes = reconcile_boxes(raw_boxes, row.mask, expected=len(name))
        if len(boxes) != len(name):
            raise ValueError(f"box reconciliation failed for {name!r}: "
                             f"got {len(boxes)} boxes")
        for ch, box in zip(name, boxes):
            glyph, b2b = crop_char_with_offset(row.mask, box, row.baseline_y)
            shape_key = (glyph.shape, b2b, glyph.tobytes())
            if ch in seen_shapes and shape_key in seen_shapes[ch]:
                continue
            seen_shapes.setdefault(ch, set()).add(shape_key)
            templates.setdefault(ch, []).append(
                Template(char=ch, image=glyph, bottom_to_baseline=b2b)
            )

    return templates


if __name__ == "__main__":
    ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
    sys.path.insert(0, str(ROOT / "src"))
    from templates import save_templates  # noqa: E402

    GT = [
        "zerkaa", "Stupre", "Bright", "CuteAngel",
        "XanDeusVult", "Jieha", "KFusion", "Okuu",
        "Poisonuss", "LetMeShieldU", "Endeauvirre", "integrationA",
    ]
    im = np.array(Image.open(ROOT / "samples" / "raid_party_01.png").convert("RGB"))
    templates = extract_from_screenshot(im, GT)
    total = sum(len(v) for v in templates.values())
    print(f"extracted {len(templates)} chars, {total} template variants:")
    for ch, tpls in sorted(templates.items()):
        shapes = [f"{t.width}x{t.height}(b2b={t.bottom_to_baseline})" for t in tpls]
        print(f"  {ch!r}: {len(tpls)} variant(s) {shapes}")

    out_dir = ROOT / "templates" / "raid_party" / "default"
    save_templates(out_dir, templates)
    print(f"\nsaved to {out_dir}")
