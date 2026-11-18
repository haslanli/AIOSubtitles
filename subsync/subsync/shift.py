"""One steady offset, and the frame-rate scales, between a subtitle and the release's own line starts.

Most subtitles differ from a release only by a constant offset, sometimes with a frame-rate change: a PAL speed-up
(Finding Nemo's English: +1.43 s throughout), or a subtitle timed at 24 fps on a 23.976 release (7 s of drift over two
hours). That offset is the peak of a histogram of start differences (each candidate start against the reference starts
within MAX_OFFSET_S), found in milliseconds; the copy it gives is then judged by the same gate as an alass copy
(pipeline.shift_sync), so alass only runs where one offset is not enough (another cut, ads, drift). SCALES, BIN_S and
END_WEIGHT are the audio route's too (audiofit).

Line ends (D3) vote only where starts can't decide: when several start peaks are within PEAK_TIE of the best
(dialogue in a steady rhythm, a short subtitle), each is scored by its start votes plus END_WEIGHT times its end votes.
A clear start peak always wins, since the gate measures starts (an author who starts lines early but ends them on the
release's ends keeps the start peak). Ends are used only when most of the release's lines have a known duration.
"""
import bisect
import statistics

import numpy as np

from .relgate import lands

FILM = 24000 / 1001     # 23.976 fps
# the frame-rate scales tried, none first, then each pair both ways: 25 against 23.976 (PAL), 24 against 23.976, 25
# against 24
SCALES = (1.0, 25 / FILM, FILM / 25, 24 / FILM, FILM / 24, 25 / 24, 24 / 25)
MAX_OFFSET_S = 120.0
BIN_S = 0.05
NEAR_S = 0.1            # a candidate start "lands" on a reference start within this
SCALE_MARGIN = 1.1      # a frame-rate scale must land 10 % more starts than none
END_WEIGHT = 0.5        # an end that lands counts half a start
PEAK_TIE = 0.9          # start peaks within this share of the best are a tie that ends may break
PEAK_APART = 10         # bins (0.5 s) between two peaks counted apart
MAX_PEAKS = 100
KNOWN_ENDS = 0.5        # ends are used when at least this share of the release's lines have a known end
STARTS_AT_ONCE = 256    # candidate starts binned together, against only the reference starts they can reach
PAIRS_AT_ONCE = 1 << 20  # start differences held at once (8 MiB): the histogram's memory doesn't grow with the lines


def _votes(cand, ref, offset):
    return sum(lands(ref, c + offset, NEAR_S) for c in cand)


def _window(cand, ref, max_offset):
    """Every (reference - candidate) difference within max_offset binned (numpy), each bin with its two neighbours:
    (window, n), bin i being the offset (i - n) * BIN_S; None when no difference is within max_offset. cand and ref in
    any order. The candidate starts are taken STARTS_AT_ONCE at a time, each block only against the reference starts
    it can reach and at most PAIRS_AT_ONCE differences at once, so a long or crafted track (tens of thousands of lines,
    whose every pair would take gigabytes) costs time, not memory; a 1,500-line subtitle against a 1,700-line track
    takes milliseconds."""
    cand, ref = np.sort(np.asarray(cand)), np.sort(np.asarray(ref))
    n = int(round(max_offset / BIN_S))
    counts = np.zeros(2 * n + 1, dtype=np.int64)
    reach = max_offset + BIN_S                # a bin past the window: rounding never leaves out a pair the test keeps
    for a in range(0, len(cand), STARTS_AT_ONCE):
        block = cand[a:a + STARTS_AT_ONCE]
        near = ref[np.searchsorted(ref, block[0] - reach):np.searchsorted(ref, block[-1] + reach, "right")]
        step = max(1, PAIRS_AT_ONCE // len(block))
        for b in range(0, len(near), step):
            diffs = np.subtract.outer(near[b:b + step], block).ravel()
            diffs = diffs[np.abs(diffs) <= max_offset]
            counts += np.bincount(np.rint(diffs / BIN_S).astype(np.int64) + n, minlength=2 * n + 1)
    if not counts.any():
        return None
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


def _offset(cand, ref, max_offset, ends=None):
    """The offset that lands the most of cand on ref (of equal peaks, the smallest offset), refined to the median of
    the differences near the peak. ends: (candidate ends, sorted release ends); given, start peaks within PEAK_TIE of
    the best are told apart by their end votes."""
    got = _window(cand, ref, max_offset)
    if got is None:
        return 0.0
    window, n = got
    best = window.max()
    tie = PEAK_TIE * best if ends else best            # with no ends to tell peaks apart, only the best peak counts
    order = sorted(np.flatnonzero(window >= tie), key=lambda i: (-int(window[i]), abs(int(i) - n)))
    if not ends:
        return _refine(cand, ref, int(order[0]) - n)
    cand_ends, ref_ends = ends
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
        offset = _offset(scaled, ref, max_offset, (sorted(e * scale for e in ends[0]), ends[1]) if ends else None)
        votes = _votes(scaled, ref, offset)
        need = votes if scale == 1.0 else votes / SCALE_MARGIN
        if best is None or need > best[3]:
            best = (scale, offset, votes, need)
    return best[0], best[1], best[2]
