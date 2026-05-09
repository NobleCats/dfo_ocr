"""Template matching with baseline alignment + small-shift tolerance.

Each candidate and template glyph is tight-cropped and carries a
`bottom_to_baseline` offset so we can align by the font baseline rather than
by the top or bottom of the bitmap. This eliminates errors from descender
vs non-descender rows.

After baseline alignment, both glyphs are placed on a common canvas. We
slide the template +/-1 pixel in x and y over the candidate and report the
best score. Width mismatches > 1 px are rejected.

Score: pixel agreement weighted toward foreground pixels (Jaccard-like) so
that two near-identical sparse glyphs don't tie with totally different
shapes that happen to share lots of background.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np

from segment import tight_crop
from templates import Template


@dataclass
class Match:
    char: str
    score: float
    runner_up: str | None = None
    runner_up_score: float = 0.0


@dataclass
class SegmentCandidate:
    matches: list[Match]
    mean_score: float
    adjusted_score: float


def _baseline_row(h: int, b2b: int) -> int:
    """Row index of the baseline within a tight-cropped glyph of height h
    with bottom_to_baseline offset b2b."""
    return h - 1 + b2b


def _pad_to_canvas(img: np.ndarray, b2b: int,
                   canvas_above: int, canvas_below: int) -> np.ndarray:
    """Pad a tight-cropped glyph so that its baseline lands at row
    `canvas_above` of a canvas of height `canvas_above + canvas_below + 1`.
    Width is preserved; caller handles width alignment separately."""
    h = img.shape[0]
    baseline_row = _baseline_row(h, b2b)
    pad_top = canvas_above - baseline_row
    pad_bot = canvas_below - (h - 1 - baseline_row)
    if pad_top < 0 or pad_bot < 0:
        return None  # glyph extends beyond canvas
    if pad_top == 0 and pad_bot == 0:
        return img
    return np.pad(img, ((pad_top, pad_bot), (0, 0)), mode="constant")


def _agreement_with_slide(cand: np.ndarray, tpl: np.ndarray,
                          dy_max: int = 1, dx_max: int = 1) -> float:
    """Best pixel agreement between cand and tpl, sliding tpl over cand
    within +/-dy_max rows and +/-dx_max cols. Both must have same shape."""
    cand_b = cand > 0
    tpl_b = tpl > 0
    H, W = cand_b.shape
    # Place cand and tpl into a padded canvas, slide tpl
    pad = max(dy_max, dx_max)
    cand_p = np.pad(cand_b, ((pad, pad), (pad, pad)))
    H_p, W_p = cand_p.shape
    tpl_p_base = np.zeros_like(cand_p)
    tpl_p_base[pad:pad + H, pad:pad + W] = tpl_b

    best = -1.0
    for dy in range(-dy_max, dy_max + 1):
        for dx in range(-dx_max, dx_max + 1):
            # Use slicing translation (not np.roll, which wraps).
            shifted = np.zeros_like(tpl_p_base)
            src_y0 = max(0, -dy); src_y1 = H_p - max(0, dy)
            dst_y0 = max(0, dy); dst_y1 = H_p - max(0, -dy)
            src_x0 = max(0, -dx); src_x1 = W_p - max(0, dx)
            dst_x0 = max(0, dx); dst_x1 = W_p - max(0, -dx)
            shifted[dst_y0:dst_y1, dst_x0:dst_x1] = \
                tpl_p_base[src_y0:src_y1, src_x0:src_x1]
            inter = (cand_p & shifted).sum()
            union = (cand_p | shifted).sum()
            jaccard = inter / union if union > 0 else 0.0
            if jaccard > best:
                best = jaccard
    return float(best)


def _score_against(cand: np.ndarray, cand_b2b: int,
                   tpl: Template,
                   width_tol: int, height_tol: int) -> float:
    """Score one candidate against one template. Returns -1 if shape is
    incompatible."""
    h_c, w_c = cand.shape
    h_t, w_t = tpl.image.shape
    if abs(w_t - w_c) > width_tol:
        return -1.0
    if abs(h_t - h_c) > height_tol:
        return -1.0
    cand_baseline = _baseline_row(h_c, cand_b2b)
    tpl_baseline = _baseline_row(h_t, tpl.bottom_to_baseline)
    canvas_above = max(cand_baseline, tpl_baseline)
    canvas_below = max(h_c - 1 - cand_baseline, h_t - 1 - tpl_baseline)
    cand_padded = _pad_to_canvas(cand, cand_b2b, canvas_above, canvas_below)
    tpl_padded = _pad_to_canvas(tpl.image, tpl.bottom_to_baseline,
                                canvas_above, canvas_below)
    if cand_padded is None or tpl_padded is None:
        return -1.0
    max_w = max(cand_padded.shape[1], tpl_padded.shape[1])
    def pad_w(a, w):
        if a.shape[1] == w:
            return a
        extra = w - a.shape[1]
        left = extra // 2
        right = extra - left
        return np.pad(a, ((0, 0), (left, right)), mode="constant")
    a = pad_w(cand_padded, max_w)
    b = pad_w(tpl_padded, max_w)
    return _agreement_with_slide(a, b, dy_max=1, dx_max=2)


def match_char(candidate: np.ndarray, candidate_b2b: int,
               templates: dict[str, list[Template]],
               width_tol: int = 1, height_tol: int = 1) -> Match:
    """Score the candidate against every template variant and return the best
    matching character. For chars with multiple template variants we take the
    max score across variants."""
    cand = (candidate > 0).astype(np.uint8) * 255

    best_char = ""
    best_score = -1.0
    second_char: str | None = None
    second_score = -1.0

    for ch, variants in templates.items():
        # Best score across variants of this character
        ch_score = -1.0
        for tpl in variants:
            s = _score_against(cand, candidate_b2b, tpl, width_tol, height_tol)
            if s > ch_score:
                ch_score = s
        if ch_score < 0:
            continue
        if ch_score > best_score:
            second_char, second_score = best_char, best_score
            best_char, best_score = ch, ch_score
        elif ch_score > second_score:
            second_char, second_score = ch, ch_score

    if best_char == "|" and second_char == "l" and second_score >= best_score - 0.03:
        best_char, second_char = second_char, best_char
        best_score, second_score = second_score, best_score
    if (
        best_char == "B" and second_char == "8"
        and second_score >= best_score - 0.04
        and _has_open_upper_left(cand)
    ):
        best_char, second_char = second_char, best_char
        best_score, second_score = second_score, best_score
    if (
        best_char == "Z" and second_char == "2"
        and second_score >= best_score - 0.08
        and _has_open_upper_left(cand)
    ):
        best_char, second_char = second_char, best_char
        best_score, second_score = second_score, best_score

    return Match(char=best_char, score=best_score,
                 runner_up=second_char, runner_up_score=second_score)


def _has_open_upper_left(glyph: np.ndarray) -> bool:
    """Distinguish rounded digits like 2/8 from letters like Z/B.

    In this font, 2 and 8 have an empty upper-left corner, while Z/B start
    their top stroke at the left edge. Use only as a close-score tie-break.
    """
    rows_with = (glyph > 0).any(axis=1)
    cols_with = (glyph > 0).any(axis=0)
    if not rows_with.any() or not cols_with.any():
        return False
    top = int(np.argmax(rows_with))
    h, w = glyph.shape
    y1 = min(h, top + 2)
    return not (glyph[top:y1, 0] > 0).any()


def match_row(row_mask: np.ndarray,
              boxes: list[tuple[int, int]],
              baseline_y: int,
              templates: dict[str, Template],
              width_tol: int = 1, height_tol: int = 1) -> tuple[str, list[Match]]:
    """Match every char in a row, return assembled string and per-char results.

    For each connected glyph cluster, compare the single-glyph interpretation
    against plausible multi-glyph segmentations. Multi-glyph candidates pay a
    split penalty, so a visually plausible single glyph such as H/U is not
    split into Fl/Ll just because the two pieces score well independently.
    """
    matches: list[Match] = []
    for l, r in boxes:
        candidate = _best_segment_candidate(row_mask, (l, r), baseline_y,
                                            templates, width_tol, height_tol)
        matches.extend(candidate.matches)
    return "".join(m.char for m in matches), matches


def _best_segment_candidate(row_mask: np.ndarray,
                            box: tuple[int, int],
                            baseline_y: int,
                            templates: dict[str, Template],
                            width_tol: int,
                            height_tol: int) -> SegmentCandidate:
    single = _match_box(row_mask, box, baseline_y, templates,
                        width_tol, height_tol)
    best = SegmentCandidate([single], single.score, single.score)
    l, r = box
    width = r - l + 1
    if width < 5:
        return best
    should_try_split = (
        single.score < 0.80 and width >= 8
    ) or (
        single.char in {"k", "K"} and single.score < 0.88
    )
    if not should_try_split:
        return best
    for candidate in _segment_candidates(row_mask, box, baseline_y, templates,
                                         width_tol, height_tol, single):
        if candidate.adjusted_score > best.adjusted_score:
            best = candidate
    return best


def _match_box(row_mask: np.ndarray,
               box: tuple[int, int],
               baseline_y: int,
               templates: dict[str, Template],
               width_tol: int,
               height_tol: int) -> Match:
    l, r = box
    glyph = row_mask[:, l:r + 1]
    rows_with = (glyph > 0).any(axis=1)
    cols_with = (glyph > 0).any(axis=0)
    if not rows_with.any() or not cols_with.any():
        return Match(char="?", score=0.0)
    top = int(np.argmax(rows_with))
    bot = len(rows_with) - 1 - int(np.argmax(rows_with[::-1]))
    left = int(np.argmax(cols_with))
    right = len(cols_with) - 1 - int(np.argmax(cols_with[::-1]))
    cropped = glyph[top:bot + 1, left:right + 1]
    cand_b2b = baseline_y - bot
    return match_char(cropped, cand_b2b, templates,
                      width_tol=width_tol, height_tol=height_tol)


def _segment_candidates(row_mask: np.ndarray,
                        box: tuple[int, int],
                        baseline_y: int,
                        templates: dict[str, Template],
                        width_tol: int,
                        height_tol: int,
                        single_match: Match) -> list[SegmentCandidate]:
    l, r = box
    width = r - l + 1
    if width < 8:
        return []
    max_parts = min(4, max(2, int(round(width / 8))))
    out: list[SegmentCandidate] = []
    match_cache: dict[tuple[int, int], Match] = {}

    def cached_match(start: int, end: int) -> Match:
        key = (start, end)
        hit = match_cache.get(key)
        if hit is not None:
            return hit
        match = _match_box(row_mask, (l + start, l + end),
                           baseline_y, templates, width_tol, height_tol)
        match_cache[key] = match
        return match

    def candidate_cuts(parts: int) -> list[list[int]]:
        cuts: list[list[int]] = []
        min_part = 1
        max_part = min(16, width - 1)
        if parts == 2:
            for c in range(min_part, width - min_part + 1):
                if c <= max_part and width - c <= max_part:
                    cuts.append([c])
        elif parts == 3:
            for a in range(min_part, width - (2 * min_part) + 1):
                for b in range(a + min_part, width - min_part + 1):
                    widths = (a, b - a, width - b)
                    if max(widths) <= max_part:
                        cuts.append([a, b])
        elif parts == 4:
            for a in range(min_part, width - (3 * min_part) + 1):
                for b in range(a + min_part, width - (2 * min_part) + 1):
                    for c in range(b + min_part, width - min_part + 1):
                        widths = (a, b - a, c - b, width - c)
                        if max(widths) <= max_part:
                            cuts.append([a, b, c])
        return cuts

    for parts in range(2, max_parts + 1):
        for cuts in candidate_cuts(parts):
            starts = [0] + cuts
            ends = [c - 1 for c in cuts] + [width - 1]
            trial: list[Match] = []
            ok = True
            for start, end in zip(starts, ends):
                m = cached_match(start, end)
                if m.score < 0.45:
                    ok = False
                    break
                trial.append(m)
            if not ok:
                continue
            mean = sum(m.score for m in trial) / len(trial)
            split_text = "".join(m.char for m in trial)
            if _reject_visual_oversplit(single_match, split_text):
                continue
            adjusted = mean - _split_penalty(width) * (len(trial) - 1)
            out.append(SegmentCandidate(trial, mean, adjusted))
    return out


def _split_penalty(width: int) -> float:
    """Cost for interpreting one connected component as multiple characters.

    Narrow components need a high penalty to prevent H->Fl and U->Ll.
    Wider clusters are more likely to be genuinely connected glyphs, so their
    split penalty is lower.
    """
    if width <= 12:
        return 0.24
    if width <= 18:
        return 0.18
    return 0.12


def _reject_visual_oversplit(single_match: Match, split_text: str) -> bool:
    if (
        (single_match.char == "U" or single_match.runner_up == "U")
        and split_text in {"IJ", "lJ", "iJ", "1J", "Ll", "LI", "Li", "L1"}
        and single_match.score >= 0.45
    ):
        return True
    if (
        (single_match.char == "H" or single_match.runner_up == "H")
        and split_text in {"Fl", "FI", "Fi", "F1"}
        and single_match.score >= 0.45
    ):
        return True
    return False
