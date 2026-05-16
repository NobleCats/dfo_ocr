"""Screen/window capture helpers."""
import numpy as np
import mss
from PIL import Image


class CaptureUnavailable(RuntimeError):
    """Raised when the requested capture target is not currently available."""


def list_visible_windows() -> list[tuple[int, str]]:
    """Return visible top-level windows as (hwnd, title)."""
    import win32gui

    windows: list[tuple[int, str]] = []

    def enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title:
            windows.append((hwnd, title))

    win32gui.EnumWindows(enum_cb, None)
    return windows


def _is_invalid_capture_window(hwnd: int) -> bool:
    import win32gui

    try:
        if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        class_name = win32gui.GetClassName(hwnd).lower()
        title = win32gui.GetWindowText(hwnd).lower()
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
        if any(name in class_name for name in excluded_classes):
            return True
        if any(name in title for name in excluded_titles):
            return True
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if left <= -30000 or top <= -30000:
            return True
        if right <= left or bottom <= top:
            return True
        if (right - left) * (bottom - top) < 100_000:
            return True
    except Exception:
        return True
    return False


def detect_y_doubling(frame: np.ndarray, sample_count: int = 32) -> int | None:
    """Return offset (0 or 1) where consecutive row pairs are bit-identical,
    or None if the frame is not Y-doubled.

    Some DFO display modes (e.g. certain UI-Scale + DPI combos on Windows)
    deliver each rendered row twice. We collapse pairs back to a single row
    before downstream processing.
    """
    h = frame.shape[0]
    if h < 8:
        return None

    def check(offset: int) -> tuple[int, int]:
        # Probe pair starts at offset, offset+2, offset+4, ...
        max_idx = h - 2  # need frame[s] and frame[s+1]
        n = min(sample_count, (max_idx - offset) // 2)
        if n < 4:
            return 0, 0
        starts = np.linspace(offset, offset + 2 * (n - 1), n, dtype=int)
        match = sum(1 for s in starts if np.array_equal(frame[s], frame[s + 1]))
        return match, n

    even_match, even_n = check(0)
    odd_match, odd_n = check(1)
    # A clean Y-doubled image has every probed pair matching. Allow a few
    # rare mismatches just in case some pixel got perturbed.
    if even_n >= 4 and even_match >= even_n - 1:
        return 0
    if odd_n >= 4 and odd_match >= odd_n - 1:
        return 1
    return None


def collapse_y_doubling(frame: np.ndarray, offset: int) -> np.ndarray:
    """Drop the duplicate row from each pair to recover native-resolution Y."""
    if offset == 0:
        # pairs (0,1), (2,3): keep rows 0, 2, 4, ...
        return frame[::2]
    # offset == 1: pairs (1,2), (3,4): keep rows 0, 1, 3, 5, ...
    h = frame.shape[0]
    keep = [0] + list(range(1, h, 2))
    return frame[keep]


class ScreenCapture:
    """Grabs frames from a monitor as RGB numpy arrays.

    `monitor_index`: 1-based index into mss monitor list. 1 = primary.
    Use `set_monitor()` to switch at runtime (e.g. when the user moves the
    game to another display).
    """

    def __init__(self, monitor_index: int = 1, auto_collapse_y: bool = False):
        self._sct = mss.mss()
        self._monitors = self._sct.monitors
        self.set_monitor(monitor_index)
        self._auto_collapse_y = auto_collapse_y
        self._y_offset_cache: int | None = -1   # -1 = not yet probed
        self.origin_xy = (self._monitor.get("left", 0), self._monitor.get("top", 0))

    @property
    def monitor_count(self) -> int:
        """Number of physical monitors (excluding the virtual all-monitors entry)."""
        return len(self._monitors) - 1

    @property
    def monitor_index(self) -> int:
        return self._monitor_index

    def set_monitor(self, monitor_index: int) -> None:
        if monitor_index < 1 or monitor_index >= len(self._monitors):
            raise ValueError(
                f"monitor_index must be 1..{len(self._monitors) - 1}, got {monitor_index}")
        self._monitor_index = monitor_index
        self._monitor = self._monitors[monitor_index]
        self.origin_xy = (self._monitor.get("left", 0), self._monitor.get("top", 0))

    def grab(self) -> np.ndarray:
        """Return current screen frame as uint8 RGB array of shape (H, W, 3).

        If auto_collapse_y is enabled, detects DFO's Y-doubled rendering
        and collapses the frame to native resolution before returning.
        """
        raw = self._sct.grab(self._monitor)
        bgra = np.frombuffer(raw.raw, dtype=np.uint8).reshape(raw.height, raw.width, 4)
        frame = bgra[:, :, 2::-1].copy()  # BGR -> RGB
        if self._auto_collapse_y:
            # Probe once on the first capture; assume the same offset thereafter.
            if self._y_offset_cache == -1:
                self._y_offset_cache = detect_y_doubling(frame)
                if self._y_offset_cache is not None:
                    print(f"[capture] detected Y-doubled rendering "
                          f"(offset={self._y_offset_cache}); collapsing to native Y")
            if self._y_offset_cache is not None:
                frame = collapse_y_doubling(frame, self._y_offset_cache)
        return frame

    def grab_monitor(self, monitor_index: int) -> np.ndarray:
        """One-shot grab from a specific monitor without changing the default."""
        if monitor_index < 1 or monitor_index >= len(self._monitors):
            raise ValueError(
                f"monitor_index must be 1..{len(self._monitors) - 1}")
        raw = self._sct.grab(self._monitors[monitor_index])
        bgra = np.frombuffer(raw.raw, dtype=np.uint8).reshape(raw.height, raw.width, 4)
        frame = bgra[:, :, 2::-1].copy()
        if self._auto_collapse_y:
            offset = detect_y_doubling(frame)
            if offset is not None:
                frame = collapse_y_doubling(frame, offset)
        return frame

    def close(self):
        self._sct.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ImageCapture:
    """Feeds a fixed screenshot through the normal capture pipeline."""

    def __init__(self, image_path: str):
        self.image_path = image_path
        self.origin_xy = (0, 0)
        try:
            self._frame = np.array(Image.open(image_path).convert("RGB"))
        except Exception as exc:
            raise CaptureUnavailable(f"unable to load screenshot: {image_path}") from exc

    def grab(self) -> np.ndarray:
        return self._frame.copy()

    @property
    def monitor_count(self) -> int:
        return 1

    @property
    def monitor_index(self) -> int:
        return 1

    def set_monitor(self, monitor_index: int) -> None:
        if monitor_index != 1:
            raise ValueError("ImageCapture does not support monitor switching")

    def grab_monitor(self, monitor_index: int) -> np.ndarray:
        return self.grab()

    def close(self):
        pass


class WindowCapture:
    """Captures a specific top-level Windows window by title substring.

    Captures the visible window rectangle with MSS. This path matches what is
    currently visible on screen.
    """

    def __init__(self, title_substring: str):
        self.title_substring = title_substring
        self.hwnd = self._find_window(title_substring)
        self.origin_xy = (0, 0)
        self._sct = mss.mss()

    @staticmethod
    def _find_window(title_substring: str) -> int:
        import win32gui

        needle = title_substring.lower()
        matches = [(hwnd, title) for hwnd, title in list_visible_windows()
                   if needle in title.lower() and not _is_invalid_capture_window(hwnd)]
        if not matches:
            raise CaptureUnavailable(f"no visible window title contains {title_substring!r}")
        # Prefer the largest matching window.
        def area(item):
            hwnd, _ = item
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
            except Exception:
                return 0
            return max(0, r - l) * max(0, b - t)
        return max(matches, key=area)[0]

    def grab(self) -> np.ndarray:
        import win32gui

        if not win32gui.IsWindow(self.hwnd) or not win32gui.IsWindowVisible(self.hwnd):
            raise CaptureUnavailable("target window is no longer available")

        try:
            left, top, right, bottom = win32gui.GetWindowRect(self.hwnd)
        except Exception as exc:
            raise CaptureUnavailable("target window is not available") from exc
        width = max(1, right - left)
        height = max(1, bottom - top)
        if width <= 1 or height <= 1:
            raise CaptureUnavailable("target window has no visible area")
        self.origin_xy = (left, top)
        return self._grab_visible_rect(left, top, width, height)

    @property
    def monitor_count(self) -> int:
        return 1

    @property
    def monitor_index(self) -> int:
        return 1

    def set_monitor(self, monitor_index: int) -> None:
        if monitor_index != 1:
            raise ValueError("WindowCapture does not support monitor switching")

    def grab_monitor(self, monitor_index: int) -> np.ndarray:
        return self.grab()

    def close(self):
        self._sct.close()

    def _grab_visible_rect(self, left: int, top: int, width: int, height: int) -> np.ndarray:
        """Capture the current visible pixels in the target window rectangle."""
        try:
            raw = self._sct.grab({
                "left": left,
                "top": top,
                "width": width,
                "height": height,
            })
        except Exception as exc:
            raise CaptureUnavailable("visible window capture failed") from exc
        bgra = np.frombuffer(raw.raw, dtype=np.uint8).reshape(raw.height, raw.width, 4)
        return bgra[:, :, 2::-1].copy()
