"""Portable-style PyQt control panel for DFOGANG Raid Helper."""

from __future__ import annotations

import os
import sys
import base64
import ctypes
from ctypes import wintypes
import json
from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QUrl, pyqtSignal
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
    QFileDialog,
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
APP_VERSION = "v1.0beta"

try:
    from build_info import BUILD_HASH as _BUILD_HASH  # noqa: E402
    _BUILD_ID = _BUILD_HASH if _BUILD_HASH and _BUILD_HASH != "dev" else "dev"
except Exception:
    _BUILD_ID = "dev"

APP_DISPLAY_NAME = f"{APP_NAME} {APP_VERSION}"
APP_VERSION_LABEL = f"{APP_VERSION} ({_BUILD_ID})"
INSTANCE_MUTEX_NAME = r"Local\DFOGANG_RaidHelper_v1"

PORTABLE_DIR = Path(r"C:\Users\Noble\Desktop\works\DFOGANG")
DEFAULT_CAPTURE_INTERVAL_MS = 0
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

GUIDE_REF_MARKER_SIZE = (734, 16)
GUIDE_REF_MARKER_LEFT_IN_WINDOW = 38
GUIDE_REF_MARKER_TOP_IN_WINDOW = 83
GUIDE_REF_WINDOW_SIZE = (812, 590)
GUIDE_REF_FIRST_ROW_TOP_DY = 36
GUIDE_REF_ROW_PITCH = 56
GUIDE_MAX_ROWS = 6

ACTION_BUTTON_SIZE = 36
ACTION_ICON_SIZE = 22

_INSTANCE_MUTEX_HANDLE = None


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def bundled_or_portable(filename: str) -> Path:
    bundled = resource_path(filename)
    if bundled.exists():
        return bundled
    return PORTABLE_DIR / filename


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

    def __init__(self, marker_x: float, marker_y: float, scale: float):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.marker_x = float(marker_x)
        self.marker_y = float(marker_y)
        self.scale = float(scale)
        self._dragging = False
        self._hover_handle = False
        self._last_global = QPoint()
        self._refresh_geometry()

    def _refresh_geometry(self) -> None:
        app = QApplication.instance()
        desktop = QRect()
        if app is not None:
            for screen in app.screens():
                desktop = desktop.united(screen.geometry())
        if not desktop.isNull():
            self.setGeometry(desktop)

    def set_scale(self, scale: float) -> None:
        self.scale = max(0.35, min(1.8, float(scale)))
        self.update()
        self.moved.emit()

    def _screen_dpr_at(self, point: QPoint) -> float:
        app = QApplication.instance()
        if app is None:
            return 1.0
        screen = app.screenAt(point)
        if screen is None and app.screens():
            screen = app.screens()[0]
        return max(float(screen.devicePixelRatio()) if screen is not None else 1.0, 1.0)

    def _physical_to_local(self, x: float, y: float) -> QPoint:
        app = QApplication.instance()
        if app is not None:
            for screen in app.screens():
                geo = screen.geometry()
                dpr = self._screen_dpr_at(geo.center())
                px0 = geo.x() * dpr
                py0 = geo.y() * dpr
                px1 = px0 + geo.width() * dpr
                py1 = py0 + geo.height() * dpr
                if px0 <= x <= px1 and py0 <= y <= py1:
                    gx = geo.x() + (x - px0) / dpr
                    gy = geo.y() + (y - py0) / dpr
                    origin = self.mapToGlobal(QPoint(0, 0))
                    return QPoint(int(round(gx - origin.x())), int(round(gy - origin.y())))
        origin = self.mapToGlobal(QPoint(0, 0))
        return QPoint(int(round(x - origin.x())), int(round(y - origin.y())))

    def _guide_rect(self) -> QRect:
        s = self.scale
        marker = self._physical_to_local(self.marker_x, self.marker_y)
        left = marker.x() - int(round(GUIDE_REF_MARKER_LEFT_IN_WINDOW * s))
        top = marker.y() - int(round(GUIDE_REF_MARKER_TOP_IN_WINDOW * s))
        return QRect(
            left,
            top,
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
        return QRect(guide.right() - size + 1, guide.top(), size, size)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self._hover_handle or self._dragging
        color = QColor(0, 154, 218, 230 if active else 80)
        width = 3 if active else 1
        painter.setPen(QPen(color, width))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        guide = self._guide_rect()
        marker = self._marker_rect()
        painter.drawRect(guide)
        painter.drawRect(marker)

        x0 = marker.left()
        x1 = marker.right()
        for row in range(GUIDE_MAX_ROWS + 1):
            y = marker.top() + int(round((GUIDE_REF_FIRST_ROW_TOP_DY + row * GUIDE_REF_ROW_PITCH) * self.scale))
            painter.drawLine(x0, y, x1, y)

        handle = self._handle_rect()
        handle_color = QColor(0, 154, 218, 240 if active else 95)
        painter.setPen(QPen(handle_color, 2 if active else 1))
        painter.setBrush(QColor(0, 154, 218, 70 if active else 20))
        painter.drawRect(handle)
        painter.end()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()
        over = self._handle_rect().contains(pos)
        if over != self._hover_handle:
            self._hover_handle = over
            self.update()
        if self._dragging:
            current = event.globalPosition().toPoint()
            delta = current - self._last_global
            dpr = self._screen_dpr_at(current)
            self.marker_x += delta.x() * dpr
            self.marker_y += delta.y() * dpr
            self._last_global = current
            self.update()
            self.moved.emit()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._handle_rect().contains(event.position().toPoint()):
            self._dragging = True
            self._last_global = event.globalPosition().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging:
            self._dragging = False
            self.update()
            self.moved.emit()

    def leaveEvent(self, event) -> None:
        if self._hover_handle:
            self._hover_handle = False
            self.update()


class ControlWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.demo = None
        self.guide_overlay: ManualGuideOverlay | None = None
        self.manual_party_apply = self._load_manual_party_apply()
        self.test_image_path = str(load_settings().get("test_image_path", "") or "")

        # Closed-but-not-yet-collected demos. Keep strong refs so worker
        # threads can finish final frames without touching destroyed QObjects.
        self._zombie_demos: list = []

        self.drag_pos: QPoint | None = None
        self.logo = QPixmap(str(bundled_or_portable("logo.png")))
        self.font_family = self._load_font()

        self.setWindowTitle(APP_DISPLAY_NAME)

        icon_path = bundled_or_portable("ch49gangraidlogo.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(430, 178)
        self.resize(450, 188)

        self._build_ui()

    def _load_font(self) -> str:
        font_path = bundled_or_portable("DNFForgedBlade-Bold.ttf")
        if font_path.exists():
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            if font_id != -1:
                families = QFontDatabase.applicationFontFamilies(font_id)
                if families:
                    return families[0]
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

        title_row.addLayout(title_box, 1)
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

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.addWidget(self.status_pill, 1)
        status_row.addWidget(self.toggle_btn)
        root.addLayout(status_row)

        manual_row = QHBoxLayout()
        manual_row.setSpacing(8)

        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(35, 180)
        self.scale_slider.setValue(int(round(float(self.manual_party_apply.get("scale", 1.0)) * 100)))
        self.scale_slider.setToolTip("Manual request-window guide scale.")
        self.scale_slider.valueChanged.connect(self._manual_scale_changed)

        self.area_btn = QPushButton("AREA")
        self.area_btn.setFixedSize(54, ACTION_BUTTON_SIZE)
        self.area_btn.setToolTip("Show or hide the manual request-window guide.")
        self.area_btn.clicked.connect(lambda _: self.toggle_manual_guide())

        self.image_btn = QPushButton("IMG")
        self.image_btn.setFixedSize(48, ACTION_BUTTON_SIZE)
        self.image_btn.setToolTip("Select a screenshot for debug testing.")
        self.image_btn.clicked.connect(lambda _: self.select_test_image())

        manual_row.addWidget(self.scale_slider, 1)
        manual_row.addWidget(self.area_btn)
        manual_row.addWidget(self.image_btn)
        root.addLayout(manual_row)

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
            matches = []

            def enum_cb(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return
                title = win32gui.GetWindowText(hwnd)
                if needle not in title.lower():
                    return
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                area = max(0, right - left) * max(0, bottom - top)
                if area > 0:
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

    def toggle_manual_guide(self) -> None:
        if self.guide_overlay is not None:
            self._sync_manual_from_guide()
            self.guide_overlay.close()
            self.guide_overlay = None
            self.area_btn.setText("AREA")
            return

        marker_x, marker_y, scale = self._initial_manual_marker()
        self.guide_overlay = ManualGuideOverlay(marker_x, marker_y, scale)
        self.guide_overlay.moved.connect(self._sync_manual_from_guide)
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

    def _sync_manual_from_guide(self) -> None:
        guide = self.guide_overlay
        if guide is None:
            return
        win_rect = self._find_game_window_rect()
        if win_rect is None:
            left, top = 0, 0
        else:
            left, top, _, _ = win_rect
        self.manual_party_apply = {
            "enabled": True,
            "marker_x_rel": round(float(guide.marker_x - left), 2),
            "marker_y_rel": round(float(guide.marker_y - top), 2),
            "scale": round(float(guide.scale), 4),
        }
        self._save_manual_party_apply()

    def select_test_image(self) -> None:
        initial = str(Path(self.test_image_path).parent) if self.test_image_path else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select screenshot",
            initial,
            "Images (*.png *.jpg *.jpeg *.bmp);;All Files (*)",
        )
        if not path:
            return
        self.test_image_path = path
        data = load_settings()
        data["test_image_path"] = self.test_image_path
        save_settings(data)
        self.status_pill.setText("IMAGE SELECTED")
        self.status_pill.setStyleSheet(self._pill_style(HIGHLIGHT_TEXT_COLOR))

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
                self.guide_overlay.close()
                self.guide_overlay = None
                self.area_btn.setText("AREA")
            from app import LiveDemo

            self.demo = LiveDemo(
                capture_interval_ms=DEFAULT_CAPTURE_INTERVAL_MS,
                demo_scores=False,
                monitor_index=None,
                window_title=DEFAULT_WINDOW_TITLE,
                neople_api_key=self.api_key_input.text().strip(),
                mode="party_apply",
                manual_party_apply=self.manual_party_apply if self.manual_party_apply.get("marker_x_rel") is not None else None,
                test_image_path=self.test_image_path or None,
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
        self.progress.hide()
        self._set_controls_enabled(True)
        self.toggle_btn.setEnabled(True)
        self._set_toggle_icon(running=False)
        self.status_pill.setText("READY")
        self.status_pill.setStyleSheet(self._pill_style(DISABLED_TEXT_COLOR))

    def closeEvent(self, event) -> None:
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

    def _overlay_unavailable(self) -> None:
        if self.demo is not None:
            self.demo.close()
            self.demo = None

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
        self.scale_slider.setEnabled(enabled)
        self.area_btn.setEnabled(enabled)
        self.image_btn.setEnabled(enabled)


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
