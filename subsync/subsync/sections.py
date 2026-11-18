"""Sectioned fits (D4): a subtitle made for a slightly different cut, fitted in sections, each with its own shift.

A subtitle for another cut of the film (a few seconds more or less per reel, other ads, recaps or intros) is one steady
shift within each stretch, not over the whole film (Inception's first copies: shifts from about 55 to 63 s across the
film, weak tenths 0, 3 and 7). The shifts worth trying are the peaks of the histogram of start differences (shift.py).
Each line "lands" under a shift when its start falls within NEAR_S of a release start (an end on a release end counts
END_WEIGHT more; a line paired by its words counts as landing under its own shift). The cut into sections is the one
that lands the most lines, less BREAK_PENALTY for each new section, found by dynamic programming over the lines in
order; a section starts only after a gap of at least GAP_S between lines. Lines that a later section's shift puts
before the end of the previous line (a scene the release cut) are left out, by the rule the audio route shares
(moved_back): lines stacked within one section, and the lines under one mistyped end, are kept.

Limits, so a bad section can't pass (D4 1-3; the copy is then judged as any other, section by section and as a whole):
at most MAX_SECTIONS sections, each at least MIN_SECTION_S long with MIN_SECTION_LINES lines, and each section's shift
landing at least SUPPORT of its lines or resting on at least MIN_ANCHORS lines paired by their words. One section is
no sectioned fit (the one-shift path covers it).
"""
import bisect

from . import shift
from .relgate import lands

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
OVERLAP_S = 0.5          # a line moved back this far over the end of the lines before it is left out
LONGEST_S = 10.0         # a line's end counts at most this long after its start where lines are left out


def moved_back(was, now):
    """The lines a shift change put back over the lines before them (a scene the release cut, its lines moved under
    the next section's shift): of dialogue lines in time order, `was` their (start, end) as the subtitle has them and
    `now` re-timed, the positions of those starting more than OVERLAP_S before the end of the kept lines before them
    where, as the subtitle has them, they didn't. Within one shift nothing is left out: lines stacked on screen stay,
    and since each end counts at most LONGEST_S after its start, one mistyped end can't take the lines under it.
    Shared by the sectioned fit and the audio route."""
    out, end_was, end_now = set(), float("-inf"), float("-inf")
    for i, ((s0, e0), (s1, e1)) in enumerate(zip(was, now)):
        if s1 < end_now - OVERLAP_S and s0 >= end_was - OVERLAP_S:
            out.add(i)
            continue
        end_was, end_now = max(end_was, min(e0, s0 + LONGEST_S)), max(end_now, min(e1, s1 + LONGEST_S))
    return out


def offsets(starts, ref, max_offset=MAX_OFFSET_S):
    """The shifts worth trying: up to MAX_OFFSETS peaks of the start-difference histogram, PEAK_APART apart. starts in
    any order; ref sorted."""
    got = shift._window(starts, ref, max_offset)
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
    return [shift._refine(starts, ref, p - n) for p in peaks]


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
            x = 1.0 if lands(ref, s[i] + o, NEAR_S) else 0.0
            if ref_e and lands(ref_e, e[i] + o, NEAR_S):
                x += END_WEIGHT
            row.append(x)
        v.append(row)
    S = MAX_SECTIONS
    neg = float("-inf")
    best = [[neg] * m for _ in range(S + 1)]
    for j in range(m):
        best[1][j] = v[0][j]
    back = [None] * n            # back[i][sec][j]: (previous sections, previous shift) at line i (from i = 1)
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
    # groups: from 2 to MAX_SECTIONS sections, one per change of shift (_assign)
    summary = []
    shifts = []
    for g in groups:
        span = s[g[-1]] - s[g[0]]
        o = shift._refine([s[i] for i in g], ref, round(offs[assign[g[0]]] / shift.BIN_S))
        landed = sum(lands(ref, s[i] + o, NEAR_S) for i in g)
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
    firsts = [lines[dial[g[0]]]["s"] for g in groups]
    for i, l in enumerate(lines):
        if i in of_line:
            o = of_line[i]
        else:                    # signs and songs: the shift of the section they fall in
            o = shifts[max(0, bisect.bisect_right(firsts, l["s"]) - 1)]
        new[i]["s"], new[i]["e"] = l["s"] + o, l["e"] + o
    dropped = moved_back([(lines[i]["s"], lines[i]["e"]) for i in dial], [(new[i]["s"], new[i]["e"]) for i in dial])
    for k in dropped:
        new[dial[k]]["drop"] = True
    return {"lines": new, "groups": [[dial[i] for i in g] for g in groups],
            "summary": {"sections": summary, "dropped": len(dropped)}}
