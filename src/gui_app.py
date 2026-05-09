"""Portable-style PyQt control panel for DFOGANG Raid Helper."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import QPoint, QSize, Qt, QUrl
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QIcon,
    QPainter,
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
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from resources import resource_path  # noqa: E402
from qt_dpi import configure_qt_high_dpi  # noqa: E402

APP_NAME = "DFOGANG Raid Helper"
APP_VERSION = "v1.0beta"
APP_DISPLAY_NAME = f"{APP_NAME} {APP_VERSION}"

PORTABLE_DIR = Path(r"C:\Users\Noble\Desktop\works\DFOGANG")
DEFAULT_CAPTURE_INTERVAL_MS = 250
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

ACTION_BUTTON_SIZE = 36
ACTION_ICON_SIZE = 22


def bundled_or_portable(filename: str) -> Path:
    bundled = resource_path(filename)
    if bundled.exists():
        return bundled
    return PORTABLE_DIR / filename


class TitleButton(QLabel):
    def __init__(self, text: str):
        super().__init__(text)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(28, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"color: {DISABLED_TEXT_COLOR}; background: transparent;")


class ControlWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.demo = None

        # Closed-but-not-yet-collected demos. Keep strong refs so worker
        # threads can finish final frames without touching destroyed QObjects.
        self._zombie_demos: list = []

        self.drag_pos: QPoint | None = None
        self.logo = QPixmap(str(bundled_or_portable("logo.png")))
        self.font_family = self._load_font()

        self.setWindowTitle(APP_DISPLAY_NAME)

        icon_path = bundled_or_portable("favicon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(330, 142)
        self.resize(350, 152)

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

        version = QLabel(APP_VERSION)
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
            from app import LiveDemo

            self.demo = LiveDemo(
                capture_interval_ms=DEFAULT_CAPTURE_INTERVAL_MS,
                demo_scores=False,
                monitor_index=None,
                window_title=DEFAULT_WINDOW_TITLE,
                neople_api_key=self.api_key_input.text().strip(),
                mode="party_apply",
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


def main() -> int:
    configure_qt_high_dpi()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)

    win = ControlWindow()
    win.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
