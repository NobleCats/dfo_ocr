"""Capture the current screen and report what the detector sees.

Run this once with the game's raid party window visible. Saves the captured
frame so we can inspect what mss actually grabbed and why detection failed.
"""
from pathlib import Path
import sys
from datetime import datetime
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from capture import ScreenCapture
from detect import detect_raid_window
import mss


def main():
    print("Capturing primary monitor...")
    with ScreenCapture() as cap:
        frame = cap.grab()
    print(f"  frame shape: {frame.shape} (HxWxC, RGB)")
    print(f"  frame size:  {frame.shape[1]}x{frame.shape[0]} pixels")
    print(f"  dtype:       {frame.dtype}")

    # All monitor info
    print("\nAll monitors (mss):")
    with mss.mss() as sct:
        for i, m in enumerate(sct.monitors):
            tag = "  ALL  " if i == 0 else f"  #{i}  "
            print(f"  {tag} {m['width']}x{m['height']} at ({m['left']},{m['top']})")

    # Save for inspection
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / f"scratch/captured_{ts}.png"
    out.parent.mkdir(exist_ok=True)
    Image.fromarray(frame).save(out)
    print(f"\nSaved capture to: {out}")

    # Run detection across full multi-scale range
    print("\nRunning detection across all scales...")
    det = detect_raid_window(frame)
    print(f"  best score:  {det.score:.3f}  (threshold 0.7)")
    print(f"  best scale:  {det.scale:.2f}")
    print(f"  found:       {det.found}")
    print(f"  window xywh: {det.window_xywh}")

    if det.found:
        # Save the detected window region for verification
        wx, wy, ww, wh = det.window_xywh
        wx2 = max(0, wx); wy2 = max(0, wy)
        ww2 = min(frame.shape[1] - wx2, ww); wh2 = min(frame.shape[0] - wy2, wh)
        crop = frame[wy2:wy2 + wh2, wx2:wx2 + ww2]
        out2 = ROOT / f"scratch/detected_window_{ts}.png"
        Image.fromarray(crop).save(out2)
        print(f"  saved detected window crop: {out2}")

    print("\nDiagnostics:")
    print("  1. Open the captured PNG and check whether the raid party")
    print("     window is visible and looks normal (not blurry, not cropped).")
    print("  2. Check the resolution: if it's much larger or smaller than 692x579,")
    print("     the marker may need rescaling.")
    print("  3. If the image is black or empty, mss is capturing the wrong monitor.")


if __name__ == "__main__":
    main()
