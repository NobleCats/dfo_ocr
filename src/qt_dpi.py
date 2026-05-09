"""Qt/Windows DPI setup shared by the GUI entry points."""
from __future__ import annotations

import sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication


_CONFIGURED = False


def configure_qt_high_dpi() -> None:
    """Configure Qt before QApplication is created.

    Capture coordinates come from MSS/Win32 in physical pixels. Qt renders in
    device-independent pixels when Windows display scaling is enabled, so keep
    Qt's reported scale factors exact and convert explicitly in the overlay.
    """
    global _CONFIGURED
    if _CONFIGURED or QGuiApplication.instance() is not None:
        return

    if sys.platform == "win32":
        try:
            import ctypes

            # PER_MONITOR_AWARE_V2 makes GetWindowRect/MSS interop use physical
            # pixels on modern Windows. Ignore failures if another library has
            # already selected the process DPI awareness.
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    _CONFIGURED = True
