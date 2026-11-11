"""One steady offset, and the 24/25 fps scales, between a subtitle and the release's own line starts.

Most subtitles differ from a release only by a constant offset, sometimes with a PAL speed-up (Finding Nemo's English:
+1.43 s throughout). That offset is the peak of a histogram of start differences (each candidate start against the
reference starts within MAX_OFFSET_S), found in milliseconds; the copy it gives is then judged by the same gate as an alass
copy (pipeline.shift_sync), so alass only runs where one offset is not enough (another cut, ads, drift).
"""
import bisect
import statistics

import numpy as np

SCALES = (1.0, 25 / (24000 / 1001), (24000 / 1001) / 25)
MAX_OFFSET_S = 120.0
BIN_S = 0.05
NEAR_S = 0.1            # a candidate start "lands" on a reference start within this
SCALE_MARGIN = 1.1      # a frame-rate scale must land 10 % more starts than none


def _votes(cand, ref, offset):
    n = 0
    for c in cand:
        k = bisect.bisect_left(ref, c + offset - NEAR_S)
        n += k < len(ref) and ref[k] <= c + offset + NEAR_S
    return n


def _offset(cand, ref, max_offset):
    """The offset that lands the most of cand on ref, refined to the median of the differences near the peak. Every
    (reference - candidate) difference within max_offset is binned at once (numpy): a 1,500-line subtitle against a
    1,700-line track is 2.5 million differences, milliseconds."""
    diffs = np.subtract.outer(np.asarray(ref), np.asarray(cand)).ravel()
    diffs = diffs[np.abs(diffs) <= max_offset]
    if not diffs.size:
        return 0.0
    n = int(round(max_offset / BIN_S))
    counts = np.bincount(np.rint(diffs / BIN_S).astype(np.int64) + n, minlength=2 * n + 1)
    window = np.convolve(counts, np.ones(3, dtype=np.int64), mode="same")   # a bin and its two neighbours
    best = window.max()
    ties = np.flatnonzero(window == best) - n
    peak = int(ties[np.argmin(np.abs(ties))])                              # of equal peaks, the smallest offset
    near = []
    for c in cand:
        k = bisect.bisect_left(ref, c + peak * BIN_S - 1.5 * BIN_S)
        while k < len(ref) and ref[k] <= c + peak * BIN_S + 1.5 * BIN_S:
            near.append(ref[k] - c)
            k += 1
    return statistics.median(near) if near else peak * BIN_S


def best_shift(cand, ref, max_offset=MAX_OFFSET_S):
    """cand, ref: line starts (seconds). Returns (scale, offset, votes): the scale and offset (new start = start * scale +
    offset) that land the most candidate starts within NEAR_S of a reference start, and how many they land."""
    ref = sorted(ref)
    if not cand or not ref:
        return 1.0, 0.0, 0
    best = None
    for scale in SCALES:
        scaled = sorted(c * scale for c in cand)
        offset = _offset(scaled, ref, max_offset)
        votes = _votes(scaled, ref, offset)
        need = votes if scale == 1.0 else votes / SCALE_MARGIN
        if best is None or need > best[3]:
            best = (scale, offset, votes, votes if scale == 1.0 else need)
    return best[0], best[1], best[2]
