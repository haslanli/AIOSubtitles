"""Lines paired by their words (D1, D2): a subtitle's lines against the file's own track, in the same language.

The index gives the track's line times, not its words; a few of its lines are read (index.line_words): runs of RUN
consecutive lines, RUNS_PER_TENTH runs in each tenth of the track's span, each run starting at a line of at least
MIN_LINE_S (the longer lines carry more words), at most MAX_LINES lines in all. Their words are kept only as hashes of
word pairs, in memory, for the job; never stored or shared ((3)).

A subtitle line and a track line say the same thing when at least SAME of the track line's word pairs are in the
subtitle line (cover) and at least SAME of the subtitle line's are in the track line (precision). A track line with
fewer than MIN_WORDS words is no anchor ("Yes.", "What?"). Where more than one subtitle line covers a track line (a line
said twice: "In his own way." 1.8 s apart in Inception), there is no anchor. A subtitle line that covers a track
line and the next one read is a merge: its start is measured against the first one's start, its end against the second
one's end. Two or three consecutive subtitle lines that each fall inside one track line and together cover it are a
split: the first one's start against the track line's start, the last one's end against its end; the others count as
paired with no start of their own. Pairs never cross (the heaviest chain in both orders).

The confirmation (D2): a copy that passes by timing must also hold on its anchors, when it has at least MIN_ANCHORS:
their median start error within the rule's own limit, and no tenth of the copy's span with at least TENTH_ANCHORS
anchors whose median error is over TENTH_MS. It can only refuse.
"""
import hashlib
import re
import statistics

MIN_WORDS = 3
SAME = 0.6
RUN = 3
RUNS_PER_TENTH = 4
MAX_LINES = 120
MIN_LINE_S = 1.2
MIN_ANCHORS = 20
TENTH_ANCHORS = 3
TENTH_MS = 500.0

TAGS = re.compile(r"\{[^}]*\}|<[^>]*>|\\[Nnh]")
WORD = re.compile(r"[^\W_]+", re.UNICODE)


def words(text):
    """A line's words: tags, case and punctuation out, digits kept."""
    return WORD.findall(TAGS.sub(" ", text or "").lower())


def _h(a, b):
    return int.from_bytes(hashlib.blake2b(f"{a} {b}".encode("utf-8"), digest_size=8).digest(), "big")


def pairs_of(text):
    """The line's word pairs as 64-bit hashes (a set), and its number of words."""
    w = words(text)
    return frozenset(_h(a, b) for a, b in zip(w, w[1:])), len(w)


def ass_text(block_text):
    """The text field of a Matroska ASS block (ReadOrder, Layer, Style, Name, MarginL, MarginR, MarginV, Effect, Text)."""
    parts = block_text.split(",", 8)
    return parts[8] if len(parts) == 9 else block_text


def pick(cues, max_lines=MAX_LINES):
    """Which of the track's lines to read: indexes into cues ((start, duration), by start), runs of RUN consecutive
    lines, RUNS_PER_TENTH runs a tenth of the span, each starting at a line of at least MIN_LINE_S where one is near."""
    if not cues:
        return []
    lo, hi = cues[0][0], cues[-1][0]
    span = max(hi - lo, 1e-9)
    tenth = {}
    for i, (s, _) in enumerate(cues):
        tenth.setdefault(min(9, int((s - lo) / span * 10)), []).append(i)
    out = set()
    for b in range(10):
        idx = tenth.get(b, [])
        if not idx:
            continue
        for r in range(RUNS_PER_TENTH):
            at = (r * len(idx)) // RUNS_PER_TENTH
            k = next((j for j in idx[at:] if cues[j][1] >= MIN_LINE_S), idx[at])
            out.update(range(k, min(k + RUN, len(cues))))
    return sorted(out)[:max_lines]


class Track:
    """The track lines that were read: their place in the reference, times and word pairs."""

    def __init__(self, cues, texts):
        """cues: the reference's (start, duration) by start; texts: {cue index: text} for the lines read."""
        self.lines = []
        for i in sorted(texts):
            p, n = pairs_of(texts[i])
            self.lines.append({"i": i, "s": cues[i][0], "d": cues[i][1], "pairs": p, "n": n})
        self.read = len(texts)

    def __len__(self):
        return len(self.lines)


def _score(t, c):
    inter = len(t & c)
    return (inter / len(t) if t else 0.0), (inter / len(c) if c else 0.0)


def match(cand_lines, track):
    """cand_lines: the subtitle's dialogue lines in time order (parse() dicts). Returns the anchors, each
    {"kind": "line"|"merge"|"split", "c": [candidate indexes], "t": [track line indexes in track.lines]}."""
    cp = [pairs_of(l.get("text", ""))[0] for l in cand_lines]
    by_pair = {}
    for i, p in enumerate(cp):
        for h in p:
            by_pair.setdefault(h, set()).add(i)
    tl = track.lines
    found = []
    for k, t in enumerate(tl):
        if t["n"] < MIN_WORDS or len(t["pairs"]) < 2:
            continue
        near = set()
        for h in t["pairs"]:
            near |= by_pair.get(h, set())
        scores = {i: _score(t["pairs"], cp[i]) for i in near}
        covering = [i for i, (cov, _) in scores.items() if cov >= SAME]
        if len(covering) > 1:
            continue                                                # said more than once: no anchor
        if len(covering) == 1:
            i = covering[0]
            prec = scores[i][1]
            if prec >= SAME:
                found.append({"kind": "line", "c": [i], "t": [k], "w": 1.0})
            elif k + 1 < len(tl) and tl[k + 1]["i"] == t["i"] + 1 and tl[k + 1]["pairs"] \
                    and _score(tl[k + 1]["pairs"], cp[i])[0] >= SAME \
                    and len(cp[i] & (t["pairs"] | tl[k + 1]["pairs"])) >= SAME * len(cp[i]):
                found.append({"kind": "merge", "c": [i], "t": [k, k + 1], "w": 1.0})
            continue
        inside = sorted(i for i, (_, prec) in scores.items() if prec >= SAME and cp[i])
        groups = []
        for i in inside:
            if groups and i == groups[-1][-1] + 1 and len(groups[-1]) < 3:
                groups[-1].append(i)
            else:
                groups.append([i])
        whole = [g for g in groups if len(g) >= 2
                 and len(t["pairs"] & frozenset().union(*(cp[i] for i in g))) >= SAME * len(t["pairs"])]
        if len(whole) == 1:
            found.append({"kind": "split", "c": whole[0], "t": [k], "w": 1.0})
    return _chain(found)


def _chain(found):
    """The heaviest set of anchors in which both the candidate and the track order rise (no crossing pairs)."""
    found.sort(key=lambda a: (a["t"][0], a["c"][0]))
    n = len(found)
    best, prev = [a["w"] for a in found], [-1] * n
    for j in range(n):
        for i in range(j):
            if found[i]["t"][-1] < found[j]["t"][0] and found[i]["c"][-1] < found[j]["c"][0] \
                    and best[i] + found[j]["w"] > best[j]:
                best[j], prev[j] = best[i] + found[j]["w"], i
    if not n:
        return []
    j = max(range(n), key=lambda x: best[x])
    out = []
    while j >= 0:
        out.append(found[j])
        j = prev[j]
    return out[::-1]


def errors(anchors, timed, track):
    """The anchors' errors for one timing of the subtitle: timed = its dialogue lines (same order and count as matched),
    with their new times. Returns [(candidate start, start error s, end error s or None)]; a split's other parts are
    paired with no start error of their own (not listed)."""
    out = []
    for a in anchors:
        first, last = timed[a["c"][0]], timed[a["c"][-1]]
        t0, t1 = track.lines[a["t"][0]], track.lines[a["t"][-1]]
        end = last["e"] - (t1["s"] + t1["d"]) if t1["d"] and t1["d"] > 0 else None
        out.append((first["s"], first["s"] - t0["s"], end))
    return out


def summary(anchors, errs):
    """What the verdict records about the anchors (no words)."""
    out = {"anchors": len(errs), "merges": sum(a["kind"] == "merge" for a in anchors),
           "splits": sum(a["kind"] == "split" for a in anchors)}
    if errs:
        out["median_ms"] = round(statistics.median(abs(e) for _, e, _ in errs) * 1000, 1)
        ends = [abs(x) for _, _, x in errs if x is not None]
        if ends:
            out["end_median_ms"] = round(statistics.median(ends) * 1000, 1)
    return out


def confirm(anchors, errs, limit_ms):
    """D2: (passes, why, summary). Fewer than MIN_ANCHORS anchors confirm nothing and refuse nothing."""
    s = summary(anchors, errs)
    if len(errs) < MIN_ANCHORS:
        return True, "too few lines paired by their words to check", s
    if s["median_ms"] > limit_ms:
        return False, "the lines' words disagree with the timing", s
    starts = [c for c, _, _ in errs]
    lo, hi = min(starts), max(starts)
    weak = []
    for b in range(10):
        a, z = lo + (hi - lo) * b / 10, lo + (hi - lo) * (b + 1) / 10
        e = [abs(x) * 1000 for c, x, _ in errs if a <= c < z or (b == 9 and c == hi)]
        if len(e) >= TENTH_ANCHORS and statistics.median(e) > TENTH_MS:
            weak.append(b)
    s["weak_tenths"] = weak
    if weak:
        return False, "a part of the runtime's words disagree with the timing", s
    return True, "the lines' words agree with the timing", s
