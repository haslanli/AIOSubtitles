"""Per-line refinement after alass.

alass moves blocks of lines; each line still carries its author's own timing. Here each dialogue line's start moves to
the start of the release-track line it corresponds to, where that correspondence is clear:
  1. a monotone one-to-one pairing of line starts within `window` (no crossings, each release line used once);
  2. kept only where the line's shift agrees with its neighbours' shifts (`tol`), or, within `end_tol`, where its end
     also lands on that release line's end (the same utterance, started earlier or later by its author);
  3. a second pass pairs lines left over with an unused release line within `tol` of where the local shift puts them;
  4. a split continuation (a line starting inside the release line its predecessor snapped to) keeps its place
     relative to the predecessor.
Other lines (extra lines, signs, songs) move by the shift interpolated between the snapped lines around them.
Durations are kept, order is kept, and no line is shown for less than `min_display` (or its original duration, if
shorter); a line that didn't overlap the next one before doesn't overlap it after.

Snapped lines start exactly where the release's lines start, so the gate reads a snapped copy as better than it
is (lines it can't pair are invisible to it); the pipeline therefore asks a snapped copy for >= 95 % within 200 ms
(on 60 synthetic cases with known speech times that left 1 near miss, 88.5 % true).
"""
import bisect
import statistics

WINDOW = 0.5
TOL = 0.3
END_TOL = 0.6
NEIGHBOURS = 8
MIN_DISPLAY = 0.5


def pair_monotone(cand, ref, window=WINDOW):
    """cand, ref: starts sorted ascending. The max-weight chain of pairs (i, j), both strictly increasing, weight
    1 - |d|/window for |d| <= window. Returns {i: j}."""
    pairs = []
    for i, c in enumerate(cand):
        k = bisect.bisect_left(ref, c - window)
        while k < len(ref) and ref[k] <= c + window:
            pairs.append((i, k, 1.0 - abs(ref[k] - c) / window + 1e-9))
            k += 1
    if not pairs:
        return {}
    m = len(ref)
    tree = [(0.0, -1)] * (m + 1)   # Fenwick tree over j: the best chain ending at a release line <= j

    def query(j):
        best = (0.0, -1)
        while j > 0:
            if tree[j][0] > best[0]:
                best = tree[j]
            j -= j & -j
        return best

    def update(j, val):
        j += 1
        while j <= m:
            if val[0] > tree[j][0]:
                tree[j] = val
            j += j & -j

    prev, score = [-1] * len(pairs), [0.0] * len(pairs)
    p = 0
    while p < len(pairs):           # all pairs of one line are scored before any is inserted: i strictly increases
        q = p
        while q < len(pairs) and pairs[q][0] == pairs[p][0]:
            q += 1
        for t in range(p, q):
            b = query(pairs[t][1])
            score[t], prev[t] = b[0] + pairs[t][2], b[1]
        for t in range(p, q):
            update(pairs[t][1], (score[t], t))
        p = q
    t = max(range(len(pairs)), key=lambda x: score[x])
    out = {}
    while t >= 0:
        out[pairs[t][0]] = pairs[t][1]
        t = prev[t]
    return out


def _local_median(keys, vals, x, k):
    """Median of the values at the k positions nearest to x (keys sorted), x itself excluded."""
    pos = bisect.bisect_left(keys, x)
    lo, hi, got = pos - 1, pos, []
    while len(got) < k and (lo >= 0 or hi < len(keys)):
        if hi < len(keys) and keys[hi] == x:
            hi += 1
            continue
        if lo >= 0 and (hi >= len(keys) or x - keys[lo] <= keys[hi] - x):
            got.append(vals[lo])
            lo -= 1
        else:
            got.append(vals[hi])
            hi += 1
    return statistics.median(got) if got else None


class _Curve:
    """The shift at any time: linear between snapped lines, constant beyond the first and last."""

    def __init__(self, delta, cand):
        ks = sorted(delta)
        self.t = [cand[a] for a in ks]
        self.d = [delta[a] for a in ks]

    def at(self, t):
        if not self.t:
            return 0.0
        p = bisect.bisect_left(self.t, t)
        if p == 0:
            return self.d[0]
        if p >= len(self.t):
            return self.d[-1]
        t0, t1, d0, d1 = self.t[p - 1], self.t[p], self.d[p - 1], self.d[p]
        return d0 if t1 == t0 else d0 + (d1 - d0) * (t - t0) / (t1 - t0)


def snap(lines, ref_cues, window=WINDOW, tol=TOL, end_tol=END_TOL, k=NEIGHBOURS, min_display=MIN_DISPLAY):
    """lines: parse() dicts (s, e, kind, ...) in file order; ref_cues: [(start, duration)] of the release track.
    Returns (new lines in the same order, stats)."""
    rc = sorted(ref_cues)
    ref = [s for s, _ in rc]
    order = sorted((i for i, l in enumerate(lines) if l["kind"] == "dialogue"), key=lambda i: lines[i]["s"])
    cand = [lines[i]["s"] for i in order]
    cend = [lines[i]["e"] for i in order]
    pairs = pair_monotone(cand, ref, window)
    delta = {a: ref[j] - cand[a] for a, j in pairs.items()}
    for _ in range(2):              # neighbour agreement
        keys = sorted(delta)
        vals = [delta[a] for a in keys]
        keep = {}
        for a in keys:
            med = _local_median(keys, vals, a, k)
            off = abs(delta[a] - med) if med is not None else 0.0
            if off <= tol:
                keep[a] = delta[a]
            elif end_tol is not None and off <= end_tol:
                j = pairs[a]
                if abs(cend[a] + delta[a] - (ref[j] + rc[j][1])) <= 0.25:
                    keep[a] = delta[a]
        delta = keep
    used = {pairs[a] for a in delta}
    curve = _Curve(delta, cand)     # second pass: leftovers near where the local shift puts them
    extra = {}
    for a in range(len(cand)):
        if a in delta:
            continue
        p = cand[a] + curve.at(cand[a])
        j = bisect.bisect_left(ref, p - tol)
        best = None
        while j < len(ref) and ref[j] <= p + tol:
            if j not in used and (best is None or abs(ref[j] - p) < abs(ref[best] - p)):
                best = j
            j += 1
        if best is not None:
            extra[a] = best
    merged = {a: pairs[a] for a in delta}
    merged.update(extra)
    ks = sorted(merged)
    for x, y in zip(ks, ks[1:]):    # an extra pair that would cross its neighbour is dropped
        if merged[y] <= merged[x] and y in extra:
            extra.pop(y, None)
    for a, j in extra.items():
        if j not in used:
            used.add(j)
            delta[a] = ref[j] - cand[a]
    curve = _Curve(delta, cand)
    cont = {}
    for a in range(1, len(cand)):   # split continuations follow their predecessor
        if a in delta or (a - 1) not in delta:
            continue
        j = bisect.bisect_left(ref, cand[a - 1] + delta[a - 1] - 1e-6)
        if j < len(rc) and rc[j][0] + 0.2 < cand[a] + delta[a - 1] < rc[j][0] + rc[j][1]:
            cont[a] = delta[a - 1]
    new = [dict(l) for l in lines]
    snapped = set()
    for a, i in enumerate(order):
        if a in delta:
            d = delta[a]
            snapped.add(i)
        else:
            d = cont.get(a, curve.at(cand[a]))
        new[i]["s"] = lines[i]["s"] + d
        new[i]["e"] = lines[i]["e"] + d
    for i, l in enumerate(lines):   # signs and songs: the interpolated shift, never snapped
        if l["kind"] != "dialogue":
            d = curve.at(l["s"])
            new[i]["s"], new[i]["e"] = l["s"] + d, l["e"] + d
    for x, y in zip(order, order[1:]):
        if new[y]["s"] < new[x]["s"]:
            if y in snapped and x not in snapped:
                new[x]["s"] = new[y]["s"]
            else:
                new[y]["s"] = new[x]["s"]
    for x, y in zip(order, order[1:] + [None]):
        dur0 = lines[x]["e"] - lines[x]["s"]
        need = min(min_display, dur0) if dur0 > 0 else 0.0
        e = new[x]["e"]
        if y is not None and lines[x]["e"] <= lines[y]["s"] + 1e-6 and e > new[y]["s"]:
            e = new[y]["s"]
        new[x]["e"] = max(e, new[x]["s"] + need)
    moves = [abs(new[i]["s"] - lines[i]["s"]) for i in order]
    return new, {"dialogue": len(order), "snapped": len(snapped), "continuations": len(cont),
                 "snapped_share": round(len(snapped) / len(order), 3) if order else 0.0,
                 "median_move_ms": round(statistics.median(moves) * 1000, 1) if moves else 0.0}
