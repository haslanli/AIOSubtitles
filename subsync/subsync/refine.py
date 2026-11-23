"""Per-line refinement after alass.

alass moves blocks of lines; each line still carries its author's own timing. Here each dialogue line's start moves to
the start of the release-track line it corresponds to, where that correspondence is clear:
  1. a monotone one-to-one pairing of line starts within WINDOW (no crossings, each release line used once);
  2. kept only where the line's shift agrees with the median shift of the NEIGHBOURS paired lines nearest it (within
     TOL), or, within END_TOL, where its end also lands on that release line's end, within END_MATCH_S (the same
     utterance, started earlier or later by its author; not where the release line's duration is unknown);
  3. a second pass pairs lines left over with an unused release line within TOL of where the local shift puts them
     (the nearest leftover line gets a release line two of them want), kept only where it crosses no other pair;
  4. a split continuation (a line starting more than SPLIT_INTO_S into the release line its predecessor snapped to,
     relgate's rule) keeps its place relative to the predecessor.
Other lines (extra lines, signs, songs, and the lines a sectioned fit left out: drop) move by the shift interpolated
between the snapped lines around them; the lines left out count in no stat.
Order is kept: snapped lines are in order as they are, and every other line is held between the lines around it.
Each line keeps its duration except where it is cut or held: a line that didn't overlap the next one before is cut
where the next one now starts, but no line is shown for less than MIN_DISPLAY (or its original duration, if shorter);
that minimum wins, so a line whose next one now starts sooner after it than that still overlaps it.

Snapped lines start exactly where the release's lines start, so the gate reads a snapped copy as better than it
is (lines it can't pair are invisible to it); the pipeline therefore asks a snapped copy for >= 95 % within 200 ms
(on 60 synthetic cases with known speech times that left 1 near miss, 88.5 % true).
"""
import bisect
import statistics

from .relgate import EPS_S, SPLIT_INTO_S

WINDOW = 0.5              # a line and a release line pair when their starts are this close (seconds)
TOL = 0.3                 # a paired line's shift agrees with its neighbours' within this
END_TOL = 0.6             # or within this, when its end lands on its release line's end too
END_MATCH_S = 0.25        # an end this close to its release line's end lands on it
AGREEMENT_PASSES = 2      # the neighbour agreement, run again over the pairs the first run kept
NEIGHBOURS = 8            # the paired lines nearest in time whose shifts a line's is checked against
MIN_DISPLAY = 0.5         # the shortest a line is shown (or its original duration, if shorter)


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


def snap(lines, ref_cues):
    """lines: parse() dicts (s, e, kind, ...) in file order; ref_cues: [(start, duration)] of the release track.
    Returns (new lines in the same order, stats)."""
    rc = sorted(ref_cues)
    ref = [s for s, _ in rc]
    order = sorted((i for i, l in enumerate(lines) if l["kind"] == "dialogue" and not l.get("drop")),
                   key=lambda i: lines[i]["s"])
    cand = [lines[i]["s"] for i in order]
    cend = [lines[i]["e"] for i in order]
    pairs = pair_monotone(cand, ref)
    delta = {a: ref[j] - cand[a] for a, j in pairs.items()}
    for _ in range(AGREEMENT_PASSES):   # neighbour agreement
        keys = sorted(delta)
        vals = [delta[a] for a in keys]
        keep = {}
        for a in keys:
            med = _local_median(keys, vals, a, NEIGHBOURS)
            off = abs(delta[a] - med) if med is not None else 0.0
            if off <= TOL:
                keep[a] = delta[a]
            elif off <= END_TOL:
                rs, rd = rc[pairs[a]]
                if rd > 0 and abs(cend[a] + delta[a] - (rs + rd)) <= END_MATCH_S:
                    keep[a] = delta[a]
        delta = keep
    snapped_to = {a: pairs[a] for a in delta}   # line -> the release line it snapped to
    used = set(snapped_to.values())
    curve = _Curve(delta, cand)     # second pass: leftovers near where the local shift puts them
    claims = {}                     # release line -> (distance, line): the nearest leftover line that wants it
    for a in range(len(cand)):
        if a in delta:
            continue
        p = cand[a] + curve.at(cand[a])
        j = bisect.bisect_left(ref, p - TOL)
        best = None
        while j < len(ref) and ref[j] <= p + TOL:
            if j not in used and (best is None or abs(ref[j] - p) < abs(ref[best] - p)):
                best = j
            j += 1
        if best is not None and (best not in claims or abs(ref[best] - p) < claims[best][0]):
            claims[best] = (abs(ref[best] - p), a)
    extra = {a: j for j, (_, a) in claims.items()}
    kept, last = sorted(delta), -1
    for a in range(len(cand)):      # an extra pair is kept only between its neighbours' release lines: no crossing
        if a in snapped_to:
            last = snapped_to[a]
        elif a in extra:
            after = bisect.bisect_right(kept, a)
            if last < extra[a] < (snapped_to[kept[after]] if after < len(kept) else len(ref)):
                last = snapped_to[a] = extra[a]
                delta[a] = ref[last] - cand[a]
    curve = _Curve(delta, cand)
    cont = {}
    for a in range(1, len(cand)):   # split continuations follow their predecessor
        if a in delta or (a - 1) not in delta:
            continue
        rs, rd = rc[snapped_to[a - 1]]
        if rs + SPLIT_INTO_S < cand[a] + delta[a - 1] < rs + rd:
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
    for i, l in enumerate(lines):   # signs, songs and lines left out: the interpolated shift, never snapped
        if l["kind"] != "dialogue" or l.get("drop"):
            d = curve.at(l["s"])
            new[i]["s"], new[i]["e"] = l["s"] + d, l["e"] + d
    # order: snapped lines are in order (the pairing never crosses) and never move for a line that isn't
    # (two on release lines that start together get one start); every other line starts no earlier than the line before
    # it, then no later than the line after it, in two passes, so a line held back (a continuation past the next line's
    # release line) holds back the lines before it too
    steps = list(zip(order, order[1:]))
    for x, y in steps:
        if new[y]["s"] < new[x]["s"] and (y not in snapped or x in snapped):
            new[y]["s"] = new[x]["s"]
    for x, y in reversed(steps):
        if new[x]["s"] > new[y]["s"] and x not in snapped:
            new[x]["s"] = new[y]["s"]
    for x, y in zip(order, order[1:] + [None]):
        dur0 = lines[x]["e"] - lines[x]["s"]
        need = min(MIN_DISPLAY, dur0) if dur0 > 0 else 0.0
        e = new[x]["e"]
        if y is not None and lines[x]["e"] <= lines[y]["s"] + EPS_S and e > new[y]["s"]:
            e = new[y]["s"]
        new[x]["e"] = max(e, new[x]["s"] + need)
    moves = [abs(new[i]["s"] - lines[i]["s"]) for i in order]
    return new, {"dialogue": len(order), "snapped": len(snapped), "continuations": len(cont),
                 "snapped_share": round(len(snapped) / len(order), 3) if order else 0.0,
                 "median_move_ms": round(statistics.median(moves) * 1000, 1) if moves else 0.0}
