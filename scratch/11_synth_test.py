"""Synthesize test cases by embedding the sample at various positions and
scales in a larger canvas. Verifies detect + recognize end-to-end without
new screenshots."""
from pathlib import Path
import sys
import numpy as np
from PIL import Image

ROOT = Path(r"C:\Users\Noble\Desktop\dfo_ocr")
sys.path.insert(0, str(ROOT / "src"))
from recognize import detect_and_recognize, load_default_templates  # noqa


GT = ["zerkaa", "Stupre", "Bright", "CuteAngel", "XanDeusVult", "Jieha",
      "KFusion", "Okuu", "Poisonuss", "LetMeShieldU", "Endeauvirre", "integrationA"]


def embed(sample: np.ndarray, canvas_size: tuple[int, int],
          offset: tuple[int, int], scale: float) -> np.ndarray:
    """Place `sample` (resized by `scale`) at `offset` in a black canvas
    of `canvas_size`. Returns the canvas image."""
    h, w = sample.shape[:2]
    new_h = int(round(h * scale))
    new_w = int(round(w * scale))
    resized = np.array(Image.fromarray(sample).resize((new_w, new_h),
                                                       Image.LANCZOS))
    canvas = np.full((canvas_size[1], canvas_size[0], 3), 25, dtype=np.uint8)
    ox, oy = offset
    canvas[oy:oy + new_h, ox:ox + new_w] = resized
    return canvas


sample = np.array(Image.open(ROOT / "samples" / "raid_party_01.png").convert("RGB"))
templates = load_default_templates()


def test_case(label, canvas_size, offset, scale):
    canvas = embed(sample, canvas_size, offset, scale)
    det, rows = detect_and_recognize(canvas, templates)
    pred = [r.name for r in rows]
    correct = sum(1 for p, g in zip(pred, GT) if p == g)
    print(f"\n{label}:")
    print(f"  canvas={canvas_size}  offset={offset}  scale={scale}")
    print(f"  detected: found={det.found}  score={det.score:.3f}  scale={det.scale:.2f}  window={det.window_xywh}")
    print(f"  recognition: {correct}/{len(GT)} correct")
    if correct < len(GT):
        for p, g in zip(pred, GT):
            mark = "OK" if p == g else "FAIL"
            print(f"    {mark}: {p!r:25s} expected {g!r}")
    return correct == len(GT) and det.found


tests = [
    ("baseline (full screenshot)", (692, 579), (0, 0), 1.0),
    ("offset in 1920x1080 canvas", (1920, 1080), (614, 250), 1.0),
    ("scale 1.5x in 2560x1440",   (2560, 1440), (200, 100), 1.5),
    ("scale 0.75x small",          (800, 600),   (50, 10),   0.75),
    ("scale 2.0x huge",            (1920, 1200), (50, 0),    2.0),
]

passed = sum(1 for t in tests if test_case(*t))
print(f"\n=== {passed}/{len(tests)} test cases passed ===")
