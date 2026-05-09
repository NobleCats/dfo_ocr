"""
Smoke test: capture screen every 1s, overlay a timestamp annotation near top-left.
Runs for ~10 seconds then exits.
"""
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PyQt6.QtCore import QTimer
from capture import ScreenCapture
from overlay import OverlayWindow

DURATION_MS = 10_000
INTERVAL_MS = 1_000


def main():
    overlay = OverlayWindow()
    cap = ScreenCapture()

    tick_count = [0]

    def tick():
        frame = cap.grab()
        ts = datetime.now().strftime("%H:%M:%S")
        shape_info = f"{frame.shape[1]}x{frame.shape[0]}"
        overlay.set_annotations([
            {"x": 40, "y": 60, "text": f"captured at {ts}  ({shape_info})"},
        ])
        tick_count[0] += 1

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(INTERVAL_MS)

    def stop():
        timer.stop()
        cap.close()
        overlay._app.quit()

    QTimer.singleShot(DURATION_MS, stop)

    tick()  # immediate first frame so overlay shows something right away
    overlay.run()


if __name__ == "__main__":
    main()
