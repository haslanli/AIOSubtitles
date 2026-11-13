"""The better matcher: line ends, lines paired by their words, sectioned fits, more speech windows on close calls.

Pure pieces on synthetic timings and fakes: no network, no alass, no Silero.
"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subsync import mkv, mp4, pipeline, sections, shift, textpair  # noqa: E402
from subsync.reader import ReadFailed, Refused  # noqa: E402
from subsync.relgate import relgate  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def rhythm(n, seed, start=20.0):
    """n release lines: (start, duration), durations 1-4 s, gaps 0.3-3 s."""
    r = random.Random(seed)
    t, out = start, []
    for _ in range(n):
        d = r.uniform(1.0, 4.0)
        out.append((t, d))
        t += d + r.uniform(0.3, 3.0)
    return out


def periodic(n, step=3.0, seed=5, start=20.0):
    """n release lines starting every `step` seconds exactly, with durations that differ: starts alone can't tell one
    shift by a multiple of `step` from another, ends can."""
    r = random.Random(seed)
    return [(start + i * step, r.uniform(0.6, 2.6)) for i in range(n)]


class Index:
    def __init__(self, tracks):
        self.tracks = tracks

    def text_tracks(self):
        return self.tracks

    def image_tracks(self):
        return []


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
        index = Index([track(cues)])
        text = srt([(s, s + d + 0.1, f"line {i}") for i, (s, d) in enumerate(cues)])
        chk = pipeline.check_original(index, text, "eng")
        self.assertTrue(chk["in_sync"])
        self.assertAlmostEqual(chk["gate_original"]["end_median_ms"], 100, delta=2)

    def test_shift_sync_records_end_errors_on_the_copy(self):
        cues = rhythm(300, 5)
        index = Index([track(cues)])
        text = srt([(s + 7.0, s + d + 7.0, f"line {i}") for i, (s, d) in enumerate(cues)])
        r = pipeline.shift_sync(index, text, "srt", "eng")
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


class WordIndex(Index):
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

    def test_ass_block_text_is_its_last_field(self):
        self.assertEqual(textpair.ass_text("12,0,Default,,0,0,0,,Hello, there"), "Hello, there")

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
        index.track_places[t["number"]] = [(s, c, None) for s, c, _ in index.track_places[t["number"]]]
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
        r = pipeline.shift_sync(index, text, "srt", "eng", text_pairing=True)
        self.assertEqual(r["outcome"], "synced")
        self.assertTrue(r["reason"].startswith("passes"))
        self.assertEqual(index.asked, [])
        self.assertTrue(pipeline.settles(r))

    def test_a_clearly_better_pass_reads_the_words_once_and_is_confirmed(self):
        lines = dialogue(500, 22)
        index = WordIndex(lines)
        text = srt([(c["s"], c["e"], c["text"]) for c in timed(lines, shift_s=9.0, jitter=0.17, seed=3)])
        r = pipeline.shift_sync(index, text, "srt", "eng", text_pairing=True, snap=False, better={})
        self.assertEqual(r["outcome"], "synced", r.get("reason"))
        self.assertIn("clearly better", r["reason"])
        self.assertEqual(len(index.asked), 1)
        self.assertLessEqual(len(index.asked[0]), textpair.MAX_LINES)
        self.assertTrue(r["text"]["ok"])
        pipeline.shift_sync(index, text, "srt", "eng", text_pairing=True, snap=False, better={})
        self.assertEqual(len(index.asked), 1)                  # kept on the file's index

    def test_words_that_disagree_refuse_a_timing_pass(self):
        lines = dialogue(400, 23)
        index = WordIndex(lines)
        ref = pipeline.choose_reference(index, 400, "eng")
        self.assertIsNotNone(pipeline.track_words(index, ref, True))      # read earlier in this job
        wrong = [(lines[i][0] + 6.0, lines[i][0] + lines[i][1] + 6.0, lines[i + 1][2]) for i in range(len(lines) - 1)]
        r = pipeline.shift_sync(index, srt(wrong), "srt", "eng", text_pairing=True)
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
        pipeline.shift_sync(index, text, "srt", "rus", text_pairing=True)
        pipeline.shift_sync(index, text, "srt", "eng", text_pairing=False)
        self.assertEqual(index.asked, [])


def cut_film(n, seed, cuts, gap_at_cut=3.0):
    """A film's lines (start, duration) and the same film in another cut: `cuts` = {line index: seconds the release adds
    (or, negative, removes) just before that line}. Returns (release cues, subtitle cues): the subtitle is timed to the
    other cut, so its shift changes at each cut."""
    base = rhythm(n, seed)
    out, extra, prev_end = [], 0.0, None
    sub = []
    for i, (st, d) in enumerate(base):
        if i in cuts:
            if prev_end is not None and st - prev_end < gap_at_cut:   # cuts fall at a scene change: a pause
                bump = gap_at_cut - (st - prev_end)
                base = base[:i] + [(x + bump, y) for x, y in base[i:]]
                st = base[i][0]
            extra += cuts[i]
        out.append((st + extra, d))
        sub.append((st, d))
        prev_end = st + d
    return out, sub


def judge_sections(release, sub_lines, lang="eng", snap=True, better=None, jitter=0.03, seed=1):
    """The sectioned fit of a subtitle (sub_lines: (start, duration)) and its judgement, as sync_subtitle does it."""
    r = random.Random(seed)
    text = srt([(st + r.gauss(0, jitter), st + d, f"line {i}") for i, (st, d) in enumerate(sub_lines)])
    lines = pipeline.parse(text)
    index = Index([track(release)])
    ref = pipeline.choose_reference(index, len(lines), lang)
    gate, recall = pipeline.reference_gate(ref, len(lines))
    sect = sections.fit(lines, ref["cues"])
    if sect is None or sect["lines"] is None:
        return None, sect
    out = {"gate_original": pipeline.gate_lines(gate, lines)}
    res = pipeline.judge_copy(dict(out, sections=sect["summary"]), text, lines, sect["lines"], ref, gate, recall, snap,
                              0.95, better, how=" (sections)", cross=pipeline.is_cross(ref, lang), groups=sect["groups"])
    return res, sect


class SectionsTest(unittest.TestCase):
    """D4: a subtitle for another cut fitted in sections, each checked on its own; limits so a bad section can't pass."""

    def test_two_cuts_of_inception_kind_pass_in_two_sections(self):
        release, sub = cut_film(900, 7, {450: 8.0})
        sub = [(st - 55.0, d) for st, d in sub]                   # +55 s, then +63 s after the cut
        res, sect = judge_sections(release, sub)
        self.assertEqual([round(x["offset_s"]) for x in sect["summary"]["sections"]], [55, 63])
        self.assertEqual(res["outcome"], "synced", res.get("reason"))
        self.assertTrue(res["reason"].endswith("(sections)") or "(sections)" in res["reason"])
        self.assertTrue(all(c["ok"] for c in res["section_checks"]))

    def test_three_ad_breaks_pass(self):
        release, sub = cut_film(800, 9, {200: 30.0, 400: 30.0, 600: 30.0})
        sub = [(st - 5.0, d) for st, d in sub]
        res, sect = judge_sections(release, sub)
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
        res, sect = judge_sections(release, base)
        self.assertNotEqual((res or {}).get("outcome"), "synced")
        if res is not None:
            self.assertTrue(res["gate_synced"]["missing_scene"])

    def test_a_short_section_is_refused(self):
        release, sub = cut_film(900, 15, {400: 6.0, 412: -6.0})   # 12 lines with their own shift
        res, sect = judge_sections(release, sub)
        if sect and sect["lines"] is not None:
            self.assertTrue(all(len(g) >= sections.MIN_SECTION_LINES for g in sect["groups"]))
        else:
            self.assertTrue(sect is None or "short" in sect["summary"]["why"] or "lands" in sect["summary"]["why"])

    def test_at_most_eight_sections(self):
        cuts = {100 * k: 9.0 for k in range(1, 10)}               # ten stretches
        release, sub = cut_film(1000, 17, cuts)
        res, sect = judge_sections(release, sub)
        self.assertNotEqual((res or {}).get("outcome"), "synced")
        if sect and sect["lines"] is not None:
            self.assertLessEqual(len(sect["groups"]), sections.MAX_SECTIONS)

    def test_sections_begin_only_after_a_pause(self):
        release, sub = cut_film(900, 19, {300: 12.0, 600: -7.0})
        _, sect = judge_sections(release, sub)
        self.assertEqual(len(sect["groups"]), 3)
        text_lines = pipeline.parse(srt([(st, st + d, "x") for st, d in sub]))
        for a, b in zip(sect["groups"], sect["groups"][1:]):
            self.assertGreaterEqual(text_lines[b[0]]["s"] - text_lines[a[-1]]["e"], sections.GAP_S - 1e-6)

    def test_another_films_subtitle_never_passes(self):
        release = rhythm(900, 7)
        passed = 0
        for k in range(40):
            res, _ = judge_sections(release, rhythm(900, 1000 + k), jitter=0.05, seed=k, better={})
            passed += (res or {}).get("outcome") == "synced"
        self.assertEqual(passed, 0)

    def test_a_neighbouring_episode_never_passes(self):
        release = rhythm(500, 31)
        res, _ = judge_sections(release, rhythm(500, 32), better={})
        self.assertNotEqual((res or {}).get("outcome"), "synced")

    def test_words_are_asked_only_once_a_sectioned_fit_is_possible(self):
        release = rhythm(600, 41)
        lines = pipeline.parse(srt([(st - 20.0, st - 20.0 + d, "x") for st, d in release]))
        asked = []
        self.assertIsNone(sections.fit(lines, release, lambda: asked.append(1) or {}))
        self.assertEqual(asked, [])

    def test_lines_left_out_are_not_measured(self):
        cues = rhythm(300, 6)
        index = Index([track(cues)])
        ref = pipeline.choose_reference(index, 300, "eng")
        gate, _ = pipeline.reference_gate(ref, 300)
        lines = [{"s": s, "e": s + d, "text": "x", "kind": "dialogue"} for s, d in cues]
        far = [dict(l, s=l["s"] + 50, e=l["e"] + 50, drop=True) for l in lines[:40]] + lines[40:]
        self.assertTrue(pipeline.gate_lines(gate, far)["pass"])
        self.assertEqual(pipeline.gate_lines(gate, far)["n"], 260)

    def test_one_steady_shift_is_no_sectioned_fit(self):
        release = rhythm(600, 41)
        _, sect = judge_sections(release, [(st - 20.0, d) for st, d in release])
        self.assertIsNone(sect)


AUDIO_BETTER = {"matched": 25, "miss": 0.30, "median_ms": 150.0, "within": 0.55, "min_shift_s": 1.0}


def dense(n, seed):
    """Talkative dialogue: a line every ~1.5 s, so a 30 s window hears ~20 lines."""
    r = random.Random(seed)
    t, out = 20.0, []
    for _ in range(n):
        d = r.uniform(0.8, 1.6)
        out.append((t, d))
        t += d + r.uniform(0.2, 0.7)
    return out


def truth_fit(truth, starts, durations=None, extra=0, budget=None, rate=0, end_pad=-0.6, fit_class=None, **kw):
    """The audio fit on a synthetic timeline: speech starts ~80 ms off each line's start and ends `end_pad` from its end;
    windows come straight from it (no audio, no Silero), with speech-run ends. Returns (fit object, result)."""
    import numpy as np
    from subsync.audiofit import AudioFit
    r = random.Random(5)
    speech = [(st + r.gauss(0, 0.08), st + d + (end_pad or 0.0)) for st, d in truth]
    end = truth[-1][0] + truth[-1][1] + 30

    class TheIndex:
        size = int(rate * end)
        audio_bytes = 0

        def duration(self):
            return end

    base = fit_class or AudioFit

    class TruthFit(base):
        def _read_window(self, start, length):
            self.index.audio_bytes += int(length * rate)
            self.windows_read += 1
            self.audio_s += length
            n = int(round(length * 100))
            mask, onsets, offsets = np.zeros(n, dtype=bool), [], []
            for a, b in speech:
                if b <= start or a >= start + length:
                    continue
                mask[max(0, int((a - start) * 100)):min(n, int((b - start) * 100))] = True
                if a >= start:
                    onsets.append(a - self.lead)
                if b <= start + length:
                    offsets.append(b)
            w = {"start": start, "len": length, "mask": mask, "onsets": np.array(onsets)}
            if end_pad is not None:          # None: windows without speech-run ends (as before)
                w["offsets"] = np.array(offsets)
            return w
    durations = durations or [d for _, d in truth]
    lines = [{"s": st, "e": st + d, "text": "x", "kind": "dialogue"} for st, d in zip(starts, durations)]
    fit = TruthFit(TheIndex(), lines, vad=object(), better=kw.pop("better", AUDIO_BETTER), extra=extra, budget=budget,
                   **kw)
    return fit, fit.run()


class CloseCallTest(unittest.TestCase):
    """D5: up to 12 more windows only on a close call, within the audio budget."""

    def fit(self):
        from subsync.audiofit import AudioFit
        return AudioFit(Index([]), [], vad=object(), better=AUDIO_BETTER, extra=12)

    def test_what_counts_as_a_close_call(self):
        f = self.fit()
        plan = {"fit": [], "check": [], "window_s": 30.0}
        near = {"matched": 30, "miss": 0.25, "median_ms": 160.0, "within200": 0.50}
        far = {"matched": 30, "miss": 0.25, "median_ms": 400.0, "within200": 0.20}
        self.assertEqual(f.close_call({"decision": "keep_original", "plan": plan, "gate_new": near}), "close to the bar")
        self.assertIsNone(f.close_call({"decision": "keep_original", "plan": plan, "gate_new": far, "gate_old": far}))
        self.assertEqual(f.close_call({"decision": "synced", "plan": plan, "gate_new": dict(near, matched=30)}),
                         "passed on few lines")
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
        from subsync.audiofit import AudioFit

        class Forced(AudioFit):
            def close_call(self, r):
                return "test" if r.get("plan") and not r.get("close_call") else None
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
        from subsync.audiofit import AudioFit

        class Forced(AudioFit):
            def close_call(self, r):
                return "test" if r.get("plan") and not r.get("close_call") else None
        truth = rhythm(700, 11)
        starts = [st - 9.0 + random.Random(i).gauss(0, 0.15) for i, (st, _) in enumerate(truth)]
        f, r = truth_fit(truth, starts, extra=12, fit_class=Forced)
        W = r["plan"]["window_s"]
        for a in r["plan"]["fit"]:
            for b in r["plan"]["check"]:
                self.assertGreaterEqual(abs(a - b), W - 1e-6)

    def test_never_past_the_audio_budget(self):
        from subsync.audiofit import COST, AudioFit

        class Forced(AudioFit):
            def close_call(self, r):
                return "test" if r.get("plan") and not r.get("close_call") else None
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
            if r["decision"] != "keep_original":
                accepted.append((seed, r["decision"], r.get("why")))
        self.assertEqual(accepted, [])


class SpeechEndsTest(unittest.TestCase):
    """D3 on the audio route: speech-run ends, against the subtitle's own lag, where a shift change is placed."""

    def test_the_subtitles_own_end_lag_is_measured(self):
        truth = dense(1200, 5)
        from subsync.audiofit import AudioFit
        fit, _ = truth_fit(truth, [st for st, _ in truth], end_pad=-0.6)
        windows = [fit.read_window(st, 30.0) for st in (200.0, 600.0, 1000.0)]
        self.assertAlmostEqual(fit.end_lag(1.0, 0.0, windows), -0.6, delta=0.05)
        self.assertIsInstance(fit, AudioFit)

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
