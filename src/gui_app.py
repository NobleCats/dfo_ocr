"""Portable-style PyQt control panel for DFOGANG Raid Helper."""

from __future__ import annotations

import os
import sys
import base64
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygon,
)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from resources import resource_path  # noqa: E402
from qt_dpi import configure_qt_high_dpi  # noqa: E402

APP_NAME = "DFOGANG Raid Helper"
APP_VERSION = "v1.1beta"

try:
    from build_info import BUILD_HASH as _BUILD_HASH  # noqa: E402
    _BUILD_ID = _BUILD_HASH if _BUILD_HASH and _BUILD_HASH != "dev" else "dev"
except Exception:
    _BUILD_ID = "dev"

APP_DISPLAY_NAME = f"{APP_NAME} {APP_VERSION}"
APP_VERSION_LABEL = f"{APP_VERSION} ({_BUILD_ID})"
INSTANCE_MUTEX_NAME = r"Local\DFOGANG_RaidHelper_v1"

DEFAULT_CAPTURE_INTERVAL_MS = 80
NEOPLE_KEY_URL = "https://www.dfoneople.com/developers/manage/app/list"

CANVAS_BG_COLOR = "#1e1e1e"
CANVAS_BORDER_RADIUS = 8
HIGHLIGHT_TEXT_COLOR = "#58a6ff"
NORMAL_TEXT_COLOR = "#a0aec0"
DISABLED_TEXT_COLOR = "#718096"
INPUT_BG_COLOR = "#1a1a1a"
LOGO_OPACITY = 0.1
LOGO_HEIGHT_RATIO = 0.6
LOGO_TOP_MARGIN_RATIO = 0.05
DEFAULT_WINDOW_TITLE = "Dungeon Fighter Online"
SETTINGS_FILE = "settings.json"

GUIDE_REF_MARKER_SIZE = (1050, 26)
GUIDE_REF_MARKER_LEFT_IN_WINDOW = 16
GUIDE_REF_MARKER_TOP_IN_WINDOW = 110
GUIDE_REF_WINDOW_SIZE = (1096, 896)
GUIDE_REF_TITLE_BOTTOM_Y = 32
GUIDE_REF_TITLE_LEFT_OFFSET = 2       # px from guide left
GUIDE_REF_TITLE_SIZE = (1092, 32)     # guide_title.png dimensions
GUIDE_REF_BUTTON_SIZE = (276, 40)     # guide_button.png dimensions
GUIDE_REF_BUTTON_RIGHT_MARGIN = 20    # px from guide right edge
GUIDE_REF_BUTTON_BOTTOM_MARGIN = 20   # px from guide bottom edge
GUIDE_OVERLAY_IMAGE_ALPHA = 0.35      # opacity for title/tab/button alignment aids
GUIDE_REF_SLOT_LEFT_IN_WINDOW = 18
GUIDE_REF_FIRST_ROW_TOP_IN_WINDOW = 147
GUIDE_REF_ROW_WIDTH = 1042
GUIDE_REF_ROW_HEIGHT = 64
GUIDE_REF_ROW_PITCH = 74
GUIDE_MAX_ROWS = 9

MAGNET_SEARCH_PAD = 120          # px of padding around guide when searching for template
MAGNET_MIN_MATCH_SCORE = 0.42    # cv2.TM_CCOEFF_NORMED threshold for confident match
MAGNET_LIVE_SEARCH_PAD = 48
MAGNET_LIVE_REACQUIRE_PAD = 160
MAGNET_LIVE_INTERVAL_MS = 80
MAGNET_LIVE_DEADBAND_PX = 2.0
MAGNET_LIVE_MAX_CONSISTENCY_PX = 10
MAGNET_SCALE_SEARCH_RADIUS_PCT = 6
MAGNET_MAX_SCALE_DELTA = 0.035
MAGNET_MAX_CONSISTENCY_PX = 45

ACTION_BUTTON_SIZE = 36
ACTION_ICON_SIZE = 22

_INSTANCE_MUTEX_HANDLE = None


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def bundled_resource(filename: str) -> Path | None:
    """Return a bundled app resource, or None if it is unavailable.

    Release builds must not probe developer-machine absolute paths. A missing
    optional resource should degrade to Qt defaults instead of aborting startup.
    """
    path = resource_path(filename)
    try:
        return path if path.exists() else None
    except OSError:
        return None


def log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "DFOGANG_RaidHelper"


def acquire_single_instance() -> bool:
    """Return False when another Raid Helper instance is already running."""
    global _INSTANCE_MUTEX_HANDLE

    if sys.platform != "win32":
        return True

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
        if not handle:
            return True

        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False

        _INSTANCE_MUTEX_HANDLE = handle
    except Exception:
        return True

    return True


def _settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        root = Path(base)
    else:
        root = Path.home()
    return root / "DFOGANG_RaidHelper" / SETTINGS_FILE


def load_settings() -> dict:
    try:
        path = _settings_path()
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        path = _settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
    except Exception:
        pass


def _append_debug_log(filename: str, message: str) -> None:
    try:
        path = _settings_path().parent / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{stamp} {message}\n")
    except Exception:
        pass


def _protect_secret(value: str) -> str:
    raw = value.encode("utf-8")
    if sys.platform != "win32":
        return "plain:" + base64.b64encode(raw).decode("ascii")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p

    in_buf = ctypes.create_string_buffer(raw)
    in_blob = _DataBlob(len(raw), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char)))
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return "dpapi:" + base64.b64encode(encrypted).decode("ascii")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _unprotect_secret(value: str) -> str:
    if value.startswith("plain:"):
        return base64.b64decode(value[6:].encode("ascii")).decode("utf-8")
    if not value.startswith("dpapi:") or sys.platform != "win32":
        return ""

    encrypted = base64.b64decode(value[6:].encode("ascii"))
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p

    in_buf = ctypes.create_string_buffer(encrypted)
    in_blob = _DataBlob(
        len(encrypted), ctypes.cast(in_buf, ctypes.POINTER(ctypes.c_char))
    )
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        return ""
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return raw.decode("utf-8")
    finally:
        kernel32.LocalFree(out_blob.pbData)


def load_api_key() -> str:
    try:
        data = load_settings()
        return _unprotect_secret(str(data.get("neople_api_key", "")))
    except Exception:
        return ""


def save_api_key(api_key: str) -> None:
    try:
        data = load_settings()
        data = {"neople_api_key": _protect_secret(api_key.strip()) if api_key.strip() else ""}
        existing = load_settings()
        existing.update(data)
        save_settings(existing)
    except Exception:
        pass


class TitleButton(QLabel):
    def __init__(self, text: str):
        super().__init__(text)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(28, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"color: {DISABLED_TEXT_COLOR}; background: transparent;")


class ManualGuideOverlay(QWidget):
    moved = pyqtSignal()
    scale_changed = pyqtSignal(int)  # emits int(scale * 100) for slider sync

    def __init__(self, marker_x: float, marker_y: float, scale: float):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.scale = float(scale)
        self.guide_x = float(marker_x) - GUIDE_REF_MARKER_LEFT_IN_WINDOW * self.scale
        self.guide_y = float(marker_y) - GUIDE_REF_MARKER_TOP_IN_WINDOW * self.scale
        self._dragging = False
        self._resizing = False
        self._hover_handle = False
        self._hover_resize = False
        self._running = False
        self._last_global = QPoint()
        self._screen_maps: list[tuple[QRect, QRectF, float]] = []
        self._title_pixmap = self._load_guide_pixmap("resources/guide_title.png")
        self._tab_pixmap = self._load_guide_pixmap("resources/guide_tab.png")
        self._button_pixmap = self._load_guide_pixmap("resources/guide_button.png")
        self._refresh_geometry()

    @staticmethod
    def _load_guide_pixmap(rel_path: str) -> "QPixmap":
        p = bundled_resource(rel_path)
        if p is not None:
            pm = QPixmap(str(p))
            if not pm.isNull():
                return pm
        return QPixmap()

    @property
    def marker_x(self) -> float:
        return self.guide_x + GUIDE_REF_MARKER_LEFT_IN_WINDOW * self.scale

    @property
    def marker_y(self) -> float:
        return self.guide_y + GUIDE_REF_MARKER_TOP_IN_WINDOW * self.scale

    def _refresh_geometry(self) -> None:
        app = QApplication.instance()
        desktop = QRect()
        if app is not None:
            for screen in app.screens():
                desktop = desktop.united(screen.geometry())
        if not desktop.isNull():
            self.setGeometry(desktop)
        self._screen_maps = self._build_screen_maps()
        self.update()

    def _build_screen_maps(self) -> list[tuple[QRect, QRectF, float]]:
        app = QApplication.instance()
        if app is None:
            return []
        screens = app.screens()
        physical_screens = self._physical_screens()
        maps: list[tuple[QRect, QRectF, float]] = []
        used: set[int] = set()
        for screen in screens:
            logical = screen.geometry()
            dpr = max(float(screen.devicePixelRatio()), 1.0)
            expected_x = logical.x() * dpr
            expected_y = logical.y() * dpr
            expected_w = logical.width() * dpr
            expected_h = logical.height() * dpr
            # Match by position+size, not index — Win32 and Qt may enumerate monitors in different order
            best_idx = None
            best_delta = float("inf")
            for candidate_idx, physical in enumerate(physical_screens):
                if candidate_idx in used:
                    continue
                size_delta = abs(physical.width() - expected_w) + abs(physical.height() - expected_h)
                if size_delta > 8:
                    continue
                pos_delta = abs(physical.x() - expected_x) + abs(physical.y() - expected_y)
                total_delta = size_delta + pos_delta
                if total_delta < best_delta:
                    best_idx = candidate_idx
                    best_delta = total_delta
            if best_idx is not None:
                used.add(best_idx)
                physical = physical_screens[best_idx]
            else:
                physical = QRectF(expected_x, expected_y, expected_w, expected_h)
            maps.append((logical, physical, dpr))
        return maps

    def _physical_screens(self) -> list[QRectF]:
        try:
            import win32api

            out: list[QRectF] = []
            for monitor, _dc, _rect in win32api.EnumDisplayMonitors():
                info = win32api.GetMonitorInfo(monitor)
                left, top, right, bottom = info["Monitor"]
                out.append(QRectF(left, top, right - left, bottom - top))
            return out
        except Exception:
            return []

    def _force_topmost(self) -> None:
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.SetWindowPos(
                wintypes.HWND(hwnd),
                wintypes.HWND(-1),  # HWND_TOPMOST
                0,
                0,
                0,
                0,
                0x0001 | 0x0002 | 0x0010 | 0x0040,  # NOSIZE | NOMOVE | NOACTIVATE | SHOWWINDOW
            )
        except Exception:
            pass

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_geometry()
        self._force_topmost()
        self._log_geometry("show")

    def set_running(self, running: bool) -> None:
        self._running = running
        self._dragging = False
        self._resizing = False
        self._hover_handle = False
        self._hover_resize = False
        self.update()

    def set_scale(self, scale: float) -> None:
        self.scale = max(0.35, min(1.8, float(scale)))
        self.update()
        self.moved.emit()
        self.scale_changed.emit(int(round(self.scale * 100)))
        self._log_geometry("scale")

    def _screen_dpr_at(self, point: QPoint) -> float:
        app = QApplication.instance()
        if app is None:
            return 1.0
        screen = app.screenAt(point)
        if screen is None and app.screens():
            screen = app.screens()[0]
        return max(float(screen.devicePixelRatio()) if screen is not None else 1.0, 1.0)

    def _physical_to_local(self, x: float, y: float) -> QPoint:
        for logical, physical, dpr in self._screen_maps:
            if physical.contains(x, y):
                gx = logical.x() + (x - physical.x()) / dpr
                gy = logical.y() + (y - physical.y()) / dpr
                origin = self.mapToGlobal(QPoint(0, 0))
                return QPoint(int(round(gx - origin.x())), int(round(gy - origin.y())))
        origin = self.mapToGlobal(QPoint(0, 0))
        return QPoint(int(round(x - origin.x())), int(round(y - origin.y())))

    def _resize_handle_rect(self) -> QRect:
        guide = self._guide_rect()
        size = max(14, int(round(18 * self.scale)))
        return QRect(guide.right() - size, guide.bottom() - size, size, size)

    def _log_geometry(self, reason: str) -> None:
        guide = self._guide_rect()
        marker = self._marker_rect()
        handle = self._handle_rect()
        resize = self._resize_handle_rect()
        _append_debug_log(
            "manual_guide.log",
            (
                f"{reason} overlay={self.geometry().getRect()} "
                f"global_origin={self.mapToGlobal(QPoint(0, 0)).x()},{self.mapToGlobal(QPoint(0, 0)).y()} "
                f"guide_physical={self.guide_x:.2f},{self.guide_y:.2f} "
                f"marker_physical={self.marker_x:.2f},{self.marker_y:.2f} scale={self.scale:.4f} "
                f"guide={guide.getRect()} marker={marker.getRect()} "
                f"handle={handle.getRect()} resize={resize.getRect()} "
                f"maps={[ (m[0].getRect(), (m[1].x(), m[1].y(), m[1].width(), m[1].height()), m[2]) for m in self._screen_maps ]}"
            ),
        )

    def _guide_rect(self) -> QRect:
        s = self.scale
        guide = self._physical_to_local(self.guide_x, self.guide_y)
        return QRect(
            guide.x(),
            guide.y(),
            int(round(GUIDE_REF_WINDOW_SIZE[0] * s)),
            int(round(GUIDE_REF_WINDOW_SIZE[1] * s)),
        )

    def _marker_rect(self) -> QRect:
        marker = self._physical_to_local(self.marker_x, self.marker_y)
        return QRect(
            marker.x(),
            marker.y(),
            int(round(GUIDE_REF_MARKER_SIZE[0] * self.scale)),
            int(round(GUIDE_REF_MARKER_SIZE[1] * self.scale)),
        )

    def _handle_rect(self) -> QRect:
        guide = self._guide_rect()
        size = max(14, int(round(18 * self.scale)))
        return QRect(guide.left(), guide.top(), size, size)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        guide = self._guide_rect()
        marker = self._marker_rect()
        s = self.scale

        if self._running:
            # Nearly invisible solid outline while recognition is active.
            painter.setOpacity(0.18)
            painter.setPen(QPen(QColor(0, 190, 255, 255), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(guide)
            title_y = guide.top() + int(round(GUIDE_REF_TITLE_BOTTOM_Y * s))
            painter.drawLine(guide.left(), title_y, guide.right(), title_y)
            painter.drawRect(marker)
            row_left = guide.left() + int(round(GUIDE_REF_SLOT_LEFT_IN_WINDOW * s))
            row_width = int(round(GUIDE_REF_ROW_WIDTH * s))
            row_height = int(round(GUIDE_REF_ROW_HEIGHT * s))
            for row in range(GUIDE_MAX_ROWS):
                row_top_y = guide.top() + int(round(
                    (GUIDE_REF_FIRST_ROW_TOP_IN_WINDOW + row * GUIDE_REF_ROW_PITCH) * s))
                painter.drawRect(QRect(row_left, row_top_y, row_width, row_height))
            painter.setOpacity(0.25)
            painter.setPen(QPen(QColor(140, 140, 140, 255), 1))
            painter.setBrush(QColor(100, 100, 100, 80))
            painter.drawRect(self._handle_rect())
            painter.drawRect(self._resize_handle_rect())
            painter.end()
            return

        # Draw reference images as alignment aids.
        painter.setOpacity(GUIDE_OVERLAY_IMAGE_ALPHA)
        if not self._title_pixmap.isNull():
            tw = int(round(GUIDE_REF_TITLE_SIZE[0] * s))
            th = int(round(GUIDE_REF_TITLE_SIZE[1] * s))
            scaled_title = self._title_pixmap.scaled(
                tw, th,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(
                guide.left() + int(round(GUIDE_REF_TITLE_LEFT_OFFSET * s)),
                guide.top(),
                scaled_title,
            )
        if not self._tab_pixmap.isNull():
            scaled_tab = self._tab_pixmap.scaled(
                marker.width(), marker.height(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(marker.left(), marker.top(), scaled_tab)
        if not self._button_pixmap.isNull():
            bw = int(round(GUIDE_REF_BUTTON_SIZE[0] * s))
            bh = int(round(GUIDE_REF_BUTTON_SIZE[1] * s))
            scaled_btn = self._button_pixmap.scaled(
                bw, bh,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            bx = guide.right() - int(round(GUIDE_REF_BUTTON_RIGHT_MARGIN * s)) - bw
            by = guide.bottom() - int(round(GUIDE_REF_BUTTON_BOTTOM_MARGIN * s)) - bh
            painter.drawPixmap(bx, by, scaled_btn)
        painter.setOpacity(1.0)

        drag_active = not self._running and (self._hover_handle or self._dragging)
        resize_active = not self._running and (self._hover_resize or self._resizing)
        any_active = drag_active or resize_active
        outline_color = QColor(255, 0, 0, 255) if any_active else QColor(0, 190, 255, 255)
        painter.setPen(QPen(outline_color, 4 if any_active else 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        painter.drawRect(guide)
        title_y = guide.top() + int(round(GUIDE_REF_TITLE_BOTTOM_Y * s))
        painter.drawLine(guide.left(), title_y, guide.right(), title_y)
        painter.drawRect(marker)

        row_left = guide.left() + int(round(GUIDE_REF_SLOT_LEFT_IN_WINDOW * s))
        row_width = int(round(GUIDE_REF_ROW_WIDTH * s))
        row_height = int(round(GUIDE_REF_ROW_HEIGHT * s))
        for row in range(GUIDE_MAX_ROWS):
            row_top = guide.top() + int(round((GUIDE_REF_FIRST_ROW_TOP_IN_WINDOW + row * GUIDE_REF_ROW_PITCH) * s))
            painter.drawRect(QRect(row_left, row_top, row_width, row_height))

        # Drag handle — top-left.  Gray when locked (running), yellow when interactive.
        handle = self._handle_rect()
        if self._running:
            hc = QColor(160, 160, 160, 200)
            painter.setPen(QPen(QColor(160, 160, 160, 220), 3))
        else:
            hc = QColor(255, 255, 0, 210 if drag_active else 170)
            painter.setPen(QPen(QColor(255, 255, 0, 255), 3 if drag_active else 2))
        painter.setBrush(hc)
        painter.drawRect(handle)

        # Resize handle — bottom-right.  Gray when locked (running), teal/orange when interactive.
        resize_handle = self._resize_handle_rect()
        if self._running:
            rc = QColor(160, 160, 160, 200)
            painter.setPen(QPen(QColor(160, 160, 160, 220), 3))
        else:
            rc = QColor(255, 120, 0, 255) if resize_active else QColor(0, 220, 180, 255)
            painter.setPen(QPen(rc, 3 if resize_active else 2))
        painter.setBrush(QColor(rc.red(), rc.green(), rc.blue(), 210 if resize_active else 170))
        painter.drawRect(resize_handle)

        painter.end()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        if not self._running:
            over_drag = self._handle_rect().contains(pos)
            over_resize = self._resize_handle_rect().contains(pos)
            if over_drag != self._hover_handle or over_resize != self._hover_resize:
                self._hover_handle = over_drag
                self._hover_resize = over_resize
                self.update()
        if self._dragging:
            current = event.globalPosition().toPoint()
            delta = current - self._last_global
            dpr = self._screen_dpr_at(current)
            self.guide_x += delta.x() * dpr
            self.guide_y += delta.y() * dpr
            self._last_global = current
            self.update()
            self.moved.emit()
            self._log_geometry("move")
        elif self._resizing:
            guide = self._guide_rect()
            new_w = max(1, pos.x() - guide.left())
            new_scale = max(0.35, min(1.8, new_w / GUIDE_REF_WINDOW_SIZE[0]))
            self.scale = new_scale
            self.scale_changed.emit(int(round(new_scale * 100)))
            self.update()

    def mousePressEvent(self, event) -> None:
        if self._running:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            if self._handle_rect().contains(pos):
                self._dragging = True
                self._last_global = event.globalPosition().toPoint()
                self.update()
            elif self._resize_handle_rect().contains(pos):
                self._resizing = True
                self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging:
            self._dragging = False
            self.update()
            self.moved.emit()
        if self._resizing:
            self._resizing = False
            self.update()
            self.moved.emit()
            self._log_geometry("resize")

    def leaveEvent(self, event) -> None:
        if self._hover_handle or self._hover_resize:
            self._hover_handle = False
            self._hover_resize = False
            self.update()


class ControlWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.demo = None
        self.guide_overlay: ManualGuideOverlay | None = None
        self.manual_party_apply = self._load_manual_party_apply()
        self._manual_mode: bool = bool(load_settings().get("manual_mode", False))

        self._magnet_enabled: bool = False
        self._magnet_in_flight: bool = False
        self._magnet_live_misses: int = 0
        self._magnet_timer = QTimer(self)
        self._magnet_timer.setInterval(MAGNET_LIVE_INTERVAL_MS)
        self._magnet_timer.timeout.connect(self._run_magnet_live_tick)

        # Closed-but-not-yet-collected demos. Keep strong refs so worker
        # threads can finish final frames without touching destroyed QObjects.
        self._zombie_demos: list = []

        self.drag_pos: QPoint | None = None
        logo_path = bundled_resource("resources/ch49gangraidlogo.png")
        self.logo = QPixmap(str(logo_path)) if logo_path is not None else QPixmap()
        self.font_family = self._load_font()

        self.setWindowTitle(APP_DISPLAY_NAME)

        icon_path = bundled_resource("resources/ch49gangraidlogo.ico")
        if icon_path is not None:
            self.setWindowIcon(QIcon(str(icon_path)))

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(430, 178)
        self.resize(450, 188)

        self._build_ui()

    def _load_font(self) -> str:
        font_path = bundled_resource("resources/DNFForgedBlade-Bold.ttf")
        if font_path is not None:
            try:
                font_id = QFontDatabase.addApplicationFont(str(font_path))
                if font_id != -1:
                    families = QFontDatabase.applicationFontFamilies(font_id)
                    if families:
                        return families[0]
            except OSError:
                pass
        return "Segoe UI"

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(8)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)

        title_box = QVBoxLayout()
        title_box.setSpacing(0)

        title = QLabel(APP_NAME)
        title.setFont(QFont(self.font_family, 16, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {NORMAL_TEXT_COLOR}; background: transparent;")

        version = QLabel(APP_VERSION_LABEL)
        version.setFont(QFont(self.font_family, 8, QFont.Weight.Bold))
        version.setStyleSheet(f"color: {DISABLED_TEXT_COLOR}; background: transparent;")

        title_box.addWidget(title)
        title_box.addWidget(version)

        self.min_btn = TitleButton("_")
        self.close_btn = TitleButton("X")
        self.min_btn.mousePressEvent = lambda event: self.showMinimized()
        self.close_btn.mousePressEvent = lambda event: self.close()
        self.log_btn = QPushButton("LOG")
        self.log_btn.setFixedSize(48, ACTION_BUTTON_SIZE)
        self.log_btn.setToolTip("Open the folder containing debug logs.")
        self.log_btn.clicked.connect(lambda _: self._open_log_folder())

        title_row.addLayout(title_box, 1)
        title_row.addWidget(self.log_btn)
        title_row.addWidget(self.min_btn)
        title_row.addWidget(self.close_btn)
        root.addLayout(title_row)

        api_row = QHBoxLayout()
        api_row.setSpacing(8)

        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("Neople API Key")
        self.api_key_input.setToolTip(
            "Register and receive a key from Neople Developers, then paste it here."
        )
        self.api_key_input.setText(load_api_key())
        self.api_key_input.editingFinished.connect(
            lambda: save_api_key(self.api_key_input.text())
        )

        self.api_help_btn = QPushButton("?")
        self.api_help_btn.setFixedSize(ACTION_BUTTON_SIZE, ACTION_BUTTON_SIZE)
        self.api_help_btn.setToolTip(
            "Needed to try fame-based search when name recognition fails."
        )
        self.api_help_btn.clicked.connect(lambda _: self._open_api_key_page())

        api_row.addWidget(self.api_key_input, 1)
        api_row.addWidget(self.api_help_btn)
        root.addLayout(api_row)

        self.status_pill = QLabel("READY")
        self.status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_pill.setFont(QFont(self.font_family, 18, QFont.Weight.Bold))
        self.status_pill.setFixedHeight(31)
        self.status_pill.setStyleSheet(self._pill_style(DISABLED_TEXT_COLOR))

        self.toggle_btn = QPushButton()
        self.toggle_btn.setFixedSize(ACTION_BUTTON_SIZE, ACTION_BUTTON_SIZE)
        self.toggle_btn.setIconSize(QSize(ACTION_ICON_SIZE, ACTION_ICON_SIZE))
        self.toggle_btn.clicked.connect(lambda _: self.toggle_overlay())
        self._set_toggle_icon(running=False)

        self.mode_btn = QPushButton("AUTO")
        self.mode_btn.setFixedSize(64, ACTION_BUTTON_SIZE)
        self.mode_btn.setToolTip(
            "AUTO: automatically detect the request window.\n"
            "MANUAL: use the AREA guide to specify the position."
        )
        self.mode_btn.clicked.connect(lambda _: self.toggle_capture_mode())

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.addWidget(self.status_pill, 1)
        status_row.addWidget(self.mode_btn)
        status_row.addWidget(self.toggle_btn)
        root.addLayout(status_row)

        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(35, 180)
        self.scale_slider.setValue(int(round(float(self.manual_party_apply.get("scale", 1.0)) * 100)))
        self.scale_slider.setToolTip("Manual request-window guide scale.")
        self.scale_slider.valueChanged.connect(self._manual_scale_changed)

        self.area_btn = QPushButton("AREA")
        self.area_btn.setFixedSize(54, ACTION_BUTTON_SIZE)
        self.area_btn.setToolTip("Show or hide the manual request-window guide.")
        self.area_btn.clicked.connect(lambda _: self.toggle_manual_guide())

        self.magnet_btn = QPushButton("MAG")
        self.magnet_btn.setFixedSize(48, ACTION_BUTTON_SIZE)
        self.magnet_btn.setToolTip(
            "Snap to nearby anchors, then keep the guide following while enabled."
        )
        self.magnet_btn.clicked.connect(lambda _: self._toggle_magnet())

        self._manual_row = QWidget()
        manual_row = QHBoxLayout(self._manual_row)
        manual_row.setContentsMargins(0, 0, 0, 0)
        manual_row.setSpacing(8)
        manual_row.addWidget(self.scale_slider, 1)
        manual_row.addWidget(self.magnet_btn)
        manual_row.addWidget(self.area_btn)
        root.addWidget(self._manual_row)

        self._apply_mode_ui()

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(5)
        self.progress.hide()
        root.addWidget(self.progress)

        self.setStyleSheet(
            f"""
            QWidget {{
                color: {NORMAL_TEXT_COLOR};
                font-family: '{self.font_family}';
                background: transparent;
            }}
            QLabel {{
                color: {NORMAL_TEXT_COLOR};
                background: transparent;
            }}
            QPushButton {{
                background-color: {INPUT_BG_COLOR};
                border: none;
                border-radius: 8px;
                color: {HIGHLIGHT_TEXT_COLOR};
                font-size: 16px;
                font-weight: bold;
                padding: 0px;
            }}
            QPushButton:hover {{
                background-color: rgba(26, 26, 26, 220);
                color: {HIGHLIGHT_TEXT_COLOR};
            }}
            QPushButton:disabled {{
                color: {DISABLED_TEXT_COLOR};
                background-color: {INPUT_BG_COLOR};
            }}
            QLineEdit {{
                background-color: {INPUT_BG_COLOR};
                border: none;
                border-radius: 8px;
                color: {NORMAL_TEXT_COLOR};
                padding: 0px 10px;
                selection-background-color: {HIGHLIGHT_TEXT_COLOR};
            }}
            QLineEdit:disabled {{
                color: {DISABLED_TEXT_COLOR};
                background-color: {INPUT_BG_COLOR};
            }}
            QProgressBar {{
                background: {INPUT_BG_COLOR};
                border: none;
                border-radius: 4px;
            }}
            QProgressBar::chunk {{
                background: {HIGHLIGHT_TEXT_COLOR};
                border-radius: 3px;
            }}
            """
        )

    def _pill_style(self, color: str) -> str:
        return (
            "background: transparent;"
            "border: none;"
            f"color: {color};"
            "font-weight: bold;"
        )

    def _icon(self, kind: str, color: str) -> QIcon:
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))

        if kind == "stop":
            painter.drawRoundedRect(6, 6, 12, 12, 2, 2)
        else:
            painter.drawPolygon(
                QPolygon(
                    [
                        QPoint(8, 5),
                        QPoint(19, 12),
                        QPoint(8, 19),
                    ]
                )
            )

        painter.end()
        return QIcon(pixmap)

    def _set_toggle_icon(self, running: bool) -> None:
        kind = "stop" if running else "play"
        color = HIGHLIGHT_TEXT_COLOR if self.toggle_btn.isEnabled() else DISABLED_TEXT_COLOR
        self.toggle_btn.setIcon(self._icon(kind, color))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg_color = QColor(CANVAS_BG_COLOR)
        bg_color.setAlpha(int(255 * 0.9))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg_color)
        painter.drawRoundedRect(self.rect(), CANVAS_BORDER_RADIUS, CANVAS_BORDER_RADIUS)

        if not self.logo.isNull():
            pixmap = self.logo.scaledToHeight(
                int(self.height() * LOGO_HEIGHT_RATIO),
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - pixmap.width()) // 2
            y = int(self.height() * LOGO_TOP_MARGIN_RATIO)

            painter.setOpacity(LOGO_OPACITY)
            painter.drawPixmap(x, y, pixmap)
            painter.setOpacity(1.0)

        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event) -> None:
        if self.drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self.drag_pos)

    def mouseReleaseEvent(self, event) -> None:
        self.drag_pos = None

    def _load_manual_party_apply(self) -> dict:
        data = load_settings().get("manual_party_apply", {})
        return data if isinstance(data, dict) else {}

    def _save_manual_party_apply(self) -> None:
        data = load_settings()
        if self.manual_party_apply:
            data["manual_party_apply"] = self.manual_party_apply
        else:
            data.pop("manual_party_apply", None)
        save_settings(data)

    def _find_game_window_rect(self) -> tuple[int, int, int, int] | None:
        try:
            import win32gui

            needle = DEFAULT_WINDOW_TITLE.lower()
            excluded_classes = (
                "chrome_widgetwin",
                "mozilla",
                "applicationframewindow",
                "ieframe",
                "cascadia",
            )
            excluded_titles = (
                "google chrome",
                "microsoft edge",
                "mozilla firefox",
                "brave",
                "opera",
            )
            matches = []

            def enum_cb(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return
                if win32gui.IsIconic(hwnd):
                    return
                title = win32gui.GetWindowText(hwnd)
                title_l = title.lower()
                if needle not in title_l:
                    return
                class_l = win32gui.GetClassName(hwnd).lower()
                if any(name in class_l for name in excluded_classes):
                    return
                if any(name in title_l for name in excluded_titles):
                    return
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                if left <= -30000 or top <= -30000:
                    return
                if right <= left or bottom <= top:
                    return
                area = max(0, right - left) * max(0, bottom - top)
                if area >= 100_000:
                    matches.append((area, left, top, right, bottom))

            win32gui.EnumWindows(enum_cb, None)
            if not matches:
                return None
            _, left, top, right, bottom = max(matches, key=lambda item: item[0])
            return left, top, right, bottom
        except Exception:
            return None

    def _initial_manual_marker(self) -> tuple[float, float, float]:
        scale = float(self.manual_party_apply.get("scale", 1.0))
        if "marker_x_abs" in self.manual_party_apply:
            return (
                float(self.manual_party_apply["marker_x_abs"]),
                float(self.manual_party_apply["marker_y_abs"]),
                scale,
            )
        win_rect = self._find_game_window_rect()
        if win_rect is not None and "marker_x_rel" in self.manual_party_apply:
            left, top, _, _ = win_rect
            return (
                left + float(self.manual_party_apply.get("marker_x_rel", 0.0)),
                top + float(self.manual_party_apply.get("marker_y_rel", 0.0)),
                scale,
            )
        if win_rect is not None:
            left, top, _, _ = win_rect
            return (
                left + GUIDE_REF_MARKER_LEFT_IN_WINDOW * scale,
                top + GUIDE_REF_MARKER_TOP_IN_WINDOW * scale,
                scale,
            )
        return 120.0, 120.0, scale

    def toggle_capture_mode(self) -> None:
        """Switch between AUTO (template detection) and MANUAL (AREA guide)."""
        if self.demo is not None:
            return  # cannot switch while running
        if self.guide_overlay is not None:
            self._stop_magnet("mode-switch")
            self._sync_manual_from_guide()
            self.guide_overlay.close()
            self.guide_overlay = None
            self.area_btn.setText("AREA")
        self._manual_mode = not self._manual_mode
        data = load_settings()
        data["manual_mode"] = self._manual_mode
        save_settings(data)
        self._apply_mode_ui()

    def _apply_mode_ui(self) -> None:
        self.mode_btn.setText("MANUAL" if self._manual_mode else "AUTO")
        self._manual_row.setVisible(self._manual_mode)

    def _sync_slider_from_guide(self, value: int) -> None:
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(value)
        self.scale_slider.blockSignals(False)

    def toggle_manual_guide(self) -> None:
        if self.guide_overlay is not None:
            self._stop_magnet("guide-hidden")
            self._sync_manual_from_guide()
            self.guide_overlay.close()
            self.guide_overlay = None
            self.area_btn.setText("AREA")
            return

        marker_x, marker_y, scale = self._initial_manual_marker()
        self.guide_overlay = ManualGuideOverlay(marker_x, marker_y, scale)
        self.guide_overlay.moved.connect(self._sync_manual_from_guide)
        self.guide_overlay.scale_changed.connect(self._sync_slider_from_guide)
        self.guide_overlay.show()
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(int(round(scale * 100)))
        self.scale_slider.blockSignals(False)
        self.area_btn.setText("HIDE")
        self._sync_manual_from_guide()

    def _manual_scale_changed(self, value: int) -> None:
        scale = max(0.35, min(1.8, value / 100.0))
        if self.guide_overlay is not None:
            self.guide_overlay.set_scale(scale)
        else:
            self.manual_party_apply["scale"] = scale
            self._save_manual_party_apply()

    def _toggle_magnet(self) -> None:
        if self._magnet_enabled:
            self._stop_magnet("button")
            return
        self._start_magnet()

    def _start_magnet(self) -> None:
        _append_debug_log("manual_guide.log", "magnet start")
        if self.guide_overlay is None:
            self.toggle_manual_guide()

        self._set_magnet_active(True)
        ok = self._safe_run_magnet_align(allow_scale=True, live=False)
        _append_debug_log("manual_guide.log", f"magnet initial ok={ok}")
        if ok:
            self._magnet_timer.start()
        else:
            self._stop_magnet("initial-failed")

    def _stop_magnet(self, reason: str) -> None:
        if self._magnet_timer.isActive():
            self._magnet_timer.stop()
        if self._magnet_enabled:
            _append_debug_log("manual_guide.log", f"magnet stop reason={reason}")
        self._magnet_in_flight = False
        self._magnet_live_misses = 0
        self._set_magnet_active(False)

    def _run_magnet_live_tick(self) -> None:
        if not self._magnet_enabled or self._magnet_in_flight:
            return
        if self.guide_overlay is None or not self.guide_overlay.isVisible():
            self._stop_magnet("guide-missing")
            return
        if self._safe_run_magnet_align(allow_scale=False, live=True):
            self._magnet_live_misses = 0
        else:
            self._magnet_live_misses = min(self._magnet_live_misses + 1, 10)

    def _safe_run_magnet_align(self, *, allow_scale: bool, live: bool) -> bool:
        self._magnet_in_flight = True
        try:
            return self._run_magnet_align(allow_scale=allow_scale, live=live)
        except BaseException as exc:
            _append_debug_log(
                "manual_guide.log",
                f"magnet crashed: {type(exc).__name__}: {exc}",
            )
            return False
        finally:
            self._magnet_in_flight = False

    def _set_magnet_active(self, active: bool) -> None:
        self._magnet_enabled = active
        if active:
            self.magnet_btn.setStyleSheet(
                f"background-color: {HIGHLIGHT_TEXT_COLOR}; color: #000000;"
            )
        else:
            self.magnet_btn.setStyleSheet("")

    def _run_magnet_align(self, *, allow_scale: bool = True, live: bool = False) -> bool:
        """Template-match guide anchors on screen to fine-tune position and scale."""
        verbose = not live
        if verbose:
            _append_debug_log("manual_guide.log", "magnet step=import")
        try:
            import cv2
            import numpy as np
            import mss as _mss
            from PIL import Image
        except Exception as exc:
            _append_debug_log(
                "manual_guide.log",
                f"magnet import failed: {type(exc).__name__}: {exc}",
            )
            return False

        guide = self.guide_overlay
        if guide is not None:
            gx = guide.guide_x
            gy = guide.guide_y
            current_scale = guide.scale
        elif "guide_x_abs" in self.manual_party_apply:
            gx = float(self.manual_party_apply["guide_x_abs"])
            gy = float(self.manual_party_apply["guide_y_abs"])
            current_scale = float(self.manual_party_apply.get("scale", 1.0))
        else:
            _append_debug_log("manual_guide.log", "magnet failed: no guide calibration")
            return False
        if verbose:
            _append_debug_log(
                "manual_guide.log",
                f"magnet step=geometry guide={gx:.2f},{gy:.2f} scale={current_scale:.4f}",
            )

        def _first_resource(*names: str) -> Path | None:
            for name in names:
                p = bundled_resource(name)
                if p is not None:
                    return p
            return None

        anchors = [
            {
                "name": "title",
                "path": _first_resource("resources/100pctTitle.png", "resources/guide_title.png"),
                "ref_size": GUIDE_REF_TITLE_SIZE,
                "offset": (GUIDE_REF_TITLE_LEFT_OFFSET, 0),
                "weight": 0.9,
            },
            {
                "name": "tab",
                "path": _first_resource("resources/100pctTab.png", "resources/guide_tab.png"),
                "ref_size": GUIDE_REF_MARKER_SIZE,
                "offset": (GUIDE_REF_MARKER_LEFT_IN_WINDOW, GUIDE_REF_MARKER_TOP_IN_WINDOW),
                "weight": 1.2,
            },
            {
                "name": "button",
                "path": _first_resource("resources/100pctButton.png", "resources/guide_button.png"),
                "ref_size": GUIDE_REF_BUTTON_SIZE,
                "offset": (
                    GUIDE_REF_WINDOW_SIZE[0] - GUIDE_REF_BUTTON_RIGHT_MARGIN - GUIDE_REF_BUTTON_SIZE[0],
                    GUIDE_REF_WINDOW_SIZE[1] - GUIDE_REF_BUTTON_BOTTOM_MARGIN - GUIDE_REF_BUTTON_SIZE[1],
                ),
                "weight": 1.0,
            },
        ]
        anchors = [a for a in anchors if a["path"] is not None]
        reacquiring = live and self._magnet_live_misses >= 2
        if live and not reacquiring:
            top_anchors = [a for a in anchors if a["name"] in {"title", "tab"}]
            if top_anchors:
                anchors = top_anchors
        if not anchors:
            _append_debug_log("manual_guide.log", "magnet failed: no anchor templates")
            return False
        if verbose:
            _append_debug_log(
                "manual_guide.log",
                "magnet step=anchors " + ",".join(str(a["name"]) for a in anchors),
            )

        if live:
            gw = max(
                (anchor["offset"][0] + anchor["ref_size"][0]) * current_scale
                for anchor in anchors
            )
            gh = max(
                (anchor["offset"][1] + anchor["ref_size"][1]) * current_scale
                for anchor in anchors
            )
            pad = MAGNET_LIVE_REACQUIRE_PAD if reacquiring else MAGNET_LIVE_SEARCH_PAD
        else:
            gw = GUIDE_REF_WINDOW_SIZE[0] * current_scale
            gh = GUIDE_REF_WINDOW_SIZE[1] * current_scale
            pad = MAGNET_SEARCH_PAD

        try:
            with _mss.mss() as sct:
                virtual = sct.monitors[0]
                vx = int(virtual.get("left", 0))
                vy = int(virtual.get("top", 0))
                vw = int(virtual.get("width", 0))
                vh = int(virtual.get("height", 0))
                sx = int(max(vx, gx - pad))
                sy = int(max(vy, gy - pad))
                ex = int(min(vx + vw, gx + gw + pad))
                ey = int(min(vy + vh, gy + gh + pad))
                sw = max(0, ex - sx)
                sh = max(0, ey - sy)
                if sw < 20 or sh < 20:
                    _append_debug_log("manual_guide.log", "magnet failed: empty search region")
                    return False
                if verbose:
                    _append_debug_log(
                        "manual_guide.log",
                        f"magnet step=capture rect=({sx},{sy},{sw},{sh}) virtual=({vx},{vy},{vw},{vh})",
                    )
                shot = sct.grab({"left": sx, "top": sy, "width": sw, "height": sh})
            search_img = np.ascontiguousarray(np.array(shot)[:, :, :3])
        except Exception as exc:
            _append_debug_log(
                "manual_guide.log",
                f"magnet capture failed: {type(exc).__name__}: {exc}",
            )
            return False

        templates = []
        try:
            for anchor in anchors:
                rgba = np.array(Image.open(str(anchor["path"])).convert("RGBA"))
                bgr = np.ascontiguousarray(rgba[:, :, :3][:, :, ::-1])
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                templates.append((anchor, gray))
        except Exception as exc:
            _append_debug_log(
                "manual_guide.log",
                f"magnet template failed: {type(exc).__name__}: {exc}",
            )
            return False

        best_score = -1.0
        best_guide_x = gx
        best_guide_y = gy
        best_scale = current_scale
        best_hits: list[str] = []
        best_hit_count = 0
        search_gray = cv2.cvtColor(search_img, cv2.COLOR_BGR2GRAY)

        try:
            if allow_scale:
                scale_offsets = range(-MAGNET_SCALE_SEARCH_RADIUS_PCT, MAGNET_SCALE_SEARCH_RADIUS_PCT + 1)
            else:
                scale_offsets = (0,)

            for pct_offset in scale_offsets:
                s = max(0.35, min(1.8, current_scale * (100 + pct_offset) / 100.0))
                if allow_scale and abs(s - current_scale) > MAGNET_MAX_SCALE_DELTA:
                    continue
                hits = []
                for anchor, tmpl_gray in templates:
                    ref_w, ref_h = anchor["ref_size"]
                    tw = int(round(ref_w * s))
                    th = int(round(ref_h * s))
                    if tw < 8 or th < 4 or tw >= search_gray.shape[1] or th >= search_gray.shape[0]:
                        continue
                    off_x, off_y = anchor["offset"]
                    exp_x = gx + off_x * s
                    exp_y = gy + off_y * s
                    crop_x0 = max(0, int(round(exp_x - sx - pad)))
                    crop_y0 = max(0, int(round(exp_y - sy - pad)))
                    crop_x1 = min(search_gray.shape[1], int(round(exp_x - sx + tw + pad)))
                    crop_y1 = min(search_gray.shape[0], int(round(exp_y - sy + th + pad)))
                    if crop_x1 - crop_x0 <= tw or crop_y1 - crop_y0 <= th:
                        continue
                    tmpl = cv2.resize(
                        tmpl_gray,
                        (tw, th),
                        interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC,
                    )
                    crop = search_gray[crop_y0:crop_y1, crop_x0:crop_x1]
                    result = cv2.matchTemplate(crop, tmpl, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(result)
                    hit_x = sx + crop_x0 + max_loc[0]
                    hit_y = sy + crop_y0 + max_loc[1]
                    guide_guess_x = hit_x - off_x * s
                    guide_guess_y = hit_y - off_y * s
                    hits.append((anchor["name"], float(max_val), float(anchor["weight"]), guide_guess_x, guide_guess_y))
                usable = [h for h in hits if h[1] >= MAGNET_MIN_MATCH_SCORE]
                if not usable:
                    continue
                if allow_scale and abs(s - current_scale) > 0.001 and len(usable) < 2:
                    continue
                weight_sum = sum(score * weight for _, score, weight, _, _ in usable)
                if weight_sum <= 0:
                    continue
                guess_x = sum(x * score * weight for _, score, weight, x, _ in usable) / weight_sum
                guess_y = sum(y * score * weight for _, score, weight, _, y in usable) / weight_sum
                consistency = max(
                    ((x - guess_x) ** 2 + (y - guess_y) ** 2) ** 0.5
                    for _, _, _, x, y in usable
                )
                max_consistency = (
                    MAGNET_LIVE_MAX_CONSISTENCY_PX if live else MAGNET_MAX_CONSISTENCY_PX
                )
                if len(usable) >= 2 and consistency > max_consistency * max(0.75, s):
                    continue
                avg_score = sum(score * weight for _, score, weight, _, _ in usable) / sum(weight for _, _, weight, _, _ in usable)
                scale_penalty = abs(s - current_scale) * 1.7 if allow_scale else 0.0
                combined = (
                    avg_score
                    + min(len(usable), 3) * 0.05
                    - min(consistency / 120.0, 0.50)
                    - scale_penalty
                )
                if combined > best_score:
                    best_score = combined
                    best_guide_x = guess_x
                    best_guide_y = guess_y
                    best_scale = s
                    best_hits = [f"{name}:{score:.3f}" for name, score, _, _, _ in usable]
                    best_hit_count = len(usable)
        except Exception as exc:
            _append_debug_log(
                "manual_guide.log",
                f"magnet match failed: {type(exc).__name__}: {exc}",
            )
            return False

        if best_score < MAGNET_MIN_MATCH_SCORE:
            if verbose:
                _append_debug_log(
                    "manual_guide.log",
                    f"magnet no match: score={best_score:.4f}",
                )
            return False
        if live and len(templates) >= 2 and best_hit_count < 2:
            return False

        if not allow_scale or best_hit_count < 2:
            best_scale = current_scale
        else:
            lower = current_scale - MAGNET_MAX_SCALE_DELTA
            upper = current_scale + MAGNET_MAX_SCALE_DELTA
            best_scale = max(lower, min(upper, best_scale))

        new_guide_x = float(best_guide_x)
        new_guide_y = float(best_guide_y)
        new_marker_x = new_guide_x + GUIDE_REF_MARKER_LEFT_IN_WINDOW * best_scale
        new_marker_y = new_guide_y + GUIDE_REF_MARKER_TOP_IN_WINDOW * best_scale

        if guide is not None:
            dx = new_guide_x - float(guide.guide_x)
            dy = new_guide_y - float(guide.guide_y)
            if (
                live
                and ((dx * dx + dy * dy) ** 0.5) < MAGNET_LIVE_DEADBAND_PX
                and abs(float(guide.scale) - best_scale) < 0.0005
            ):
                return True
            guide.guide_x = round(new_guide_x) if live else new_guide_x
            guide.guide_y = round(new_guide_y) if live else new_guide_y
            guide.scale = best_scale
            guide.update()
            guide.moved.emit()
            if not live:
                guide.scale_changed.emit(int(round(best_scale * 100)))
        else:
            s = best_scale
            self.manual_party_apply = {
                "enabled": True,
                "guide_x_abs": round(new_guide_x, 2),
                "guide_y_abs": round(new_guide_y, 2),
                "guide_w": round(GUIDE_REF_WINDOW_SIZE[0] * s, 2),
                "guide_h": round(GUIDE_REF_WINDOW_SIZE[1] * s, 2),
                "marker_x_abs": round(new_marker_x, 2),
                "marker_y_abs": round(new_marker_y, 2),
                "scale": round(s, 4),
            }
            self._save_manual_party_apply()
            self.scale_slider.blockSignals(True)
            self.scale_slider.setValue(int(round(s * 100)))
            self.scale_slider.blockSignals(False)

        if verbose:
            _append_debug_log(
                "manual_guide.log",
                (
                    f"magnet matched score={best_score:.4f} "
                    f"guide={new_guide_x:.2f},{new_guide_y:.2f} "
                    f"marker={new_marker_x:.2f},{new_marker_y:.2f} "
                    f"scale={best_scale:.4f} hits={best_hits}"
                ),
            )
        return True

    def _sync_manual_from_guide(self) -> None:
        guide = self.guide_overlay
        if guide is None:
            return
        s = guide.scale
        self.manual_party_apply = {
            "enabled": True,
            "guide_x_abs": round(float(guide.guide_x), 2),
            "guide_y_abs": round(float(guide.guide_y), 2),
            "guide_w": round(GUIDE_REF_WINDOW_SIZE[0] * s, 2),
            "guide_h": round(GUIDE_REF_WINDOW_SIZE[1] * s, 2),
            "marker_x_abs": round(float(guide.marker_x), 2),
            "marker_y_abs": round(float(guide.marker_y), 2),
            "scale": round(s, 4),
        }
        self._save_manual_party_apply()

    def toggle_overlay(self) -> None:
        if self.demo is not None:
            self.stop_overlay()
        else:
            self.start_overlay()

    def start_overlay(self) -> None:
        self.stop_overlay()

        self.toggle_btn.setEnabled(False)
        self._set_toggle_icon(running=False)
        self._set_controls_enabled(False)
        self.progress.show()
        self.status_pill.setText("STARTING")
        self.status_pill.setStyleSheet(self._pill_style(HIGHLIGHT_TEXT_COLOR))

        try:
            save_api_key(self.api_key_input.text())
            self._sync_manual_from_guide()
            if self.guide_overlay is not None:
                self.guide_overlay.set_running(True)
            from app import LiveDemo

            manual_cfg = None
            if self._manual_mode and (
                self.manual_party_apply.get("marker_x_abs") is not None
                or self.manual_party_apply.get("marker_x_rel") is not None
            ):
                manual_cfg = self.manual_party_apply

            self.demo = LiveDemo(
                capture_interval_ms=DEFAULT_CAPTURE_INTERVAL_MS,
                demo_scores=False,
                monitor_index=None,
                window_title=DEFAULT_WINDOW_TITLE,
                neople_api_key=self.api_key_input.text().strip(),
                mode="party_apply",
                manual_party_apply=manual_cfg,
                unavailable_callback=self._overlay_unavailable,
                waiting_callback=self._overlay_waiting,
                recovered_callback=self._overlay_recovered,
                ready_callback=self._overlay_ready,
            )
            self.demo.start()
        except Exception as exc:
            self.demo = None
            self.progress.hide()
            self._set_controls_enabled(True)
            self.toggle_btn.setEnabled(True)
            self._set_toggle_icon(running=False)
            self.status_pill.setText("START FAILED")
            self.status_pill.setStyleSheet(self._pill_style("#f85149"))
            self._log(str(exc))
            return

        self.progress.hide()
        self._set_controls_enabled(False)
        self.toggle_btn.setEnabled(True)
        self._set_toggle_icon(running=True)

        # Show LOADING until the first frame is processed.
        self.status_pill.setText("LOADING")
        self.status_pill.setStyleSheet(self._pill_style(HIGHLIGHT_TEXT_COLOR))

    def stop_overlay(self) -> None:
        if self.demo is not None:
            self.demo.close()
            self._zombie_demos.append(self.demo)
            del self._zombie_demos[:-2]

        self.demo = None
        if self.guide_overlay is not None:
            self.guide_overlay.set_running(False)
        self.progress.hide()
        self._set_controls_enabled(True)
        self.toggle_btn.setEnabled(True)
        self._set_toggle_icon(running=False)
        self.status_pill.setText("READY")
        self.status_pill.setStyleSheet(self._pill_style(DISABLED_TEXT_COLOR))

    def closeEvent(self, event) -> None:
        self._stop_magnet("app-close")
        if self.guide_overlay is not None:
            self._sync_manual_from_guide()
            self.guide_overlay.close()
            self.guide_overlay = None
        self.stop_overlay()
        super().closeEvent(event)

        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _log(self, message: str) -> None:
        # Keep release GUI quiet.
        pass

    def _open_api_key_page(self) -> None:
        QDesktopServices.openUrl(QUrl(NEOPLE_KEY_URL))

    def _open_log_folder(self) -> None:
        path = log_dir()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _overlay_unavailable(self) -> None:
        if self.demo is not None:
            self.demo.close()
            self.demo = None
        if self.guide_overlay is not None:
            self.guide_overlay.set_running(False)
        self.progress.hide()
        self._set_controls_enabled(True)
        self.toggle_btn.setEnabled(True)
        self._set_toggle_icon(running=False)
        self.status_pill.setText("RAID WINDOW CLOSED")
        self.status_pill.setStyleSheet(self._pill_style(DISABLED_TEXT_COLOR))

    def _overlay_waiting(self) -> None:
        self.status_pill.setText("WAITING FOR RAID WINDOW")
        self.status_pill.setStyleSheet(self._pill_style(HIGHLIGHT_TEXT_COLOR))

    def _overlay_recovered(self) -> None:
        self.status_pill.setText("RUNNING")
        self.status_pill.setStyleSheet(self._pill_style(HIGHLIGHT_TEXT_COLOR))

    def _overlay_ready(self) -> None:
        """Fired once after the first frame has been fully processed."""
        if self.status_pill.text() == "LOADING":
            self.status_pill.setText("RUNNING")
            self.status_pill.setStyleSheet(self._pill_style(HIGHLIGHT_TEXT_COLOR))

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.api_key_input.setEnabled(enabled)
        self.api_help_btn.setEnabled(enabled)
        self.mode_btn.setEnabled(enabled)
        self.scale_slider.setEnabled(enabled)
        self.area_btn.setEnabled(enabled)


def main() -> int:
    if not acquire_single_instance():
        return 0

    try:
        import pyi_splash
    except Exception:
        pyi_splash = None

    configure_qt_high_dpi()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)

    win = ControlWindow()
    win.show()

    if pyi_splash is not None:
        try:
            pyi_splash.close()
        except Exception:
            pass

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
