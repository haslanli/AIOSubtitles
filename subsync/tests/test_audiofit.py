"""The audio fit's own pieces: windows the file's audio can't fill (past its end, none it can decode, decoded short),
the correlation of a window with only the stretch of the subtitle it can reach, the windows' places on any runtime, a
subtitle signal no longer than the windows can read; where a shift change is placed (a scene the subtitle or the file
lacks) and which lines are left out, held-out windows that no window read for the fit overlaps, each held-out part
judged, windows without speech, the audio budget while a change is narrowed down, and the record of one segment."""
import collections
import math
import random
import sys
import threading
import unittest
from unittest import mock

import numpy as np

from helpers import (AUDIO_BETTER, FIXTURES, matroska, Memory, RangeServer, rhythm, timeline_fit,  # noqa: E402
                     truth_fit)
from subsync import audiofit  # noqa: E402
from subsync.audiofit import DT, OMAX, AudioFit  # noqa: E402
from subsync.mp4 import NoAudioThere, NotMp4, open_index  # noqa: E402
from subsync.reader import RangeReader, ReadFailed  # noqa: E402
from subsync.settings import Settings  # noqa: E402


class LoudVad:
    """Speech wherever the samples are loud, as (first sample, end sample) runs."""

    def speech(self, audio):
        edges = np.diff(np.concatenate([[0], (np.abs(audio) > 0.1).astype(np.int8), [0]]))
        return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))


class SpeechIndex:
    """A file whose audio is loud during `speech` ((start, end) s) and silent elsewhere. Past `audio_end` its windows
    raise NoAudioThere, as Mp4Index's do past the audio's last sample; `decode(start, pcm)` may give a window's decoder
    output instead (cut short, or nothing)."""

    def __init__(self, speech, duration, audio_end=None, decode=None):
        self.speech, self.end, self.audio_end, self.decode = speech, duration, audio_end, decode
        self.reads = []

    def duration(self):
        return self.end

    def window_pcm(self, start, length, sr=16000, ffmpeg="ffmpeg"):
        self.reads.append(start)
        if self.audio_end is not None and start >= self.audio_end:
            raise NoAudioThere("no audio samples there")
        n = int(length * sr)
        x = np.zeros(n, dtype=np.int16)
        for a, b in self.speech:
            a, b = max(a, start), min(b, start + length, self.audio_end or b)
            if a < b:
                x[int((a - start) * sr):int((b - start) * sr)] = 8000
        pcm = x.tobytes()
        return self.decode(start, pcm) if self.decode else pcm


def film(seed=11):
    """A synthetic film's speech and a subtitle 9 s early in another author's timing style."""
    truth = rhythm(700, seed)
    r = random.Random(5)
    speech = [(s + r.gauss(0, 0.08), s + d - 0.4) for s, d in truth]
    rr = random.Random(1)
    lines = [{"s": s - 9.0 + rr.gauss(0, 0.15), "e": s - 9.0 + d, "text": "x", "kind": "dialogue"} for s, d in truth]
    return speech, lines, truth[-1][0] + truth[-1][1] + 30


class UnreadableWindowTest(unittest.TestCase):
    """A window the file's audio can't fill doesn't end the audio job with an error: past the end of the audio it is a
    window without speech; a file with no audio this module can decode keeps its original, with the reason."""

    def test_a_window_past_the_end_of_the_audio_is_a_window_without_speech(self):
        speech, lines, end = film()
        idx = SpeechIndex(speech, end, audio_end=0.8 * end)
        fit = AudioFit(idx, lines, vad=LoudVad(), better=AUDIO_BETTER)
        out = fit.run()                                                    # no error out of the fit
        self.assertIn(out["decision"], ("synced", "keep_original"))
        past = [k for k in fit.cache if k[0] >= idx.audio_end]
        self.assertTrue(past)                                              # windows were planned past the audio
        for k in past:
            w = fit.cache[k]
            self.assertEqual((len(w["mask"]), int(w["mask"].sum()), len(w["onsets"])), (int(k[1] * 100), 0, 0))
            self.assertEqual(w["len"], k[1])

    def test_a_window_without_audio_is_kept_like_any_window(self):
        idx = SpeechIndex([(105.0, 107.0)], 6000.0, audio_end=100.0)
        fit = AudioFit(idx, [{"s": 1.0, "e": 2.0, "text": "x", "kind": "dialogue"}], vad=LoudVad())
        w = fit.read_window(100.0, 30.0)
        self.assertFalse(w["mask"].any())
        self.assertIs(fit.read_window(100.0, 30.0), w)
        self.assertEqual((idx.reads, fit.windows_read), ([100.0], 1))

    def test_a_file_with_no_audio_to_decode_keeps_its_original_with_the_reason(self):
        class NoAudio(SpeechIndex):
            def window_pcm(self, start, length, sr=16000, ffmpeg="ffmpeg"):
                raise NotMp4("no audio this module can decode")
        speech, lines, end = film()
        out = AudioFit(NoAudio(speech, end), lines, vad=LoudVad(), better=AUDIO_BETTER).run()
        self.assertEqual(out["decision"], "keep_original")
        self.assertIn("no audio this module can decode", out["why"])
        self.assertEqual(out["windows"], 0)

    def test_an_mp4_decoder_that_times_out_gives_what_it_decoded(self):
        from subsync.mkv import pipe_until
        srv = RangeServer(FIXTURES)
        try:
            idx = open_index(RangeReader(srv.url("tiny.mp4"), "test"))
            # a decoder that gives 1,000 samples and then runs past its time (300 s, here 1 s)
            hangs = [sys.executable, "-c", "import sys, time; sys.stdout.buffer.write(b'\\x01\\x00' * 1000); "
                                           "sys.stdout.buffer.flush(); sys.stdin.buffer.read(); time.sleep(30)"]
            with mock.patch("subsync.mp4.decoder_cmd", lambda *a, **k: hangs), \
                    mock.patch("subsync.mp4.pipe_until", lambda *a, **k: pipe_until(*a, **k, timeout=1.0)):
                self.assertEqual(idx.window_pcm(20.0, 10.0), b"\x01\x00" * 1000)
                fit = AudioFit(idx, [{"s": 1.0, "e": 2.0, "text": "x", "kind": "dialogue"}], vad=LoudVad())
                with self.assertRaises(ReadFailed):                       # and a fit takes it as a short window
                    fit.read_window(20.0, 10.0)
        finally:
            srv.close()


class SeekIndexTest(unittest.TestCase):
    """An MKV's audio window is read from the seek index's cue point before it to the one after it; without a
    seek index (Cues) that is from the first cluster on, while the audio budget was checked against an estimate only:
    31 times the budget was read on a test file. Such a file keeps its originals before any audio is read, and every
    window counts what it really reads, so the budget holds."""

    def index(self, cue_points, times_estimate):
        """A file as an MkvIndex plans its windows: each window's read costs `times_estimate` times its estimate."""
        speech, lines, end = film()

        class Mkv(SpeechIndex):
            def window_bytes(self, start, length):
                return int(times_estimate * audiofit.COST * length * self.size / self.duration())

            def window_pcm(self, start, length, sr=16000, ffmpeg="ffmpeg"):
                self.audio_bytes = getattr(self, "audio_bytes", 0) + self.window_bytes(start, length)
                return super().window_pcm(start, length, sr, ffmpeg)
        idx = Mkv(speech, end)
        idx.cue_points, idx.size = cue_points, int(end * 500_000)      # 4 Mbit/s
        return idx, lines

    def test_an_mkv_without_a_seek_index_keeps_its_originals_without_reading_audio(self):
        idx, lines = self.index([], 16)
        out = AudioFit(idx, lines, vad=LoudVad(), better=AUDIO_BETTER, budget=10 ** 9).run()
        self.assertEqual((out["decision"], out["why"], out["windows"]), ("keep_original", "no seek index", 0))
        self.assertEqual(idx.reads, [])

    def test_a_seek_index_too_sparse_for_the_budget_keeps_its_originals_without_reading_audio(self):
        idx, lines = self.index([(0.0, 0)], audiofit.SPARSE_INDEX + 1)
        out = AudioFit(idx, lines, vad=LoudVad(), better=AUDIO_BETTER, budget=10 ** 9).run()
        self.assertEqual((out["decision"], out["why"]), ("keep_original", "seek index too sparse for the audio budget"))
        self.assertEqual(idx.reads, [])

    def test_each_window_counts_what_it_really_reads(self):
        idx, lines = self.index([(0.0, 0)], 3)
        one = idx.window_bytes(0.0, 30.0)
        fit = AudioFit(idx, lines, vad=LoudVad(), budget=int(2.5 * one))
        fit.prefetch([100.0, 200.0, 300.0, 400.0], 30.0)             # by their estimates, all four would fit
        self.assertEqual(len(idx.reads), 2)
        fit.read_window(100.0, 30.0)                                  # read already
        with self.assertRaises(audiofit.AudioBudget):
            fit.read_window(300.0, 30.0)
        self.assertLessEqual(idx.audio_bytes, fit.budget)

    def test_a_real_mkv_without_cues_has_no_seek_index(self):
        from subsync.mkv import MkvIndex
        index = MkvIndex(Memory(matroska())).read_head().read_cues()
        fit = AudioFit(index, [{"s": 1.0, "e": 2.0, "text": "x", "kind": "dialogue"}], vad=LoudVad(), budget=10 ** 9)
        self.assertEqual(fit.unseekable(), "no seek index")


class ShortWindowTest(unittest.TestCase):
    """A window decoded short (a read or a decoder that stopped early) is a failed read, never kept for the file's
    other subtitles; a window that runs past the file's end only has to hold what is in the file."""

    def fit(self, decode, duration=6000.0):
        idx = SpeechIndex([(15.0, 17.0), (5992.0, 5995.0)], duration, decode=decode)
        return idx, AudioFit(idx, [{"s": 1.0, "e": 2.0, "text": "x", "kind": "dialogue"}], vad=LoudVad())

    def test_an_empty_window_is_a_failed_read_and_never_kept(self):
        idx, fit = self.fit(lambda start, pcm: b"")
        for _ in range(2):
            with self.assertRaises(ReadFailed):
                fit.read_window(10.0, 30.0)
        self.assertEqual((idx.reads, fit.cache), ([10.0, 10.0], {}))      # read again the next time, nothing kept

    def test_half_a_window_is_a_failed_read(self):
        idx, fit = self.fit(lambda start, pcm: pcm[:len(pcm) // 2])
        with self.assertRaises(ReadFailed):
            fit.read_window(10.0, 30.0)
        self.assertEqual(fit.cache, {})

    def test_a_window_decoded_nearly_whole_counts_what_was_decoded(self):
        idx, fit = self.fit(lambda start, pcm: pcm[:int(len(pcm) * 0.97) // 2 * 2])
        w = fit.read_window(10.0, 30.0)
        self.assertAlmostEqual(w["len"], 29.1, delta=0.01)
        self.assertEqual(len(w["onsets"]), 1)

    def test_a_window_past_the_files_end_holds_what_is_in_the_file(self):
        idx, fit = self.fit(lambda start, pcm: pcm[:int((6000.0 - start) * 16000) * 2])
        w = fit.read_window(5990.0, 30.0)
        self.assertAlmostEqual(w["len"], 10.0)
        self.assertEqual(len(w["onsets"]), 1)

    def test_a_score_per_frame_never_divides_by_zero(self):
        empty = {"start": 0.0, "len": 0.0, "mask": np.zeros(0, dtype=bool)}
        offs = np.arange(-3, 4) * DT
        self.assertEqual(AudioFit.zat(empty, (offs, np.zeros(7)), 0.0), 0.0)

    def test_planned_windows_decoded_to_nothing_end_the_fit_as_a_failed_read(self):
        speech, lines, end = film()
        idx = SpeechIndex(speech, end, decode=lambda start, pcm: b"" if start > end / 2 else pcm)
        fit = AudioFit(idx, lines, vad=LoudVad(), better=AUDIO_BETTER)
        with self.assertRaises(ReadFailed):                                # never ZeroDivisionError
            fit.run()
        self.assertTrue([s for s in idx.reads if s > end / 2])
        self.assertFalse([k for k in fit.cache if k[0] > end / 2])        # none of them kept for the file


def whole_film_corr(w, ssig):
    """The correlation as it was computed before, one FFT of the whole subtitle signal per window (the reference)."""
    v = np.where(w["mask"][::5], 1.0, -1.0)
    k0, pad = int(round(w["start"] / DT)), int(OMAX / DT)
    s = np.concatenate([-np.ones(pad), ssig, -np.ones(pad + len(v))])
    n = len(s) + len(v)
    full = np.fft.irfft(np.fft.rfft(s, n) * np.conj(np.fft.rfft(v, n)), n)
    offs = np.arange(-pad, pad + 1)
    return offs * DT, full[k0 - offs + pad]


class CorrelationTest(unittest.TestCase):
    """A window is correlated with only the stretch of the subtitle's signal it can reach at -OMAX..OMAX: the same
    scores as one FFT of the whole film's signal per window, which took most of a fit's CPU."""

    def setUp(self):
        r = np.random.default_rng(7)
        self.ssig = np.where(r.random(140_000) < 0.4, 1.0, -1.0)         # a 2 h subtitle's signal
        self.masks = [r.random(n) < 0.5 for n in (3000, 2000, 1167)]       # 30 s, 20 s and a shorter window

    def test_the_scores_are_the_whole_signals(self):
        for start in (0.0, 3.0, 599.0, 1000.0, 3500.5, 6900.0, 6999.0, 7000.0):   # the signal ends at 7000 s
            for mask in self.masks:
                w = {"start": start, "mask": mask}
                offs, want = whole_film_corr(w, self.ssig)
                got_offs, got = AudioFit.fast_corr(w, self.ssig)
                np.testing.assert_allclose(got_offs, offs)
                np.testing.assert_allclose(got, want, atol=1e-6, err_msg=f"window at {start}")

    def test_past_the_signals_end_there_are_no_lines(self):
        w = {"start": 7300.0, "mask": self.masks[0]}
        v = np.where(w["mask"][::5], 1.0, -1.0)
        k0, n = int(round(7300.0 / DT)), len(self.ssig)
        offs, got = AudioFit.fast_corr(w, self.ssig)
        for off in (-12000, -6000, -1, 0, 3000, 6000, 12000):
            sig = np.array([self.ssig[x] if 0 <= x < n else -1.0 for x in range(k0 - off, k0 - off + len(v))])
            self.assertEqual(got[off + 12000], float(sig @ v), off)

    def test_a_window_reads_only_the_stretch_it_can_reach(self):
        class Reach:
            """A subtitle signal that serves slices up to `most` frames long and refuses to be read whole."""

            def __init__(self, sig, most):
                self.sig, self.most = sig, most

            def __len__(self):
                return len(self.sig)

            def __getitem__(self, key):
                if not isinstance(key, slice) or (key.stop or len(self.sig)) - (key.start or 0) > self.most:
                    raise AssertionError(f"read {key} of the subtitle signal")
                return self.sig[key]

            def __array__(self, *args, **kwargs):
                raise AssertionError("the whole subtitle signal was read")
        mask = self.masks[0]
        reach = 2 * int(OMAX / DT) + len(mask[::5])
        for start in (3.0, 3500.5, 6999.0):
            w = {"start": start, "mask": mask}
            _, got = AudioFit.fast_corr(w, Reach(self.ssig, reach))
            np.testing.assert_allclose(got, whole_film_corr(w, self.ssig)[1], atol=1e-6, err_msg=f"window at {start}")

    def test_scores_are_whole_frames_so_ties_never_depend_on_rounding(self):
        for start in (3.0, 3500.5):
            _, got = AudioFit.fast_corr({"start": start, "mask": self.masks[0]}, self.ssig)
            np.testing.assert_array_equal(got, np.round(got))

    def test_a_tie_goes_to_the_offset_nearest_the_one_asked(self):
        vals = np.zeros(len(audiofit.LAGS))
        vals[[100, 12010, 20000]] = 5.0                       # -594.95 s, +0.5 s and +399.95 s score the same
        self.assertEqual(audiofit.LAGS[audiofit.peak(vals, 0.0)], audiofit.LAGS[12010])
        self.assertEqual(audiofit.LAGS[audiofit.peak(vals, 390.0)], audiofit.LAGS[20000])
        self.assertEqual(audiofit.LAGS[audiofit.peak(vals, -600.0)], audiofit.LAGS[100])


class CurveReuseTest(unittest.TestCase):
    """The chosen scale's curves from the scale loop are what labels the fit windows: each fit window is correlated
    once per scale, never again for its label."""

    def test_each_fit_window_is_correlated_once_per_scale(self):
        calls, signals = collections.Counter(), []

        class Counted(AudioFit):
            @staticmethod
            def fast_corr(w, ssig):
                signals.append(ssig)           # kept, so no two signals share an id
                calls[(id(ssig), w["start"])] += 1
                return AudioFit.fast_corr(w, ssig)
        speech, lines, end = film()
        out = Counted(SpeechIndex(speech, end), lines, vad=LoudVad(), better=AUDIO_BETTER).run()
        self.assertEqual(out["decision"], "synced")
        fit = set(out["plan"]["fit"])
        per_window = [n for (_, start), n in calls.items() if start in fit]
        self.assertEqual(len(per_window), len(audiofit.SCALES) * len(fit))
        self.assertEqual(set(per_window), {1})


def scanned(fit, usual, lo, hi, avoid=()):
    """place() as it scanned every 2 s step of the stretch (the reference)."""
    import bisect
    W, starts = fit.W, sorted(line["s"] for line in fit.lines)

    def lines_in(t):
        return bisect.bisect_left(starts, t + W) - bisect.bisect_left(starts, t)

    def free(t):
        return all(abs(t - a) >= W for a in avoid)
    if free(usual) and lines_in(usual) >= audiofit.MIN_WINDOW_LINES:
        return usual
    best, best_n = usual, lines_in(usual) if free(usual) else -1
    t = lo
    while t + W <= hi + 1e-6:
        if free(t) and lines_in(t) > best_n:
            best, best_n = t, lines_in(t)
        t += 2.0
    return best


class PlaceTest(unittest.TestCase):
    """Where a window goes no longer depends on the runtime's length: an absurd duration (1e17 s, where adding 2 s to
    a float changes nothing) ended neither the plan nor the audio lane it holds."""

    def fit(self, lines, w=30.0):
        fit = AudioFit(SpeechIndex([], 7200.0), lines, vad=LoudVad())
        fit.W = w
        return fit

    def test_planning_ends_at_once_on_an_absurd_duration(self):
        r = random.Random(4)
        lines = [{"s": r.uniform(0, 7000), "e": 7001.0, "text": "x", "kind": "dialogue"} for _ in range(400)]
        for D in (1e9, 1e17):
            fit = self.fit(lines)
            margin = 0.03 * D
            done = []
            t = threading.Thread(target=lambda: done.append(fit.plan(D, margin, (D - 2 * margin) / fit.K)), daemon=True)
            t.start()
            t.join(10)
            self.assertTrue(done, f"plan() still running after 10 s for a {D:g} s runtime")
            self.assertEqual(len(done[0]["fit"]), fit.K)

    def test_the_place_is_the_two_second_scans(self):
        r = random.Random(3)
        for _ in range(500):
            lines = [{"s": r.uniform(0, 7200), "e": 0.0, "text": "x", "kind": "dialogue"}
                     for _ in range(r.choice([0, 3, 10, 50, 300]))]
            fit = self.fit(lines, w=r.choice([30.0, 12.5, 14.9, 20.0]))
            lo = r.uniform(0, 6000)
            hi = lo + r.uniform(0, 900)
            usual = r.uniform(lo, hi)
            avoid = [r.uniform(lo - 60, hi + 60) for _ in range(r.choice([0, 0, 2, 8]))]
            self.assertAlmostEqual(fit.place(usual, lo, hi, avoid), scanned(fit, usual, lo, hi, avoid), delta=1e-6)


class SignalLengthTest(unittest.TestCase):
    """One line ending hours late (99:59:59) doesn't make the subtitle's signal millions of frames long: it runs only
    as far as a window can read, and the fit is the same."""

    def test_one_line_ending_hours_late(self):
        truth = rhythm(700, 11)
        end = truth[-1][0] + truth[-1][1] + 30
        most = int((end + OMAX + 2 * 30.0) / DT) + 1

        class Bounded(AudioFit):
            @staticmethod
            def sub_signal(lines, scale, n):
                if n > most:
                    raise AssertionError(f"a subtitle signal of {n} frames")
                return AudioFit.sub_signal(lines, scale, n)
        starts = [s - 9.0 + random.Random(i).gauss(0, 0.15) for i, (s, _) in enumerate(truth)]
        durations = [d for _, d in truth]
        _, plain = truth_fit(truth, starts, durations=durations, fit_class=Bounded)
        durations[-1] = 99 * 3600 + 59 * 60 + 59 - starts[-1]
        _, out = truth_fit(truth, starts, durations=durations, fit_class=Bounded)
        self.assertEqual(out["decision"], "synced")
        self.assertAlmostEqual(out["moved_s"], 9.0, delta=0.15)
        for key in ("decision", "why", "model", "moved_s", "gate_new", "segment_offsets"):
            self.assertEqual(out[key], plain[key], key)


class GateBoundaryTest(unittest.TestCase):
    def test_an_error_of_exactly_the_bar_counts_as_within_it(self):
        # Onsets and starts on a millisecond grid exactly 200 ms (100 ms) apart, a hair either side as floats
        ms = [600_000 + 3_217 * i for i in range(200)]
        g = AudioFit.gate([(t + 200) / 1000 - t / 1000 for t in ms])
        self.assertEqual((g["within200"], g["within200_misses_as_fail"]), (1.0, 1.0))
        self.assertTrue(AudioFit.gate([(t + 100) / 1000 - t / 1000 for t in ms])["pass"])   # the median bar: 100 ms


class BetterDefaultTest(unittest.TestCase):
    """The second way to pass has one set of defaults: settings' and the close call's when the rule is off."""

    def test_settings_default_is_the_audio_fits(self):
        self.assertEqual(Settings({}).audio_better, audiofit.BETTER)
        self.assertIsNone(Settings({"SUBSYNC_AUDIO_BETTER": "off"}).audio_better)

    def test_with_the_rule_off_a_close_call_is_measured_against_the_defaults(self):
        fit = AudioFit(SpeechIndex([], 600.0), [], vad=LoudVad(), better=None, extra=12)
        plan = {"fit": [], "check": [], "window_s": 30.0}
        near = {"matched": 30, "miss": 0.25, "median_ms": 160.0, "within200": 0.50}
        self.assertEqual(fit.close_call({"decision": "keep_original", "plan": plan, "gate_new": near}), "close to the bar")
        self.assertIsNone(fit.close_call({"decision": "keep_original", "plan": plan,
                                          "gate_new": dict(near, median_ms=400.0)}))


def scene_film(at, scene, sub=None, seed=11, early=9.0):
    """A subtitle (`sub`, (start, duration); rhythm(1419, seed) by default) `early` s early of a release with a scene of
    `scene` s more (> 0: the release adds it, with talk of its own) or fewer (< 0: the subtitle's lines of it are
    missing from the release) at subtitle time `at`. Returns the release's lines, the subtitle's, and where each
    subtitle line belongs on the release (None: a line of the scene the release lacks)."""
    sub = sub or rhythm(1419, seed)
    if scene > 0:
        r = random.Random(seed + 99)
        talk, t = [], at + early + 2.0
        while t < at + early + scene - 3:
            d = r.uniform(1.0, 4.0)
            talk.append((t, d))
            t += d + r.uniform(0.3, 3.0)
        true = [s + early + (scene if s >= at else 0.0) for s, _ in sub]
        return sorted([(x, d) for x, (_, d) in zip(true, sub)] + talk), sub, true
    true = [None if at <= s < at - scene else s + early + (scene if s >= at else 0.0) for s, _ in sub]
    return [(x, d) for x, (_, d) in zip(true, sub) if x is not None], sub, true


def placed(res, true):
    """(kept lines more than 1 s from where they belong, kept lines of the scene the release lacks, other lines left
    out) of a copy."""
    off = scene = dropped = 0
    for line, x in zip(res["lines"], true):
        if line.get("drop"):
            dropped += x is not None
        elif x is None:
            scene += 1
        elif abs(line["s"] - x) > 1.0:
            off += 1
    return off, scene, dropped


class ShiftChangeTest(unittest.TestCase):
    """Where the offset changes (a subtitle for another cut), on speech that ends with its lines: the change is placed
    by the windows that fit one offset or the other, and only the lines of a scene the file lacks are left out."""

    def fit(self, at, scene, **kw):
        truth, sub, true = scene_film(at, scene)
        _, res = truth_fit(truth, [s for s, _ in sub], durations=[d for _, d in sub], end_pad=0.0, **kw)
        return res, true

    def test_a_scene_the_subtitle_lacks_moves_only_the_lines_after_it(self):
        # A window inside the added scene fit neither offset and stopped the narrowing; the change went to the
        # earliest line of a bracket minutes wide: 58 to 162 lines published a scene-length late
        for at, scene in ((1500, 120), (3300, 60), (2600, 90)):
            with self.subTest(at=at, scene=scene):
                res, true = self.fit(at, scene)
                self.assertEqual(res["decision"], "synced")
                self.assertLessEqual(placed(res, true)[0], 20)

    def test_a_scene_the_file_lacks_is_left_out_and_the_rest_placed(self):
        # A missing scene over about 90 s found no change at all; every line kept the first offset, and the
        # rest of the film was published the scene's length late
        for at, scene in ((3000, -100), (3000, -300), (1500, -300), (4000, -150)):
            with self.subTest(at=at, scene=scene):
                res, true = self.fit(at, scene)
                self.assertEqual(res["decision"], "synced")
                self.assertEqual(len(res["model"]["segments"]), 1)
                off, scene_kept, dropped = placed(res, true)
                self.assertEqual(off, 0)
                self.assertLessEqual((scene_kept, dropped), (1, 1))

    def test_lines_on_screen_together_and_one_wrong_end_are_kept(self):
        # With one offset and no change, every line starting under an earlier line's end was left out of the
        # copy: a second speaker's lines, and the 869 lines under one end typed an hour late
        truth = rhythm(1700, 11)
        r = random.Random(1)
        starts = [s - 9.0 + r.gauss(0, 0.03) for s, _ in truth]
        stacked = [d for _, d in truth]
        for i in range(20, 1600, 43):
            stacked[i] = truth[i + 1][0] - truth[i][0] + 2.0          # on screen 2 s into the next line
        typo = [d for _, d in truth]
        typo[30] += 3600.0
        for durations in (stacked, typo):
            _, res = truth_fit(truth, starts, durations=durations)
            self.assertEqual(res["decision"], "synced")
            self.assertEqual(res["model"]["dropped"], 0)
            self.assertFalse(any(line.get("drop") for line in res["lines"]))

    def test_one_segment_is_recorded_with_the_offset_applied(self):
        # With one segment the lines moved by its own offset, while the record and the "under 100 ms is never
        # published" rule took the overall one (lines moved +9 s, model -291 s)
        class NoChange(AudioFit):      # a change seen but never placed: every line keeps the first window's offset
            def narrow(self, lo, hi, oa, ob, avoid, curve):
                return math.inf, -math.inf, [], []
        truth, sub, _ = scene_film(1500, -300)
        _, res = truth_fit(truth, [s for s, _ in sub], durations=[d for _, d in sub], end_pad=0.0, fit_class=NoChange)
        self.assertEqual(len(res["segment_offsets"]), 1)
        self.assertEqual(res["model"]["offset"], round(res["segment_offsets"][0], 2))    # not the overall -291 s
        self.assertAlmostEqual(res["model"]["offset"], 9.0, delta=0.3)


class HeldOutTest(unittest.TestCase):
    """The held-out windows are never used for the fit: no window read for it (a confirmation, a step narrowing
    down a shift change) overlaps one, the extra held-out windows keep off every window read, and each counts once."""

    @staticmethod
    def recorded(truth, starts, durations=None, **kw):
        calls = []

        class Recorded(AudioFit):
            def read_window(self, start, length):
                calls.append(start)
                return super().read_window(start, length)
        fit, res = truth_fit(truth, starts, durations=durations, fit_class=Recorded, **kw)
        return fit, res, calls

    def test_no_window_read_for_the_fit_overlaps_a_held_out_one(self):
        # The second step narrowing down a change landed exactly on a held-out window: it placed the change,
        # then confirmed it, and the original's check counted it twice
        truth, sub, _ = scene_film(1050, -40)
        fit, res, calls = self.recorded(truth, [s for s, _ in sub], [d for _, d in sub], end_pad=0.0)
        self.assertEqual(len(res["model"]["segments"]), 1)
        W = res["plan"]["window_s"]
        for c in res["plan"]["check"]:
            self.assertEqual([x for x in calls if abs(x - c) < W - 1e-6], [c], c)

    def test_extra_held_out_windows_keep_off_the_fit_windows(self):
        # On a film under about 17 minutes the extra held-out windows, read when too few lines
        # match, overlapped fit windows;: they went to the first stretches of the film, not where the copy
        # matched the fewest lines
        film = rhythm(215, 11)                                                  # 15 minutes
        end = film[-1][0] + film[-1][1] + 30
        margin, vspan = 0.03 * end, (end - 0.06 * end) / 4
        quiet = (margin + 2 * vspan, margin + 3 * vspan)                        # the third stretch: nothing heard
        spoken = [(s, d) for s, d in film[::2] if not quiet[0] <= s <= quiet[1]]   # elsewhere half the lines
        starts = [s - 9.0 + random.Random(i).gauss(0, 0.05) for i, (s, _) in enumerate(film)]
        fit, res, calls = self.recorded(spoken, starts, [d for _, d in film], end_pad=None)
        plan, W = res["plan"], res["plan"]["window_s"]
        held = len(plan["check"])
        at = max(i for i in range(len(calls) - held + 1) if calls[i:i + held] == plan["check"])
        extra, fitting = calls[at + held:], calls[:at]
        self.assertTrue(extra, "no extra held-out window was read")
        for x in extra:
            self.assertTrue(all(abs(x - f) >= W - 1e-6 for f in fitting + plan["check"]), (x, fitting))
        self.assertTrue(quiet[0] <= extra[0] < quiet[1], (extra, quiet))       # where the copy matched nothing


class HeldOutPartTest(unittest.TestCase):
    """The held-out windows judged part by part, beside the pooled numbers. A copy clearly off in one part
    meets neither way to pass; the verdict is a close call that reads the windows beside that part, and when the part
    is still off the original is kept."""

    def off_in_one_part(self, extra=12):
        truth = rhythm(700, 11)
        end = truth[-1][0] + truth[-1][1] + 30
        margin, span = 0.03 * end, (end - 0.06 * end) / 8
        lo, hi = margin + 2.5 * span + 30, margin + 3.5 * span - 45     # around held-out window 1, off every fit window
        r = random.Random(1)
        starts = [s - 9.0 + r.gauss(0, 0.03) + (20.0 if lo <= s <= hi else 0.0) for s, _ in truth]
        fit, res = truth_fit(truth, starts, extra=extra, end_pad=None)
        return fit, res, (lo, hi)

    def test_a_copy_off_in_one_part_keeps_the_original(self):
        # 4.5 minutes of the copy 20 s off: pooled, its held-out numbers pass "clearly better", and it was published
        fit, res, (lo, hi) = self.off_in_one_part()
        first = res["close_call"]["first"]
        self.assertEqual((first["decision"], res["close_call"]["why"]), ("keep_original", "close to the bar"))
        self.assertTrue(fit.meets_better(dict(first["gate_new"], parts_off=[])))     # without the part rule
        self.assertEqual(res["decision"], "keep_original")
        self.assertTrue(res["gate_new"]["parts_off"])
        W = res["plan"]["window_s"]
        self.assertGreaterEqual(sum(lo <= c and c + W <= hi for c in res["plan"]["check"]), 3)   # read beside it
        _, alone, _ = self.off_in_one_part(extra=0)                     # no close calls: the first verdict decides
        self.assertEqual(alone["decision"], "keep_original")
        self.assertTrue(alone["gate_new"]["parts_off"])

    def test_a_part_the_copy_is_off_in(self):
        fit = AudioFit(SpeechIndex([], 600.0), [], vad=LoudVad())
        w = {"start": 100.0, "len": 30.0, "onsets": np.arange(101.0, 129.0, 2.0)}       # 14 onsets
        original = [x + 0.05 for x in np.arange(101.0, 129.0, 2.0)]                    # the subtitle's 14 lines there
        self.assertFalse(fit.part_off(original, original, [w]))
        self.assertTrue(fit.part_off([x + 0.7 for x in original], original, [w]))       # none within 200 ms
        self.assertTrue(fit.part_off([], original, [w]))         # the copy moved every line away from the speech
        self.assertFalse(fit.part_off([], original[:4], [w]))    # where the subtitle has few lines itself
        self.assertFalse(fit.part_off(original[:4], original[:4], [w]))

    def test_copies_by_other_authors_keep_their_verdicts(self):
        # calibration: a right copy by another author has about 60 % of its lines within 200 ms; the part rule
        # refuses none of them that the pooled numbers decide (sweeps: 480 copies, 0 changed; wrong parts 64 % of
        # windows)
        for seed in range(6):
            truth = rhythm(700, 11 + seed)
            r = random.Random(1000 + seed)
            starts = [s - 9.0 + r.gauss(0, 0.22) for s, _ in truth]
            with mock.patch.object(AudioFit, "part_off", return_value=False):
                pooled = truth_fit(truth, starts, extra=12, end_pad=None)[1]
            res = truth_fit(truth, starts, extra=12, end_pad=None)[1]
            self.assertEqual((res["decision"], res.get("why")), (pooled["decision"], pooled.get("why")), seed)


class SpeechlessWindowTest(unittest.TestCase):
    """Fit windows where the subtitle has lines but no speech is heard (music, singing, noise) favoured any
    offset that moved the lines out of them; they now score no offset for the whole film."""

    PAL = 25 / (24000 / 1001)

    def fit(self, seed, scale, off, deaf=5, unrelated=False):
        truth = rhythm(1600, seed)
        r = random.Random(seed + 100)
        end = truth[-1][0] + truth[-1][1] + 30
        margin, span = 0.03 * end, (end - 0.06 * end) / 8
        zones = [(margin + (i + 0.5) * span - 40, margin + (i + 0.5) * span + 40) for i in r.sample(range(8), deaf)]
        heard = [(s, d) for s, d in truth if not any(lo <= s <= hi for lo, hi in zones)]
        if unrelated:
            other = rhythm(1600, 5000 + seed)
            starts, durations = [s for s, _ in other], [d for _, d in other]
        else:
            starts = [(s - off) / scale + r.gauss(0, 0.05) for s, _ in truth]
            durations = [d / scale for _, d in truth]
        rr = random.Random(5)
        speech = [(s + rr.gauss(0, 0.08), s + d) for s, d in heard]

        class Index:
            def duration(self):
                return end
        lines = [{"s": s, "e": s + d, "text": "x", "kind": "dialogue"} for s, d in zip(starts, durations)]
        return timeline_fit(speech)(Index(), lines, vad=object(), better=AUDIO_BETTER).run()

    def test_a_pal_subtitle_on_a_film_of_music_parts(self):
        # 5 of 8 fit windows deaf: the overall offset ran minutes away and no speed change won (seeds 1, 2, 3, 5 of 6)
        for seed in (1, 2, 3):
            with self.subTest(seed=seed):
                res = self.fit(seed, self.PAL, 2.0)
                self.assertEqual(res["decision"], "synced")
                self.assertAlmostEqual(res["model"]["scale"], self.PAL, places=4)

    def test_another_films_subtitle_is_still_never_accepted(self):
        self.assertEqual([seed for seed in range(8) if self.fit(seed, 1.0, 0.0, unrelated=True)["decision"]
                          != "keep_original"], [])


class RuleRevisionTest(unittest.TestCase):
    def test_verdicts_of_the_old_rules_are_judged_again(self):
        # the held-out parts, the shift changes, the lines left out and the one-shift path's pairs came in revision 11
        from subsync import RULE_REVISION
        self.assertGreaterEqual(RULE_REVISION, 11)


class NarrowingBudgetTest(unittest.TestCase):
    """Windows read while a shift change is narrowed down count toward the file's audio budget like any other;
    when the next one would pass it, the change is placed from the windows read and the copy judged as usual."""

    def test_the_window_length_is_rounded_down(self):
        # Rounded to the nearest tenth, the planned windows could cost 0.3 % over the budget
        rate = 1_000_000
        budget = 14.96 * (8 + 4 + audiofit.PLANNED) * audiofit.COST * rate
        length = audiofit.planned_window(budget, rate)
        self.assertEqual(length, 14.9)
        self.assertLessEqual((8 + 4 + audiofit.PLANNED) * audiofit.COST * rate * length, budget)

    def test_a_short_window_fit_keeps_what_it_read(self):
        # Windows shortened to fit the budget (15 s on a 24 Mbit/s file) take 5 or 6 steps to narrow a change
        # down, not the 4 planned: the read that would pass the budget threw the whole fit away ("audio budget reached")
        r = random.Random(3)
        truth, t = [], 20.0
        for _ in range(2800):                       # a line every ~2 s: 15 s held-out windows still hear enough
            d = r.uniform(0.8, 1.6)
            truth.append((t, d))
            t += d + r.uniform(0.2, 0.7)
        end = truth[-1][0] + truth[-1][1] + 30
        rate = 3_000_000
        budget = int(15.0 * (8 + 4 + audiofit.PLANNED) * audiofit.COST * rate) + 1

        class Index:
            size = rate * end
            audio_bytes = 0

            def duration(self):
                return end
        r = random.Random(5)
        speech = [(s + r.gauss(0, 0.08), s + d) for s, d in truth]
        half = len(truth) // 2                      # the subtitle 9 s early, then 17 s: another cut
        lines = [{"s": s - (9.0 if i < half else 17.0), "e": s + d - (9.0 if i < half else 17.0), "text": "x",
                  "kind": "dialogue"} for i, (s, d) in enumerate(truth)]
        index = Index()
        fit = timeline_fit(speech, rate=audiofit.COST * rate, ends=True)(index, lines, vad=object(),
                                                                        better=AUDIO_BETTER, budget=budget)
        res = fit.run()
        self.assertEqual(fit.W, 15.0)
        self.assertLessEqual(index.audio_bytes, budget)
        self.assertEqual(res["decision"], "synced", res.get("why"))     # it was "audio budget reached (900 MB)"
        self.assertEqual(len(res["model"]["segments"]), 1)
        off = sum(not line.get("drop") and abs(line["s"] - s) > 1.0 for line, (s, _) in zip(res["lines"], truth))
        self.assertLessEqual(off, 20)               # the change placed within the windows read

if __name__ == "__main__":
    unittest.main()
