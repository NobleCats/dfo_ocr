"""Merge raid_party native + downscaled chat templates and test."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))
from templates import load_templates, Template  # noqa
from recognize import recognize_raid_party  # noqa

sys.path.insert(0, str(ROOT / "scratch"))
from importlib import import_module
ds = import_module("19_chat_downscale_test")


GT = ["zerkaa", "Stupre", "Bright", "CuteAngel", "XanDeusVult", "Jieha",
      "KFusion", "Okuu", "Poisonuss", "LetMeShieldU", "Endeauvirre", "integrationA"]


def merge_libraries(primary: dict[str, list[Template]],
                    secondary: dict[str, list[Template]]
                    ) -> dict[str, list[Template]]:
    """Merge two template dicts. For each char, primary's variants come first
    (so they're tried first). Both libraries' variants for the same char are
    pooled."""
    merged: dict[str, list[Template]] = {}
    for ch, variants in primary.items():
        merged[ch] = list(variants)
    for ch, variants in secondary.items():
        merged.setdefault(ch, []).extend(variants)
    return merged


def main():
    raid = load_templates(ROOT / "templates" / "raid_party" / "default")
    chat = load_templates(ROOT / "templates" / "chat" / "default")

    print(f"raid_party: {len(raid)} chars, {sum(len(v) for v in raid.values())} templates")
    print(f"chat:       {len(chat)} chars, {sum(len(v) for v in chat.values())} templates")
    print(f"chars only in chat: {sorted(set(chat) - set(raid))}\n")

    im = np.array(Image.open(ROOT / "samples" / "raid_party_01.png").convert("RGB"))

    print(f"{'scale':<6}  {'correct':<8}  predictions")
    print("-" * 60)
    for scale in [0.74, 0.75, 0.76, 0.77, 0.78]:
        chat_scaled = ds.downscale_library(chat, scale)
        merged = merge_libraries(raid, chat_scaled)
        rows = recognize_raid_party(im, merged)
        preds = [r.name for r in rows]
        correct = sum(1 for p, g in zip(preds, GT) if p == g)
        wrong = [(p, g) for p, g in zip(preds, GT) if p != g]
        print(f"{scale:<6.2f}  {correct:>2}/{len(GT)}     {preds}")
        if wrong:
            for p, g in wrong:
                print(f"           wrong: {p!r:25s} expected {g!r}")


if __name__ == "__main__":
    main()
