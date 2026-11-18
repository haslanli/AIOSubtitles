"""The better matcher: line ends, lines paired by their words, sectioned fits, more speech windows on close calls.

Pure pieces on synthetic timings and fakes: no network, no alass, no Silero.
"""
import os
import random
import threading
import time
import unittest
from array import array
from unittest import mock

from helpers import AUDIO_BETTER, FakeIndex, FIXTURES, rhythm, truth_fit
from subsync import mkv, mp4, pipeline, sections, shift, textpair  # noqa: E402
from subsync.audiofit import AudioFit  # noqa: E402
from subsync.reader import ReadFailed, Refused  # noqa: E402
from subsync.relgate import relgate  # noqa: E402


def periodic(n, step=3.0, seed=5, start=20.0):
    """n release lines starting every `step` seconds exactly, with durations that differ: starts alone can't tell one
    shift by a multiple of `step` from another, ends can."""
    r = random.Random(seed)
    return [(start + i * step, r.uniform(0.6, 2.6)) for i in range(n)]


def track(cues, lang="en", number=3):
    return {"number": number, "language": lang, "codec": "S_TEXT/UTF8", "name": "", "forced": False, "cues": cues}


def srt(lines):
    from subsync.subs import ts
    return "\n".join(f"{i}\n{ts(s)} --> {ts(e)}\n{t}\n" for i, (s, e, t) in enumerate(lines, 1))


class ShiftEndsTest(unittest.TestCase):
    """Line ends vote in the one-shift histogram only where starts can't decide: a start peak stays a start peak."""

    def test_ends_break_a_tie_between_start_offsets(self):
        cues = periodic(400)
        sub = cues[100:300]
        cand = [s - 6.0 for s, _ in sub]
        cand_ends = [s + d - 6.0 for s, d in sub]
        ref = [s for s, _ in cues]
        ref_ends = [s + d for s, d in cues]
        _, plain, _ = shift.best_shift(cand, ref)
        self.assertNotAlmostEqual(plain, 6.0, delta=0.5)           # every multiple of 3 s lands every start
        scale, offset, votes = shift.best_shift(cand, ref, cand_ends=cand_ends, ref_ends=ref_ends)
        self.assertEqual(scale, 1.0)
        self.assertAlmostEqual(offset, 6.0, delta=0.05)
        self.assertEqual(votes, 200)

    def test_ends_never_outvote_a_clear_start_peak(self):
        cues = rhythm(600, 11)
        cand = [s - 2.3 for s, _ in cues]                           # its author starts every line 300 ms early
        cand_ends = [s + d - 2.0 for s, d in cues]                  # and ends it with the release's line
        ref, ref_ends = [s for s, _ in cues], [s + d for s, d in cues]
        _, offset, _ = shift.best_shift(cand, ref, cand_ends=cand_ends, ref_ends=ref_ends)
        self.assertAlmostEqual(offset, 2.3, delta=0.03)              # the gate measures starts

    def test_unknown_durations_change_nothing(self):
        for seed in range(6):
            cues = rhythm(500, 20 + seed)
            r = random.Random(seed)
            cand = sorted(s + 4.2 + r.gauss(0, 0.15) for s, _ in cues[50:450])
            ref = [s for s, _ in cues]
            self.assertEqual(shift.best_shift(cand, ref),
                             shift.best_shift(cand, ref, cand_ends=[c + 2.0 for c in cand], ref_ends=[None] * len(ref)))


class GateEndsTest(unittest.TestCase):
    """The gate records how far line ends are from the release's ends (D3): recorded, never part of the pass."""

    def setUp(self):
        self.cues = rhythm(500, 3)
        self.ref = [s for s, _ in self.cues]
        self.dur = [d for _, d in self.cues]

    def test_end_errors_are_recorded(self):
        r = random.Random(1)
        cand = [s + r.gauss(0, 0.05) for s in self.ref]
        ends = [s + d + 0.25 for s, d in self.cues]                 # ends 250 ms after the release's
        g = relgate(self.ref, cand, self.dur, cand_ends=ends)
        self.assertAlmostEqual(g["end_median_ms"], 250, delta=25)
        self.assertGreater(g["end_within300"], 0.9)
        self.assertIn("end_signed_median_ms", g)

    def test_end_errors_never_change_the_verdict(self):
        r = random.Random(2)
        for sigma in (0.03, 0.12, 0.3):
            cand = [s + r.gauss(0, sigma) for s in self.ref]
            far_ends = [s + d + 3.0 for s, d in self.cues]
            with_ends = relgate(self.ref, cand, self.dur, cand_ends=far_ends)
            without = relgate(self.ref, cand, self.dur)
            self.assertEqual(with_ends["pass"], without["pass"])
            self.assertEqual({k: v for k, v in with_ends.items() if not k.startswith("end_")}, without)

    def test_no_durations_no_end_errors(self):
        g = relgate(self.ref, self.ref, None, cand_ends=[s + 1 for s in self.ref])
        self.assertNotIn("end_median_ms", g)
        unknown = relgate(self.ref, self.ref, [0.0] * len(self.ref), cand_ends=[s + 1 for s in self.ref])
        self.assertNotIn("end_median_ms", unknown)

    def test_ends_follow_their_lines_whatever_the_order_given(self):
        cand = list(reversed(self.ref))
        ends = [s + d for s, d in reversed(self.cues)]
        g = relgate(self.ref, cand, self.dur, cand_ends=ends)
        self.assertEqual(g["end_median_ms"], 0.0)


class PipelineEndsTest(unittest.TestCase):
    """The verdicts' gates carry the end errors when the file's track has durations."""

    def test_check_original_records_end_errors(self):
        cues = rhythm(300, 4)
        index = FakeIndex([track(cues)])
        text = srt([(s, s + d + 0.1, f"line {i}") for i, (s, d) in enumerate(cues)])
        chk = pipeline.check_original(index, text, "eng")
        self.assertTrue(chk["in_sync"])
        self.assertAlmostEqual(chk["gate_original"]["end_median_ms"], 100, delta=2)

    def test_shift_sync_records_end_errors_on_the_copy(self):
        cues = rhythm(300, 5)
        index = FakeIndex([track(cues)])
        text = srt([(s + 7.0, s + d + 7.0, f"line {i}") for i, (s, d) in enumerate(cues)])
        r = pipeline.shift_sync(index, text, "eng")
        self.assertEqual(r["outcome"], "synced")
        self.assertLess(r["gate_synced"]["end_median_ms"], 5)


VOCAB = ("time money house water night light dream river stone heart road friend father mother brother sister city "
         "story letter window garden forest winter summer morning evening doctor captain soldier machine engine signal "
         "truth secret promise mistake answer question reason memory future island bridge station train ticket").split()


def sentence(r, n=None):
    return " ".join(r.choice(VOCAB) for _ in range(n or r.randint(4, 8))).capitalize() + "."


def dialogue(n, seed):
    """n release lines with their words: (start, duration, text)."""
    r = random.Random(seed)
    return [(s, d, sentence(r)) for s, d in rhythm(n, seed)]


class Bytes:
    """A RangeReader stand-in over a file in memory: counts reads and their lengths."""

    def __init__(self, path, refuse=False):
        with open(path, "rb") as f:
            self.b = f.read()
        self.size, self.reads, self.refuse = len(self.b), [], refuse
        self.fail = None      # fail(n) -> True: the n-th read (from 0) fails as a dead connection does (ReadFailed)

    def get(self, start, length):
        self.reads.append(length)
        if self.refuse:
            raise Refused("HTTP 403")
        if self.fail and self.fail(len(self.reads) - 1):
            raise ReadFailed("URLError")
        return self.b[start:start + length]


class WordIndex(FakeIndex):
    """A file index whose text track's words can be read (index.line_words): counts what is asked."""

    def __init__(self, lines, lang="en"):
        super().__init__([track([(s, d) for s, d, _ in lines], lang)])
        self.text = {s: t for s, _, t in lines}
        self.asked = []

    def line_words(self, number, starts):
        self.asked.append(list(starts))
        return {s: self.text[s] for s in starts if s in self.text}


def timed(lines, shift_s=0.0, jitter=0.0, seed=1):
    r = random.Random(seed)
    out = []
    for s, d, t in lines:
        e = r.gauss(0, jitter) if jitter else 0.0
        out.append({"s": s + shift_s + e, "e": s + d + shift_s + e, "text": t, "kind": "dialogue"})
    return out


class TextWordsTest(unittest.TestCase):
    def test_words_leave_out_tags_case_and_punctuation(self):
        self.assertEqual(textpair.words("{\\i1}Where's the <b>TRAIN</b>,\\Nfather? 9:15!"),
                         ["where", "s", "the", "train", "father", "9", "15"])

    def test_words_are_read_as_the_old_pattern_read_them_and_in_linear_time(self):
        import re
        old = re.compile(r"\{[^}]*\}|<[^>]*>|\\[Nnh]")
        rng = random.Random(3)
        for _ in range(4000):
            text = "".join(rng.choice("{}<>ab \\Nnh") for _ in range(rng.randrange(0, 14)))
            self.assertEqual(textpair.words(text), textpair.WORD.findall(old.sub(" ", text).lower()), text)
        t0 = time.monotonic()
        for opener in "{<":   # 100,000 openers with no closer after them took about 3 s
            self.assertEqual(textpair.words("{\\i1}Go<br>on " + opener * 100000), ["go", "on"])
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_at_most_120_lines_in_runs_of_three_spread_over_the_film(self):
        cues = [(s, d) for s, d, _ in dialogue(1500, 3)]
        picks = textpair.pick(cues)
        self.assertLessEqual(len(picks), textpair.MAX_LINES)
        self.assertGreaterEqual(len(picks), 100)
        lo, hi = cues[0][0], cues[-1][0]
        tenths = {min(9, int((cues[i][0] - lo) / (hi - lo) * 10)) for i in picks}
        self.assertEqual(tenths, set(range(10)))
        runs = sum(1 for a, b in zip(picks, picks[1:]) if b == a + 1)
        self.assertGreater(runs, len(picks) / 2)

    def test_each_run_starts_in_its_own_quarter_of_the_tenth_when_long_lines_are_few(self):
        # 400 lines of 1.0 s, the last of each tenth 2.0 s long: every run once started at that one long line
        cues = [(i * 2.0, 2.0 if i % 40 == 39 else 1.0) for i in range(400)]
        picks = textpair.pick(cues)
        for tenth in range(10):
            for quarter in range(4):
                first = tenth * 40 + quarter * 10
                self.assertTrue(any(first <= i < first + 10 for i in picks), (tenth, quarter))
        self.assertGreaterEqual(len(picks), 100)
        self.assertLessEqual(len(picks), textpair.MAX_LINES)
        self.assertIn(39, picks)                             # a long line in a run's own quarter still starts it


class TextMatchTest(unittest.TestCase):
    def setUp(self):
        self.lines = dialogue(400, 8)
        self.cues = [(s, d) for s, d, _ in self.lines]
        self.track = textpair.Track(self.cues, {i: self.lines[i][2] for i in textpair.pick(self.cues)})

    def test_the_same_lines_pair_whatever_their_timing(self):
        anchors = textpair.match(timed(self.lines, shift_s=-57.0), self.track)
        self.assertGreater(len(anchors), 0.8 * len(self.track))
        self.assertTrue(all(a["kind"] == "line" for a in anchors))
        errs = textpair.errors(anchors, timed(self.lines, shift_s=0.0), self.track)
        self.assertTrue(all(abs(e) < 1e-9 for _, e, _ in errs))

    def test_a_merged_line_pairs_with_both_its_parts(self):
        cand = timed(self.lines)
        k = next(a for a in textpair.pick(self.cues) if a + 1 in textpair.pick(self.cues) and a > 5)
        merged = dict(cand[k], e=cand[k + 1]["e"], text=cand[k]["text"] + " " + cand[k + 1]["text"])
        cand = cand[:k] + [merged] + cand[k + 2:]
        anchors = textpair.match(cand, self.track)
        m = [a for a in anchors if a["kind"] == "merge"]
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["c"], [k])
        errs = dict((a["c"][0], e) for a, e in zip(anchors, textpair.errors(anchors, cand, self.track)))
        self.assertEqual(errs[k][1], 0.0)
        self.assertEqual(errs[k][2], 0.0)

    def test_a_split_line_pairs_by_its_first_part(self):
        cand = timed(self.lines)
        k = textpair.pick(self.cues)[4]
        s, d, t = self.lines[k]
        w = t.rstrip(".").split()
        if len(w) < 6:
            w += ["river", "stone", "night"]
            self.lines[k] = (s, d, " ".join(w) + ".")
            self.track = textpair.Track(self.cues, {i: self.lines[i][2] for i in textpair.pick(self.cues)})
        half = len(w) // 2
        a = {"s": s, "e": s + d / 2, "text": " ".join(w[:half]), "kind": "dialogue"}
        b = {"s": s + d / 2 + 0.05, "e": s + d, "text": " ".join(w[half:]), "kind": "dialogue"}
        cand = cand[:k] + [a, b] + cand[k + 1:]
        anchors = textpair.match(cand, self.track)
        sp = [x for x in anchors if x["kind"] == "split"]
        self.assertEqual([x["c"] for x in sp], [[k, k + 1]])

    def test_a_line_said_twice_is_no_anchor(self):
        lines = list(self.lines)
        k = textpair.pick(self.cues)[7]
        lines[k + 30] = (lines[k + 30][0], lines[k + 30][1], lines[k][2])     # the same words later in the film
        anchors = textpair.match(timed(lines), self.track)
        self.assertFalse(any(self.track.lines[a["t"][0]]["i"] == k for a in anchors))

    def test_short_lines_are_no_anchors(self):
        lines = [(s, d, "Yes.") for s, d, _ in self.lines]
        track = textpair.Track(self.cues, {i: "Yes." for i in textpair.pick(self.cues)})
        self.assertEqual(textpair.match(timed(lines), track), [])

    def test_another_film_or_another_translation_pairs_little(self):
        other = dialogue(400, 99)
        self.assertLess(len(textpair.match(timed(other), self.track)), textpair.MIN_ANCHORS)

    def test_pairs_never_cross(self):
        cand = timed(self.lines)
        swapped = cand[:]
        swapped[100:300] = cand[200:300] + cand[100:200]        # two scenes in another order
        anchors = textpair.match(swapped, self.track)
        cs = [a["c"][0] for a in anchors]
        ts = [a["t"][0] for a in anchors]
        self.assertEqual(cs, sorted(cs))
        self.assertEqual(ts, sorted(ts))


class TextConfirmTest(unittest.TestCase):
    """D2: a copy that passes by timing must also hold on its words; it can only refuse."""

    def setUp(self):
        self.lines = dialogue(500, 12)
        self.cues = [(s, d) for s, d, _ in self.lines]
        self.track = textpair.Track(self.cues, {i: self.lines[i][2] for i in textpair.pick(self.cues)})

    def confirm(self, cand, limit=100.0):
        anchors = textpair.match(cand, self.track)
        return textpair.confirm(anchors, textpair.errors(anchors, cand, self.track), limit)

    def test_right_lines_at_the_right_times_pass(self):
        ok, why, s = self.confirm(timed(self.lines, jitter=0.04))
        self.assertTrue(ok, why)
        self.assertGreaterEqual(s["anchors"], textpair.MIN_ANCHORS)

    def test_every_line_on_its_neighbours_start_is_refused(self):
        """Timing alone can't see it: every start sits on a release start (the gate passes), one line off."""
        cand = [{"s": self.lines[i][0], "e": self.lines[i][0] + self.lines[i][1], "text": self.lines[i + 1][2],
                 "kind": "dialogue"} for i in range(len(self.lines) - 1)]
        g = relgate([s for s, _ in self.cues], [c["s"] for c in cand], [d for _, d in self.cues])
        self.assertTrue(g["pass"])
        ok, why, _ = self.confirm(cand)
        self.assertFalse(ok)
        self.assertIn("words", why)

    def test_one_part_of_the_film_off_is_refused(self):
        cand = timed(self.lines)
        lo, hi = cand[0]["s"], cand[-1]["s"]
        for c in cand:
            if lo + 0.4 * (hi - lo) <= c["s"] < lo + 0.5 * (hi - lo):
                c["s"] += 1.5
                c["e"] += 1.5
        ok, why, s = self.confirm(cand)
        self.assertFalse(ok)
        self.assertTrue(s["weak_tenths"])

    def test_too_few_anchors_refuse_nothing(self):
        cand = timed(dialogue(500, 77))
        ok, why, s = self.confirm(cand)
        self.assertTrue(ok)
        self.assertLess(s["anchors"], textpair.MIN_ANCHORS)

    def test_clearly_better_has_its_own_limit(self):
        cand = timed(self.lines, shift_s=0.15)
        self.assertFalse(self.confirm(cand, 100.0)[0])
        self.assertTrue(self.confirm(cand, 200.0)[0])


class TextConfirmFewAnchorsTest(unittest.TestCase):
    """With fewer than 40 anchors in all, a tenth's words alone refuse a copy only with at least 5 anchors in it, or
    when that part is weak by timing too (the timing gate's weak tenths, matched by time). Kingdom of Heaven DC's English:
    21-22 anchors read from its PGS track, refused on tenth 3 alone (about 3 anchors), no tenth weak by timing."""

    @staticmethod
    def errs(total, off, tenth=3, off_s=0.8):
        """`total` anchors over 0-1000 s: `off` of them in `tenth` with `off_s` errors, the rest 20 ms off in the others."""
        out = [(tenth * 100.0 + 10.0 + 80.0 * k / max(1, off - 1), off_s, None) for k in range(off)]
        out += [(0.0, 0.02, None), (1000.0, -0.02, None)]
        others = [b for b in range(10) if b != tenth]
        for j in range(total - off - 2):
            out.append((others[j % 9] * 100.0 + 10.0 + 7.0 * (j // 9), 0.02 if j % 2 else -0.02, None))
        return sorted(out)

    def confirm(self, total, off, timing_weak=()):
        errs = self.errs(total, off)
        self.assertEqual(len(errs), total)
        return textpair.confirm([{"kind": "line"}] * total, errs, 200.0, timing_weak)

    def test_few_anchors_and_three_off_in_one_tenth_pass(self):
        ok, why, s = self.confirm(21, 3)
        self.assertTrue(ok, why)
        self.assertEqual(s["weak_tenths"], [])
        self.assertEqual(s["spared_tenths"], [3])

    def test_many_anchors_and_three_off_in_one_tenth_still_refuse(self):
        ok, why, s = self.confirm(45, 3)
        self.assertFalse(ok)
        self.assertEqual(s["weak_tenths"], [3])
        self.assertIn("part of the runtime", why)

    def test_a_tenth_weak_by_both_refuses(self):
        ok, _, s = self.confirm(25, 3, timing_weak=[(300.0, 400.0)])
        self.assertFalse(ok)
        self.assertEqual(s["weak_tenths"], [3])

    def test_five_off_in_one_tenth_refuse(self):
        self.assertFalse(self.confirm(25, 5)[0])

    def test_four_off_with_the_timing_sound_pass(self):
        self.assertTrue(self.confirm(25, 4)[0])

    def test_the_timing_tenths_are_matched_by_time(self):
        self.assertTrue(self.confirm(25, 3, timing_weak=[(700.0, 800.0)])[0])
        self.assertFalse(self.confirm(25, 3, timing_weak=[(390.0, 480.0)])[0])     # overlaps the tenth

    def test_a_tenth_exactly_on_the_bar_is_not_weak(self):
        # Anchors exactly 500 ms off (millisecond times), 500.0000000000001 ms as floats
        exactly = 1074 / 1000 - 574 / 1000
        ok, why, s = textpair.confirm([{"kind": "line"}] * 45, self.errs(45, 5, off_s=exactly), 200.0)
        self.assertTrue(ok, why)
        self.assertEqual(s["weak_tenths"], [])


class PipelineFewAnchorsTest(unittest.TestCase):
    """through judge_copy: the timing being confirmed hands its gate's weak tenths to the word check."""

    def setUp(self):
        base = dialogue(300, 31)
        cues = [(s, d) for s, d, _ in base]
        picks = textpair.pick(cues)
        lo, hi = cues[picks[0]][0], cues[picks[-1]][0]
        self.at = lambda s: (s - lo) / (hi - lo)
        by = {}
        for i in picks:
            by.setdefault(min(9, int(self.at(cues[i][0]) * 10)), []).append(i)
        self.off = [i for i in by[3] if 0.32 < self.at(cues[i][0]) < 0.38][:3]
        self.assertEqual(len(self.off), 3)
        keep = {picks[0], picks[-1], *self.off}
        for b in range(10):
            if b != 3:
                keep.update(by[b][:3])
        self.lines = [(s, d, t if i in keep else "Yes.") for i, (s, d, t) in enumerate(base)]   # words only there
        self.text = srt([(s + 9.0, s + d + 9.0, t) for s, d, t in self.lines])                   # 9 s off
        self.index = WordIndex(self.lines)

    def judge(self, moved):
        """The copy with every line in place, but `moved` {line index: seconds}, judged as a one-shift copy is."""
        lines = pipeline.parse(self.text)
        ref = pipeline.choose_reference(self.index, len(lines), "eng")
        gate, recall = pipeline.reference_gate(ref, len(lines))
        new = [dict(l, s=s + moved.get(i, 0.0), e=s + d + moved.get(i, 0.0))
               for i, (l, (s, d, _)) in enumerate(zip(lines, self.lines))]
        words = pipeline.TextCheck(self.index, ref, lines, "eng", True)
        out = {"gate_original": pipeline.gate_lines(gate, lines)}
        return pipeline.judge_copy(out, self.text, lines, new, ref, gate, recall, snap=False, better={}, words=words,
                                   may_read=True)

    def test_three_anchors_off_with_the_timing_sound_pass(self):
        r = self.judge({i: 0.8 for i in self.off})
        self.assertEqual(r["outcome"], "synced", r.get("reason"))
        self.assertGreaterEqual(r["text"]["anchors"], textpair.MIN_ANCHORS)
        self.assertLess(r["text"]["anchors"], textpair.FEW_ANCHORS)
        self.assertEqual(r["text"]["spared_tenths"], [3])

    def test_three_anchors_off_where_the_timing_is_weak_refuse(self):
        part = {i: 0.35 for i, (s, _, _) in enumerate(self.lines) if 0.3 <= self.at(s) < 0.4}
        part.update({i: 0.8 for i in self.off})
        r = self.judge(part)
        self.assertTrue(r["better"]["pass"])                       # "clearly better" by timing, a 200 ms tenth weak
        self.assertTrue(r["gate_synced"]["weak_tenths"])
        self.assertEqual(r["outcome"], "keep_original")
        self.assertEqual(r["reason"], "a part of the runtime's words disagree with the timing")


class TrackWordsReadTest(unittest.TestCase):
    """The track's words read from each line's block (Matroska) or sample (MP4): one bounded read each."""

    def test_matroska_lines_read_from_their_blocks(self):
        r = Bytes(os.path.join(FIXTURES, "tiny.mkv"))
        index = mkv.MkvIndex(r).read_head().read_cues()
        t = index.text_tracks()[0]
        starts = [s for s, _ in t["cues"]]
        got = index.line_words(t["number"], starts)
        self.assertEqual([got[s] for s in starts], [f"Line {i}" for i in range(1, len(starts) + 1)])
        self.assertTrue(all(n <= mkv.WORD_READ for n in r.reads[-len(starts):]))

    def test_mp4_lines_read_from_their_samples(self):
        r = Bytes(os.path.join(FIXTURES, "tiny.mp4"))
        index = mp4.Mp4Index(r).read_head().read_cues()
        t = index.text_tracks()[0]
        starts = [s for s, _ in t["cues"]]
        got = index.line_words(t["number"], starts)
        self.assertEqual([got[s] for s in starts], [f"Line {i}" for i in range(1, len(starts) + 1)])

    def test_one_read_per_line_asked(self):
        r = Bytes(os.path.join(FIXTURES, "tiny.mkv"))
        index = mkv.MkvIndex(r).read_head().read_cues()
        t = index.text_tracks()[0]
        before = len(r.reads)
        index.line_words(t["number"], [s for s, _ in t["cues"]][:5])
        self.assertEqual(len(r.reads) - before, 5)
        self.assertTrue(all(n == mkv.WORD_READ for n in r.reads[before:]))
        self.assertEqual(index.words_bytes, 5 * mkv.WORD_READ)

    def test_cues_without_block_places_give_no_words(self):
        r = Bytes(os.path.join(FIXTURES, "tiny.mkv"))
        index = mkv.MkvIndex(r).read_head().read_cues()
        t = index.text_tracks()[0]
        places = index.track_places[t["number"]][1]       # where each line's block is looked for: no cue says
        places[:] = array("q", [-1] * len(places))
        before = len(r.reads)
        self.assertEqual(index.line_words(t["number"], [s for s, _ in t["cues"]]), {})
        self.assertEqual(len(r.reads), before)

    def test_a_block_found_at_two_places_is_no_answer(self):
        good = bytes([0xA3, 0x8A, 0x83, 0, 0, 0x80]) + b"Hello!"
        self.assertEqual(mkv.block_payload(good, 3), b"Hello!")
        both = bytes([0xA3, 0x85, 0x83, 0, 0, 0x80, 0x41]) + good      # size bytes that look like a block, then one
        self.assertIsNone(mkv.block_payload(both, 3))

    def test_a_refusal_stops_the_reads(self):
        r = Bytes(os.path.join(FIXTURES, "tiny.mkv"))
        index = mkv.MkvIndex(r).read_head().read_cues()
        t = index.text_tracks()[0]
        r.refuse, before = True, len(r.reads)
        with self.assertRaises(Refused):
            index.line_words(t["number"], [s for s, _ in t["cues"]])
        self.assertLessEqual(len(r.reads) - before, mkv.WORD_READS_AT_ONCE + 1)


class LineReadsStopTest(unittest.TestCase):
    """A track's line reads stop after MAX_FAILED_IN_A_ROW failures in a row (a host that doesn't answer is not
    asked for every line); a failure now and then stops nothing. One read at a time, so the counts are exact."""

    def setUp(self):
        self.old = mkv.WORD_READS_AT_ONCE
        mkv.WORD_READS_AT_ONCE = 1

    def tearDown(self):
        mkv.WORD_READS_AT_ONCE = self.old

    def lines(self, name):
        r = Bytes(os.path.join(FIXTURES, name))
        index = (mkv.MkvIndex(r) if name.endswith(".mkv") else mp4.Mp4Index(r)).read_head().read_cues()
        t = index.text_tracks()[0]
        return r, index, t, [s for s, _ in t["cues"]]

    def test_matroska_word_reads_stop_after_three_failures_in_a_row(self):
        r, index, t, starts = self.lines("tiny.mkv")
        self.assertGreater(len(starts), mkv.MAX_FAILED_IN_A_ROW + 2)
        before = len(r.reads)
        r.fail = lambda n: n >= before
        self.assertEqual(index.line_words(t["number"], starts), {})
        self.assertEqual(len(r.reads) - before, mkv.MAX_FAILED_IN_A_ROW)

    def test_mp4_word_reads_stop_after_three_failures_in_a_row(self):
        r, index, t, starts = self.lines("tiny.mp4")
        self.assertGreater(len(starts), mkv.MAX_FAILED_IN_A_ROW + 2)
        before = len(r.reads)
        r.fail = lambda n: n >= before
        self.assertEqual(index.line_words(t["number"], starts), {})
        self.assertEqual(len(r.reads) - before, mkv.MAX_FAILED_IN_A_ROW)

    def test_a_failure_now_and_then_stops_nothing(self):
        for name in ("tiny.mkv", "tiny.mp4"):
            r, index, t, starts = self.lines(name)
            before = len(r.reads)
            r.fail = lambda n: n >= before and (n - before) % 3 == 1      # every third read fails, then two don't
            got = index.line_words(t["number"], starts)
            self.assertEqual(len(r.reads) - before, len(starts), name)
            self.assertEqual(len(got), len(starts) - len(range(1, len(starts), 3)), name)

    def test_four_reads_at_once_stop_too(self):
        mkv.WORD_READS_AT_ONCE = self.old                                  # as in the service
        r, index, t, starts = self.lines("tiny.mkv")
        many = (starts * 10)[:40]
        before = len(r.reads)
        r.fail = lambda n: n >= before
        index.line_words(t["number"], many)
        self.assertLessEqual(len(r.reads) - before, mkv.MAX_FAILED_IN_A_ROW + self.old - 1)

    def test_two_failures_then_a_read_start_the_count_again(self):
        r, index, t, starts = self.lines("tiny.mkv")
        before = len(r.reads)
        r.fail = lambda n: n >= before and (n - before) % 3 != 2           # fail, fail, read: never three in a row
        index.line_words(t["number"], starts)
        self.assertEqual(len(r.reads) - before, len(starts))


class PipelineWordsTest(unittest.TestCase):
    """Where words are read (D1) and what they refuse (D2), through the pipeline."""

    def test_a_main_rule_pass_reads_no_words(self):
        lines = dialogue(400, 21)
        index = WordIndex(lines)
        text = srt([(c["s"], c["e"], c["text"]) for c in timed(lines, shift_s=6.0)])
        r = pipeline.shift_sync(index, text, "eng", text_pairing=True)
        self.assertEqual(r["outcome"], "synced")
        self.assertTrue(r["reason"].startswith("passes"))
        self.assertEqual(index.asked, [])
        self.assertTrue(pipeline.settles(r))

    def test_a_clearly_better_pass_reads_the_words_once_and_is_confirmed(self):
        lines = dialogue(500, 22)
        index = WordIndex(lines)
        text = srt([(c["s"], c["e"], c["text"]) for c in timed(lines, shift_s=9.0, jitter=0.17, seed=3)])
        r = pipeline.shift_sync(index, text, "eng", text_pairing=True, snap=False, better={})
        self.assertEqual(r["outcome"], "synced", r.get("reason"))
        self.assertIn("clearly better", r["reason"])
        self.assertEqual(len(index.asked), 1)
        self.assertLessEqual(len(index.asked[0]), textpair.MAX_LINES)
        self.assertTrue(r["text"]["ok"])
        pipeline.shift_sync(index, text, "eng", text_pairing=True, snap=False, better={})
        self.assertEqual(len(index.asked), 1)                  # kept on the file's index

    def test_words_that_disagree_refuse_a_timing_pass(self):
        lines = dialogue(400, 23)
        index = WordIndex(lines)
        ref = pipeline.choose_reference(index, 400, "eng")
        self.assertIsNotNone(pipeline.track_words(index, ref, True))      # read earlier in this job
        wrong = [(lines[i][0] + 6.0, lines[i][0] + lines[i][1] + 6.0, lines[i + 1][2]) for i in range(len(lines) - 1)]
        r = pipeline.shift_sync(index, srt(wrong), "eng", text_pairing=True)
        self.assertEqual(r["outcome"], "keep_original")
        self.assertIn("words", r["reason"])

    def test_an_original_in_sync_is_checked_only_against_words_already_read(self):
        lines = dialogue(300, 24)
        index = WordIndex(lines)
        wrong = [(lines[i][0], lines[i][0] + lines[i][1], lines[i + 1][2]) for i in range(len(lines) - 1)]
        chk = pipeline.check_original(index, srt(wrong), "eng", text_pairing=True)
        self.assertTrue(chk["in_sync"])
        self.assertEqual(index.asked, [])
        pipeline.track_words(index, pipeline.choose_reference(index, 300, "eng"), True)
        chk = pipeline.check_original(index, srt(wrong), "eng", text_pairing=True)
        self.assertFalse(chk["in_sync"])
        self.assertFalse(chk["text"]["ok"])

    def test_no_words_across_languages_or_with_the_setting_off(self):
        lines = dialogue(500, 25)
        text = srt([(c["s"], c["e"], c["text"]) for c in timed(lines, shift_s=9.0, jitter=0.17, seed=3)])
        index = WordIndex(lines, lang="en")
        pipeline.shift_sync(index, text, "rus", text_pairing=True)
        pipeline.shift_sync(index, text, "eng", text_pairing=False)
        self.assertEqual(index.asked, [])


class TrackWordsLockTest(unittest.TestCase):
    """A track's words are read once per file under that track's own lock, so languages checked against
    different tracks read them at the same time (Spanish's lane waited for English's ~120 reads), and
    one track asked for at once is still read once."""

    def setUp(self):
        self.lines = dialogue(300, 31)
        self.text = {s: t for s, _, t in self.lines}

    def index(self, read):
        """A file with an English (3) and a Spanish (4) track of the same lines, whose words `read` gives."""
        cues = [(s, d) for s, d, _ in self.lines]
        index = FakeIndex([track(cues, "en", 3), track(cues, "es", 4)])
        index.line_words = read
        return index, [dict(t, route="cues") for t in index.text_tracks()]

    def test_two_tracks_are_read_at_the_same_time(self):
        started, overlapped = {3: threading.Event(), 4: threading.Event()}, {}

        def read(number, starts):
            started[number].set()
            overlapped[number] = started[7 - number].wait(2)   # the other track's read starts while this one runs
            return {s: self.text[s] for s in starts}
        index, refs = self.index(read)
        threads = [threading.Thread(target=pipeline.track_words, args=(index, ref, True)) for ref in refs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(overlapped, {3: True, 4: True})
        self.assertTrue(all(pipeline.track_words(index, ref, False) for ref in refs))     # both kept for the job

    def test_one_track_asked_for_at_once_is_read_once(self):
        asked, go = [], threading.Event()

        def read(number, starts):
            asked.append(number)
            go.wait(5)
            return {s: self.text[s] for s in starts}
        index, refs = self.index(read)
        got = []
        threads = [threading.Thread(target=lambda: got.append(pipeline.track_words(index, refs[0], True)))
                   for _ in range(3)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        go.set()
        for t in threads:
            t.join(10)
        self.assertEqual(asked, [3])
        self.assertEqual(len(got), 3)
        self.assertTrue(got[0] is not None and all(g is got[0] for g in got))

    def test_a_track_already_read_waits_for_no_other_read(self):
        go = threading.Event()

        def read(number, starts):
            if number == 3:
                go.wait(5)                                   # English's reads hang
            return {s: self.text[s] for s in starts}
        index, (english, spanish) = self.index(read)
        self.assertIsNotNone(pipeline.track_words(index, spanish, True))
        hanging = threading.Thread(target=pipeline.track_words, args=(index, english, True))
        hanging.start()
        try:
            t0 = time.monotonic()
            self.assertIsNotNone(pipeline.track_words(index, spanish, True))
            self.assertLess(time.monotonic() - t0, 1.0)
        finally:
            go.set()
            hanging.join(10)


class AudioLockTest(unittest.TestCase):
    """On the audio route the fit runs under the lock the caller gives (one audio fit at a time), and runs
    as well without one."""

    def test_the_fit_holds_the_lock_it_is_given(self):
        lock, held = threading.Lock(), []

        def fit_tracks(index, lines, **kw):
            held.append(lock.locked())
            return {"decision": "keep_original", "why": "a stand-in"}
        text = srt([(s, s + d, t) for s, d, t in dialogue(40, 32)])
        with mock.patch.object(pipeline, "fit_tracks", fit_tracks):
            locked = pipeline.sync_subtitle(FakeIndex(), text, "eng", vad=object(), audio_lock=lock)
            unlocked = pipeline.sync_subtitle(FakeIndex(), text, "eng", vad=object())
        self.assertEqual(held, [True, False])
        self.assertFalse(lock.locked())
        for r in (locked, unlocked):
            self.assertEqual((r["route"], r["outcome"], r["reason"]), ("audio", "keep_original", "a stand-in"))


def cut_film(n, seed, cuts, gap_at_cut=3.0):
    """A film's lines (start, duration) and the same film in another cut: `cuts` = {line index: seconds the release adds
    (or, negative, removes) just before that line}. Returns (release cues, subtitle cues): the subtitle is timed to the
    other cut, so its shift changes at each cut. Cuts fall at a scene change: where the line before a cut ends less than
    `gap_at_cut` before it, that line and every one after it start later, in both cuts."""
    out, sub = [], []
    extra, pause, prev_end = 0.0, 0.0, None
    for i, (st, d) in enumerate(rhythm(n, seed)):
        st += pause
        if i in cuts:
            if prev_end is not None and st - prev_end < gap_at_cut:
                pause += gap_at_cut - (st - prev_end)
                st = prev_end + gap_at_cut
            extra += cuts[i]
        out.append((st + extra, d))
        sub.append((st, d))
        prev_end = st + d
    return out, sub


def last_scene(lines, seed, gaps, added):
    """A film (rhythm(600, 15)) and after it a scene of `lines` lines, `gaps` (shortest, longest) seconds apart. The
    release has the film 20 s later (an intro the subtitle lacks) and the scene `added` seconds later still. Returns
    (release cues, subtitle cues): the scene is a section of its own."""
    base = rhythm(600, 15)
    r = random.Random(seed)
    t, scene = base[-1][0] + base[-1][1] + 4.0, []
    for _ in range(lines):
        d = r.uniform(0.8, 1.6)
        scene.append((t, d))
        t += d + r.uniform(*gaps)
    return [(st + 20.0, d) for st, d in base] + [(st + 20.0 + added, d) for st, d in scene], base + scene


def judge_sections(release, sub_lines, lang="eng", snap=True, better=None, jitter=0.03, seed=1, texts=None):
    """A subtitle (sub_lines: (start, duration)) synced by sync_subtitle itself to a file whose track has the `release`
    cues, with the sectioned fit on and no alass behind it. `texts`: each line's words, the same in the subtitle's line
    and the release's line of the same index; the track's words can then be read and text pairing is on. Returns
    (sync_subtitle's result, what sections.fit gave, the sectioned copy's own judgement or None when none was made)."""
    r = random.Random(seed)
    says = texts or [f"line {i}" for i in range(len(sub_lines))]
    text = srt([(st + r.gauss(0, jitter), st + d, t) for (st, d), t in zip(sub_lines, says)])
    index = WordIndex([(st, d, t) for (st, d), t in zip(release, texts)]) if texts else FakeIndex([track(release)])
    seen, fit, judge = {}, sections.fit, pipeline.judge_copy

    def fit_seen(*a, **kw):
        seen["sect"] = fit(*a, **kw)
        return seen["sect"]

    def judge_seen(*a, **kw):
        got = judge(*a, **kw)
        if kw.get("how") == " (sections)":
            seen["judged"] = got
        return got
    with mock.patch.object(sections, "fit", fit_seen), mock.patch.object(pipeline, "judge_copy", judge_seen), \
            mock.patch.object(pipeline.alass_mod, "run", side_effect=pipeline.alass_mod.AlassFailed("no alass here")):
        res = pipeline.sync_subtitle(index, text, lang, audio=False, snap=snap, better=better, sectioned=True,
                                     text_pairing=bool(texts), unsettled=bool(texts))
    if "sect" not in seen:
        raise AssertionError(f"sync_subtitle never tried the sectioned fit: {res['reason']}")
    return res, seen["sect"], seen.get("judged")


class CutFilmTest(unittest.TestCase):
    """The other cut SectionsTest's subtitles are timed to. A pause made at a cut moves every line after it."""

    def test_lines_stay_in_order_with_a_pause_at_each_cut(self):
        for n, seed, cuts in ((800, 9, {200: 30.0, 400: 30.0, 600: 30.0}),
                              (1000, 17, {100 * k: 9.0 for k in range(1, 10)}), (900, 15, {400: 6.0, 412: -6.0})):
            with self.subTest(seed=seed):
                base = rhythm(n, seed)
                release, sub = cut_film(n, seed, cuts)
                self.assertEqual([d for _, d in sub], [d for _, d in base])
                self.assertEqual([d for _, d in release], [d for _, d in base])
                for i in range(1, n):
                    gap = sub[i][0] - (sub[i - 1][0] + sub[i - 1][1])
                    if i in cuts:
                        self.assertGreaterEqual(gap, 3.0 - 1e-9, i)                  # a pause at the cut
                    else:
                        self.assertAlmostEqual(gap, base[i][0] - (base[i - 1][0] + base[i - 1][1]), delta=1e-9,
                                               msg=i)                               # as the film has it
                    added = sum(v for k, v in cuts.items() if k <= i)
                    self.assertAlmostEqual(release[i][0] - sub[i][0], added, delta=1e-9, msg=i)


class SectionsTest(unittest.TestCase):
    """D4: a subtitle for another cut fitted in sections, each checked on its own; limits so a bad section can't pass."""

    def test_two_cuts_of_inception_kind_pass_in_two_sections(self):
        release, sub = cut_film(900, 7, {450: 8.0})
        sub = [(st - 55.0, d) for st, d in sub]                   # +55 s, then +63 s after the cut
        res, sect, _ = judge_sections(release, sub)
        self.assertEqual([round(x["offset_s"]) for x in sect["summary"]["sections"]], [55, 63])
        self.assertEqual(res["outcome"], "synced", res.get("reason"))
        self.assertIn("(sections)", res["reason"])               # " (snapped)" may follow it
        self.assertTrue(all(c["ok"] for c in res["section_checks"]))

    def test_lines_paired_by_their_words_anchor_the_sections(self):
        # the track's words are read once a sectioned fit is possible (TextCheck.shifts), and each section counts the
        # lines its words pair under its own shift
        release, sub = cut_film(900, 7, {450: 8.0})
        sub = [(st - 55.0, d) for st, d in sub]
        r = random.Random(7)
        res, sect, _ = judge_sections(release, sub, texts=[sentence(r) for _ in release])
        self.assertEqual(res["outcome"], "synced", res.get("reason"))
        self.assertIn("(sections)", res["reason"])
        self.assertEqual([round(x["offset_s"]) for x in sect["summary"]["sections"]], [55, 63])
        self.assertTrue(all(x["anchors"] >= sections.MIN_ANCHORS for x in sect["summary"]["sections"]),
                        sect["summary"]["sections"])
        self.assertTrue(res["text"]["ok"])                       # and the copy is confirmed by the same words

    def test_three_ad_breaks_pass(self):
        release, sub = cut_film(800, 9, {200: 30.0, 400: 30.0, 600: 30.0})
        sub = [(st - 5.0, d) for st, d in sub]
        res, sect, _ = judge_sections(release, sub)
        self.assertEqual([round(x["offset_s"]) for x in sect["summary"]["sections"]], [5, 35, 65, 95])
        self.assertEqual(res["outcome"], "synced", res.get("reason"))

    def test_a_scene_the_subtitle_lacks_still_fails(self):
        base = rhythm(900, 13)
        r = random.Random(4)
        scene = []
        t = base[449][0] + base[449][1] + 3.0
        while len(scene) < 80:
            d = r.uniform(1.0, 3.0)
            scene.append((t, d))
            t += d + r.uniform(0.5, 2.5)
        added = t - (base[450][0]) + 3.0
        release = base[:450] + scene + [(st + added, d) for st, d in base[450:]]
        res, _, judged = judge_sections(release, base)
        self.assertNotEqual(res["outcome"], "synced")
        self.assertIsNotNone(judged)                              # the sectioned copy was judged, and refused for it
        self.assertTrue(judged["gate_synced"]["missing_scene"])

    def short_section(self, release, sub, lines):
        """The fit of a subtitle whose last section breaks one of the size rules: refused for it, and recorded."""
        res, sect, judged = judge_sections(release, sub)
        self.assertIsNone(sect["lines"])
        self.assertEqual(sect["summary"]["why"], "a section is too short")
        self.assertEqual([x["lines"] for x in sect["summary"]["sections"]], [600, lines])
        self.assertIsNone(judged)
        self.assertEqual((res["outcome"], res["sections"]["why"]), ("keep_original", "a section is too short"))
        return sect["summary"]["sections"][-1]

    def test_a_section_shorter_than_the_shortest_span_is_refused(self):
        last = self.short_section(*last_scene(30, 3, (0.2, 0.7), 7.0), 30)     # 30 lines in 50 s
        self.assertLess(last["to_s"] - last["from_s"], sections.MIN_SECTION_S)
        self.assertGreaterEqual(last["lines"], sections.MIN_SECTION_LINES)

    def test_a_section_with_too_few_lines_is_refused(self):
        last = self.short_section(*last_scene(18, 4, (7.0, 10.0), 30.0), 18)   # 18 lines over 162 s
        self.assertGreaterEqual(last["to_s"] - last["from_s"], sections.MIN_SECTION_S)
        self.assertLess(last["lines"], sections.MIN_SECTION_LINES)

    def test_at_most_eight_sections(self):
        cuts = {100 * k: 9.0 for k in range(1, 10)}               # ten stretches
        release, sub = cut_film(1000, 17, cuts)
        res, sect, _ = judge_sections(release, sub)
        self.assertNotEqual(res["outcome"], "synced")
        if sect and sect["lines"] is not None:
            self.assertLessEqual(len(sect["groups"]), sections.MAX_SECTIONS)

    def test_sections_begin_only_after_a_pause(self):
        release, sub = cut_film(900, 19, {300: 12.0, 600: -7.0})
        _, sect, _ = judge_sections(release, sub)
        self.assertEqual(len(sect["groups"]), 3)
        text_lines = pipeline.parse(srt([(st, st + d, "x") for st, d in sub]))
        for a, b in zip(sect["groups"], sect["groups"][1:]):
            self.assertGreaterEqual(text_lines[b[0]]["s"] - text_lines[a[-1]]["e"], sections.GAP_S - 1e-6)

    def test_another_films_subtitle_never_passes(self):
        release = rhythm(900, 7)
        passed = 0
        for k in range(40):
            res, _, _ = judge_sections(release, rhythm(900, 1000 + k), jitter=0.05, seed=k, better={})
            passed += res["outcome"] == "synced"
        self.assertEqual(passed, 0)

    def test_a_neighbouring_episode_never_passes(self):
        release = rhythm(500, 31)
        res, _, _ = judge_sections(release, rhythm(500, 32), better={})
        self.assertNotEqual(res["outcome"], "synced")

    def test_words_are_asked_only_once_a_sectioned_fit_is_possible(self):
        release = rhythm(600, 41)
        lines = pipeline.parse(srt([(st - 20.0, st - 20.0 + d, "x") for st, d in release]))
        asked = []
        self.assertIsNone(sections.fit(lines, release, lambda: asked.append(1) or {}))
        self.assertEqual(asked, [])

    def test_lines_left_out_are_not_measured(self):
        cues = rhythm(300, 6)
        index = FakeIndex([track(cues)])
        ref = pipeline.choose_reference(index, 300, "eng")
        gate, _ = pipeline.reference_gate(ref, 300)
        lines = [{"s": s, "e": s + d, "text": "x", "kind": "dialogue"} for s, d in cues]
        far = [dict(l, s=l["s"] + 50, e=l["e"] + 50, drop=True) for l in lines[:40]] + lines[40:]
        self.assertTrue(pipeline.gate_lines(gate, far)["pass"])
        self.assertEqual(pipeline.gate_lines(gate, far)["n"], 260)

    def test_lines_stacked_in_a_section_or_under_a_wrong_end_are_kept(self):
        # A second speaker's line and every line under one end typed 100 s late were left out of the copy,
        # though no section's shift moved them back (one rule with the audio route: sections.moved_back)
        release, sub = cut_film(900, 7, {450: 8.0})
        sub = [(st - 55.0, d) for st, d in sub]
        stacked = sorted(sub + [(sub[i][0] + 0.4, 1.0) for i in range(20, 420, 10)])    # in the first section
        long_end = list(sub)
        long_end[200] = (sub[200][0], 100.0)
        for cues in (stacked, long_end):
            lines = pipeline.parse(srt([(st, st + d, "x") for st, d in cues]))
            got = sections.fit(lines, release)
            self.assertEqual([round(x["offset_s"]) for x in got["summary"]["sections"]], [55, 63])
            self.assertEqual(got["summary"]["dropped"], 0)
            self.assertFalse(any(line.get("drop") for line in got["lines"]))

    def test_only_lines_a_later_shift_moves_back_are_left_out(self):
        was = [(0.0, 2.0), (3.0, 5.0), (6.0, 8.0), (9.0, 11.0), (12.0, 14.0), (15.0, 17.0), (18.0, 20.0)]
        now = [(s + 10, e + 10) for s, e in was[:3]] + [(s + 2, e + 2) for s, e in was[3:]]   # a scene the file lacks
        self.assertEqual(sections.moved_back(was, now), {3, 4, 5})
        stacked = [(0.0, 3.0), (1.0, 2.5), (4.0, 6.0)]                 # on screen together, under one shift
        self.assertEqual(sections.moved_back(stacked, [(s + 5, e + 5) for s, e in stacked]), set())
        typo = [(0.0, 3600.0)] + [(3.0 * i, 3.0 * i + 2) for i in range(1, 50)]
        self.assertEqual(sections.moved_back(typo, typo), set())
        moved = [(s + 10, e + 10) for s, e in typo[:20]] + [(s - 5, e - 5) for s, e in typo[20:]]
        self.assertEqual(sections.moved_back(typo, moved), {20, 21, 22, 23, 24})   # an end an hour late hides none

    def test_one_steady_shift_is_no_sectioned_fit(self):
        release = rhythm(600, 41)
        _, sect, _ = judge_sections(release, [(st - 20.0, d) for st, d in release])
        self.assertIsNone(sect)


def dense(n, seed):
    """Talkative dialogue: a line every ~1.5 s, so a 30 s window hears ~20 lines."""
    r = random.Random(seed)
    t, out = 20.0, []
    for _ in range(n):
        d = r.uniform(0.8, 1.6)
        out.append((t, d))
        t += d + r.uniform(0.2, 0.7)
    return out


class Forced(AudioFit):
    """An audio fit whose first verdict is always a close call: it reads its extra windows and judges again."""

    def close_call(self, r):
        return "test" if r.get("plan") and not r.get("close_call") else None


class CloseCallTest(unittest.TestCase):
    """D5: up to 12 more windows only on a close call, within the audio budget."""

    def fit(self):
        return AudioFit(FakeIndex(), [], vad=object(), better=AUDIO_BETTER, extra=12)

    def test_what_counts_as_a_close_call(self):
        f = self.fit()
        plan = {"fit": [], "check": [], "window_s": 30.0}
        near = {"matched": 30, "miss": 0.25, "median_ms": 160.0, "within200": 0.50}
        far = {"matched": 30, "miss": 0.25, "median_ms": 400.0, "within200": 0.20}
        self.assertEqual(f.close_call({"decision": "keep_original", "plan": plan, "gate_new": near}), "close to the bar")
        self.assertIsNone(f.close_call({"decision": "keep_original", "plan": plan, "gate_new": far, "gate_old": far}))
        self.assertEqual(f.close_call({"decision": "synced", "plan": plan, "gate_new": near}), "passed on few lines")
        self.assertIsNone(f.close_call({"decision": "synced", "plan": plan, "gate_new": dict(near, matched=60)}))
        self.assertEqual(f.close_call({"decision": "synced", "plan": plan, "gate_new": dict(near, matched=60),
                                       "segment_offsets": [0.0, 9.0], "seq": [(100, 0.0), (400, 0.0), (800, 9.0)]}),
                         "a shift change rests on few windows")
        self.assertIsNone(f.close_call({"decision": "keep_original", "why": "audio budget reached", "gate_new": near}))

    def test_a_clear_verdict_reads_no_extra_window(self):
        truth = dense(1600, 3)
        starts = [st - 9.0 + random.Random(i).gauss(0, 0.03) for i, (st, _) in enumerate(truth)]
        f0, r0 = truth_fit(truth, starts, extra=0)
        f1, r1 = truth_fit(truth, starts, extra=12)
        self.assertEqual(r1["decision"], "synced")
        self.assertGreaterEqual(r1["gate_new"]["matched"], 40)
        self.assertNotIn("close_call", r1)
        self.assertEqual(f0.windows_read, f1.windows_read)

    def test_a_close_call_reads_at_most_12_more_windows_and_judges_again(self):
        truth = rhythm(700, 11)
        starts = [st - 9.0 + random.Random(i).gauss(0, 0.15) for i, (st, _) in enumerate(truth)]
        f0, r0 = truth_fit(truth, starts, extra=0)
        f1, r1 = truth_fit(truth, starts, extra=12, fit_class=Forced)
        self.assertEqual(r1["close_call"]["why"], "test")
        self.assertGreater(r1["close_call"]["windows"], 0)
        self.assertLessEqual(f1.windows_read - f0.windows_read, 12)
        self.assertEqual(r1["close_call"]["windows"], f1.windows_read - f0.windows_read)
        self.assertGreater(len(r1["plan"]["check"]), len(r0["plan"]["check"]))
        self.assertEqual(r1["decision"], r0["decision"])

    def test_extra_fit_windows_never_overlap_a_held_out_window(self):
        truth = rhythm(700, 11)
        starts = [st - 9.0 + random.Random(i).gauss(0, 0.15) for i, (st, _) in enumerate(truth)]
        f, r = truth_fit(truth, starts, extra=12, fit_class=Forced)
        W = r["plan"]["window_s"]
        for a in r["plan"]["fit"]:
            for b in r["plan"]["check"]:
                self.assertGreaterEqual(abs(a - b), W - 1e-6)

    def test_never_past_the_audio_budget(self):
        from subsync.audiofit import COST
        truth = rhythm(700, 11)
        starts = [st - 9.0 + random.Random(i).gauss(0, 0.15) for i, (st, _) in enumerate(truth)]
        rate = 200_000
        f0, _ = truth_fit(truth, starts, rate=rate)
        budget = f0.index.audio_bytes + 3 * COST * 30.0 * rate + 1
        f1, r1 = truth_fit(truth, starts, rate=rate, budget=budget, extra=12, fit_class=Forced)
        self.assertLess(r1["close_call"]["windows"], 12)                 # the budget, not the 12, set the number
        self.assertLessEqual(f1.index.audio_bytes, budget)

    def test_another_films_subtitle_is_never_accepted_with_more_windows(self):
        truth = rhythm(700, 11)
        accepted = []
        for seed in range(40):
            other = rhythm(700, 3000 + seed)
            _, r = truth_fit(truth, [st for st, _ in other], durations=[d for _, d in other], extra=12)
            first = r.get("close_call", {}).get("first", r)        # the verdict before any more windows (extra=0's)
            if r["decision"] != "keep_original" or first["decision"] != "keep_original":
                accepted.append((seed, r["decision"], r.get("why"), first["decision"]))
        self.assertEqual(accepted, [])


class SpeechEndsTest(unittest.TestCase):
    """D3 on the audio route: speech-run ends, against the subtitle's own lag, where a shift change is placed."""

    def test_the_subtitles_own_end_lag_is_measured(self):
        truth = dense(1200, 5)
        fit, _ = truth_fit(truth, [st for st, _ in truth], end_pad=-0.6)
        windows = [fit.read_window(st, 30.0) for st in (200.0, 600.0, 1000.0)]
        self.assertAlmostEqual(fit.end_lag(1.0, 0.0, windows), -0.6, delta=0.05)
        self.assertIs(type(fit).end_lag, AudioFit.end_lag)      # AudioFit's own: the stand-in only reads windows

    def test_a_shift_change_is_placed_with_ends_too(self):
        truth = rhythm(900, 104)
        half = len(truth) // 2
        starts = [st - 9.0 if i < half else st - 17.0 for i, (st, _) in enumerate(truth)]
        _, r = truth_fit(truth, starts)
        self.assertEqual(r["decision"], "synced", r.get("why"))
        self.assertEqual(len(r["model"]["segments"]), 1)
        self.assertLessEqual(abs(r["model"]["segments"][0]["after_rank"] - half), 10)
        self.assertAlmostEqual(r["end_lag_ms"], -600, delta=150)      # the subtitle's own end lag, measured

    def test_no_speech_ends_no_lag(self):
        truth = rhythm(900, 104)
        half = len(truth) // 2
        starts = [st - 9.0 if i < half else st - 17.0 for i, (st, _) in enumerate(truth)]
        _, r = truth_fit(truth, starts, end_pad=None)
        self.assertIsNone(r["end_lag_ms"])
        self.assertEqual(r["decision"], "synced")


if __name__ == "__main__":
    unittest.main()
