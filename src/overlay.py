"""
Transparent always-on-top overlay for DFO score annotations.

The window covers the full virtual desktop, passes all mouse/keyboard input
through to apps below (WindowTransparentForInput), and renders white text
with a 1px black outline for readability on any background.
"""
import sys
from dataclasses import dataclass
from typing import List, Dict

import mss
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import Qt, QPoint, QRect, QRectF
from PyQt6.QtGui import QPainter, QColor, QFont, QPen

from qt_dpi import configure_qt_high_dpi


@dataclass(frozen=True)
class _ScreenMap:
    logical: QRect
    physical: QRectF
    dpr: float


@dataclass(frozen=True)
class _PhysicalScreen:
    rect: QRectF
    names: frozenset[str]


class OverlayWindow(QWidget):
    def __init__(self):
        configure_qt_high_dpi()
        self._app = QApplication.instance() or QApplication(sys.argv)

        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.Tool                     # no taskbar icon
        )
        super().__init__(None, flags)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        desktop = QRect()
        for screen in self._app.screens():
            desktop = desktop.united(screen.geometry())
        self.setGeometry(desktop)

        self._annotations: List[Dict] = []
        self._font = QFont("Arial", 14, QFont.Weight.Bold)
        self._screen_maps: list[_ScreenMap] = []
        self._refresh_screen_maps()
        for screen in self._app.screens():
            self._connect_screen_updates(screen)
        self._app.screenAdded.connect(lambda screen: self._handle_screen_added(screen))
        self._app.screenRemoved.connect(lambda _screen: self._refresh_screen_maps())

    def _handle_screen_added(self, screen) -> None:
        self._connect_screen_updates(screen)
        self._refresh_screen_maps()

    def _connect_screen_updates(self, screen) -> None:
        screen.geometryChanged.connect(lambda *_args: self._refresh_screen_maps())
        screen.physicalDotsPerInchChanged.connect(lambda *_args: self._refresh_screen_maps())
        screen.logicalDotsPerInchChanged.connect(lambda *_args: self._refresh_screen_maps())

    def _refresh_screen_maps(self) -> None:
        desktop = QRect()
        screens = self._app.screens()
        for screen in screens:
            desktop = desktop.united(screen.geometry())
        if not desktop.isNull():
            self.setGeometry(desktop)

        self._screen_maps = self._build_screen_maps(screens)
        self.update()

    def _build_screen_maps(self, screens) -> list[_ScreenMap]:
        physical_screens = self._physical_screens()
        maps: list[_ScreenMap] = []
        used: set[int] = set()

        for idx, screen in enumerate(screens):
            logical = screen.geometry()
            dpr = max(float(screen.devicePixelRatio()), 1.0)
            match_idx = self._match_physical_screen(screen.name(), logical, dpr,
                                                    physical_screens, used)
            if match_idx is None and idx < len(physical_screens) and idx not in used:
                match_idx = idx

            if match_idx is not None:
                used.add(match_idx)
                physical = physical_screens[match_idx].rect
            else:
                physical = QRectF(
                    logical.x() * dpr,
                    logical.y() * dpr,
                    logical.width() * dpr,
                    logical.height() * dpr,
                )
            maps.append(_ScreenMap(logical=logical, physical=physical, dpr=dpr))
        return maps

    def _physical_screens(self) -> list[_PhysicalScreen]:
        physical_screens = self._win32_physical_screens()
        if physical_screens:
            return physical_screens
        return self._mss_physical_screens()

    def _win32_physical_screens(self) -> list[_PhysicalScreen]:
        try:
            import win32api

            out: list[_PhysicalScreen] = []
            for monitor, _dc, _rect in win32api.EnumDisplayMonitors():
                info = win32api.GetMonitorInfo(monitor)
                left, top, right, bottom = info["Monitor"]
                device = info.get("Device", "")
                names = self._screen_name_aliases(device)
                out.append(_PhysicalScreen(
                    rect=QRectF(left, top, right - left, bottom - top),
                    names=frozenset(names),
                ))
            return out
        except Exception:
            return []

    def _mss_physical_screens(self) -> list[_PhysicalScreen]:
        try:
            with mss.mss() as sct:
                monitors = sct.monitors[1:]
                return [
                    _PhysicalScreen(
                        rect=QRectF(mon["left"], mon["top"], mon["width"], mon["height"]),
                        names=frozenset(),
                    )
                    for mon in monitors
                ]
        except Exception:
            return []

    def _screen_name_aliases(self, name: str) -> set[str]:
        if not name:
            return set()
        aliases = {name}
        aliases.add(name.replace("\\\\.\\", ""))
        aliases.add(name.replace("\\", ""))
        return {alias.lower() for alias in aliases if alias}

    def _match_physical_screen(
        self,
        screen_name: str,
        logical: QRect,
        dpr: float,
        physical_screens: list[_PhysicalScreen],
        used: set[int],
    ) -> int | None:
        aliases = self._screen_name_aliases(screen_name)
        for idx, physical in enumerate(physical_screens):
            if idx not in used and aliases.intersection(physical.names):
                return idx

        expected_w = logical.width() * dpr
        expected_h = logical.height() * dpr
        best_idx = None
        best_delta = float("inf")
        for idx, physical in enumerate(physical_screens):
            if idx in used:
                continue
            delta = (
                abs(physical.rect.width() - expected_w)
                + abs(physical.rect.height() - expected_h)
            )
            if delta < best_delta:
                best_idx = idx
                best_delta = delta
        return best_idx if best_delta <= 4 else None

    def set_annotations(self, annotations: List[Dict]):
        """Replace displayed annotations and schedule a repaint.

        Each annotation: {"x": int, "y": int, "text": str}
        Coordinates are in physical screen pixels from MSS/Win32 capture.
        """
        self._annotations = annotations
        self.update()

    def _physical_to_overlay_logical(self, x: float, y: float) -> tuple[int, int]:
        if not self._screen_maps:
            origin = self.mapToGlobal(QPoint(0, 0))
            return int(round(x - origin.x())), int(round(y - origin.y()))

        screen_map = self._screen_map_for_physical_point(x, y)
        gx = screen_map.logical.x() + (x - screen_map.physical.x()) / screen_map.dpr
        gy = screen_map.logical.y() + (y - screen_map.physical.y()) / screen_map.dpr

        origin = self.mapToGlobal(QPoint(0, 0))
        return int(round(gx - origin.x())), int(round(gy - origin.y()))

    def _screen_map_for_physical_point(self, x: float, y: float) -> _ScreenMap:
        for screen_map in self._screen_maps:
            if screen_map.physical.contains(x, y):
                return screen_map

        def distance_to_center(screen_map: _ScreenMap) -> float:
            center = screen_map.physical.center()
            return (center.x() - x) ** 2 + (center.y() - y) ** 2

        return min(self._screen_maps, key=distance_to_center)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setFont(self._font)

        for ann in self._annotations:
            lx, ly = self._physical_to_overlay_logical(ann["x"], ann["y"])
            text = ann["text"]
            color = ann.get("color", (255, 255, 255))   # default white

            painter.setPen(QPen(QColor(0, 0, 0)))
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                painter.drawText(lx + dx, ly + dy, text)

            painter.setPen(QPen(QColor(*color)))
            painter.drawText(lx, ly, text)

        painter.end()

    def run(self):
        """Start the Qt event loop (blocking)."""
        self.show()
        sys.exit(self._app.exec())
