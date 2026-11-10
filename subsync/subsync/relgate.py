"""The quality bar, measured against the release's own text track (sub-to-sub).

Each candidate dialogue line is paired one-to-one with the reference line whose start is nearest, within 1 s
(greedy by distance). Unpaired lines (different sentence splits, extra lines, far off) don't count, but at least
60 % of the candidate's lines must pair, and every tenth of the runtime with >= 5 lines needs >= 50 % of its lines
paired within 200 ms (coverage: a far-off part such as a cut can't hide among unpaired lines). Pass: paired >= 60 %,
median |start error| <= 100 ms, >= `within_share` (90 %) of paired lines within 200 ms, coverage met.

Splits (on when the reference's durations are given): a candidate line that starts inside
the reference line its well-paired predecessor (within 200 ms) starts, at least 200 ms in, is the second half of a
line the release keeps whole. It has no start of its own to measure: it counts as paired and is left out of the
errors and the coverage tenths. On 60 synthetic cases this added no false accept.
"""
import bisect
import statistics

MIN_PAIRED = 0.60
WINDOW = 1.0
MEDIAN_MS = 100
WITHIN_MS = 200
WITHIN_SHARE = 0.90
TENTHS = 10
TENTH_MIN_LINES = 5
TENTH_MIN_SHARE = 0.5
SPLIT_INTO_S = 0.2


def _pair(ref, cand):
    pairs = []
    for i, c in enumerate(cand):
        k = bisect.bisect_left(ref, c - WINDOW)
        while k < len(ref) and ref[k] <= c + WINDOW:
            pairs.append((abs(ref[k] - c), i, k))
            k += 1
    pairs.sort()
    used_c, used_r, m = set(), set(), {}
    for _, i, k in pairs:
        if i in used_c or k in used_r:
            continue
        used_c.add(i)
        used_r.add(k)
        m[i] = k
    return m


def span_share(ref, cand):
    """How much of the reference's span (first to last line) the candidate's lines cover (both sorted)."""
    if len(ref) < 2 or not cand:
        return 0.0
    lo, hi = max(ref[0], cand[0]), min(ref[-1], cand[-1])
    return max(0.0, hi - lo) / (ref[-1] - ref[0]) if ref[-1] > ref[0] else 0.0


def relgate(ref_starts, cand_starts, ref_durations=None, within_share=WITHIN_SHARE, min_span=None, ref_recall=1.0):
    """ref_durations: the reference lines' durations, in ref_starts' order; given, split lines count as paired.
    min_span: the share of the release's span the candidate must cover; a subtitle for half the film
    (a CD2 file) or another cut can pass on the part it has, so it fails here.
    ref_recall: an image-subtitle reference keeps only the line starts it is sure of, so even a
    perfect candidate pairs only about that share of its lines: the paired and per-tenth minimums are scaled by it
    (<= 1). The error bar itself (median, share within 200 ms) is unchanged."""
    order = sorted(range(len(ref_starts)), key=lambda x: ref_starts[x])
    ref = [ref_starts[x] for x in order]
    dur = [ref_durations[x] for x in order] if ref_durations is not None else None
    cand = sorted(cand_starts)
    n = len(cand)
    recall = max(0.0, min(1.0, ref_recall))
    if not n or not ref:
        return {"n": n, "paired": 0.0, "pass": False, "why": "empty"}
    m = _pair(ref, cand)
    split = set()
    if dur is not None:
        for i in range(1, n):
            if i in m and abs(cand[i] - ref[m[i]]) <= WITHIN_MS / 1000:
                continue
            p = i - 1
            while p >= 0 and p in split:
                p -= 1
            if p >= 0 and p in m and abs(cand[p] - ref[m[p]]) <= WITHIN_MS / 1000:
                rs, rd = ref[m[p]], dur[m[p]]
                if rs + SPLIT_INTO_S < cand[i] < rs + rd:
                    split.add(i)
        if split:
            keep = [i for i in range(n) if i not in split]
            m = {keep[a]: k for a, k in _pair(ref, [cand[i] for i in keep]).items()}
    err = {i: cand[i] - ref[k] for i, k in m.items()}
    e = [abs(x) * 1000 for x in err.values()]
    if not e:
        return {"n": n, "paired": 0.0, "pass": False, "why": "nothing paired"}
    med = statistics.median(e)
    share = sum(x <= WITHIN_MS for x in e) / len(e)
    paired = (len(e) + len(split)) / n
    lo, hi = cand[0], cand[-1]
    weak = []
    for b in range(TENTHS):
        a, z = lo + (hi - lo) * b / TENTHS, lo + (hi - lo) * (b + 1) / TENTHS
        idx = [i for i, c in enumerate(cand) if (a <= c < z or (b == TENTHS - 1 and c == hi)) and i not in split]
        if len(idx) >= TENTH_MIN_LINES:
            good = sum(i in err and abs(err[i]) <= WITHIN_MS / 1000 for i in idx)
            if good / len(idx) < TENTH_MIN_SHARE * recall:
                weak.append(b)
    span = span_share(ref, cand)
    ok = paired >= MIN_PAIRED * recall and med <= MEDIAN_MS and share >= within_share and not weak
    if min_span is not None and span < min_span:
        ok = False
    out = {"n": n, "paired": round(paired, 3), "median_ms": round(med, 1), "within200": round(share, 3),
           "signed_median_ms": round(statistics.median(err.values()) * 1000, 1), "weak_tenths": weak,
           "span": round(span, 3), "pass": ok}
    if dur is not None:
        out["splits"] = len(split)
    if within_share != WITHIN_SHARE:
        out["within_needed"] = within_share
    if recall < 1.0:
        out["ref_recall"] = round(recall, 3)
    return out
