"""Bake the production template library by merging:
  - raid_party native templates (pixel-perfect, captured at the target scale)
  - chat templates downscaled by `CHAT_TO_RAID_SCALE`

The chat templates fill in the long-tail characters (digits, special chars,
uncommon letters) that aren't yet present in the raid_party library.

Run this whenever raid_party or chat templates are updated. The output is
written to `templates/merged/default/` and is loaded by default at runtime.
"""
from __future__ import annotations
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from templates import load_templates, save_templates, Template  # noqa: E402


# At UI Scale 100%, the raid party font is identical to the chat font, so
# chat templates need no downscaling. The user's UI Scale (set on startup)
# multiplies this factor; e.g. at UI Scale 80% we'd use 0.80.
CHAT_TO_RAID_SCALE = 1.0


def downscale_template(tpl: Template, scale: float) -> Template:
    h, w = tpl.image.shape
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    img_small = Image.fromarray(tpl.image).resize((new_w, new_h), Image.LANCZOS)
    arr = np.array(img_small)
    binary = (arr > 100).astype(np.uint8) * 255
    rows_with = (binary > 0).any(axis=1)
    cols_with = (binary > 0).any(axis=0)
    if rows_with.any() and cols_with.any():
        top = int(np.argmax(rows_with))
        bot = len(rows_with) - 1 - int(np.argmax(rows_with[::-1]))
        left = int(np.argmax(cols_with))
        right = len(cols_with) - 1 - int(np.argmax(cols_with[::-1]))
        binary = binary[top:bot + 1, left:right + 1]
    return Template(char=tpl.char, image=binary,
                    bottom_to_baseline=int(round(tpl.bottom_to_baseline * scale)))


def bake(ui_scale: float = 1.0) -> dict[str, list[Template]]:
    """Build the production library for a given user UI Scale.

    Chat templates are the source of truth (captured at the game's native
    100% rendering). For non-100% UI Scale, all chat templates are scaled
    by `ui_scale`. The legacy raid_party library is included unscaled and
    only useful for the specific old captures it was extracted from.
    """
    raid = load_templates(ROOT / "templates" / "raid_party" / "default")
    chat = load_templates(ROOT / "templates" / "chat" / "default")

    merged: dict[str, list[Template]] = {}
    for ch, variants in chat.items():
        if abs(ui_scale - 1.0) < 1e-3:
            scaled = list(variants)
        else:
            scaled = [downscale_template(t, ui_scale) for t in variants]
        seen = set()
        unique = []
        for t in scaled:
            key = (t.image.shape, t.bottom_to_baseline, t.image.tobytes())
            if key not in seen:
                seen.add(key)
                unique.append(t)
        merged.setdefault(ch, []).extend(unique)
    # Legacy raid_party templates appended last; they're size-mismatched at
    # other UI scales but harmless because match_char filters by width/height.
    for ch, variants in raid.items():
        if ch == "B":
            continue
        merged.setdefault(ch, []).extend(variants)
    return merged


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ui-scale", type=float, default=1.0,
                        help="user's UI Scale (1.0 = 100%%, 0.69 = 69%%)")
    args = parser.parse_args()

    out_dir = ROOT / "templates" / "merged" / "default"
    if out_dir.exists():
        for f in out_dir.glob("*.png"):
            f.unlink()
        if (out_dir / "manifest.json").exists():
            (out_dir / "manifest.json").unlink()
    out_dir.mkdir(parents=True, exist_ok=True)

    merged = bake(ui_scale=args.ui_scale)
    save_templates(out_dir, merged)
    total = sum(len(v) for v in merged.values())
    print(f"baked merged library at UI Scale {args.ui_scale}: "
          f"{len(merged)} chars, {total} variants -> {out_dir}")
