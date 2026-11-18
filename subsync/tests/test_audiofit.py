"""The audio fit's own pieces: windows the file's audio can't fill (past its end, none it can decode, decoded short),
the correlation of a window with only the stretch of the subtitle it can reach, the windows' places on any runtime, and
a subtitle signal no longer than the windows can read."""
import collections
import os
import random
import subprocess
import sys
import threading
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subsync import audiofit  # noqa: E402
from subsync.audiofit import DT, OMAX, AudioFit  # noqa: E402
from subsync.mp4 import NoAudioThere, NotMp4, open_index  # noqa: E402
from subsync.reader import RangeReader, ReadFailed  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from helpers import FIXTURES, RangeServer  # noqa: E402
from test_core import AUDIO_BETTER, rhythm  # noqa: E402
from test_matcher import truth_fit  # noqa: E402


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
        srv = RangeServer(FIXTURES)
        try:
            idx = open_index(RangeReader(srv.url("tiny.mp4"), "test"))
            timeout = subprocess.TimeoutExpired(["ffmpeg"], 300, output=b"\x01\x00" * 1000)
            with mock.patch("subsync.mp4.subprocess.run", side_effect=timeout):
                self.assertEqual(idx.window_pcm(20.0, 10.0), b"\x01\x00" * 1000)
                fit = AudioFit(idx, [{"s": 1.0, "e": 2.0, "text": "x", "kind": "dialogue"}], vad=LoudVad())
                with self.assertRaises(ReadFailed):                       # and a fit takes it as a short window
                    fit.read_window(20.0, 10.0)
        finally:
            srv.close()


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


if __name__ == "__main__":
    unittest.main()
