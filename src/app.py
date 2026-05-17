"""End-to-end live demo: capture screen -> detect raid window ->
recognize names -> overlay them on screen.

Phase 1 MVP: shows the recognized names next to each row (score lookup is
a Phase 5 task, so for now we display the name itself). This validates that
the whole pipeline works in real time before we wire up dfogang.
"""
from __future__ import annotations
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from concurrent.futures import Future, ThreadPoolExecutor

from capture import CaptureUnavailable, ImageCapture, ScreenCapture, WindowCapture, list_visible_windows  # noqa: E402
from overlay import OverlayWindow            # noqa: E402
from dfogang import DfogangClient, ScoreInfo  # noqa: E402
from neople import NeopleClient, name_similarity  # noqa: E402
from party_apply import (PartyApplyDetection, PartyApplyRow,
                         build_manual_party_apply_detection,
                         detect_party_apply, recognize_party_apply)  # noqa: E402
import general_ocr  # noqa: E402
from qt_dpi import configure_qt_high_dpi  # noqa: E402


# UI scale at which the party_apply geometry was captured. Detection's `scale`
# is relative to this: capture-at-69%-setting → scale=1.0.
PARTY_APPLY_REF_UI_PCT = 100.0


def _norm_for_cache(s: str) -> str:
    """Cache-key normalizer: collapse whitespace + lowercase. Tolerant
    enough that minor frame-to-frame OCR jitter doesn't blow up the cache."""
    return " ".join(s.lower().split())


# Score is rendered to the right of the Name column. Offsets in reference-
# scale pixels (multiplied by detected scale at runtime).
SCORE_X_OFFSET = 6
SCORE_Y_OFFSET = -8

# When detection has no hint (raid window closed), full multi-scale scans
# are expensive (~1-2s each on a 2K capture). Throttle them so we don't pile
# up in-flight cold scans that delay recovery when the window reopens — but
# keep the interval short enough that recovery feels responsive after the
# user drags the window past the hint search radius.
COLD_SCAN_MIN_INTERVAL_S = 0.0

# How long a "pending" score lookup is allowed to remain unanswered before we
# treat it as stuck and let a fresh attempt go out. Larger than the dfogang
# client's own in-flight timeout to avoid double-scheduling under normal load.
PENDING_TTL_S = 15.0
LOCAL_CACHE_TTL_S = 180.0
PARTY_APPLY_HINT_GRACE_S = 120.0

# Minimum class-OCR confidence to bother hitting the Neople API. Below this,
# the OCR output is so garbled that match_jobs lands on the wrong class
# tree and pulls 40+ unrelated characters. Empirical: confidence 0.15 in
# the Enchantress→'Frchartver' case yielded 41 wrong candidates; 0.4 is a
# practical floor where the matched word at least shares ~half its glyphs.
PARTY_APPLY_MIN_CLASS_CONF = 0.4

# RGB colors for the score text.
COLOR_BUFFER = (0x81, 0xc7, 0x84)
COLOR_DPS = (0x58, 0xa6, 0xff)
COLOR_NEUTRAL = (220, 220, 220)


# Sentinel value meaning "fetch is in flight, retry next tick".
_PENDING = object()


_logger: logging.Logger | None = None


def _log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "DFOGANG_RaidHelper"


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    # Attach handlers at the *parent* "dfogang" logger so all sibling modules
    # ("dfogang.neople", "dfogang.general_ocr", "dfogang.app") propagate into
    # the same files. Previously only "dfogang.app" had handlers and the
    # rest's logs went nowhere — including useful diagnostics like the
    # jobs-index dump.
    parent = logging.getLogger("dfogang")
    parent.setLevel(logging.DEBUG)
    if not parent.handlers:
        formatter = logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
            datefmt="%H:%M:%S")
        try:
            d = _log_dir()
            d.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                d / "debug.log", maxBytes=2_000_000, backupCount=2,
                encoding="utf-8")
            handler.setFormatter(formatter)
            parent.addHandler(handler)
        except Exception:
            parent.addHandler(logging.NullHandler())
        try:
            repo_log = ROOT / "logs" / "log.txt"
            repo_log.parent.mkdir(parents=True, exist_ok=True)
            run_handler = logging.FileHandler(repo_log, mode="w",
                                              encoding="utf-8")
            run_handler.setFormatter(formatter)
            parent.addHandler(run_handler)
        except Exception:
            pass
    log = logging.getLogger("dfogang.app")
    log.setLevel(logging.DEBUG)
    # Propagate up to "dfogang" so it shares the file handlers with siblings.
    log.propagate = True
    _logger = log
    return log


def _safe_print(message: str) -> None:
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        print(message)
    except Exception:
        pass


def build_annotations(det, rows, get_info, origin_xy: tuple[int, int] = (0, 0)) -> list[dict]:
    """Convert (detection, recognized rows) -> overlay annotations in screen
    coordinates. `get_info(name)` returns a `ScoreInfo`, `_PENDING`, or
    `None` (not on dfogang)."""
    s = det.scale
    nx, ny, nw, nh = det.name_col_xywh
    origin_x, origin_y = origin_xy
    score_x = origin_x + nx + nw + int(round(SCORE_X_OFFSET * s))
    out: list[dict] = []
    for r in rows:
        if not r.name:
            continue
        y_top, y_bot = r.y_abs
        y_center = (y_top + y_bot) // 2
        info = get_info(r.name)
        if info is _PENDING:
            text = f"{r.name}  ..."
            color = COLOR_NEUTRAL
        elif info is None:
            text = r.name
            color = COLOR_NEUTRAL
        else:
            display_name = info.name or r.name
            text = f"{display_name}  {info.score}"
            color = COLOR_BUFFER if info.is_buffer else COLOR_DPS
        out.append({
            "x": score_x,
            "y": origin_y + y_center + int(round(SCORE_Y_OFFSET * s)),
            "text": text,
            "color": color,
        })
    return out


class _FrameEmitter(QObject):
    processed = pyqtSignal(object)
    unavailable = pyqtSignal()
    failed = pyqtSignal(str)
    # Worker threads emit this when they update the resolve / score caches
    # so the overlay refreshes immediately without waiting for the next
    # capture tick. Saves up to one full capture interval of latency on
    # the first appearance of a row.
    refresh_overlay = pyqtSignal()


class LiveDemo:
    def __init__(self, capture_interval_ms: int = 200, demo_scores: bool = True,
                 monitor_index: int | None = None, ui_scale: float = 1.0,
                 window_title: str | None = None,
                 neople_api_key: str = "",
                 mode: str = "party_apply",
                 manual_party_apply: dict | None = None,
                 test_image_path: str | None = None,
                 unavailable_callback=None,
                 waiting_callback=None,
                 recovered_callback=None,
                 ready_callback=None):
        configure_qt_high_dpi()
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.overlay = OverlayWindow()
        self.capture = None
        self.window_title = window_title
        self.monitor_index = monitor_index
        self.ui_scale = ui_scale
        self.mode = mode
        self.manual_party_apply = manual_party_apply or None
        self.test_image_path = test_image_path
        if mode != "party_apply":
            raise ValueError(f"unknown mode {mode!r}")
        self.unavailable_callback = unavailable_callback
        self.waiting_callback = waiting_callback
        self.recovered_callback = recovered_callback
        self.ready_callback = ready_callback
        self.capture_interval_ms = capture_interval_ms
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.tick)
        self._frame_emitter = _FrameEmitter()
        self._frame_emitter.processed.connect(self._apply_frame_result)
        self._frame_emitter.unavailable.connect(self._handle_capture_unavailable)
        self._frame_emitter.failed.connect(self._handle_frame_error)
        self._frame_emitter.refresh_overlay.connect(self._refresh_overlay_from_cache)
        # Snapshot of the most recent successful party_apply frame: needed
        # by `refresh_overlay` to rebuild annotations using the latest
        # cache state without waiting for a fresh capture tick.
        self._last_pa_result: dict | None = None
        self._frame_executor = ThreadPoolExecutor(max_workers=1)
        self._frame_in_flight = False
        self._frame_count = 0
        # Most recent captured frame, kept as a numpy array for the debug
        # window's "Save Frame" button. Plain assignment is thread-safe in
        # CPython (refcounted swap of the binding).
        self._last_frame: np.ndarray | None = None
        self._last_frame_meta: dict | None = None
        # Monitor selection: explicit index, or auto-pick the monitor showing
        # the raid window. None = not yet locked → re-scan on detection failure.
        self._monitor_locked = monitor_index is not None or window_title is not None
        self._rescan_countdown = 0   # frames until next monitor rescan
        self._last_party_apply_hint: PartyApplyDetection | None = None
        self._last_party_apply_hint_seen_at: float = 0.0
        self._last_pa_cold_scan_t: float = 0.0
        # Sticky once learned. After the first successful detection we
        # narrow cold scans to a band around this scale, dropping the
        # warm-restart latency (party_apply window closed → reopened) from
        # ~2s full multi-scale scan to ~100ms.
        self._last_pa_scale: float | None = None
        # After this many consecutive cold-scan misses with a cached
        # `_last_pa_scale`, give up the narrow band and re-discover via
        # a full sweep (covers the rare case where the user changed UI
        # scale while the window was closed).
        self._pa_narrow_misses: int = 0
        # Set True after the first frame has been fully processed; the GUI
        # uses this to flip "LOADING" → "RUNNING".
        self._first_frame_emitted: bool = False
        # Manual-mode guide screen capture: grabbed fresh each frame from the
        # physical screen rect that the guide covers, so OCR always works on
        # the actual pixels under the guide (live game OR test image on screen).
        self._manual_sct = None
        self._manual_guide_frame: np.ndarray | None = None
        self._manual_guide_origin: tuple[int, int] = (0, 0)

        self.dfogang = DfogangClient(demo=demo_scores, neople_api_key=neople_api_key)
        self.neople = NeopleClient(api_key=neople_api_key)
        self._resolve_executor = ThreadPoolExecutor(max_workers=4)
        self._score_executor = ThreadPoolExecutor(max_workers=2)
        self._score_cache: dict[str, tuple[ScoreInfo | None, float]] = {}
        # name -> started_at. Entries expire after PENDING_TTL_S so a stuck
        # worker thread can't permanently block a retry.
        self._score_pending: dict[str, float] = {}
        self._fame_resolve_cache: dict[tuple[str, int], str | None] = {}
        self._fame_resolve_pending: dict[tuple[str, int], float] = {}

        # party_apply: OCR row -> resolved canonical name (or None).
        # Key is a tuple of OCR signals, deterministic for the same UI state.
        self._pa_resolve_cache: dict[tuple, tuple[str | None, float]] = {}
        self._pa_resolve_pending: dict[tuple, float] = {}
        # Stable cache ignores noisy name OCR but includes row index, fame, and
        # class so the same visible row does not re-query on every OCR jitter.
        self._pa_stable_resolve_cache: dict[tuple, tuple[str | None, float]] = {}

        # API result cache, keyed on (fame, class_norm, name_norm):
        #   _pa_resolve_cache[key] = canonical_name (str) or None
        #   _pa_candidate_logged   = keys with successful resolves — never
        #                            re-attempt (per user spec)
        #   _pa_candidate_pending  = keys with API call in flight
        # Failures are NOT logged; the next capture tick will retry
        # (the OCR result might land slightly differently and succeed).
        self._pa_candidate_logged: dict[tuple, float] = {}
        self._pa_candidate_pending: set[tuple] = set()
        self._last_detection = None
        self._last_hint = None
        # Sticky once learned. Lets cold scans skip irrelevant Y-candidates
        # (3x speedup) and survives detection loss across raid-window close.
        self._last_y_factor: float | None = None
        self._last_cold_scan_t: float = 0.0
        self._batch_future: Future | None = None
        self._closed = False
        self._waiting_for_window = False
        self._log = _get_logger()
        self._last_found = False
        self._last_state_change_t = time.perf_counter()
        self._log.info("LiveDemo init  ui_scale=%.3f  interval=%dms  "
                       "monitor=%s  window_title=%r  test_image=%r  log_dir=%s",
                       ui_scale, capture_interval_ms, monitor_index,
                       window_title, test_image_path, _log_dir())

    def _is_pending_stale(self, started_at: float) -> bool:
        return (time.perf_counter() - started_at) > PENDING_TTL_S

    def _cache_fresh(self, fetched_at: float) -> bool:
        return (time.perf_counter() - fetched_at) <= LOCAL_CACHE_TTL_S

    def _cached_score(self, name: str):
        entry = self._score_cache.get(name)
        if entry is None:
            return _PENDING
        info, fetched_at = entry
        if self._cache_fresh(fetched_at):
            return info
        self._score_cache.pop(name, None)
        return _PENDING

    def _cached_resolve(self, cache: dict[tuple, tuple[str | None, float]], key: tuple):
        entry = cache.get(key)
        if entry is None:
            return _PENDING
        canonical, fetched_at = entry
        if self._cache_fresh(fetched_at):
            return canonical
        cache.pop(key, None)
        return _PENDING

    def _set_resolve_cache(self, cache: dict[tuple, tuple[str | None, float]],
                           key: tuple, canonical: str | None) -> None:
        cache[key] = (canonical, time.perf_counter())

    def _candidate_logged_recent(self, key: tuple) -> bool:
        fetched_at = self._pa_candidate_logged.get(key)
        if fetched_at is None:
            return False
        if self._cache_fresh(fetched_at):
            return True
        self._pa_candidate_logged.pop(key, None)
        return False

    def _mark_candidate_logged(self, key: tuple) -> None:
        self._pa_candidate_logged[key] = time.perf_counter()

    def get_info(self, name: str):
        """Non-blocking info lookup: cached ScoreInfo (or None), or `_PENDING`
        if a fetch is in flight."""
        cached = self._cached_score(name)
        if cached is not _PENDING:
            return cached
        started_at = self._score_pending.get(name)
        if started_at is None or self._is_pending_stale(started_at):
            if started_at is not None:
                self._log.warning("score lookup pending %.1fs for %r — retrying",
                                  time.perf_counter() - started_at, name)
            self._score_pending[name] = time.perf_counter()
            self._score_executor.submit(self._fetch_info, name)
        return _PENDING

    def _schedule_scores(self, names: list[str]) -> None:
        missing = []
        now = time.perf_counter()
        for name in names:
            if not name or self._cached_score(name) is not _PENDING:
                continue
            started_at = self._score_pending.get(name)
            if started_at is not None and not self._is_pending_stale(started_at):
                continue
            missing.append(name)
            self._score_pending[name] = now
        if not missing:
            return
        self._score_executor.submit(self._fetch_many_info, missing)

    def _fetch_many_info(self, names: list[str]) -> None:
        try:
            results = self.dfogang.get_many_info(names)
        except Exception as e:
            _safe_print(f"[score] batch fetch error: {e}")
            results = {name: None for name in names}
        for name in names:
            self._score_cache[name] = (results.get(name), time.perf_counter())
            self._score_pending.pop(name, None)

    def _schedule_fame_resolves(self, rows: list, fames: dict) -> None:
        now = time.perf_counter()
        for row in rows:
            fame_result = fames.get(row.y_abs)
            fame = getattr(fame_result, "fame", None)
            if not row.name or fame is None:
                continue
            key = (row.name, fame)
            if key in self._fame_resolve_cache:
                continue
            started_at = self._fame_resolve_pending.get(key)
            if started_at is not None and not self._is_pending_stale(started_at):
                continue
            self._fame_resolve_pending[key] = now
            self._resolve_executor.submit(self._fetch_fame_resolve, row.name, fame)

    def _fetch_fame_resolve(self, name: str, fame: int) -> None:
        key = (name, fame)
        try:
            canonical = self.dfogang.resolve_name_by_fame(name, fame)
        except Exception as exc:
            self._log.debug("fame resolve failed for %r fame=%s: %s", name, fame, exc)
            canonical = None
        self._fame_resolve_cache[key] = canonical
        self._fame_resolve_pending.pop(key, None)

    def _fetch_info(self, name: str) -> None:
        info = None
        err = None
        try:
            info = self.dfogang.get_info(name)
        except NotImplementedError:
            err = "NotImplementedError"
        except Exception as e:
            err = str(e)
            _safe_print(f"[score] {name!r} fetch error: {e}")
        if err:
            self._log.warning("dfogang  %r → ERROR: %s", name, err)
        elif info is None:
            self._log.info("dfogang  %r → not registered", name)
        else:
            self._log.info("dfogang  %r → score=%s buffer=%s fame=%s",
                           info.name or name, info.score, info.is_buffer,
                           info.fame)
        self._score_cache[name] = (info, time.perf_counter())
        self._score_pending.pop(name, None)
        self._safe_emit(self._frame_emitter.refresh_overlay)

    def _scan_all_monitors(self) -> int | None:
        """Try detection on every monitor; return the index of the one with
        the highest detection score (if any exceeds threshold), else None."""
        from detect import detect_raid_window_with_y_candidates
        best_idx, best_score = None, 0.0
        for idx in range(1, self.capture.monitor_count + 1):
            try:
                frame = self.capture.grab_monitor(idx)
            except Exception:
                continue
            det, _ = detect_raid_window_with_y_candidates(frame)
            if det.found and det.score > best_score:
                best_idx, best_score = idx, det.score
        return best_idx

    def tick(self):
        if self._closed or self._frame_in_flight:
            return
        self._frame_in_flight = True
        self._frame_executor.submit(self._process_frame)

    def _schedule_next_frame(self, delay_ms: int = 0) -> None:
        if self._closed:
            return
        self.timer.start(max(0, int(delay_ms)))

    def _safe_emit(self, signal, *args):
        """Emit a signal, swallowing the 'wrapped C/C++ object has been
        deleted' error that can occur if LiveDemo was shut down while a
        worker frame was in flight."""
        if self._closed:
            return
        try:
            signal.emit(*args)
        except RuntimeError:
            pass

    def _process_frame(self):
        if self._closed:
            return
        t0 = time.perf_counter()
        try:
            if self.mode == "party_apply" and self._has_manual_guide_capture():
                t_cap0 = time.perf_counter()
                manual_det = self._manual_party_apply_detection(None)
                cap_ms = (time.perf_counter() - t_cap0) * 1000
                if self._closed:
                    return
                if manual_det is not None and self._manual_guide_frame is not None:
                    return self._process_frame_party_apply(
                        t0, cap_ms, self._manual_guide_frame, manual_det=manual_det
                    )

            if self.capture is None:
                if self.test_image_path:
                    self.capture = ImageCapture(self.test_image_path)
                else:
                    self.capture = WindowCapture(self.window_title) if self.window_title else ScreenCapture()
                if self.monitor_index is not None and not self.window_title:
                    self.capture.set_monitor(self.monitor_index)
            t_cap0 = time.perf_counter()
            frame = self.capture.grab()
            cap_ms = (time.perf_counter() - t_cap0) * 1000
            if self._closed:
                return
            if self.mode == "party_apply":
                return self._process_frame_party_apply(t0, cap_ms, frame)
            raise RuntimeError(f"unsupported mode {self.mode!r}")
        except CaptureUnavailable as e:
            self._log.warning("capture unavailable: %s", e)
            close = getattr(self.capture, "close", None)
            if close is not None:
                close()
            self.capture = None
            self._safe_emit(self._frame_emitter.unavailable)
            return
        except Exception as e:
            self._log.exception("frame processing failed")
            self._safe_emit(self._frame_emitter.failed, str(e))
            return

    def _has_manual_guide_capture(self) -> bool:
        cfg = self.manual_party_apply
        return bool(
            cfg
            and cfg.get("enabled", True)
            and "guide_x_abs" in cfg
            and "guide_y_abs" in cfg
            and "guide_w" in cfg
            and "guide_h" in cfg
        )

    def _process_frame_party_apply(
        self,
        t0: float,
        cap_ms: float,
        frame: np.ndarray,
        manual_det: PartyApplyDetection | None = None,
    ):
        self._last_frame = frame
        try:
            if manual_det is None:
                manual_det = self._manual_party_apply_detection(frame)
            had_hint = self._last_party_apply_hint is not None
            cold = not had_hint
            # Cold scan (no hint) is ~1.5-2s on 2K captures. Throttle it so
            # we don't burn CPU while the window is closed.
            if manual_det is None and cold and (t0 - self._last_pa_cold_scan_t) < COLD_SCAN_MIN_INTERVAL_S:
                self._safe_emit(self._frame_emitter.processed, {
                    "mode": "party_apply",
                    "det": PartyApplyDetection(False, 0.0, 1.0, (0, 0, 0, 0), []),
                    "rows": [],
                    "origin_xy": getattr(self.capture, "origin_xy", (0, 0)),
                    "elapsed_ms": (time.perf_counter() - t0) * 1000,
                    "cap_ms": cap_ms,
                    "det_ms": 0.0,
                    "skipped": True,
                    "next_delay_ms": int(COLD_SCAN_MIN_INTERVAL_S * 1000),
                })
                return
            if manual_det is None and cold:
                self._last_pa_cold_scan_t = t0
            t_det0 = time.perf_counter()
            prev_hint_xy = (self._last_party_apply_hint.marker_xywh
                            if had_hint else None)
            # Self-discover UI scale on the FIRST cold scan, then remember
            # it. Subsequent cold scans (window closed → reopened) only
            # search a narrow band around that scale — drops restart
            # latency from a 1.9s full sweep to ~100ms.
            # Pass `near_scale` whenever we know the UI scale, regardless of
            # whether we have a hint. detect_party_apply tries the hint
            # first; if hint lookup misses (window gone), it falls through
            # to the near_scale path instead of the slow full coarse+fine
            # sweep. Without this, the lost-transition frame pays a 4.5s
            # full-scan cost on a 2K capture.
            if manual_det is not None:
                scale_known = True
                det = manual_det
            else:
                scale_known = (self._last_pa_scale is not None
                               and self._pa_narrow_misses < 2)
                near = self._last_pa_scale if scale_known else None
                det = detect_party_apply(frame, hint=self._last_party_apply_hint,
                                         near_scale=near)
            det_ms = (time.perf_counter() - t_det0) * 1000
            if det.found:
                self._last_pa_scale = det.scale
                self._pa_narrow_misses = 0
            elif manual_det is None and cold and scale_known:
                self._pa_narrow_misses += 1
            elif manual_det is None and cold and not scale_known:
                # Full sweep ran (no cached scale yet). Keep counter at 0
                # so the next find populates `_last_pa_scale` cleanly.
                self._pa_narrow_misses = 0
            # Log when the marker jumped — diagnostic for window drags.
            if manual_det is None and had_hint and det.found and prev_hint_xy is not None:
                dx = det.marker_xywh[0] - prev_hint_xy[0]
                dy = det.marker_xywh[1] - prev_hint_xy[1]
                if abs(dx) > 4 or abs(dy) > 4:
                    self._log.info(
                        "marker moved  Δ=(%+d,%+d)  scan=%dms",
                        dx, dy, int(det_ms))
            elif manual_det is None and had_hint and not det.found:
                self._log.info("hint lost — falling back to cold scan next frame")
            self._last_frame_meta = {
                "found": det.found, "score": det.score, "scale": det.scale,
                "marker_xywh": det.marker_xywh,
                "frame_size": (frame.shape[1], frame.shape[0]),
                "ui_scale_factor": self.ui_scale,
            }
            recog_ms = 0.0
            if det.found:
                t_recog0 = time.perf_counter()
                # When manual mode grabbed the guide's screen region, use that
                # frame for OCR so coordinates are guide-relative throughout.
                ocr_frame = (self._manual_guide_frame
                             if manual_det is not None and self._manual_guide_frame is not None
                             else frame)
                rows = recognize_party_apply(ocr_frame, det)
                recog_ms = (time.perf_counter() - t_recog0) * 1000
            else:
                rows = []
        except CaptureUnavailable as e:
            self._log.warning("capture unavailable: %s", e)
            close = getattr(self.capture, "close", None)
            if close is not None:
                close()
            self.capture = None
            self._safe_emit(self._frame_emitter.unavailable)
            return
        except Exception as e:
            self._log.exception("party_apply frame failed")
            self._safe_emit(self._frame_emitter.failed, str(e))
            return
        if self._closed:
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000
        # Step 1 diagnostics: log only fame + class for each detected row.
        # Frame-level statistics (timings, scale, etc.) are kept at DEBUG so
        # the live debug window foregrounds the OCR output the user wants
        # to verify.
        if det.found and rows:
            for r in rows:
                fame_str = f"{r.fame:>6}" if r.fame is not None else "  ----"
                self._log.debug(
                    "row %d  fame=%s (raw=%r conf=%.2f)  class=%r (raw=%r conf=%.2f)",
                    r.index, fame_str, r.fame_text, r.fame_score,
                    r.class_name, r.class_raw, r.class_score)
        self._log.debug(
            "party_apply frame  cap=%.0f  det=%.0f  recog=%.0f  total=%.0fms  "
            "found=%s  score=%.2f  scale=%.2f  rows=%d",
            cap_ms, det_ms, recog_ms, elapsed_ms, det.found, det.score,
            det.scale, len(rows))
        # Manual mode: use the guide screen origin so overlay text is positioned
        # relative to the guide region we grabbed, not the capture source.
        if manual_det is not None and self._manual_guide_frame is not None:
            result_origin = self._manual_guide_origin
        else:
            result_origin = getattr(self.capture, "origin_xy", (0, 0))
        self._safe_emit(self._frame_emitter.processed, {
            "mode": "party_apply",
            "det": det,
            "rows": rows,
            "origin_xy": result_origin,
            "elapsed_ms": elapsed_ms,
            "cap_ms": cap_ms,
            "det_ms": det_ms,
        })

    def _manual_party_apply_detection(self, frame: np.ndarray | None) -> PartyApplyDetection | None:
        cfg = self.manual_party_apply
        if not cfg or not cfg.get("enabled", True):
            return None
        try:
            scale = float(cfg.get("scale", 1.0))
            if "guide_x_abs" in cfg:
                # Grab the physical screen region covered by the guide using mss.
                # This works for any capture mode (live window, test image, screen)
                # because we read directly from the screen pixels under the guide.
                import mss
                gx = int(round(float(cfg["guide_x_abs"])))
                gy = int(round(float(cfg["guide_y_abs"])))
                gw = int(round(float(cfg["guide_w"])))
                gh = int(round(float(cfg["guide_h"])))
                if self._manual_sct is None:
                    self._manual_sct = mss.mss()
                monitor = {"left": gx, "top": gy, "width": gw, "height": gh}
                shot = self._manual_sct.grab(monitor)
                import numpy as _np
                guide_frame = _np.array(shot)[:, :, :3]  # drop alpha, keep BGR order
                guide_frame = guide_frame[:, :, ::-1]     # BGRA→RGB? mss gives BGRA so flip to RGB
                self._manual_guide_frame = guide_frame
                self._manual_guide_origin = (gx, gy)
                # Marker is always at a fixed position inside the guide frame.
                mx_local = int(round(float(cfg["marker_x_abs"]) - gx))
                my_local = int(round(float(cfg["marker_y_abs"]) - gy))
                self._log.debug(
                    "manual guide grab rect=(%d,%d,%d,%d) frame=%s marker_local=(%d,%d)",
                    gx, gy, gw, gh, guide_frame.shape, mx_local, my_local,
                )
                det = build_manual_party_apply_detection(
                    (mx_local, my_local), scale, guide_frame.shape,
                )
                return det if det.rows_top_y else None
            elif "marker_x_abs" in cfg:
                # Legacy settings without guide_x_abs: subtract capture origin.
                origin_x, origin_y = getattr(self.capture, "origin_xy", (0, 0))
                marker_x_rel = float(cfg["marker_x_abs"]) - origin_x
                marker_y_rel = float(cfg["marker_y_abs"]) - origin_y
                self._manual_guide_frame = None
                self._manual_guide_origin = (origin_x, origin_y)
            else:
                if frame is None:
                    return None
                marker_x_rel = float(cfg["marker_x_rel"])
                marker_y_rel = float(cfg["marker_y_rel"])
                self._manual_guide_frame = None
                self._manual_guide_origin = getattr(self.capture, "origin_xy", (0, 0))
        except Exception:
            self._log.warning("invalid manual party_apply calibration: %s", cfg, exc_info=True)
            return None
        if frame is None:
            return None
        det = build_manual_party_apply_detection(
            (int(round(marker_x_rel)), int(round(marker_y_rel))),
            scale,
            frame.shape,
        )
        return det if det.rows_top_y else None

    def _apply_frame_result(self, result: dict):
        self._frame_in_flight = False
        if self._closed:
            return
        if self._waiting_for_window:
            self._waiting_for_window = False
            if self.recovered_callback is not None:
                self.recovered_callback()
        if not self._first_frame_emitted:
            self._first_frame_emitted = True
            if self.ready_callback is not None:
                self.ready_callback()
        if result.get("mode") == "party_apply":
            self._apply_party_apply_result(result)
            if not self.test_image_path:
                self._schedule_next_frame(result.get("next_delay_ms", 0))
            return
        det = result["det"]
        rows = result["rows"]
        fames = result.get("fames", {})
        elapsed_ms = result["elapsed_ms"]
        self._last_detection = det if det.found else None
        # State transitions tell us how long recovery actually takes.
        if det.found != self._last_found:
            now = time.perf_counter()
            dt = now - self._last_state_change_t
            self._log.info("STATE %s -> %s after %.2fs",
                           "found" if self._last_found else "lost",
                           "found" if det.found else "lost", dt)
            self._last_found = det.found
            self._last_state_change_t = now
        # Store y-candidate with the detection hint. infer from recognized row
        # coordinates when y-collapsed input was used.
        if det.found:
            y_factor = getattr(det, "y_factor", 1.0)
            self._last_hint = (det, y_factor)
            self._last_y_factor = y_factor
        else:
            self._last_hint = None

        if not det.found:
            self.overlay.set_annotations([])
            self._frame_count += 1
            if self._frame_count % 25 == 0:
                _safe_print(f"[tick] no raid window on monitor #{self.capture.monitor_index} "
                            f"(best score={det.score:.2f})")
            return
        # Detection succeeded → confirm monitor lock.
        self._monitor_locked = True

        self._schedule_fame_resolves(rows, fames)
        rows = self._resolve_rows_by_fame(rows, fames)
        names = [r.name for r in rows if r.name]
        self._schedule_scores(names)
        annotations = build_annotations(det, rows, self.get_info,
                                        origin_xy=result["origin_xy"])
        self.overlay.set_annotations(annotations)
        self._frame_count += 1
        if self._frame_count % 5 == 0 or self._frame_count <= 3:
            _safe_print(f"[tick {self._frame_count}] {elapsed_ms:.0f}ms  "
                        f"scale={det.scale:.2f}  {len(names)} names: {names}")
        self._schedule_next_frame(result.get("next_delay_ms", 0))

    def _apply_party_apply_result(self, result: dict) -> None:
        det: PartyApplyDetection = result["det"]
        rows: list[PartyApplyRow] = result["rows"]
        elapsed_ms = result["elapsed_ms"]

        # Track state transitions for telemetry parity with raid_party path.
        if det.found != self._last_found:
            now = time.perf_counter()
            dt = now - self._last_state_change_t
            self._log.info("party_apply STATE %s -> %s after %.2fs",
                           "found" if self._last_found else "lost",
                           "found" if det.found else "lost", dt)
            self._last_found = det.found
            self._last_state_change_t = now
        if det.found:
            self._last_party_apply_hint = det
            self._last_party_apply_hint_seen_at = time.perf_counter()
        else:
            if (
                self._last_party_apply_hint is not None
                and (time.perf_counter() - self._last_party_apply_hint_seen_at) > PARTY_APPLY_HINT_GRACE_S
            ):
                self._last_party_apply_hint = None

        if not det.found:
            self.overlay.set_annotations([])
            self._last_pa_result = None
            self._frame_count += 1
            return
        self._monitor_locked = True
        # Schedule per-row Neople resolve (single API call) immediately on
        # first sight. Successful resolves are cached forever; failed ones
        # are NOT cached so the next tick retries.
        for row in rows:
            row_key = self._pa_row_key(row)
            if row_key is None:
                continue
            stable_key = self._pa_stable_row_key(row)
            if stable_key is not None:
                canonical = self._cached_resolve(self._pa_stable_resolve_cache, stable_key)
            else:
                canonical = _PENDING
            if canonical is not _PENDING:
                self._set_resolve_cache(self._pa_resolve_cache, row_key, canonical)
                if canonical:
                    self._mark_candidate_logged(row_key)
                continue
            canonical = self._cached_resolve(self._pa_resolve_cache, row_key)
            if canonical is not _PENDING:
                if canonical:
                    self._mark_candidate_logged(row_key)
                continue
            if row_key in self._pa_candidate_pending:
                continue
            if self._candidate_logged_recent(row_key):
                continue  # success already cached
            ocr_name = (row.name or row.name_raw or "").strip()
            self._pa_candidate_pending.add(row_key)
            self._resolve_executor.submit(
                self._fetch_pa_candidates,
                row.fame, row.class_raw, ocr_name, row_key, stable_key,
                row.fame_range_min, row.fame_range_max)
        # Cache the frame result so refresh_overlay can rebuild without
        # waiting for the next capture tick.
        self._last_pa_result = {
            "det": det, "rows": rows, "origin_xy": result["origin_xy"]}
        self.overlay.set_annotations(self._build_pa_annotations(
            det, rows, result["origin_xy"]))
        self._frame_count += 1
        return

    def _pa_row_key(self, row: PartyApplyRow) -> tuple | None:
        """Cache key for a row's API state. None means the row is
        ineligible for API lookup (missing fame, low class confidence)."""
        if not self.neople.has_key:
            return None
        if not row.class_raw:
            return None
        if row.fame is None and row.fame_range_min is None:
            return None
        if row.class_score < PARTY_APPLY_MIN_CLASS_CONF:
            return None
        ocr_name = (row.name or row.name_raw or "").strip()
        return (row.fame, row.fame_range_min, row.fame_range_max,
                _norm_for_cache(row.class_raw), _norm_for_cache(ocr_name))

    def _pa_stable_row_key(self, row: PartyApplyRow) -> tuple | None:
        """Stable key for the same party-apply row across noisy name OCR.

        Fame + class are more stable than character-name OCR in this UI. Include
        row index so an accepted/replaced row has to occupy the same slot with
        the same fame/class to reuse a cached canonical.
        """
        if not self.neople.has_key:
            return None
        if not row.class_raw:
            return None
        if row.fame is None and row.fame_range_min is None:
            return None
        if row.class_score < PARTY_APPLY_MIN_CLASS_CONF:
            return None
        return (row.index, row.fame, row.fame_range_min, row.fame_range_max,
                _norm_for_cache(row.class_raw))

    def _build_pa_annotations(self, det: PartyApplyDetection,
                              rows: list[PartyApplyRow],
                              origin_xy: tuple[int, int]) -> list[dict]:
        out: list[dict] = []
        for row in rows:
            ann = self._build_pa_annotation(
                row, det, self._pa_row_key(row), origin_xy)
            if ann is not None:
                out.append(ann)
        return out

    def _refresh_overlay_from_cache(self) -> None:
        """Rebuild the overlay from the most recent party_apply frame +
        the current cache state. Triggered by worker threads when API
        or dfogang results land — saves up to one capture interval of
        latency on first appearance."""
        snap = self._last_pa_result
        if snap is None or self._closed:
            return
        self.overlay.set_annotations(self._build_pa_annotations(
            snap["det"], snap["rows"], snap["origin_xy"]))

    def _build_pa_annotation(self, row: PartyApplyRow,
                             det: PartyApplyDetection,
                             row_key: tuple | None,
                             origin_xy: tuple[int, int]) -> dict | None:
        """Compose one overlay entry next to a party_apply row.

        State machine:
          - no api key / row missing fame|class           → "<ocr_name> ?"
          - awaiting consensus / resolve pending          → "<ocr_name> ..."
          - resolve done, canonical None (API miss)       → "<ocr_name> ?"
          - canonical known, dfogang pending              → "<name>  ..."
          - canonical known, dfogang None                 → "<name>" neutral
          - canonical known, score available              → "<name>  <score>" coloured
        """
        ocr_display = (row.name or row.name_raw or "?").strip()
        if row_key is None:
            # Either no API key, or row didn't pass class-conf gate. Show the
            # OCR name with "?" so the user still sees recognition is alive.
            return self._pa_overlay_dict(det, row, origin_xy,
                                         f"{ocr_display}  ?", COLOR_NEUTRAL)
        stable_key = self._pa_stable_row_key(row)
        if stable_key is not None:
            canonical = self._cached_resolve(self._pa_stable_resolve_cache, stable_key)
        else:
            canonical = _PENDING
        if canonical is not _PENDING:
            self._set_resolve_cache(self._pa_resolve_cache, row_key, canonical)
            if canonical:
                self._mark_candidate_logged(row_key)
        else:
            canonical = self._cached_resolve(self._pa_resolve_cache, row_key)
        if canonical is _PENDING:
            text, color = f"{ocr_display}  …", COLOR_NEUTRAL
        elif not canonical:
            # API returned no confident match. Still show the OCR name so the
            # user knows the row is being processed but couldn't resolve.
            text, color = f"{ocr_display}  ?", COLOR_NEUTRAL
        else:
            info = self._cached_score(canonical)
            if info is _PENDING:
                self.get_info(canonical)  # kick lookup if not yet started
                text = f"{canonical}  …"
                color = COLOR_NEUTRAL
            elif info is None:
                text, color = canonical, COLOR_NEUTRAL
            else:
                text = f"{info.name or canonical}  {info.score}"
                color = COLOR_BUFFER if info.is_buffer else COLOR_DPS
        return self._pa_overlay_dict(det, row, origin_xy, text, color)

    @staticmethod
    def _pa_overlay_dict(det: PartyApplyDetection, row: PartyApplyRow,
                         origin_xy: tuple[int, int],
                         text: str, color: tuple[int, int, int]) -> dict:
        origin_x, origin_y = origin_xy
        mx, _, mw, _ = det.marker_xywh
        x = origin_x + mx + mw + 4
        top, bot = row.y_abs
        y = origin_y + (top + bot) // 2
        return {"x": x, "y": y, "text": text, "color": color}

    def _fetch_pa_candidates(self, fame: int | None, ocr_class: str,
                             ocr_name: str, key: tuple,
                             stable_key: tuple | None = None,
                             fame_range_min: int | None = None,
                             fame_range_max: int | None = None) -> None:
        """Single-shot resolve. Caller must have already added `key` to
        `_pa_candidate_pending`. Always clears pending; sets resolve cache
        to canonical-or-None and triggers dfogang on success."""
        canonical: str | None = None
        fame_display = (str(fame) if fame is not None
                        else f"range[{fame_range_min}..{fame_range_max}]")
        try:
            job, candidates, source = self.neople.resolve_candidates(
                fame=fame or 0, ocr_class=ocr_class, ocr_name=ocr_name,
                fame_range_min=fame_range_min, fame_range_max=fame_range_max)
        except Exception as exc:
            self._log.warning("Neople resolve failed for fame=%s class=%r: %s",
                              fame_display, ocr_class, exc)
            self._pa_candidate_pending.discard(key)
            self._safe_emit(self._frame_emitter.refresh_overlay)
            return
        if job is None:
            self._log.info("api  fame=%s  class=%r  → no Neo job match",
                           fame_display, ocr_class)
        elif not candidates:
            self._log.info(
                "api  fame=%s %s  class=%r name=%r  → no name-confident match",
                fame_display, source, ocr_class, ocr_name)
        else:
            top = candidates[0]
            self._log.info(
                "api  fame=%s %s  class=%r name=%r  → matched %r",
                fame_display, source, ocr_class, ocr_name, top.name)
            for c in candidates[:5]:
                sim = name_similarity(ocr_name, c.name) if ocr_name else 0.0
                self._log.info(
                    "       cand: name=%-20s fame=%d  server=%-7s  "
                    "grow=%-22s  sim=%.2f",
                    c.name, c.fame, c.server_id, c.job_grow_name, sim)
            canonical = top.name
        self._pa_candidate_pending.discard(key)
        self._set_resolve_cache(self._pa_resolve_cache, key, canonical)
        if canonical:
            self._mark_candidate_logged(key)
            if stable_key is not None:
                self._set_resolve_cache(self._pa_stable_resolve_cache, stable_key, canonical)
            self._log.info("commit canonical=%r → dfogang", canonical)
            # Kick off dfogang lookup right now; signal will refresh overlay.
            self.get_info(canonical)
        # Failure case: do NOT add to logged. Next capture tick will retry,
        # which is what we want — fame/name/class might re-OCR slightly
        # differently and find a hit.
        self._safe_emit(self._frame_emitter.refresh_overlay)

    def _resolve_rows_by_fame(self, rows: list, fames: dict) -> list:
        return rows

    def _handle_capture_unavailable(self):
        self._frame_in_flight = False
        if self._closed:
            return
        self.overlay.set_annotations([])
        self._last_detection = None
        self._last_hint = None
        if self.window_title:
            if not self._waiting_for_window:
                self._waiting_for_window = True
                if self.waiting_callback is not None:
                    self.waiting_callback()
            self._schedule_next_frame(self.capture_interval_ms)
            return
        self.timer.stop()
        if self.unavailable_callback is not None:
            self.unavailable_callback()

    def _handle_frame_error(self, message: str):
        self._frame_in_flight = False
        if self._closed:
            return
        self.overlay.set_annotations([])
        self._last_detection = None
        self._last_hint = None
        _safe_print(f"[tick] error: {message}")
        self._schedule_next_frame(self.capture_interval_ms)

    def run(self):
        self.start()
        try:
            sys.exit(self.app.exec())
        finally:
            self.close()

    def start(self):
        self.overlay.show()
        # EasyOCR cold-init takes ~4s. Prewarm in a daemon thread so the
        # first detected frame doesn't pay that latency mid-capture.
        if self.mode == "party_apply":
            general_ocr.prewarm_in_background()
        self._schedule_next_frame(0)

    def stop(self):
        self.timer.stop()
        self.overlay.set_annotations([])
        self.overlay.hide()

    def close(self):
        if self._closed:
            return
        self._log.info("LiveDemo close requested")
        self._closed = True
        self.stop()
        close = getattr(self.capture, "close", None)
        if close is not None:
            try:
                close()
            except Exception as e:
                # mss is thread-local; closing from a different thread than
                # grab() raises AttributeError. Harmless on shutdown.
                self._log.debug("capture close (ignored): %s", e)
        if self._manual_sct is not None:
            try:
                self._manual_sct.close()
            except Exception:
                pass
            self._manual_sct = None
        # Wait for the frame worker so a stopped LiveDemo cannot overlap a new
        # one against the shared PaddleOCR predictor. This matters most in the
        # bundled EXE, where stop/start timing is slower and overlapping OCR
        # calls can corrupt recognition for the next session.
        self._frame_executor.shutdown(wait=True, cancel_futures=True)
        self._resolve_executor.shutdown(wait=False)
        self._score_executor.shutdown(wait=False)


def main() -> int:
    import argparse
    from PIL import Image

    parser = argparse.ArgumentParser(description="DFO raid party overlay")
    parser.add_argument("--monitor", type=int, default=None,
                        help="1-based monitor index (omit to auto-detect)")
    parser.add_argument("--window-title", default=None,
                        help="capture a window whose title contains this text")
    parser.add_argument("--test-image", default=None,
                        help="run one-shot party_apply detection/OCR against a screenshot and exit")
    parser.add_argument("--list-windows", action="store_true",
                        help="print visible window titles and exit")
    parser.add_argument("--real", action="store_true",
                        help="hit the real dfogang backend (default = demo scores)")
    parser.add_argument("--interval", type=int, default=0,
                        help="restart interval in ms when capture target is unavailable (default 0)")
    parser.add_argument("--ui-scale", type=float, default=69.0,
                        help="DFO UI Scale setting percent, e.g. 100, 50, 0")
    parser.add_argument("--neople-api-key", default=os.environ.get("DFONEOPLE_API_KEY", ""),
                        help="Neople API key for fame-based name correction")
    parser.add_argument("--mode", default="party_apply",
                        choices=["party_apply"],
                        help="UI to track. Default: party_apply.")
    args = parser.parse_args()
    if args.list_windows:
        for hwnd, title in list_visible_windows():
            line = f"{hwnd}: {title}"
            print(line.encode(sys.stdout.encoding or "utf-8",
                              errors="backslashreplace").decode(sys.stdout.encoding or "utf-8"))
        return 0
    if args.test_image:
        img = np.array(Image.open(args.test_image).convert("RGB"))
        det = detect_party_apply(img)
        print(
            f"[test-image] found={det.found} score={det.score:.3f} "
            f"scale={det.scale:.3f} marker={det.marker_xywh} rows={len(det.rows_top_y)}"
        )
        rows = recognize_party_apply(img, det) if det.found else []
        for row in rows:
            print(
                f"row={row.index} fame={row.fame} fame_raw={row.fame_text!r} "
                f"class={row.class_raw!r} name={row.name_raw!r}"
            )
        return 0 if det.found else 1
    print(f"[startup] UI Scale setting={args.ui_scale:g}% "
          f"(OCR-only party_apply mode)")
    LiveDemo(capture_interval_ms=args.interval,
             demo_scores=not args.real,
             monitor_index=args.monitor,
             ui_scale=1.0,
             window_title=args.window_title,
             neople_api_key=args.neople_api_key,
             mode=args.mode,
             test_image_path=args.test_image).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
