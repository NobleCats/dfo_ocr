"""Portable-style PyQt control panel for DFOGANG Raid Helper."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PyQt6.QtCore import QPoint, QSettings, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import (QColor, QDesktopServices, QFont, QFontDatabase, QIcon,
                         QPainter, QPixmap, QPolygon, QTextCursor)
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from resources import resource_path  # noqa: E402
from qt_dpi import configure_qt_high_dpi  # noqa: E402


APP_NAME = "DFOGANG Raid Helper"
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


class DebugLogWindow(QWidget):
    """Floating log viewer that tails the rotating debug.log file.

    Errors and warnings are coloured. Polling beats wiring a Qt log handler
    because the log file is also written by background worker threads — and
    by the dfogang/Neople clients that don't go through the Qt event loop.
    """

    POLL_MS = 500
    TAIL_LINES = 200

    def __init__(self, log_path: Path | None,
                 demo_provider=None):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} — Debug")
        self.setWindowFlags(Qt.WindowType.Window
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.resize(720, 360)
        self._log_path = log_path
        self._last_size = 0
        self._last_inode_marker: tuple[int, int] | None = None  # (size, mtime)
        # Callable returning the live LiveDemo (or None). Indirection so the
        # debug window survives demo restarts.
        self._demo_provider = demo_provider

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        header = QLabel(str(log_path) if log_path else "(no log file)")
        header.setStyleSheet(f"color: {DISABLED_TEXT_COLOR};")
        header_row.addWidget(header, 1)

        self.save_frame_btn = QPushButton("Save Frame")
        self.save_frame_btn.setFixedHeight(24)
        self.save_frame_btn.setToolTip(
            "Dump the most recently captured screen frame to "
            "%LOCALAPPDATA%/DFOGANG_RaidHelper/last_frame.png for inspection.")
        self.save_frame_btn.clicked.connect(self._save_frame)
        header_row.addWidget(self.save_frame_btn)
        layout.addLayout(header_row)

        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.view.setStyleSheet(
            f"background: {INPUT_BG_COLOR}; color: {NORMAL_TEXT_COLOR};"
            f" font-family: Consolas, 'Courier New', monospace;"
            f" font-size: 12px;")
        layout.addWidget(self.view, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(self.POLL_MS)
        self._refresh()

    def _save_frame(self) -> None:
        """Dump the demo's most recent captured frame to disk."""
        demo = self._demo_provider() if self._demo_provider else None
        if demo is None or getattr(demo, "_last_frame", None) is None:
            self._flash_btn("(no frame yet — start overlay first)")
            return
        try:
            from PIL import Image
        except Exception:
            self._flash_btn("PIL unavailable")
            return
        out = (self._log_path.parent if self._log_path else Path("."))
        out.mkdir(parents=True, exist_ok=True)
        path = out / "last_frame.png"
        try:
            Image.fromarray(demo._last_frame).save(path)
        except Exception as exc:
            self._flash_btn(f"save failed: {exc}")
            return
        meta = getattr(demo, "_last_frame_meta", None)
        meta_text = ""
        if meta:
            meta_text = (
                f"  found={meta.get('found')}  "
                f"score={meta.get('score', 0):.2f}  "
                f"scale={meta.get('scale', 0):.2f}  "
                f"frame={meta.get('frame_size')}  "
                f"ui_factor={meta.get('ui_scale_factor', 0):.2f}  "
                f"near={meta.get('near_scale', 0):.2f}")
        self._flash_btn(f"saved → {path}{meta_text}")

    def _flash_btn(self, message: str) -> None:
        """Briefly replace the Save Frame button text with a status message."""
        original = self.save_frame_btn.text()
        self.save_frame_btn.setText(message[:80])
        QTimer.singleShot(3000,
                          lambda: self.save_frame_btn.setText(original))

    def _refresh(self) -> None:
        if not self._log_path or not self._log_path.exists():
            self.view.setHtml(
                f'<span style="color: {DISABLED_TEXT_COLOR};">'
                "(log file not yet created)</span>")
            return
        try:
            stat = self._log_path.stat()
        except OSError:
            return
        marker = (stat.st_size, int(stat.st_mtime))
        if marker == self._last_inode_marker:
            return
        self._last_inode_marker = marker
        try:
            with self._log_path.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return
        recent = lines[-self.TAIL_LINES:]
        # Color-render: keep it simple — style by leading level token.
        html_parts: list[str] = []
        for line in recent:
            stripped = line.rstrip("\n")
            color = NORMAL_TEXT_COLOR
            if " ERROR " in stripped or " CRITICAL " in stripped:
                color = "#f85149"
            elif " WARNING " in stripped:
                color = "#d29922"
            html_parts.append(
                f'<span style="color: {color}; white-space: pre;">'
                f'{_escape_html(stripped)}</span>')
        self.view.setHtml("<br>".join(html_parts))
        # Auto-scroll to bottom.
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.view.setTextCursor(cursor)
        self.view.ensureCursorVisible()

    def closeEvent(self, event) -> None:
        self._timer.stop()
        super().closeEvent(event)


def _escape_html(text: str) -> str:
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


def _debug_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "DFOGANG_RaidHelper" / "debug.log"


class ControlWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.demo = None
        # Closed-but-not-yet-collected demos. We hold strong refs so worker
        # threads can finish their final frame without crashing on a
        # destroyed _frame_emitter QObject.
        self._zombie_demos: list = []
        self.drag_pos: QPoint | None = None
        self.logo = QPixmap(str(bundled_or_portable("logo.png")))
        self.font_family = self._load_font()
        self.settings = QSettings("DFOGANG", "RaidHelper")

        self.setWindowTitle(APP_NAME)
        icon_path = bundled_or_portable("favicon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
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
        title = QLabel(APP_NAME)
        title.setFont(QFont(self.font_family, 16, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {NORMAL_TEXT_COLOR}; background: transparent;")

        self.min_btn = TitleButton("_")
        self.close_btn = TitleButton("X")
        self.min_btn.mousePressEvent = lambda event: self.showMinimized()
        self.close_btn.mousePressEvent = lambda event: self.close()

        title_row.addWidget(title, 1)
        title_row.addWidget(self.min_btn)
        title_row.addWidget(self.close_btn)
        root.addLayout(title_row)

        api_row = QHBoxLayout()
        api_row.setSpacing(8)
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_input.setPlaceholderText("Neople API Key")
        self.api_key_input.setText(self.settings.value("neople_api_key", "", str))
        self.api_key_input.setToolTip(
            "Register and receive key from Neople Developers, then paste it here."
        )
        self.api_key_input.editingFinished.connect(self._save_api_key)

        self.api_help_btn = QPushButton("?")
        self.api_help_btn.setFixedSize(ACTION_BUTTON_SIZE, ACTION_BUTTON_SIZE)
        self.api_help_btn.setToolTip(
            "Needed to try fame-based search when name recognition fails."
        )
        self.api_help_btn.clicked.connect(self._open_api_key_page)

        self.debug_btn = QPushButton("D")
        self.debug_btn.setFixedSize(ACTION_BUTTON_SIZE, ACTION_BUTTON_SIZE)
        self.debug_btn.setCheckable(True)
        self.debug_btn.setToolTip(
            "Toggle debug log window (recent errors & frame stats)."
        )
        self.debug_btn.clicked.connect(self._toggle_debug_window)
        self.debug_window: DebugLogWindow | None = None

        api_row.addWidget(self.api_key_input, 1)
        api_row.addWidget(self.api_help_btn)
        api_row.addWidget(self.debug_btn)
        root.addLayout(api_row)

        self.status_pill = QLabel("READY")
        self.status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_pill.setFont(QFont(self.font_family, 18, QFont.Weight.Bold))
        self.status_pill.setFixedHeight(31)
        self.status_pill.setStyleSheet(self._pill_style(DISABLED_TEXT_COLOR))

        self.toggle_btn = QPushButton()
        self.toggle_btn.setFixedSize(ACTION_BUTTON_SIZE, ACTION_BUTTON_SIZE)
        self.toggle_btn.setIconSize(QSize(ACTION_ICON_SIZE, ACTION_ICON_SIZE))
        self.toggle_btn.clicked.connect(self.toggle_overlay)
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

        self.setStyleSheet(f"""
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
        """)

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
            painter.drawPolygon(QPolygon([
                QPoint(8, 5),
                QPoint(19, 12),
                QPoint(8, 19),
            ]))
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
        self._save_api_key()
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
        # Show LOADING until the first frame is processed (PaddleOCR cold
        # init + first cold detection scan can take ~3-5s on launch).
        # _overlay_ready will flip this to RUNNING.
        self.status_pill.setText("LOADING")
        self.status_pill.setStyleSheet(self._pill_style(HIGHLIGHT_TEXT_COLOR))

    def stop_overlay(self) -> None:
        if self.demo is not None:
            self.demo.close()
            # Park the closed demo so its _frame_emitter QObject and worker
            # threads stay alive long enough to drain. Trim to last 2 to
            # avoid unbounded growth on repeated start/stop.
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
        pass

    def _save_api_key(self) -> None:
        self.settings.setValue("neople_api_key", self.api_key_input.text().strip())

    def _open_api_key_page(self) -> None:
        QDesktopServices.openUrl(QUrl(NEOPLE_KEY_URL))

    def _toggle_debug_window(self, checked: bool) -> None:
        if checked:
            if self.debug_window is None:
                self.debug_window = DebugLogWindow(
                    _debug_log_path(),
                    demo_provider=lambda: self.demo)
                # Sync the toggle button when the user closes the window.
                self.debug_window.destroyed.connect(self._on_debug_window_closed)
            self.debug_window.show()
            self.debug_window.raise_()
        else:
            if self.debug_window is not None:
                self.debug_window.close()
                self.debug_window = None

    def _on_debug_window_closed(self) -> None:
        self.debug_window = None
        self.debug_btn.setChecked(False)

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
        """Fired once after the first frame has been fully processed.
        Transitions the LOADING placeholder into the steady RUNNING state."""
        if self.status_pill.text() == "LOADING":
            self.status_pill.setText("RUNNING")
            self.status_pill.setStyleSheet(self._pill_style(HIGHLIGHT_TEXT_COLOR))

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.api_key_input.setEnabled(enabled)
        self.api_help_btn.setEnabled(enabled)
        self.debug_btn.setEnabled(True)  # debug always usable


def main() -> int:
    configure_qt_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv)
    # Stop button hides the OverlayWindow; without this, Qt may treat that as
    # "last window closed" and quit the whole app even though ControlWindow
    # is still visible.
    app.setQuitOnLastWindowClosed(False)
    win = ControlWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
