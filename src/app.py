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

from capture import CaptureUnavailable, ScreenCapture, WindowCapture, list_visible_windows  # noqa: E402
from detect import WindowDetection, detect_raid_window_with_y_candidates  # noqa: E402
from overlay import OverlayWindow            # noqa: E402
from recognize import (RecognizedRow, load_default_templates, recognize_raid_fames,
                       recognize_raid_party, RowCache,
                       ui_scale_setting_to_factor)  # noqa: E402
from dfogang import DfogangClient, ScoreInfo  # noqa: E402
from neople import NeopleClient, name_similarity  # noqa: E402
from party_apply import (PartyApplyDetection, PartyApplyRow,
                         TEMPLATE_SCALE_FOR_PARTY_APPLY,
                         TEMPLATE_SCALE_FOR_PARTY_APPLY_DIGITS,
                         detect_party_apply, recognize_party_apply)  # noqa: E402
import general_ocr  # noqa: E402
from qt_dpi import configure_qt_high_dpi  # noqa: E402


# UI scale at which the party_apply marker was captured. Detection's `scale`
# is relative to this: capture-at-69%-setting → scale=1.0.
PARTY_APPLY_REF_UI_PCT = 69.0


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
COLD_SCAN_MIN_INTERVAL_S = 0.3

# How long a "pending" score lookup is allowed to remain unanswered before we
# treat it as stuck and let a fresh attempt go out. Larger than the dfogang
# client's own in-flight timeout to avoid double-scheduling under normal load.
PENDING_TTL_S = 15.0

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
                 templates: dict | None = None,
                 neople_api_key: str = "",
                 mode: str = "party_apply",
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
        if mode not in ("party_apply", "raid_party"):
            raise ValueError(f"unknown mode {mode!r}")
        self._template_cache: dict[float, dict] = {}
        self.templates = templates
        self._active_template_scale: float | None = ui_scale if templates is not None else None
        self.unavailable_callback = unavailable_callback
        self.waiting_callback = waiting_callback
        self.recovered_callback = recovered_callback
        self.ready_callback = ready_callback
        self.capture_interval_ms = capture_interval_ms
        self.timer = QTimer()
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

        self.dfogang = DfogangClient(demo=demo_scores, neople_api_key=neople_api_key)
        self.neople = NeopleClient(api_key=neople_api_key)
        # max_workers=2 caps concurrent outbound calls. Going higher only
        # helps when the network is fast AND the backend isn't rate-limiting,
        # and risks burst-flooding the API on transient stalls.
        self._score_executor = ThreadPoolExecutor(max_workers=2)
        self._score_cache: dict[str, ScoreInfo | None] = {}
        # name -> started_at. Entries expire after PENDING_TTL_S so a stuck
        # worker thread can't permanently block a retry.
        self._score_pending: dict[str, float] = {}
        self._fame_resolve_cache: dict[tuple[str, int], str | None] = {}
        self._fame_resolve_pending: dict[tuple[str, int], float] = {}

        # party_apply: OCR row -> resolved canonical name (or None).
        # Key is a tuple of OCR signals, deterministic for the same UI state.
        self._pa_resolve_cache: dict[tuple, str | None] = {}
        self._pa_resolve_pending: dict[tuple, float] = {}
        # party_apply: stable row -> resolved canonical name. This key
        # deliberately excludes OCR'd name text, because name OCR is the
        # noisiest signal and can jitter between frames after a successful
        # resolve.
        self._pa_stable_resolve_cache: dict[tuple, str | None] = {}

        # API result cache, keyed on (fame, class_norm, name_norm):
        #   _pa_resolve_cache[key] = canonical_name (str) or None
        #   _pa_candidate_logged   = keys with successful resolves — never
        #                            re-attempt (per user spec)
        #   _pa_candidate_pending  = keys with API call in flight
        # Failures are NOT logged; the next capture tick will retry
        # (the OCR result might land slightly differently and succeed).
        self._pa_candidate_logged: set[tuple] = set()
        self._pa_candidate_pending: set[tuple] = set()
        self._last_detection = None
        self._last_hint = None
        # Sticky once learned. Lets cold scans skip irrelevant Y-candidates
        # (3x speedup) and survives detection loss across raid-window close.
        self._last_y_factor: float | None = None
        self._last_cold_scan_t: float = 0.0
        self._row_cache = RowCache()
        self._batch_future: Future | None = None
        self._closed = False
        self._waiting_for_window = False
        self._log = _get_logger()
        self._last_found = False
        self._last_state_change_t = time.perf_counter()
        self._log.info("LiveDemo init  ui_scale=%.3f  interval=%dms  "
                       "monitor=%s  window_title=%r  log_dir=%s",
                       ui_scale, capture_interval_ms, monitor_index,
                       window_title, _log_dir())

    def _get_templates_for_scale(self, scale: float) -> tuple[float, dict]:
        key = round(float(scale), 2)
        if key not in self._template_cache:
            self._log.info("loading templates for detected scale %.3f (key %.2f)", scale, key)
            self._template_cache[key] = load_default_templates(ui_scale=key)
        if self._active_template_scale != key:
            self._row_cache = RowCache()
            self._active_template_scale = key
        return key, self._template_cache[key]

    def _is_pending_stale(self, started_at: float) -> bool:
        return (time.perf_counter() - started_at) > PENDING_TTL_S

    def get_info(self, name: str):
        """Non-blocking info lookup: cached ScoreInfo (or None), or `_PENDING`
        if a fetch is in flight."""
        if name in self._score_cache:
            return self._score_cache[name]
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
            if not name or name in self._score_cache:
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
            self._score_cache[name] = results.get(name)
            self._score_pending.pop(name, None)

    def _schedule_fame_resolves(self, rows: list[RecognizedRow], fames: dict) -> None:
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
            self._score_executor.submit(self._fetch_fame_resolve, row.name, fame)

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
        self._score_cache[name] = info
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
            if self.capture is None:
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
            cold = self._last_hint is None
            if cold and (t0 - self._last_cold_scan_t) < COLD_SCAN_MIN_INTERVAL_S:
                # Skip detection: a cold scan ran very recently. Returning an
                # empty result keeps the overlay clean without burning CPU on
                # back-to-back full scans while the raid window is closed.
                empty_det = WindowDetection(
                    False, 0.0, 1.0, (0, 0, 0, 0), (0, 0, 0, 0), (0, 0))
                setattr(empty_det, "y_factor", self._last_y_factor or 1.0)
                self._log.debug(
                    "frame skip(throttle)  cap=%.0fms  since_last_cold=%.2fs  "
                    "frame=%dx%d", cap_ms, t0 - self._last_cold_scan_t,
                    frame.shape[1], frame.shape[0])
                self._safe_emit(self._frame_emitter.processed, {
                    "det": empty_det,
                    "rows": [],
                    "origin_xy": getattr(self.capture, "origin_xy", (0, 0)),
                    "elapsed_ms": (time.perf_counter() - t0) * 1000,
                    "cap_ms": cap_ms,
                    "det_ms": 0.0,
                    "skipped": True,
                    "cold": True,
                })
                return
            if cold:
                self._last_cold_scan_t = t0
            t_det0 = time.perf_counter()
            det, y_factor = detect_raid_window_with_y_candidates(
                frame,
                hint=self._last_hint,
                y_factor_filter=self._last_y_factor if cold else None,
                near_scale=self._active_template_scale,
            )
            setattr(det, "y_factor", y_factor)
            if det.found:
                template_scale, templates = self._get_templates_for_scale(det.scale)
                s = det.scale
                if y_factor != 1.0:
                    x, y, w, h = det.name_col_xywh
                    name_col = (x, int(round(y * y_factor)), w, int(round(h * y_factor)))
                    rows = recognize_raid_party(
                        frame, templates,
                        name_col_xywh=name_col,
                        scale=s,
                        y_scale=s * y_factor,
                        row_cache=self._row_cache,
                    )
                else:
                    rows = recognize_raid_party(
                        frame, templates,
                        name_col_xywh=det.name_col_xywh,
                        scale=s,
                        row_cache=self._row_cache,
                    )
                fames = recognize_raid_fames(frame, det, rows, templates)
            else:
                template_scale = self._active_template_scale
                rows = []
                fames = {}
            det_ms = (time.perf_counter() - t_det0) * 1000
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
        if self._closed:
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._log.debug(
            "frame %s%s  cap=%.0f  det=%.0f  total=%.0fms  "
            "found=%s  score=%.2f  scale=%.2f  yf=%s  rows=%d  frame=%dx%d",
            "COLD" if cold else "hot ",
            "" if cold else (" (hint)"),
            cap_ms, det_ms, elapsed_ms,
            det.found, det.score, det.scale,
            getattr(det, "y_factor", 1.0), len(rows),
            frame.shape[1], frame.shape[0])
        self._safe_emit(self._frame_emitter.processed, {
            "det": det,
            "rows": rows,
            "fames": fames,
            "origin_xy": getattr(self.capture, "origin_xy", (0, 0)),
            "elapsed_ms": elapsed_ms,
            "cap_ms": cap_ms,
            "det_ms": det_ms,
            "template_scale": template_scale,
            "skipped": False,
            "cold": cold,
        })

    def _process_frame_party_apply(self, t0: float, cap_ms: float, frame: np.ndarray):
        self._last_frame = frame
        try:
            had_hint = self._last_party_apply_hint is not None
            cold = not had_hint
            # Cold scan (no hint) is ~1.5-2s on 2K captures. Throttle it so
            # we don't burn CPU while the window is closed.
            if cold and (t0 - self._last_pa_cold_scan_t) < COLD_SCAN_MIN_INTERVAL_S:
                self._safe_emit(self._frame_emitter.processed, {
                    "mode": "party_apply",
                    "det": PartyApplyDetection(False, 0.0, 1.0, (0, 0, 0, 0), []),
                    "rows": [],
                    "origin_xy": getattr(self.capture, "origin_xy", (0, 0)),
                    "elapsed_ms": (time.perf_counter() - t0) * 1000,
                    "cap_ms": cap_ms,
                    "det_ms": 0.0,
                    "template_scale": self._active_template_scale,
                    "skipped": True,
                })
                return
            if cold:
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
            scale_known = (self._last_pa_scale is not None
                           and self._pa_narrow_misses < 2)
            near = self._last_pa_scale if scale_known else None
            det = detect_party_apply(frame, hint=self._last_party_apply_hint,
                                     near_scale=near)
            det_ms = (time.perf_counter() - t_det0) * 1000
            if det.found:
                self._last_pa_scale = det.scale
                self._pa_narrow_misses = 0
            elif cold and scale_known:
                self._pa_narrow_misses += 1
            elif cold and not scale_known:
                # Full sweep ran (no cached scale yet). Keep counter at 0
                # so the next find populates `_last_pa_scale` cleanly.
                self._pa_narrow_misses = 0
            # Log when the marker jumped — diagnostic for window drags.
            if had_hint and det.found and prev_hint_xy is not None:
                dx = det.marker_xywh[0] - prev_hint_xy[0]
                dy = det.marker_xywh[1] - prev_hint_xy[1]
                if abs(dx) > 4 or abs(dy) > 4:
                    self._log.info(
                        "marker moved  Δ=(%+d,%+d)  scan=%dms",
                        dx, dy, int(det_ms))
            elif had_hint and not det.found:
                self._log.info("hint lost — falling back to cold scan next frame")
            self._last_frame_meta = {
                "found": det.found, "score": det.score, "scale": det.scale,
                "marker_xywh": det.marker_xywh,
                "frame_size": (frame.shape[1], frame.shape[0]),
                "ui_scale_factor": self.ui_scale,
            }
            recog_ms = 0.0
            if det.found:
                # det.scale is the actual marker size relative to the 69%-UI
                # reference. Templates need to be scaled to match the visible
                # font at that detected scale.
                alpha_scale = TEMPLATE_SCALE_FOR_PARTY_APPLY * det.scale
                digit_scale = TEMPLATE_SCALE_FOR_PARTY_APPLY_DIGITS * det.scale
                template_scale, templates = self._get_templates_for_scale(alpha_scale)
                _, digit_lib = self._get_templates_for_scale(digit_scale)
                digit_templates = {ch: v for ch, v in digit_lib.items()
                                   if ch.isdigit() or ch == ","}
                t_recog0 = time.perf_counter()
                rows = recognize_party_apply(frame, det, templates, digit_templates)
                recog_ms = (time.perf_counter() - t_recog0) * 1000
            else:
                template_scale = self._active_template_scale
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
                self._log.info(
                    "row %d  fame=%s (raw=%r conf=%.2f)  class=%r (raw=%r conf=%.2f)",
                    r.index, fame_str, r.fame_text, r.fame_score,
                    r.class_name, r.class_raw, r.class_score)
        self._log.debug(
            "party_apply frame  cap=%.0f  det=%.0f  recog=%.0f  total=%.0fms  "
            "found=%s  score=%.2f  scale=%.2f  rows=%d",
            cap_ms, det_ms, recog_ms, elapsed_ms, det.found, det.score,
            det.scale, len(rows))
        self._safe_emit(self._frame_emitter.processed, {
            "mode": "party_apply",
            "det": det,
            "rows": rows,
            "origin_xy": getattr(self.capture, "origin_xy", (0, 0)),
            "elapsed_ms": elapsed_ms,
            "cap_ms": cap_ms,
            "det_ms": det_ms,
            "template_scale": template_scale,
        })

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
            return self._apply_party_apply_result(result)
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
        self._last_party_apply_hint = det if det.found else None

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
            if stable_key is not None and stable_key in self._pa_stable_resolve_cache:
                canonical = self._pa_stable_resolve_cache[stable_key]
                self._pa_resolve_cache[row_key] = canonical
                if canonical:
                    self._pa_candidate_logged.add(row_key)
                continue
            if row_key in self._pa_candidate_pending:
                continue
            if row_key in self._pa_candidate_logged:
                continue  # success already cached
            ocr_name = (row.name or row.name_raw or "").strip()
            self._pa_candidate_pending.add(row_key)
            self._score_executor.submit(
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

        Fame + class are more stable than character-name OCR in this UI. This
        prevents a row that already resolved successfully from being queried
        again when the visible name jitters on later frames.
        """
        if not self.neople.has_key:
            return None
        if not row.class_raw:
            return None
        if row.fame is None and row.fame_range_min is None:
            return None
        if row.class_score < PARTY_APPLY_MIN_CLASS_CONF:
            return None
        return (row.fame, row.fame_range_min, row.fame_range_max,
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
        if stable_key is not None and stable_key in self._pa_stable_resolve_cache:
            canonical = self._pa_stable_resolve_cache[stable_key]
            self._pa_resolve_cache[row_key] = canonical
            if canonical:
                self._pa_candidate_logged.add(row_key)
        else:
            canonical = self._pa_resolve_cache.get(row_key, _PENDING)
        if canonical is _PENDING:
            text, color = f"{ocr_display}  …", COLOR_NEUTRAL
        elif not canonical:
            # API returned no confident match. Still show the OCR name so the
            # user knows the row is being processed but couldn't resolve.
            text, color = f"{ocr_display}  ?", COLOR_NEUTRAL
        else:
            info = self._score_cache.get(canonical, _PENDING)
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
            self._pa_resolve_cache[key] = None
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
        self._pa_resolve_cache[key] = canonical
        if canonical:
            self._pa_candidate_logged.add(key)  # success → never re-attempt
            if stable_key is not None:
                self._pa_stable_resolve_cache[stable_key] = canonical
            self._log.info("commit canonical=%r → dfogang", canonical)
            # Kick off dfogang lookup right now; signal will refresh overlay.
            self.get_info(canonical)
        # Failure case: do NOT add to logged. Next capture tick will retry,
        # which is what we want — fame/name/class might re-OCR slightly
        # differently and find a hit.
        self._safe_emit(self._frame_emitter.refresh_overlay)

    def _resolve_rows_by_fame(self, rows: list[RecognizedRow], fames: dict) -> list[RecognizedRow]:
        resolved: list[RecognizedRow] = []
        for row in rows:
            fame_result = fames.get(row.y_abs)
            fame = getattr(fame_result, "fame", None)
            canonical = self._fame_resolve_cache.get((row.name, fame)) if fame is not None else None
            if canonical and canonical != row.name:
                resolved.append(RecognizedRow(
                    y_abs=row.y_abs,
                    name=canonical,
                    char_matches=row.char_matches,
                ))
            else:
                resolved.append(row)
        return resolved

    def _handle_capture_unavailable(self):
        self._frame_in_flight = False
        if self._closed:
            return
        self.overlay.set_annotations([])
        self._last_detection = None
        self._last_hint = None
        self._row_cache = RowCache()
        if self.window_title:
            if not self._waiting_for_window:
                self._waiting_for_window = True
                if self.waiting_callback is not None:
                    self.waiting_callback()
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
        self._row_cache = RowCache()
        _safe_print(f"[tick] error: {message}")

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
        self.timer.start(self.capture_interval_ms)

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
        # wait=False so a stuck cold scan doesn't freeze the UI; the worker
        # checks self._closed before emitting and _safe_emit swallows any
        # late RuntimeError from a destroyed Qt object.
        self._frame_executor.shutdown(wait=False)
        self._score_executor.shutdown(wait=False)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DFO raid party overlay")
    parser.add_argument("--monitor", type=int, default=None,
                        help="1-based monitor index (omit to auto-detect)")
    parser.add_argument("--window-title", default=None,
                        help="capture a window whose title contains this text")
    parser.add_argument("--list-windows", action="store_true",
                        help="print visible window titles and exit")
    parser.add_argument("--real", action="store_true",
                        help="hit the real dfogang backend (default = demo scores)")
    parser.add_argument("--interval", type=int, default=200,
                        help="capture interval in ms (default 200 = 5fps)")
    parser.add_argument("--ui-scale", type=float, required=True,
                        help="DFO UI Scale setting percent, e.g. 100, 50, 0")
    parser.add_argument("--neople-api-key", default=os.environ.get("DFONEOPLE_API_KEY", ""),
                        help="Neople API key for fame-based name correction")
    parser.add_argument("--mode", default="party_apply",
                        choices=["party_apply", "raid_party"],
                        help="UI to track. Default: party_apply.")
    args = parser.parse_args()
    if args.list_windows:
        for hwnd, title in list_visible_windows():
            line = f"{hwnd}: {title}"
            print(line.encode(sys.stdout.encoding or "utf-8",
                              errors="backslashreplace").decode(sys.stdout.encoding or "utf-8"))
        raise SystemExit(0)
    ui_scale = ui_scale_setting_to_factor(args.ui_scale)
    print(f"[startup] UI Scale setting={args.ui_scale:g}% "
          f"(template scale={ui_scale:.3f}x)")
    LiveDemo(capture_interval_ms=args.interval,
             demo_scores=not args.real,
             monitor_index=args.monitor,
             ui_scale=ui_scale,
             window_title=args.window_title,
             neople_api_key=args.neople_api_key,
             mode=args.mode).run()
