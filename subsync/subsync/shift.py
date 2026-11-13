"""One steady offset, and the 24/25 fps scales, between a subtitle and the release's own line starts.

Most subtitles differ from a release only by a constant offset, sometimes with a PAL speed-up (Finding Nemo's English:
+1.43 s throughout). That offset is the peak of a histogram of start differences (each candidate start against the
reference starts within MAX_OFFSET_S), found in milliseconds; the copy it gives is then judged by the same gate as an alass
copy (pipeline.shift_sync), so alass only runs where one offset is not enough (another cut, ads, drift).

Line ends (D3) vote only where starts can't decide: when several start peaks are within PEAK_TIE of the best
(dialogue in a steady rhythm, a short subtitle), each is scored by its start votes plus END_WEIGHT times its end votes.
A clear start peak always wins, since the gate measures starts (an author who starts lines early but ends them on the
release's ends keeps the start peak). Ends are used only when most of the release's lines have a known duration.
"""
import bisect
import statistics

import numpy as np

SCALES = (1.0, 25 / (24000 / 1001), (24000 / 1001) / 25)
MAX_OFFSET_S = 120.0
BIN_S = 0.05
NEAR_S = 0.1            # a candidate start "lands" on a reference start within this
SCALE_MARGIN = 1.1      # a frame-rate scale must land 10 % more starts than none
END_WEIGHT = 0.5        # an end that lands counts half a start
PEAK_TIE = 0.9          # start peaks within this share of the best are a tie that ends may break
PEAK_APART = 10         # bins (0.5 s) between two peaks counted apart
MAX_PEAKS = 100
KNOWN_ENDS = 0.5        # ends are used when at least this share of the release's lines have a known end


def _votes(cand, ref, offset):
    n = 0
    for c in cand:
        k = bisect.bisect_left(ref, c + offset - NEAR_S)
        n += k < len(ref) and ref[k] <= c + offset + NEAR_S
    return n


def _window(cand, ref, max_offset):
    """Every (reference - candidate) difference within max_offset binned at once (numpy; a 1,500-line subtitle against a
    1,700-line track is 2.5 million differences, milliseconds), each bin with its two neighbours: (window, n), bin i
    being the offset (i - n) * BIN_S; None when no difference is within max_offset."""
    diffs = np.subtract.outer(np.asarray(ref), np.asarray(cand)).ravel()
    diffs = diffs[np.abs(diffs) <= max_offset]
    if not diffs.size:
        return None
    n = int(round(max_offset / BIN_S))
    counts = np.bincount(np.rint(diffs / BIN_S).astype(np.int64) + n, minlength=2 * n + 1)
    return np.convolve(counts, np.ones(3, dtype=np.int64), mode="same"), n


def _refine(cand, ref, peak):
    """The median of the differences within 1.5 bins of a peak (in bins)."""
    near = []
    for c in cand:
        k = bisect.bisect_left(ref, c + peak * BIN_S - 1.5 * BIN_S)
        while k < len(ref) and ref[k] <= c + peak * BIN_S + 1.5 * BIN_S:
            near.append(ref[k] - c)
            k += 1
    return statistics.median(near) if near else peak * BIN_S


def _offset(cand, ref, max_offset):
    """The offset that lands the most of cand on ref, refined to the median of the differences near the peak."""
    got = _window(cand, ref, max_offset)
    if got is None:
        return 0.0
    window, n = got
    best = window.max()
    ties = np.flatnonzero(window == best) - n
    peak = int(ties[np.argmin(np.abs(ties))])                              # of equal peaks, the smallest offset
    return _refine(cand, ref, peak)


def _offset_with_ends(cand, ref, max_offset, cand_ends, ref_ends):
    """As _offset, but start peaks within PEAK_TIE of the best are told apart by their end votes."""
    got = _window(cand, ref, max_offset)
    if got is None:
        return 0.0
    window, n = got
    best = window.max()
    order = sorted(np.flatnonzero(window >= PEAK_TIE * best), key=lambda i: (-int(window[i]), abs(int(i) - n)))
    peaks = []
    for i in order:
        if all(abs(int(i) - p) >= PEAK_APART for p in peaks):
            peaks.append(int(i))
            if len(peaks) >= MAX_PEAKS:
                break
    if len(peaks) == 1:
        return _refine(cand, ref, peaks[0] - n)
    scored = []
    for p in peaks:
        off = _refine(cand, ref, p - n)
        score = _votes(cand, ref, off) + END_WEIGHT * _votes(cand_ends, ref_ends, off)
        scored.append((-score, abs(off), off))
    return min(scored)[2]


def _ends(cand_ends, ref_ends):
    """(candidate ends, sorted known release ends) when they can be used, else None."""
    if cand_ends is None or ref_ends is None:
        return None
    known = sorted(e for e in ref_ends if e is not None)
    if not ref_ends or len(known) < KNOWN_ENDS * len(ref_ends):
        return None
    return [e for e in cand_ends if e is not None], known


def best_shift(cand, ref, max_offset=MAX_OFFSET_S, cand_ends=None, ref_ends=None):
    """cand, ref: line starts (seconds). Returns (scale, offset, votes): the scale and offset (new start = start * scale +
    offset) that land the most candidate starts within NEAR_S of a reference start, and how many they land.
    cand_ends, ref_ends: the lines' ends (None for an unknown one), used to break ties between start peaks."""
    ref = sorted(ref)
    if not cand or not ref:
        return 1.0, 0.0, 0
    ends = _ends(cand_ends, ref_ends)
    best = None
    for scale in SCALES:
        scaled = sorted(c * scale for c in cand)
        if ends:
            offset = _offset_with_ends(scaled, ref, max_offset, sorted(e * scale for e in ends[0]), ends[1])
        else:
            offset = _offset(scaled, ref, max_offset)
        votes = _votes(scaled, ref, offset)
        need = votes if scale == 1.0 else votes / SCALE_MARGIN
        if best is None or need > best[3]:
            best = (scale, offset, votes, votes if scale == 1.0 else need)
    return best[0], best[1], best[2]
