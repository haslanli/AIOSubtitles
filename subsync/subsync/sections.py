"""Sectioned fits (D4): a subtitle made for a slightly different cut, fitted in sections, each with its own shift.

A subtitle for another cut of the film (a few seconds more or less per reel, other ads, recaps or intros) is one steady
shift within each stretch, not over the whole film (Inception's first copies: shifts from about 55 to 63 s across the
film, weak tenths 0, 3 and 7). The shifts worth trying are the peaks of the histogram of start differences (shift.py).
Each line "lands" under a shift when its start falls within NEAR_S of a release start (an end on a release end counts
END_WEIGHT more; a line paired by its words counts as landing under its own shift). The cut into sections is the one
that lands the most lines, less BREAK_PENALTY for each new section, found by dynamic programming over the lines in
order; a section starts only after a gap of at least GAP_S between lines. Lines that a later section's shift puts
before the end of the previous line (a scene the release cut) are left out, as on the audio route.

Limits, so a bad section can't pass (D4 1-3; the copy is then judged as any other, section by section and as a whole):
at most MAX_SECTIONS sections, each at least MIN_SECTION_S long with MIN_SECTION_LINES lines, and each section's shift
landing at least SUPPORT of its lines or resting on at least MIN_ANCHORS lines paired by their words. One section is
no sectioned fit (the one-shift path covers it).
"""
import bisect

from . import shift

NEAR_S = shift.NEAR_S
END_WEIGHT = shift.END_WEIGHT
BREAK_PENALTY = 10.0
MAX_SECTIONS = 8
MIN_SECTION_S = 120.0
MIN_SECTION_LINES = 20
SUPPORT = 0.60
MIN_ANCHORS = 6
GAP_S = 2.0
MAX_OFFSETS = 12
MIN_PEAK = 10            # landings a shift needs to be tried at all
MAX_OFFSET_S = 600.0     # an extended cut adds scenes of minutes; one shift looks only 120 s either way
OVERLAP_S = 0.5


def _lands(ref, t):
    k = bisect.bisect_left(ref, t - NEAR_S)
    return k < len(ref) and ref[k] <= t + NEAR_S


def offsets(starts, ref, max_offset=MAX_OFFSET_S):
    """The shifts worth trying: up to MAX_OFFSETS peaks of the start-difference histogram, PEAK_APART apart."""
    got = shift._window(sorted(starts), ref, max_offset)
    if got is None:
        return []
    window, n = got
    order = sorted(range(len(window)), key=lambda i: (-int(window[i]), abs(i - n)))
    peaks = []
    for i in order:
        if window[i] < MIN_PEAK or len(peaks) >= MAX_OFFSETS:
            break
        if all(abs(i - p) >= shift.PEAK_APART for p in peaks):
            peaks.append(i)
    return [shift._refine(sorted(starts), ref, p - n) for p in peaks]


def _assign(s, e, ref, ref_e, offs, anchors):
    """The dynamic programme: each line's shift (index into offs) and the sections as runs of line indexes, or
    (None, None) when the best cut has a single section."""
    n, m = len(s), len(offs)
    v = []
    for i in range(n):
        a = anchors.get(i)
        row = []
        for o in offs:
            if a is not None and abs(a - o) <= NEAR_S:
                row.append(1.0 + END_WEIGHT)
                continue
            x = 1.0 if _lands(ref, s[i] + o) else 0.0
            if ref_e and _lands(ref_e, e[i] + o):
                x += END_WEIGHT
            row.append(x)
        v.append(row)
    S = MAX_SECTIONS
    neg = float("-inf")
    best = [[neg] * m for _ in range(S + 1)]
    for j in range(m):
        best[1][j] = v[0][j]
    back = [None] * n            # back[i][sec][j]: (previous sections, previous shift) at line i
    back[0] = [[None] * m for _ in range(S + 1)]
    for i in range(1, n):
        gap = s[i] - e[i - 1] >= GAP_S
        cur = [[neg] * m for _ in range(S + 1)]
        bk = [[None] * m for _ in range(S + 1)]
        for sec in range(1, S + 1):
            prev = best[sec - 1] if sec > 1 else None
            top = sorted(range(m), key=lambda k: -prev[k])[:2] if prev and gap else []
            for j in range(m):
                stay = best[sec][j]
                sw, swk = neg, None
                for k in top:
                    if k != j and prev[k] > neg:
                        sw, swk = prev[k] - BREAK_PENALTY, k
                        break
                if stay >= sw and stay > neg:
                    cur[sec][j], bk[sec][j] = stay + v[i][j], (sec, j)
                elif sw > neg:
                    cur[sec][j], bk[sec][j] = sw + v[i][j], (sec - 1, swk)
        best, back[i] = cur, bk
    sec, j = max(((a, b) for a in range(1, S + 1) for b in range(m)), key=lambda x: best[x[0]][x[1]])
    if best[sec][j] == neg or sec < 2:
        return None, None
    assign = [0] * n
    for i in range(n - 1, -1, -1):
        assign[i] = j
        if i:
            sec, j = back[i][sec][j]
    groups = []
    for i in range(n):
        if groups and assign[i] == assign[groups[-1][-1]]:
            groups[-1].append(i)
        else:
            groups.append([i])
    return assign, groups


def fit(lines, ref_cues, anchors=None):
    """lines: parse() dicts in file order; ref_cues: the reference's (start, duration). anchors: {dialogue index in time
    order: the shift its words give}, or a function giving them, called only once a sectioned fit is possible (the
    words are read only then). Returns None, or {"lines": the re-timed lines (dropped ones marked), "groups":
    [line indexes of each section, in file order], "summary": what the verdict records}."""
    dial = sorted((i for i, l in enumerate(lines) if l["kind"] == "dialogue"), key=lambda i: lines[i]["s"])
    n = len(dial)
    if n < 2 * MIN_SECTION_LINES:
        return None
    s = [lines[i]["s"] for i in dial]
    e = [lines[i]["e"] for i in dial]
    ref = sorted(x for x, _ in ref_cues)
    ref_e = sorted(x + d for x, d in ref_cues if d and d > 0)
    offs = offsets(s, ref)
    if len(offs) < 2:
        return None
    assign, groups = _assign(s, e, ref, ref_e, offs, {})
    if groups is None:
        return None
    if callable(anchors):       # the words are read only now that a sectioned fit is possible
        anchors = anchors()
    anchors = anchors or {}
    if anchors:
        a2, g2 = _assign(s, e, ref, ref_e, offs, anchors)
        if g2 is not None:
            assign, groups = a2, g2
    if len(groups) < 2 or len(groups) > MAX_SECTIONS:
        return None
    summary = []
    shifts = []
    for g in groups:
        span = s[g[-1]] - s[g[0]]
        o = shift._refine([s[i] for i in g], ref, round(offs[assign[g[0]]] / shift.BIN_S))
        landed = sum(_lands(ref, s[i] + o) for i in g)
        agree = sum(1 for i in g if i in anchors and abs(anchors[i] - o) <= NEAR_S)
        summary.append({"from_s": round(s[g[0]], 1), "to_s": round(s[g[-1]], 1), "lines": len(g),
                        "offset_s": round(o, 3), "landed": round(landed / len(g), 3), "anchors": agree})
        if len(g) < MIN_SECTION_LINES or span < MIN_SECTION_S:
            return {"lines": None, "summary": {"sections": summary, "why": "a section is too short"}}
        if landed < SUPPORT * len(g) and agree < MIN_ANCHORS:
            return {"lines": None, "summary": {"sections": summary, "why": "a section's shift lands too few lines"}}
        shifts.append(o)
    of_line = {}
    for g, o in zip(groups, shifts):
        for i in g:
            of_line[dial[i]] = o
    new = [dict(l) for l in lines]
    times = [(lines[dial[g[0]]]["s"], o) for g, o in zip(groups, shifts)]
    for i, l in enumerate(lines):
        if i in of_line:
            o = of_line[i]
        else:                    # signs and songs: the shift of the section they fall in
            k = bisect.bisect_right([t for t, _ in times], l["s"]) - 1
            o = times[max(0, k)][1]
        new[i]["s"], new[i]["e"] = l["s"] + o, l["e"] + o
    dropped, last_end = 0, float("-inf")
    for i in dial:
        if new[i]["s"] < last_end - OVERLAP_S:
            new[i]["drop"] = True
            dropped += 1
            continue
        last_end = max(last_end, new[i]["e"])
    return {"lines": new, "groups": [[dial[i] for i in g] for g in groups],
            "summary": {"sections": summary, "dropped": dropped}}
