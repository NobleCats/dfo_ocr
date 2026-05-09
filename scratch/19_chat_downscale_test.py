"""Downscale chat templates and test on raid_party_01.png.

Tries multiple scale factors and picks the best.
"""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))
from templates import load_templates, save_templates, Template  # noqa
from recognize import recognize_raid_party  # noqa


GT = ["zerkaa", "Stupre", "Bright", "CuteAngel", "XanDeusVult", "Jieha",
      "KFusion", "Okuu", "Poisonuss", "LetMeShieldU", "Endeauvirre", "integrationA"]


def downscale_template(tpl: Template, scale: float) -> Template:
    """Downscale a binary template by `scale` using LANCZOS, then re-binarize."""
    h, w = tpl.image.shape
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    img = Image.fromarray(tpl.image)
    img_small = img.resize((new_w, new_h), Image.LANCZOS)
    arr = np.array(img_small)
    binary = (arr > 100).astype(np.uint8) * 255
    # Tight crop
    rows_with = (binary > 0).any(axis=1)
    cols_with = (binary > 0).any(axis=0)
    if rows_with.any() and cols_with.any():
        top = int(np.argmax(rows_with))
        bot = len(rows_with) - 1 - int(np.argmax(rows_with[::-1]))
        left = int(np.argmax(cols_with))
        right = len(cols_with) - 1 - int(np.argmax(cols_with[::-1]))
        binary = binary[top:bot + 1, left:right + 1]
    # Adjust b2b: if original had descender (b2b<0), the descender height also scales
    new_b2b = int(round(tpl.bottom_to_baseline * scale))
    return Template(char=tpl.char, image=binary, bottom_to_baseline=new_b2b)


def downscale_library(chat_tpls: dict[str, list[Template]],
                      scale: float) -> dict[str, list[Template]]:
    out: dict[str, list[Template]] = {}
    for ch, variants in chat_tpls.items():
        scaled = [downscale_template(t, scale) for t in variants]
        # De-duplicate
        seen = set()
        unique = []
        for t in scaled:
            key = (t.image.shape, t.bottom_to_baseline, t.image.tobytes())
            if key not in seen:
                seen.add(key)
                unique.append(t)
        out[ch] = unique
    return out


def main():
    chat_tpls = load_templates(ROOT / "templates" / "chat" / "default")
    print(f"loaded {len(chat_tpls)} chat chars, "
          f"{sum(len(v) for v in chat_tpls.values())} templates total\n")

    im = np.array(Image.open(ROOT / "samples" / "raid_party_01.png").convert("RGB"))

    print(f"{'scale':<6}  {'correct':<8}  predictions")
    print("-" * 60)
    for scale in [0.74, 0.75, 0.76, 0.77, 0.78]:
        scaled_tpls = downscale_library(chat_tpls, scale)
        rows = recognize_raid_party(im, scaled_tpls)
        preds = [r.name for r in rows]
        correct = sum(1 for p, g in zip(preds, GT) if p == g)
        print(f"{scale:<6.2f}  {correct:>2}/{len(GT)}     {preds}")


if __name__ == "__main__":
    main()
