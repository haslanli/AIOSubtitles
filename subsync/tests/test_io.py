"""Range reads and the Matroska and MP4 indexes against a local range server; the pipeline on the tiny fixture; the
worker's choice among subtitles, its fast and shift paths, its read caps and first-read retries; the installer's
self-test."""
import array
import http.client
import io
import json
import os
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
from unittest import mock

from helpers import (back_to_back, BETTER, closed_after, FakeIndex, FIXTURES, have, pgs_left_out, pgs_track,
                     RangeServer, realistic, rules, run_once, srt_of, start_server, stop_server, TempDir, traced_memory,
                     translated, watch_the_log)
from subsync import imageref, mkv, mp4, pipeline, realdebrid, selftest, sources
from subsync.addon import App
from subsync.mkv import MkvIndex, NotMatroska, oshash, pipe_until
from subsync.mp4 import Mp4Index, open_index
from subsync.reader import (CapReached, CHUNK, IndexTooLarge, MAX_RE_RESOLVE, RangeReader, ReadFailed, Refused,
                            SIZE_TOLERANCE, Stopped)
from subsync.settings import Settings
from subsync.store import REFUSED_BACKOFF_S, Store
from subsync.subs import parse, to_srt, ts
from subsync.worker import Job, JobGuard, READ_RETRY_S, Safety, Turn, UsenetFinder, Worker

TINY = os.path.join(FIXTURES, "tiny.mkv")
# the alass program: SUBSYNC_ALASS, else alass on the path (as the image names it), else alass-cli (as Homebrew and
# `cargo install alass-cli` name it); the tests that run it are skipped when there is none
ALASS = os.environ.get("SUBSYNC_ALASS") or shutil.which("alass") or shutil.which("alass-cli") or "alass"
needs_alass = unittest.skipUnless(have(ALASS), "alass (or alass-cli) not found; set SUBSYNC_ALASS to its path")


def read_text(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


class Guard:
    def __init__(self, stop_after=None):
        self.calls, self.got, self.stop_after = 0, 0, stop_after

    def before_read(self, length):
        if self.stop_after is not None and self.calls >= self.stop_after:
            raise Stopped("test stop")
        self.calls += 1

    def after_read(self, n, asked=None):
        self.got += n


class ReplyRangeTest(unittest.TestCase):
    def test_a_number_a_reply_writes_is_read_in_ascii_digits_only(self):
        # A first byte, a total or a Content-Length longer than int converts (4,300 digits) raised ValueError
        # out of the read; such a number is none, as one written in other digits
        from subsync.reader import reply_range
        self.assertEqual(reply_range({"Content-Range": "bytes 5-9/100"}, 206), (5, 100))
        self.assertEqual(reply_range({"Content-Range": "bytes 0-9/*", "Content-Length": "100"}, 200), (0, 100))
        long = "1" * 5000
        for headers, status, got in (({"Content-Range": f"bytes 0-9/{long}"}, 206, (0, None)),
                                     ({"Content-Range": f"bytes {long}-9/100"}, 206, (None, 100)),
                                     ({"Content-Length": long}, 200, (None, None)),
                                     ({"Content-Range": "bytes 0-9/\u00b9\u00b2"}, 206, (0, None)),
                                     ({"Content-Length": "\u00b2"}, 200, (None, None))):
            with self.subTest(headers={k: v[:12] for k, v in headers.items()}):
                self.assertEqual(reply_range(headers, status), got)


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.srv = RangeServer(FIXTURES)

    def tearDown(self):
        self.srv.close()

    def test_bounded_read_closes_and_counts(self):
        g = Guard()
        r = RangeReader(self.srv.url("tiny.mkv"), "test", g)
        data = r.get(100, 1000)
        self.assertEqual(len(data), 1000)
        self.assertEqual(r.size, os.path.getsize(TINY))
        self.assertEqual(g.got, 1000)
        self.assertEqual(self.srv.headers[-1].get("Connection"), "close")
        self.assertEqual(self.srv.headers[-1].get("Range"), "bytes=100-1099")

    def test_403_is_refused(self):
        self.srv.fault = "403"
        with self.assertRaises(Refused):
            RangeReader(self.srv.url("tiny.mkv"), "test").get(0, 10)

    def test_ignored_range_reads_only_what_was_asked(self):
        self.srv.fault = "ignore-range"
        r = RangeReader(self.srv.url("tiny.mkv"), "test")
        self.assertEqual(len(r.get(0, 500)), 500)
        with self.assertRaises(ReadFailed):
            r.get(1000, 10)  # a server that ignores ranges can't serve an offset

    def test_size_change_fails(self):
        r = RangeReader(self.srv.url("tiny.mkv"), "test", size=os.path.getsize(TINY))
        self.srv.fault = "wrong-size"
        with self.assertRaises(ReadFailed):
            r.get(0, 10)

    def test_guard_stops_before_reading(self):
        r = RangeReader(self.srv.url("tiny.mkv"), "test", Guard(stop_after=0))
        with self.assertRaises(Stopped):
            r.get(0, 10)
        self.assertEqual(self.srv.log, [])

    def test_missing_file(self):
        with self.assertRaises(ReadFailed):
            RangeReader(self.srv.url("nope.mkv"), "test").get(0, 10)

    def test_an_error_status_is_a_refusal_or_a_failed_read_for_both_reads(self):
        # Stream (audio windows, the judging pipe) mapped an error status by its own copy, which no test
        # ran: 403 and 429 are refusals (the worker backs off), any other a failed read that is not one; each is one
        # request that received nothing, its reservation given back
        def answering(status):
            def opener(request, timeout):
                raise urllib.error.HTTPError(request.full_url, status, "X", {}, None)
            return opener
        for status, refusal in ((403, True), (429, True), (404, False), (500, False)):
            for name, read in ReplyCheckTest.reads():
                with self.subTest(status=status, read=name):
                    guard = mock.Mock()
                    r = RangeReader("http://127.0.0.1:9/f.mkv", "t", guard, opener=answering(status))
                    with self.assertRaises(ReadFailed) as caught:
                        read(r, 0, 10)
                    self.assertIs(isinstance(caught.exception, Refused), refusal)
                    self.assertEqual(str(caught.exception), f"HTTP {status}")
                    self.assertFalse(caught.exception.connection)
                    self.assertEqual((r.requests, r.bytes, r.finished), (1, 0, 0))
                    guard.after_read.assert_called_once_with(0, 10)
        for fault, name, kind in (("403", "tiny.mkv", Refused), (None, "nope.mkv", ReadFailed)):   # end to end
            self.srv.fault = fault
            for read_name, read in ReplyCheckTest.reads():
                with self.subTest(fault=fault, read=read_name):
                    with self.assertRaises(kind) as caught:
                        read(RangeReader(self.srv.url(name), "test"), 0, 10)
                    self.assertIs(isinstance(caught.exception, Refused), kind is Refused)

    def test_a_read_past_the_end_or_of_nothing_asks_for_nothing(self):
        # Stream's own early returns, which no test ran
        size = os.path.getsize(TINY)
        for name, read in ReplyCheckTest.reads():
            with self.subTest(read=name):
                g = Guard()
                r = RangeReader(self.srv.url("tiny.mkv"), "test", g, size=size)
                self.assertEqual(read(r, 100, 0), b"")
                with self.assertRaises(ReadFailed) as caught:
                    read(r, size, 10)
                self.assertEqual(str(caught.exception), "range past the end")
                self.assertEqual((g.calls, r.requests), (0, 0))
        self.assertEqual(self.srv.log, [])

    def test_a_streamed_read_is_one_request_and_counts_only_what_was_taken(self):
        g = Guard()
        r = RangeReader(self.srv.url("tiny.mkv"), "test", g)
        it = r.stream(100, 100000, chunk=4096)
        first = next(it) + next(it)
        it.close()                                            # the caller stops early: the connection is closed
        with open(TINY, "rb") as f:
            f.seek(100)
            self.assertEqual(first, f.read(8192))
        self.assertEqual((g.got, r.bytes, r.requests), (8192, 8192, 1))
        self.assertEqual(self.srv.headers[-1].get("Range"), "bytes=100-100099")
        self.assertEqual(self.srv.headers[-1].get("Connection"), "close")

    def test_a_streamed_read_to_the_end_returns_the_range(self):
        r = RangeReader(self.srv.url("tiny.mkv"), "test", size=os.path.getsize(TINY))
        data = b"".join(r.stream(1000, 50000, chunk=7000))
        with open(TINY, "rb") as f:
            f.seek(1000)
            self.assertEqual(data, f.read(50000))
        self.srv.fault = "wrong-size"
        with self.assertRaises(ReadFailed):
            b"".join(r.stream(0, 10))


class MkvTest(unittest.TestCase):
    def setUp(self):
        self.srv = RangeServer(FIXTURES)

    def tearDown(self):
        self.srv.close()

    def test_text_track_timing_from_cues_and_hash(self):
        r = RangeReader(self.srv.url("tiny.mkv"), "test")
        idx = MkvIndex(r).read_head().read_cues()
        tracks = idx.text_tracks()
        self.assertEqual(len(tracks), 1)
        t = tracks[0]
        self.assertEqual(t["codec"], "S_TEXT/UTF8")
        self.assertFalse(t["forced"])
        truth = parse(read_text("tiny.srt"))
        self.assertEqual(len(t["cues"]), len(truth))
        for (s, d), l in zip(t["cues"], truth):
            self.assertAlmostEqual(s, l["s"], places=2)
            self.assertAlmostEqual(d, l["e"] - l["s"], places=2)
        with open(TINY, "rb") as f:
            data = f.read()
        self.assertEqual(idx.read_hash(), oshash(len(data), data[:65536], data[-65536:]))
        self.assertTrue(89.0 <= idx.duration() <= 96.0)   # 90 s of video; the last subtitle ends near 94 s
        self.assertEqual(idx.audio_track_index(), 0)

    def test_window_byte_range_is_bounded(self):
        r = RangeReader(self.srv.url("tiny.mkv"), "test")
        idx = MkvIndex(r).read_head().read_cues()
        a, b = idx.byte_range(30.0, 10.0)
        self.assertTrue(0 < a < b <= idx.size)
        self.assertLess(b - a, idx.size)
        # From the cluster cued at or before the window's start less its margin (the decoder primes
        # there, -ss still starts the window on time; an earlier cluster would be read for nothing), on past the first
        # cluster cued after its end, and a fraction of the file, as an MP4 window's read
        self.assertEqual(a, max(p for t, p in idx.cue_points if t <= 30.0 - mkv.WINDOW_MARGIN_S))
        self.assertLess(min(p for t, p in idx.cue_points if t > 40.0), b)
        self.assertLess(b - a, idx.size // 3)

    @unittest.skipUnless(have("ffmpeg"), "ffmpeg not installed")
    def test_window_pcm_decodes(self):
        r = RangeReader(self.srv.url("tiny.mkv"), "test")
        idx = MkvIndex(r).read_head().read_cues()
        pcm = idx.window_pcm(30.0, 10.0, 16000)
        self.assertAlmostEqual(len(pcm) / 2 / 16000, 10.0, delta=0.1)


class PipeUntilTest(unittest.TestCase):
    """An audio window is fed to the decoder from one streamed read, and the read stops once the decoder's output holds
    the whole window (a Matroska window's byte range runs to the next seek point, which can be far past the window)."""

    def test_feeding_stops_once_the_output_is_complete(self):
        taken = []

        def chunks():                                        # a 2 MB range
            for i in range(200):
                taken.append(i)
                yield b"x" * 10000
        echo = [sys.executable, "-c", "import sys\nwhile True:\n b=sys.stdin.buffer.read1(4096)\n"
                                      " if not b: break\n sys.stdout.buffer.write(b); sys.stdout.buffer.flush()"]
        out = pipe_until(echo, b"head", chunks(), need=5000)
        self.assertGreaterEqual(len(out), 5000)
        # what was read past the need is a fixed amount (the pipe's 64 KB buffer and a chunk or two), not the range
        self.assertLess(len(taken), 40)

    def test_a_decoder_that_ends_by_itself_stops_the_feeding(self):
        taken = []

        def chunks():
            for i in range(200):
                taken.append(i)
                yield b"x" * 1000
        out = pipe_until([sys.executable, "-c", "import sys; sys.stdout.write('done')"], b"", chunks(), need=10 ** 9)
        self.assertEqual(out, b"done")
        self.assertLess(len(taken), 200)


class Mp4Test(unittest.TestCase):
    """tiny.mp4 / tiny-fast.mp4: tiny.mkv remuxed (same video, audio and lines; the text track as mov_text), with the
    movie box at the end and at the start; tiny-nosub.mp4 has no subtitle track."""

    def setUp(self):
        self.srv = RangeServer(FIXTURES)
        self.want = [(l["s"], l["e"] - l["s"]) for l in parse(read_text("tiny.srt"))]

    def tearDown(self):
        self.srv.close()

    def index(self, name):
        return open_index(RangeReader(self.srv.url(name), "test"))

    def test_the_text_tracks_lines_come_from_the_movie_box_wherever_it_is(self):
        for name in ("tiny.mp4", "tiny-fast.mp4"):
            idx = self.index(name)
            self.assertIsInstance(idx, Mp4Index)
            tracks = idx.text_tracks()
            self.assertEqual(len(tracks), 1, name)
            self.assertEqual((tracks[0]["language"], tracks[0]["forced"]), ("eng", False))
            cues = tracks[0]["cues"]
            self.assertEqual(len(cues), len(self.want), name)
            for (s, d), (ws, wd) in zip(cues, self.want):
                self.assertAlmostEqual(s, ws, delta=0.002)
                self.assertAlmostEqual(d, wd, delta=0.002)
            self.assertEqual(idx.image_tracks(), [])
            self.assertAlmostEqual(idx.duration(), 90.0, delta=5.0)

    def test_the_index_is_chosen_by_the_files_first_bytes(self):
        self.assertIsInstance(self.index("tiny.mkv"), MkvIndex)
        self.assertEqual(len(self.index("tiny.mkv").text_tracks()[0]["cues"]), len(self.want))
        with self.assertRaises(NotMatroska):
            self.index("tiny.srt")

    def test_a_file_without_a_subtitle_track_has_no_text_tracks(self):
        self.assertEqual(self.index("tiny-nosub.mp4").text_tracks(), [])

    def test_the_hash_is_the_opensubtitles_hash(self):
        path = os.path.join(FIXTURES, "tiny.mp4")
        with open(path, "rb") as f:
            data = f.read()
        self.assertEqual(self.index("tiny.mp4").read_hash(), oshash(len(data), data[:65536], data[-65536:]))

    @unittest.skipUnless(have("ffmpeg"), "ffmpeg not installed")
    def test_an_audio_window_is_a_bounded_read_and_starts_where_the_lines_start(self):
        for name in ("tiny.mp4", "tiny-nosub.mp4"):
            idx = self.index(name)
            before = idx.r.bytes
            idx.window_pcm(20.0, 10.0, 16000)
            self.assertLess(idx.r.bytes - before, idx.size // 3)   # what the window's read takes, its margins included
            pcm = idx.window_pcm(20.0, 40.0, 16000)
            self.assertAlmostEqual(len(pcm) / 2 / 16000, 40.0, delta=0.1)
            x = array.array("h", pcm[:len(pcm) // 2 * 2])
            onsets, before = [], False          # the fixture's tone is on during each line
            for i in range(0, len(x) - 160, 160):
                loud = sum(abs(v) for v in x[i:i + 160]) / 160 > 1000
                if loud and not before:
                    onsets.append(20.0 + i / 16000)
                before = loud
            self.assertGreater(len(onsets), 8, name)
            errors = [min((o - s for s, _ in self.want), key=abs) for o in onsets]
            self.assertLess(abs(statistics.median(errors)), 0.04, name)   # the edit list (encoder delay) is applied


class ListedSizeTest(unittest.TestCase):
    """A listing rounds a file's size (0.01 GiB: up to 5.4 MB off); the real size is taken from the first reply."""

    def setUp(self):
        self.srv = RangeServer(FIXTURES)
        self.real = os.path.getsize(TINY)

    def tearDown(self):
        self.srv.close()

    def test_a_listed_size_within_the_rounding_slack_is_accepted_and_replaced_by_the_real_one(self):
        r = RangeReader(self.srv.url("tiny.mkv"), "test", size=self.real + 100, size_tolerance=0.0005, size_slack=200)
        r.get(0, 64)
        self.assertEqual(r.size, self.real)

    def test_a_listed_size_outside_the_slack_is_still_refused(self):
        r = RangeReader(self.srv.url("tiny.mkv"), "test", size=self.real + 100, size_tolerance=0.0005, size_slack=50)
        with self.assertRaises(ReadFailed) as e:
            r.get(0, 64)
        self.assertEqual(str(e.exception), "size differs")

    def test_without_slack_the_old_tolerance_applies(self):
        r = RangeReader(self.srv.url("tiny.mkv"), "test", size=self.real + 100, size_tolerance=0.0005)
        with self.assertRaises(ReadFailed):
            r.get(0, 64)


@needs_alass
class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.srv = RangeServer(FIXTURES)
        self.idx = MkvIndex(RangeReader(self.srv.url("tiny.mkv"), "test")).read_head().read_cues()

    def tearDown(self):
        self.srv.close()

    def run_one(self, name):
        text = read_text(name)
        return pipeline.sync_subtitle(self.idx, text, "eng", rules(), alass=ALASS, audio=False)

    def test_correct_subtitle_is_in_sync_and_not_republished(self):
        r = self.run_one("tiny.srt")
        self.assertEqual(r["outcome"], "in_sync")
        self.assertNotIn("body", r)

    def test_webvtt_and_a_malformed_index_line_still_reach_alass(self):
        # alass refuses both; the pipeline hands it a plain SRT of the parsed timings
        src = read_text("tiny-offset.srt").replace("\r\n", "\n")
        vtt = "WEBVTT\n\n" + "\n".join(l.replace(",", ".") if "-->" in l else l for l in src.split("\n")
                                         if not l.strip().isdigit())
        bad = "0\n00:00:00 --> 00:00:05\nAd line with no milliseconds\n\n" + src
        for text in (vtt, bad):
            r = pipeline.sync_subtitle(self.idx, text, "eng", rules(), alass=ALASS, audio=False)
            self.assertNotIn("alass_error", r)
            self.assertEqual(r["outcome"], "synced")

    def test_alass_own_words_name_its_failure(self):
        # alass prints its errors on its normal output, which was thrown away: a failure said "alass exit 1"
        from subsync import alass
        with self.assertRaises(alass.AlassFailed) as e:
            alass.run("not a subtitle\n", read_text("tiny.srt"), alass=ALASS)
        self.assertRegex(str(e.exception), r"^alass exit 1: \S.{0,199}$")

    def styled_offset(self):
        """tiny-offset.srt in another author's timing: lines up to 280 ms earlier or later, a fixed pattern."""
        pattern = (0, 120, -150, 260, -90, 30, -280, 180, 60, -210)
        lines, out, n = parse(read_text("tiny-offset.srt")), [], 0

        for i, l in enumerate(lines):
            d = pattern[i % len(pattern)] / 1000
            n += 1
            out.append("%d\n%s --> %s\n%s\n" % (n, ts(l["s"] + d), ts(l["e"] + d), l["text"]))
        return "\n".join(out)

    def test_a_copy_clearly_better_than_the_original_is_offered_only_with_the_rule_on(self):
        text = self.styled_offset()
        kw = dict(alass=ALASS, audio=False)
        off = pipeline.sync_subtitle(self.idx, text, "eng", rules(snap=False), **kw)
        on = pipeline.sync_subtitle(self.idx, text, "eng", rules(snap=False, better={}), **kw)
        self.assertFalse(off["gate_synced"]["pass"])
        self.assertEqual(off["outcome"], "keep_original")
        self.assertNotIn("better", off)
        self.assertEqual(on["outcome"], "synced", on.get("better"))
        self.assertEqual(on["reason"], "clearly better than the original")
        self.assertEqual(len(parse(on["body"])), len(parse(text)))

    def test_an_offset_subtitle_is_synced_against_an_mp4s_text_track(self):
        idx = open_index(RangeReader(self.srv.url("tiny.mp4"), "test"))
        r = pipeline.sync_subtitle(idx, read_text("tiny-offset.srt"), "eng", rules(), alass=ALASS, audio=False)
        self.assertEqual((r["route"], r["outcome"]), ("cues", "synced"))
        self.assertEqual(r["reference"]["codec"], "mp4/tx3g")

    def test_offset_subtitle_is_synced_and_passes(self):
        r = self.run_one("tiny-offset.srt")
        self.assertEqual(r["route"], "cues")
        self.assertFalse(r["gate_original"]["pass"])
        self.assertEqual(r["outcome"], "synced")
        self.assertTrue(r["gate_synced"]["pass"])
        out = parse(r["body"])
        truth = parse(read_text("tiny.srt"))
        self.assertLess(max(abs(a["s"] - b["s"]) for a, b in zip(out, truth)), 0.1)


# -- candidate order, image refs ----------------------------------------------------------------------------------
@needs_alass
class ImageRoutePipelineTest(unittest.TestCase):
    def setUp(self):
        self.ref = realistic(300, 7)
        self.idx = FakeIndex(image=[pgs_track(self.ref)])

    def run_one(self, text, image_ref=True):
        return pipeline.sync_subtitle(self.idx, text, "eng", rules(image_ref=image_ref), alass=ALASS, audio=False)

    def test_offset_subtitle_synced_against_an_image_track(self):
        r = self.run_one(srt_of(self.ref, shift=2.7, jitter=0.05))
        self.assertEqual(r["route"], "image")
        self.assertEqual(r["outcome"], "synced")
        out = parse(r["body"])
        err = sorted(abs(o["s"] - s) for o, (s, _) in zip(out, self.ref))
        self.assertLess(err[len(err) // 2], 0.1)

    def test_wrong_episode_is_kept_original(self):
        r = self.run_one(srt_of(realistic(300, 8)))
        self.assertEqual(r["route"], "image")
        self.assertEqual(r["outcome"], "keep_original")

    def test_off_by_default_falls_back_as_before(self):
        r = self.run_one(srt_of(self.ref, shift=2.7), image_ref=False)
        self.assertIsNone(r["route"])
        self.assertEqual(r["outcome"], "keep_original")


class ImageTranslationTest(unittest.TestCase):
    """Kingdom of Heaven's director's cut has only image tracks; its Russian director's-cut subtitles were
    all "below the bar" because the gate expected every line to pair, while the track pins only its sure starts. With the
    gate scaled to them, a translation for this cut passes; one for another cut stays refused by its missing scene."""

    def setUp(self):
        self.ref = back_to_back(1100, 31)
        self.idx = FakeIndex(image=[pgs_left_out(self.ref)])

    def run_one(self, text):
        return pipeline.sync_subtitle(self.idx, text, "rus", rules(image_ref=True, better=BETTER), alass=ALASS,
                                      audio=False)

    def test_the_track_pins_only_its_sure_starts(self):
        share = imageref.reference(self.idx.image_tracks()[0])["stats"]["sure_share"]
        self.assertLess(share, 0.85)

    @needs_alass
    def test_a_translation_for_this_cut_passes(self):
        for seed in (4, 5, 6):
            r = self.run_one(translated(self.ref, seed, shift=3.4))
            self.assertEqual((r["route"], r["outcome"]), ("image", "synced"), (seed, r.get("gate_synced")))
            self.assertIn("aligned to another language", r["reason"])   # Russian against the English image track

    @needs_alass
    def test_a_translation_for_another_cut_is_kept_original(self):
        mid = self.ref[len(self.ref) // 2][0]
        r = self.run_one(translated(self.ref, 4, shift=3.4, cut=(mid, mid + 420.0)))
        self.assertEqual((r["route"], r["outcome"]), ("image", "keep_original"))
        self.assertTrue(r["gate_synced"]["missing_scene"])


class WorkerFixture(unittest.TestCase):
    """A file whose English text track has 200 lines, and three English candidates in the viewer's order: one with
    broken timing, one 2.7 s off, one in sync, made for the file's release. No tests of its own: each class below has
    its own, so none runs again under another class's settings or skips."""

    def setUp(self):
        watch_the_log(self)
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.ref = realistic(200, 3)
        files = {"broken.srt": to_srt([{"s": 0.02 + i * 0.001, "e": 0.03 + i * 0.001, "text": "x", "kind": "dialogue"}
                                       for i in range(40)]),
                 "offset.srt": srt_of(self.ref, shift=2.7, jitter=0.05),
                 "insync.srt": srt_of(self.ref)}
        for name, text in files.items():
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(text)
        self.srv = RangeServer(self.dir)
        self.cands = [{"id": "os-broken", "url": self.srv.url("broken.srt"), "lang": "eng", "release": "Movie HDTV"},
                      {"id": "os-offset", "url": self.srv.url("offset.srt"), "lang": "eng", "release": "Movie.DVDRip"},
                      {"id": "os-insync", "url": self.srv.url("insync.srt"), "lang": "eng",
                       "release": "Movie (2001) BluRay REMUX [GroupX]"}]
        self.idx = FakeIndex(text=[{"number": 3, "language": "eng", "codec": "S_TEXT/UTF8", "name": "", "forced": False,
                                    "cues": self.ref}])

    def tearDown(self):
        self.srv.close()
        shutil.rmtree(self.dir)

    def worker(self, **env):
        store = closed_after(self, Store(os.path.join(self.dir, "store")))
        # the switches that choose how the candidates are tried and synced, each stated (the shift path at its default),
        # so each test turns on or off only what it tests
        base = {"SUBSYNC_ALASS": ALASS, "SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_MAX_TRIES": "3",
                "SUBSYNC_RANK_BY_RELEASE": "off", "SUBSYNC_LIST_IN_SYNC": "off", "SUBSYNC_FAST_PATH": "off",
                "SUBSYNC_SHIFT_PATH": "on", "SUBSYNC_PARALLEL_LANGUAGES": "off", "SUBSYNC_IMAGE_REF": "off",
                "SUBSYNC_BEST_FIT": "off"}
        s = Settings(dict(base, **env))
        return Worker(s, store, None, None), store

    def sync(self, w, filename="[GroupX] Movie.mkv"):
        return w._sync_language(Job("movie", "tt1"), self.idx, "f-1", "eng", self.cands, "abcd", filename)


@needs_alass
class WorkerChoiceTest(WorkerFixture):
    """The order subtitles are tried in, and an in-sync original further down listed unchanged."""

    def test_viewer_order_stops_at_the_first_pass(self):
        w, store = self.worker()
        key = self.sync(w)
        r = store.get(key)
        self.assertEqual(r["candidate_id"], "os-offset")      # the broken one is rejected early, the next is synced
        self.assertEqual(store.get(key)["outcome"], "synced")
        reason = store.db.execute("SELECT reason FROM results WHERE candidate_id='os-broken'").fetchone()[0]
        self.assertIn("broken timing", reason)

    def test_release_match_first_and_in_sync_listed_unchanged(self):
        w, store = self.worker(SUBSYNC_RANK_BY_RELEASE="on", SUBSYNC_LIST_IN_SYNC="on")
        key = self.sync(w)
        r = store.get(key)
        self.assertEqual(r["candidate_id"], "os-insync")      # tried second: it names the file's group
        self.assertEqual(r["rank"], 2)                        # the store keeps the viewer's order
        self.assertTrue(r["detail"].get("unchanged"))
        self.assertEqual(store.body(key), srt_of(self.ref))   # listed as it is
        tried = [x[0] for x in store.db.execute("SELECT candidate_id FROM results ORDER BY created")]
        self.assertEqual(tried, ["os-broken", "os-insync"])

    def test_in_sync_further_down_lists_nothing_by_default(self):
        w, store = self.worker(SUBSYNC_RANK_BY_RELEASE="on")
        self.assertIsNone(self.sync(w))

    def test_switching_list_in_sync_on_lists_a_stored_in_sync_result(self):
        w, store = self.worker(SUBSYNC_RANK_BY_RELEASE="on")
        self.assertIsNone(self.sync(w))                       # stored as in_sync at the viewer's #3, nothing listed
        w2, store2 = self.worker(SUBSYNC_RANK_BY_RELEASE="on", SUBSYNC_LIST_IN_SYNC="on")
        key = self.sync(w2)                                   # same store: the cached result is listed now
        r = store2.get(key)
        self.assertEqual((r["candidate_id"], r["outcome"]), ("os-insync", "synced"))
        self.assertTrue(r["detail"].get("unchanged"))


@needs_alass
class BestFitTest(WorkerFixture):
    """Every candidate is judged and the best fit is offered first, not the first that passes; an in-sync
    original (a subtitle made for this release) beats any re-timed copy."""

    def test_an_in_sync_original_further_down_beats_a_re_timed_copy(self):
        w, store = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_LIST_IN_SYNC="on", SUBSYNC_FAST_PATH="on")
        key = self.sync(w)
        self.assertEqual(store.get(key)["candidate_id"], "os-insync")
        self.assertTrue(store.get(key)["detail"].get("unchanged"))
        self.assertEqual(store.last_good("f-1", "eng")["key"], key)

    def test_the_closer_of_two_re_timed_copies_comes_first(self):
        loose = os.path.join(self.dir, "loose.srt")
        with open(loose, "w", encoding="utf-8") as f:
            f.write(srt_of(self.ref, shift=-4.1, jitter=0.11, seed=4))
        self.cands = [self.cands[1], {"id": "os-loose", "url": self.srv.url("loose.srt"), "lang": "eng", "release": "X"}]
        self.cands = [self.cands[1], self.cands[0]]               # the looser one first in the viewer's order
        w, store = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_SNAP="off")
        key = self.sync(w)
        self.assertEqual(store.get(key)["candidate_id"], "os-offset")
        offered = [r["candidate_id"] for r in store.offers("f-1", ["eng"], {"eng": 3})]
        self.assertEqual(offered[0], "os-offset")

    def test_each_better_pass_is_published_before_the_rest_are_judged(self):
        # the offset copy passes first, the in-sync original (a better fit) later: both are published as they come,
        # so a pick waiting for the language is served the best so far
        w, store = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_LIST_IN_SYNC="on")
        seen = []

        def judged(candidate):
            return store.db.execute("SELECT COUNT(*) FROM results WHERE candidate_id=?", (candidate,)).fetchone()[0]
        # each publish, with whether the in-sync original had been judged by then
        w._publish = lambda alias, lang, key, final=False: seen.append((key, final, judged("os-insync")))
        best = self.sync(w)
        self.assertEqual([store.get(k)["candidate_id"] for k, _, _ in seen], ["os-offset", "os-insync"])
        self.assertEqual([at for _, _, at in seen], [0, 1])       # the offset copy before the original was judged
        self.assertEqual(seen[-1][:2], (best, False))             # the final publish is the job's, after every language

    def test_a_stored_pass_counts_as_the_best_so_far(self):
        # review: on a second job the in-sync original is a stored result; it is published as the best so far, so a pick
        # is not served a re-timed copy while the rest are judged
        env = {"SUBSYNC_BEST_FIT": "on", "SUBSYNC_LIST_IN_SYNC": "on", "SUBSYNC_FAST_PATH": "on"}
        w, store = self.worker(**env)
        self.sync(w)
        store.db.execute("DELETE FROM results WHERE candidate_id='os-offset'")   # judged again; the in-sync one is stored
        w2, _ = self.worker(**env)
        seen = []
        w2._publish = lambda alias, lang, key, final=False: seen.append(key)
        self.sync(w2)
        # downloads are judged as they end, so the order varies; the stored pass is published and stays the best so far
        self.assertIn("os-insync", [store.get(k)["candidate_id"] for k in seen])
        self.assertEqual(store.get(seen[-1])["candidate_id"], "os-insync")

@needs_alass
class FrameRateTest(unittest.TestCase):
    """A subtitle timed for 25 fps on a 23.976 fps release is corrected on the text route."""

    def test_a_pal_subtitle_is_synced(self):
        ref = realistic(300, 9)
        idx = FakeIndex(text=[{"number": 3, "language": "eng", "codec": "S_TEXT/UTF8", "name": "", "forced": False,
                               "cues": ref}])
        k = 23.976 / 25
        pal = to_srt([{"s": s * k + 1.5, "e": (s + d) * k + 1.5, "text": "line", "kind": "dialogue"} for s, d in ref])
        r = pipeline.sync_subtitle(idx, pal, "eng", rules(better={}), alass=ALASS, audio=False)
        self.assertEqual(r["outcome"], "synced", (r.get("reason"), r.get("gate_synced")))
        out = parse(r["body"])
        err = sorted(abs(o["s"] - s) for o, (s, _) in zip(out, ref))
        self.assertLess(err[len(err) // 2], 0.1)


# -- fast path, languages in parallel, read caps ---------------------------------------------------------------------
class FastPathTest(WorkerFixture):
    """With SUBSYNC_FAST_PATH on, every try is downloaded and its original checked first (no alass)."""

    def test_in_sync_original_listed_without_alass(self):
        # alass is not even installed for this worker: the fast path never runs it when an original is in sync
        w, store = self.worker(SUBSYNC_FAST_PATH="on", SUBSYNC_LIST_IN_SYNC="on", SUBSYNC_ALASS="/nonexistent/alass")
        key = self.sync(w)
        r = store.get(key)
        self.assertEqual((r["candidate_id"], r["outcome"], r["rank"]), ("os-insync", "synced", 2))
        self.assertTrue(r["detail"].get("fast"))
        tried = [x[0] for x in store.db.execute("SELECT candidate_id FROM results")]
        self.assertEqual(tried, ["os-insync"])                  # nothing else was re-timed or stored

    def test_viewer_first_in_sync_lists_nothing(self):
        self.cands[0], self.cands[2] = self.cands[2], self.cands[0]   # the in-sync one is the viewer's first now
        w, store = self.worker(SUBSYNC_FAST_PATH="on", SUBSYNC_LIST_IN_SYNC="on", SUBSYNC_ALASS="/nonexistent/alass")
        self.assertIsNone(self.sync(w))
        self.assertEqual([x[0] for x in store.db.execute("SELECT outcome FROM results")], ["in_sync"])

    def test_none_in_sync_is_synced_by_one_shift(self):
        self.cands = self.cands[:2]                             # broken + offset: no original is in sync
        w, store = self.worker(SUBSYNC_FAST_PATH="on", SUBSYNC_LIST_IN_SYNC="on")
        key = self.sync(w)
        r = store.get(key)
        self.assertEqual((r["candidate_id"], r["outcome"]), ("os-offset", "synced"))
        self.assertFalse(r["detail"].get("unchanged"))
        self.assertTrue(r["detail"].get("shift_fit"))           # the shift path's, before any re-timing

    @needs_alass
    def test_none_in_sync_falls_back_to_re_timing(self):
        # With the shift path off the offset one is re-timed (alass); with it on, the shift alone synced it
        self.cands = self.cands[:2]                             # broken + offset: no original is in sync
        w, store = self.worker(SUBSYNC_FAST_PATH="on", SUBSYNC_LIST_IN_SYNC="on", SUBSYNC_SHIFT_PATH="off")
        with mock.patch.object(pipeline, "sync_subtitle", wraps=pipeline.sync_subtitle) as re_timing:
            key = self.sync(w)
        r = store.get(key)
        self.assertEqual((r["candidate_id"], r["outcome"]), ("os-offset", "synced"))
        self.assertFalse(r["detail"].get("unchanged"))
        self.assertFalse(r["detail"].get("shift_fit"))
        self.assertNotIn("one shift", r["reason"])
        self.assertTrue(re_timing.called)

    def test_no_reference_and_audio_route_off_stores_nothing(self):
        # no text or image track and the audio route off: nothing is stored, so turning the route on later syncs it
        w, store = self.worker(SUBSYNC_FAST_PATH="on", SUBSYNC_AUDIO_ROUTE="off")
        self.idx = FakeIndex()
        self.cands = [self.cands[1]]
        self.assertIsNone(w._sync_language(Job("movie", "tt1"), self.idx, "f-1", "eng", self.cands, "abcd", None))
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)


class MagnetTest(WorkerFixture):
    """An original in sync as it is still starts its lines a little off the release's own (Patriot S01E01's English:
    43 ms early throughout; Burning: an author's stray line). Like AutoSync's "magnet", each line is snapped to the release
    line it clearly belongs to, and that copy is listed in place of the unchanged original."""

    def early(self):
        return srt_of(self.ref, shift=-0.043)

    def assert_on_the_release_lines(self, body):
        starts = [l["s"] for l in parse(body)]
        self.assertEqual(len(starts), len(self.ref))
        self.assertLess(max(abs(s - r) for s, (r, _) in zip(starts, self.ref)), 0.002)

    def test_a_steady_small_offset_is_snapped_away(self):
        chk = pipeline.check_original(self.idx, self.early(), "eng", rules())
        self.assertTrue(chk["in_sync"])
        self.assert_on_the_release_lines(chk["body"])
        self.assertGreater(chk["magnet"]["snapped"], 0.9 * len(self.ref))

    def test_a_rip_of_the_release_track_is_left_as_it_is(self):
        chk = pipeline.check_original(self.idx, srt_of(self.ref), "eng", rules())
        self.assertTrue(chk["in_sync"])
        self.assertNotIn("body", chk)

    def test_without_the_snap_bar_nothing_is_snapped(self):
        chk = pipeline.check_original(self.idx, self.early(), "eng", rules(snap=False))
        self.assertTrue(chk["in_sync"])
        self.assertNotIn("body", chk)

    def test_the_re_timing_route_snaps_an_in_sync_original_too(self):
        r = pipeline.sync_subtitle(self.idx, self.early(), "eng", rules(), alass=ALASS, audio=False)
        self.assertEqual(r["outcome"], "in_sync")
        self.assert_on_the_release_lines(r["body"])

    def test_the_listed_copy_is_the_snapped_one(self):
        with open(os.path.join(self.dir, "early.srt"), "w", encoding="utf-8") as f:
            f.write(self.early())
        # with the broken one beside it, not the offset one: a one-shift copy of that ranks with the originals by its fit
        self.cands = [self.cands[0], {"id": "os-early", "url": self.srv.url("early.srt"), "lang": "eng", "release": "X"}]
        for fast in ("on", "off"):
            with self.subTest(fast_path=fast):
                w, store = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_LIST_IN_SYNC="on", SUBSYNC_FAST_PATH=fast)
                key = self.sync(w)
                r = store.get(key)
                self.assertEqual((r["candidate_id"], r["outcome"]), ("os-early", "synced"))
                self.assertIn("snapped to the release", r["reason"])
                self.assertIn("magnet", r["detail"])
                self.assert_on_the_release_lines(store.body(key))
                store.db.execute("DELETE FROM results")


class CrossLanguageTest(unittest.TestCase):
    """A subtitle checked against another language's track (the file has none in its language): two translations never
    start their lines at the same moments, so the main bar cannot hold (Breaking Bad S01E02's Russian against the English
    track: 245-267 ms median after alass, every one refused; Frieren's Russian: 90 ms, 81 %). An alignment that is sound is
    accepted and its lines are snapped onto the reference's starts, as AutoSync's magnet does."""

    def setUp(self):
        self.ref = realistic(600, 41)
        self.idx = FakeIndex(text=[{"number": 3, "language": "eng", "codec": "S_TEXT/UTF8", "name": "", "forced": False,
                                    "cues": self.ref}])

    def error_ms(self, body):
        refs = sorted(s for s, _ in self.ref)
        errs = []
        for l in parse(body):
            k = min(range(len(refs)), key=lambda j: abs(refs[j] - l["s"]))
            errs.append(abs(refs[k] - l["s"]) * 1000)
        return statistics.median(errs)

    def test_a_translation_in_sync_is_listed_snapped(self):
        text = translated(self.ref, 5, shift=0.0, sigma=0.15, resplit=0.1)
        chk = pipeline.check_original(self.idx, text, "rus", rules(better=BETTER))
        self.assertTrue(chk["in_sync"], chk["gate_original"])
        self.assertTrue(chk["cross"])                                  # recorded as judged against another language
        self.assertLess(self.error_ms(chk["body"]), self.error_ms(text))

    @needs_alass
    def test_an_offset_translation_is_aligned_and_snapped(self):
        text = translated(self.ref, 6, shift=3.4, sigma=0.25, resplit=0.1)
        r = pipeline.sync_subtitle(self.idx, text, "rus", rules(better=BETTER), alass=ALASS, audio=False)
        self.assertEqual(r["outcome"], "synced", (r.get("reason"), r.get("gate_synced")))
        self.assertIn("another language", r["reason"])
        self.assertLess(self.error_ms(r["body"]), 120)

    @needs_alass
    def test_a_loosely_timed_translation_is_aligned_too(self):
        # Breaking Bad S01E02's Russian: alass aligns it (86-94 % paired) but its timing style leaves ~250 ms median, and some
        # tenths of the runtime have under half their lines within 500 ms; every tenth still pairs
        text = translated(self.ref, 8, shift=2.2, sigma=0.32, resplit=0.15)
        r = pipeline.sync_subtitle(self.idx, text, "rus", rules(better=BETTER), alass=ALASS, audio=False)
        self.assertEqual(r["outcome"], "synced", (r.get("reason"), r.get("gate_synced"), r.get("aligned")))

    @needs_alass
    def test_another_films_translation_is_kept(self):
        text = translated(realistic(600, 77), 6, shift=3.4)
        r = pipeline.sync_subtitle(self.idx, text, "rus", rules(better=BETTER), alass=ALASS, audio=False)
        self.assertEqual(r["outcome"], "keep_original")

    @needs_alass
    def test_another_cuts_translation_is_kept(self):
        mid = self.ref[len(self.ref) // 2][0]
        text = translated(self.ref, 6, shift=3.4, sigma=0.25, resplit=0.1, cut=(mid, mid + 400))
        r = pipeline.sync_subtitle(self.idx, text, "rus", rules(better=BETTER), alass=ALASS, audio=False)
        self.assertEqual(r["outcome"], "keep_original")

    def test_without_the_rule_a_translation_is_judged_as_before(self):
        text = translated(self.ref, 5, shift=0.0, sigma=0.15, resplit=0.1)
        self.assertFalse(pipeline.check_original(self.idx, text, "rus", rules(snap=False))["in_sync"])

    def test_english_is_the_preferred_other_language(self):
        pt = {"number": 6, "language": "pt", "codec": "S_TEXT/ASS", "name": "", "forced": False, "cues": realistic(620, 9)}
        idx = FakeIndex(text=[pt, self.idx.text_tracks()[0]])
        self.assertEqual(pipeline.choose_reference(idx, 500, "rus")["number"], 3)
        self.assertEqual(pipeline.choose_reference(idx, 500, "rus", fallback=())["number"], 6)


class HasReferenceTest(unittest.TestCase):
    """A file with no text or image track a subtitle could be judged against hands over to the audio route at once."""

    def test_tracks(self):
        with TempDir() as d:
            w = Worker(Settings({"SUBSYNC_DATA": d, "SUBSYNC_IMAGE_REF": "on"}), closed_after(self, Store(d)), None,
                       None)
            self.assertFalse(w._has_reference(FakeIndex()))
            lines = realistic(300, 4)
            self.assertTrue(w._has_reference(FakeIndex(image=[pgs_track(lines)])))
            text = {"number": 3, "language": "eng", "codec": "S_TEXT/UTF8", "name": "", "forced": False,
                    "cues": [(s, d) for s, d in lines]}
            self.assertTrue(w._has_reference(FakeIndex(text=[text])))
            self.assertFalse(w._has_reference(FakeIndex(text=[dict(text, forced=True)])))


class StackedShareTest(unittest.TestCase):
    """A subtitle that shows two lines at once (another layer laid over the dialogue) is told from one whose lines only
    touch (an end a few frames after the next start)."""

    def lines(self, spans):
        return [{"s": s, "e": e, "text": "x", "kind": "dialogue"} for s, e in spans]

    def test_shares(self):
        plain = self.lines([(k * 3.0, k * 3.0 + 2.5) for k in range(100)])
        touching = self.lines([(k * 3.0, k * 3.0 + 3.1) for k in range(100)])     # 0.1 s over the next start
        layered = self.lines([(k * 3.0, k * 3.0 + 2.5) for k in range(100)] + [(k * 30.0 + 0.2, k * 30.0 + 2.5) for k in range(10)])
        self.assertEqual(pipeline.stacked_share(plain), 0.0)
        self.assertEqual(pipeline.stacked_share(touching), 0.0)
        self.assertAlmostEqual(pipeline.stacked_share(layered), 10 / 110)
        self.assertEqual(pipeline.stacked_share([]), 0.0)
        typo = self.lines([(k * 3.0, k * 3.0 + 2.5) for k in range(100)])
        typo[3]["e"] = 600.0                                                    # one end time minutes off
        self.assertEqual(pipeline.stacked_share(typo), 0.01)
        signs = self.lines([(1.0, 9.0)]) + [{"s": 2.0, "e": 3.0, "text": "sign", "kind": "sign"}]
        self.assertEqual(pipeline.stacked_share(signs), 0.0)                    # only dialogue lines count


class AnchoredGuessTest(unittest.TestCase):
    """A file with no subtitle track: before the audio route can check anything, another language's first download is
    the candidate that lines up best, shifted, with the main language's best guess (Primer's YTS MP4: its Russian was 9 s
    off where the English rip fits)."""

    def setUp(self):
        self.ref = realistic(500, 23)
        self.main = srt_of(self.ref)

    def test_the_translation_that_lines_up_is_shifted_onto_it(self):
        texts = [translated(realistic(500, 99), 1, shift=0.0), translated(self.ref, 2, shift=9.0, sigma=0.1)]
        got = pipeline.anchored_guess(self.main, texts)
        self.assertEqual(got["index"], 1)
        refs = [s for s, _ in self.ref]
        errs = sorted(min(abs(l["s"] - r) for r in refs) for l in parse(got["body"]))
        self.assertLess(errs[len(errs) // 2], 0.2)

    def test_nothing_that_lines_up_gives_nothing(self):
        self.assertIsNone(pipeline.anchored_guess(self.main, [translated(realistic(500, 98), 1, shift=0.0)]))
        self.assertIsNone(pipeline.anchored_guess("not a subtitle", [translated(self.ref, 2, shift=9.0)]))

    def test_a_near_tie_goes_to_the_share_higher_before_rounding(self):
        # 200 of 600 lines (0.33333) first, 400 of 1,201 (0.33306) second; both read 0.333 rounded, and the
        # second was taken when its share was compared with the first's rounded one
        texts = [srt_of(realistic(600, 31)), srt_of(realistic(1201, 32))]
        with mock.patch.object(pipeline.shift, "best_shift", side_effect=[(1.0, 0.0, 200), (1.0, 0.0, 400)]):
            got = pipeline.anchored_guess(self.main, texts)
        self.assertEqual((got["index"], got["share"]), (0, 0.333))


class RecentreTest(unittest.TestCase):
    """A copy offered without the per-line snap keeps any steady bias alass left (Kingdom of Heaven's Russian: -57 ms
    against its image track's starts, where AutoSync's was -33 ms): it is moved by its own median error."""

    def test_a_biased_copy_is_moved_onto_the_reference(self):
        ref = realistic(400, 13)
        ref_track = {"route": "cues", "cues": ref, "number": 3, "language": "eng", "codec": "S_TEXT/UTF8"}
        r = random.Random(2)
        lines = [{"s": s + 4.0, "e": s + d + 4.0, "text": "x", "kind": "dialogue"} for s, d in ref]
        new = [dict(l, s=l["s"] - 4.0 - 0.06 + r.gauss(0, 0.15), e=l["e"] - 4.0 - 0.06) for l in lines]
        gate, recall = pipeline.reference_gate(ref_track, len(lines))
        out = pipeline.judge_copy({"gate_original": gate([l["s"] for l in lines])}, to_srt(lines), lines, new, ref_track,
                                  gate, recall, rules(snap=False, better=BETTER))
        self.assertEqual(out["outcome"], "synced")
        self.assertLess(abs(out["gate_synced"]["signed_median_ms"]), 15)
        self.assertGreater(abs(out["recentred_ms"]), 40)

    def test_a_move_that_would_make_a_passing_copy_fail_is_not_made(self):
        # Most lines 60 ms late, 7 spread out 250 ms late, 6 of one tenth 170 ms early: it passes (93 % within
        # 200 ms); moved 60 ms earlier it pairs better (94 %, median 0) but the early tenth turns weak, and it fails
        ref = [(30.0 + 10 * k, 2.0) for k in range(100)]
        ref_track = {"route": "cues", "cues": ref, "number": 3, "language": "eng", "codec": "S_TEXT/UTF8"}
        late, early = {5, 18, 31, 57, 70, 83, 96}, set(range(40, 46))
        new = [{"s": s + off, "e": s + d + off, "text": "x", "kind": "dialogue"}
               for k, (s, d) in enumerate(ref) for off in [0.25 if k in late else -0.17 if k in early else 0.06]]
        gate, _ = pipeline.reference_gate(ref_track, len(new))
        as_fitted = pipeline.gate_lines(gate, new)
        self.assertTrue(as_fitted["pass"])
        moved = pipeline.gate_lines(gate, [dict(l, s=l["s"] - 0.06, e=l["e"] - 0.06) for l in new])
        self.assertFalse(moved["pass"])                                      # what the move would have made of it
        self.assertGreaterEqual(moved["within200"], as_fitted["within200"])
        kept, g, moved_ms = pipeline.recentre(new, gate)
        self.assertEqual((g["pass"], moved_ms), (True, 0.0))
        self.assertTrue(kept is new)


class StreamedJudgingTest(WorkerFixture):
    """With best fit, each download is judged as it arrives: Kingdom of Heaven's first passes (one shift, 0.1 s each) came
    18 s after the request because 20 downloads per language were waited for first, and the pick's 14 s ran out."""

    def test_a_pass_is_published_before_a_slow_download_ends(self):
        w, store = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_FAST_PATH="on", SUBSYNC_LIST_IN_SYNC="on")
        fetch = w._fetch_one
        slow = {"os-broken"}

        def fetch_one(job, lang, rank, cand):
            if cand["id"] in slow:
                time.sleep(1.5)
            return fetch(job, lang, rank, cand)
        w._fetch_one = fetch_one
        t0, seen = time.monotonic(), []
        w._publish = lambda alias, lang, key, final=False: seen.append(time.monotonic() - t0)
        self.sync(w)
        self.assertTrue(seen)
        self.assertLess(seen[0], 1.0)                               # the in-sync one, before the slow download ended

    def test_downloads_come_in_the_order_they_end(self):
        w, _ = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_FAST_PATH="on")
        w._fetch_one = lambda job, lang, rank, cand: (time.sleep(0.4 if rank == 0 else 0.0), (rank, cand, b"x"))[1]
        got = [rank for rank, _, _ in w._fetch_stream(Job("movie", "tt1"), "eng", list(enumerate(self.cands)))]
        self.assertEqual(sorted(got), [0, 1, 2])
        self.assertEqual(got[-1], 0)

    def test_closing_the_stream_early_does_not_wait_for_every_download(self):
        w, _ = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_FAST_PATH="on")
        w._fetch_one = lambda job, lang, rank, cand: (time.sleep(0.0 if rank == 0 else 0.6), (rank, cand, b"x"))[1]
        stream = w._fetch_stream(Job("movie", "tt1"), "eng", [(k, self.cands[0]) for k in range(12)])
        self.assertEqual(next(stream)[0], 0)
        t0 = time.monotonic()
        stream.close()                                  # judging raised: the 8 downloads not started are dropped
        self.assertLess(time.monotonic() - t0, 0.3)


class ShiftPathTest(WorkerFixture):
    """A candidate off by one steady offset is synced by that shift at once, without alass (Finding Nemo's first download was
    an original because alass judged one candidate at a time and the right ones came 26 s after the request)."""

    def test_the_shift_alone_syncs_it(self):
        r = pipeline.shift_sync(self.idx, srt_of(self.ref, shift=-1.43, jitter=0.05), "eng", rules())
        self.assertEqual(r["outcome"], "synced")
        self.assertIn("one shift", r["reason"])
        starts = [l["s"] for l in parse(r["body"])]
        self.assertLess(statistics.median(abs(a - b) for a, (b, _) in zip(starts, self.ref)), 0.06)

    def test_another_films_subtitle_is_not_shifted_into_place(self):
        r = pipeline.shift_sync(self.idx, srt_of(realistic(200, 99)), "eng", rules())
        self.assertEqual(r["outcome"], "keep_original")

    def test_another_cut_is_not_shifted_into_place(self):
        cut = [c for c in self.ref if not 500 <= c[0] <= 800]
        r = pipeline.shift_sync(self.idx, srt_of(cut, shift=-2.0), "eng", rules())
        self.assertNotEqual(r["outcome"], "synced")

    def test_the_worker_needs_no_alass_for_it(self):
        self.cands = [self.cands[1]]                                  # the offset one only
        w, store = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_FAST_PATH="on", SUBSYNC_ALASS="/nonexistent/alass")
        key = self.sync(w)
        r = store.get(key)
        self.assertEqual((r["candidate_id"], r["outcome"]), ("os-offset", "synced"))
        self.assertTrue(r["detail"].get("shift_fit"))

    @needs_alass
    def test_off_means_alass_as_before(self):
        self.cands = [self.cands[1]]
        w, store = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_FAST_PATH="on", SUBSYNC_SHIFT_PATH="off")
        r = store.get(self.sync(w))
        self.assertFalse(r["detail"].get("shift_fit"))


class OwnTrackTest(WorkerFixture):
    """A candidate with more than twice the lines of the file's own track finds no reference track, but the
    file has one: the candidate is kept, never sent to the audio route (Inception: 3,753 lines against 1,738; the audio
    route started and read 979 MB). Only a file with no usable text or image track needs the audio route."""

    def long_text(self):
        return srt_of(realistic(450, 5))                      # 2.25 times the 200 lines of the file's track

    def test_a_far_longer_candidate_is_kept_without_the_audio_route(self):
        r = pipeline.sync_subtitle(self.idx, self.long_text(), "eng", rules(), alass=ALASS, audio=True, vad=object())
        self.assertEqual((r["outcome"], r["route"]), ("keep_original", None))
        self.assertIn("more than twice the lines of the file's own track", r["reason"])

    def test_a_track_of_a_few_lines_is_not_the_files_own(self):
        # a signs-only track (30 lines) against a full subtitle: the file still needs the audio route
        idx = FakeIndex(text=[{"number": 3, "language": "eng", "codec": "S_TEXT/UTF8", "name": "Signs", "forced": False,
                               "cues": self.ref[:30]}])
        r = pipeline.sync_subtitle(idx, srt_of(self.ref), "eng", rules(), alass=ALASS, audio=False)
        self.assertIn("audio route is off", r["reason"])

    @needs_alass
    def test_the_file_is_not_marked_for_the_audio_route(self):
        with open(os.path.join(self.dir, "long.srt"), "w", encoding="utf-8") as f:
            f.write(self.long_text())
        self.cands = [{"id": "os-long", "url": self.srv.url("long.srt"), "lang": "eng", "release": "Movie"}, self.cands[1]]
        # the re-timing loop is what this checks: with the shift path the offset one passes before it is reached
        w, store = self.worker(SUBSYNC_AUDIO_ROUTE="on", SUBSYNC_FAST_PATH="on", SUBSYNC_SHIFT_PATH="off")
        key = self.sync(w)
        self.assertEqual(w.pickable, {})                      # no pick needed: the file has its own track
        self.assertEqual(store.get(key)["candidate_id"], "os-offset")
        reason = store.db.execute("SELECT reason FROM results WHERE candidate_id='os-long'").fetchone()[0]
        self.assertIn("more than twice the lines", reason)


class TinyFileFixture(unittest.TestCase):
    """tiny.mkv listed for a film, with an English subtitle 2.7 s off and a Russian one in sync (tiny.srt), served
    by a local range server. No tests of its own."""

    def setUp(self):
        watch_the_log(self)
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        for name in ("tiny.mkv", "tiny.srt", "tiny-offset.srt"):
            shutil.copy(os.path.join(FIXTURES, name), self.dir)
        self.srv = RangeServer(self.dir)
        srv = self.srv

        class Aios:
            def find_row(self, *a):
                return {"url": srv.url("tiny.mkv")}

            def subtitles(self, *a):
                return [{"id": "e1", "url": srv.url("tiny-offset.srt"), "lang": "eng", "release": ""},
                        {"id": "r1", "url": srv.url("tiny.srt"), "lang": "rus", "release": ""}]

        self.aios = Aios()

    def tearDown(self):
        self.srv.close()
        shutil.rmtree(self.dir)


class ParallelLanguagesTest(TinyFileFixture):
    """process() with both languages at once: each is published, results as without parallelism."""

    @needs_alass
    def test_both_languages_published(self):
        store = closed_after(self, Store(os.path.join(self.dir, "store")))
        s = Settings({"SUBSYNC_ALASS": ALASS, "SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_PARALLEL_LANGUAGES": "on",
                      "SUBSYNC_FAST_PATH": "on", "SUBSYNC_LANGUAGES": "eng,rus"})
        w = Worker(s, store, self.aios, Safety())
        job = Job("movie", "tt1", {"filename": "tiny.mkv"})
        w.process(job)
        self.assertEqual(set(w.done[job.alias]), {"eng", "rus"})
        eng = store.get(w.done[job.alias]["eng"])
        self.assertEqual(eng["outcome"], "synced")               # the offset English one, re-timed
        rus = store.get(w.done[job.alias]["rus"])                # the Russian original is in sync: with best fit
        self.assertTrue(rus["detail"].get("unchanged"))          # it is listed as it is, like AutoSync's track 1


class PlayedFileHashTest(TinyFileFixture):
    """A request that names the played file's hash (Stremio sends it) is checked against the file read; a
    listed row that turns out to be another file is not synced, so its copies never reach this file."""

    def run_job(self, video_hash):
        store = closed_after(self, Store(os.path.join(self.dir, "store")))
        s = Settings({"SUBSYNC_ALASS": ALASS, "SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_LANGUAGES": "eng,rus"})
        w = Worker(s, store, self.aios, Safety())
        job = Job("movie", "tt1", {"filename": "tiny.mkv", "videoHash": video_hash})
        w.process(job)
        return w, store

    def test_another_file_is_not_synced(self):
        w, store = self.run_job("0123456789abcdef")
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)
        self.assertTrue(w.recently_unlisted("name:tiny.mkv|None"))   # the listing has no such file: not looked for again

    def test_a_hash_alone_names_the_played_file_never_its_title(self):
        # A request with the file's hash but no size was filed under the title alone, and the file read for
        # it was linked to the title, which then matched every other release of it
        with open(TINY, "rb") as f:
            data = f.read()
        video_hash = oshash(len(data), data[:65536], data[-65536:])
        store = closed_after(self, Store(os.path.join(self.dir, "store")))
        w = Worker(Settings({"SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_LANGUAGES": "eng,rus"}), store, self.aios,
                   Safety())
        w._sync_all = lambda *a: None                       # the file's links are made before its subtitles are judged
        job = Job("movie", "tt1", {"videoHash": video_hash})
        w.process(job)
        aliases = {alias for (alias,) in store.db.execute("SELECT alias FROM aliases")}
        self.assertEqual(aliases, {f"hash:{video_hash}|None", f"hash:{video_hash}|{len(data)}"})

    @needs_alass
    def test_the_played_file_is_synced(self):
        with open(TINY, "rb") as f:
            data = f.read()
        w, store = self.run_job(oshash(len(data), data[:65536], data[-65536:]).upper())
        self.assertGreater(store.db.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)


class SafetyTest(unittest.TestCase):
    """The refusal back-off doubles with each refusal in a row (README: SUBSYNC_403_BACKOFF_S). A read that was
    already running when a refusal came, and ends during the pause with bytes, doesn't end the refusals in a row; a
    refusal during the pause (the other lane's job, a read running then) is the same one, not a second."""

    def setUp(self):
        from helpers import Clock
        self.clock = Clock()
        self.safety = Safety(backoff_s=900, clock=self.clock)

    def test_each_refusal_in_a_row_doubles_the_pause(self):
        s = self.safety
        self.assertEqual(s.refused(), 900)
        self.clock.t += 900                                 # the pause ended: a read is refused again at once
        self.assertEqual(s.refused(), 1800)
        self.clock.t += 1800
        s.read_ok()                                         # a read after the pause got bytes: the run ended
        self.assertEqual(s.refused(), 900)

    def test_a_read_running_when_the_refusal_came_doesnt_end_the_run(self):
        s = self.safety
        self.assertEqual(s.refused(), 900)
        self.clock.t += 1
        s.read_ok()                                         # it ends with bytes during the pause
        self.assertEqual(s.refusals, 1)
        self.clock.t += 899
        self.assertEqual(s.refused(), 1800)                 # the next refusal after the pause: twice as long

    def test_a_refusal_during_the_pause_is_the_same_one(self):
        s = self.safety
        self.assertEqual(s.refused(), 900)                  # the main lane's job
        self.clock.t += 1
        self.assertEqual(s.refused(), 899)                  # the audio lane's, refused by the same limit
        self.assertEqual((s.refusals, s.refused_until), (1, self.clock.t + 899))
        self.clock.t += 899
        self.assertEqual(s.refused(), 1800)


class GuardCapTest(unittest.TestCase):
    def test_a_job_stops_at_its_byte_cap(self):
        with tempfile.TemporaryDirectory() as d:
            store = closed_after(self, Store(d))
            JobGuard(Safety(), store, 10 ** 9, 1000).before_read(500)    # under the job's cap: the read goes on
            g = JobGuard(Safety(), store, 10 ** 9, 1000)
            g.after_read(900)
            with self.assertRaises(Stopped):
                g.before_read(500)                               # over the job's cap

    def test_only_the_jobs_own_cap_is_a_cap_reached(self):
        # A stop at the job's own byte cap says which cap; the day's budget is a plain stop
        self.assertTrue(issubclass(CapReached, Stopped))
        with tempfile.TemporaryDirectory() as d:
            store = closed_after(self, Store(d))
            g = JobGuard(Safety(), store, 10 ** 9, 1000)
            g.after_read(900)
            with self.assertRaises(CapReached) as caught:
                g.before_read(500)
            self.assertEqual((caught.exception.cap, str(caught.exception)), (1000, "file byte cap"))
            budget = JobGuard(Safety(), store, 1000, 10 ** 9)
            with self.assertRaises(Stopped) as caught:
                budget.before_read(1001)
            self.assertNotIsInstance(caught.exception, CapReached)


class GuardReservationTest(unittest.TestCase):
    """A job reads in parallel (audio windows, line reads), so each read's length is reserved until its
    after_read: reads checked at once never pass a limit together, and one that got less than it asked leaves the
    room it didn't use; the job stops where reading one at a time would have stopped."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = closed_after(self, Store(self.tmp.name))

    def meanwhile(self, call):
        """call() in another thread: (the thread, what it gave: "read" or the exception)."""
        out = []

        def run():
            try:
                call()
                out.append("read")
            except Exception as e:
                out.append(e)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(0.3)
        return t, out

    def test_two_reads_checked_at_once_never_pass_the_cap_together(self):
        g = JobGuard(Safety(), self.store, 10 ** 9, 100)
        g.before_read(80)                                   # a read running
        t, out = self.meanwhile(lambda: g.before_read(80))
        self.assertTrue(t.is_alive())                       # with the running read it could pass the cap: it waits
        g.after_read(80, 80)                                # the running read got all it asked for
        t.join(5)
        self.assertIsInstance(out[0], CapReached)
        self.assertEqual((g.bytes, g.reserved), (80, 0))

    def test_a_read_that_got_less_than_it_asked_leaves_room(self):
        g = JobGuard(Safety(), self.store, 10 ** 9, 100)
        g.before_read(80)
        t, out = self.meanwhile(lambda: g.before_read(80))
        g.after_read(10, 80)                                # a window's stream stopped once the window was decoded
        t.join(5)
        self.assertEqual(out, ["read"])
        self.assertEqual((g.bytes, g.reserved), (10, 80))
        g.after_read(0, 80)                                 # a read that failed gives its reservation back too
        self.assertEqual(g.reserved, 0)

    def test_the_days_budget_and_the_persons_bytes_count_the_running_reads(self):
        day = JobGuard(Safety(), self.store, 100, 10 ** 9)
        day.before_read(80)
        t, out = self.meanwhile(lambda: day.before_read(80))
        self.assertTrue(t.is_alive())
        day.after_read(80, 80)
        t.join(5)
        self.assertIsInstance(out[0], Stopped)         # the day's budget, as one read after the other
        person = CountedLimits()
        asked = []
        person.before_read = asked.append
        g = JobGuard(Safety(), self.store, 10 ** 9, limits=person)
        g.before_read(30)
        g.before_read(20)
        self.assertEqual(asked, [30, 50])                   # the person's limit sees this job's running reads

    def test_the_reader_gives_back_what_a_failed_or_stopped_read_reserved(self):
        srv = RangeServer(FIXTURES)
        self.addCleanup(srv.close)
        g = JobGuard(Safety(), self.store, 10 ** 9, 10 ** 9)

        def no_answer(request, timeout):
            raise TimeoutError("timed out")
        for stream in (False, True):
            r = RangeReader(srv.url("tiny.mkv"), "t", g, opener=no_answer)
            with self.assertRaises(ReadFailed):
                b"".join(r.stream(0, 1000)) if stream else r.get(0, 1000)
            self.assertEqual(g.reserved, 0)
        r = RangeReader(srv.url("tiny.mkv"), "t", g)
        chunks = r.stream(0, 4096, chunk=1024)
        next(chunks)
        chunks.close()                                      # the caller stopped early
        self.assertEqual((g.reserved, g.bytes), (0, 1024))


class CountedLimits:
    """A person's limits that allow everything and count the files started."""
    who = rows_key = None

    def __init__(self):
        self.started = 0

    def may_queue(self, queued):
        return True

    def may_start_file(self):
        return True

    def file_started(self):
        self.started += 1

    def before_read(self, length):
        pass

    def has_room(self, n):
        return True

    def after_read(self, n):
        pass

    def may_start_audio(self, opened=False):
        return True

    def audio_started(self, opened=False):
        pass


class RunOnceTest(unittest.TestCase):
    """A job that can't run makes run_once fail, at once when it isn't queued and after `within` seconds when
    it never comes due; the loop used to wait for it for ever."""

    def worker(self, limits):
        d = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, d)
        return Worker(Settings({"SUBSYNC_DATA": d}), closed_after(self, Store(d)), None, Safety(), limits=limits)

    def test_a_job_that_isnt_queued_fails_at_once(self):
        class Full(CountedLimits):
            def may_queue(self, queued):
                return False
        t0 = time.monotonic()
        with self.assertRaisesRegex(AssertionError, "wasn't queued"):
            run_once(self.worker(Full()), Job("movie", "tt1", {"filename": "tiny.mkv"}))
        self.assertLess(time.monotonic() - t0, 5.0)

    def test_a_job_that_never_comes_due_fails_after_its_time(self):
        w = self.worker(CountedLimits())
        later = Job("movie", "tt1", {"filename": "tiny.mkv"})
        later.not_before = time.time() + 3600
        audio = Job("movie", "tt2", {"filename": "other.mkv"})
        audio.audio = True                                   # the audio lane's, not the main lane's
        for job in (later, audio):
            with self.subTest(job=job.video_id):
                t0 = time.monotonic()
                with self.assertRaisesRegex(AssertionError, "didn't start within 0.2 s"):
                    run_once(w, job, within=0.2)
                self.assertLess(time.monotonic() - t0, 5.0)
                self.assertFalse(w.stop_flag)                # the worker left as it was, for the next run


class CapReachedTest(unittest.TestCase):
    """A file whose index is larger than the cap stops there for good under that cap: the job isn't
    queued again, a new request reads nothing of the file and counts no file, its subtitle list and originals are still
    set, and no "(Subsync)" entry is listed for it; a raised cap reads it again. Other stops are tried again as before."""
    CAP_MB = str(200_000 / 2 ** 20)   # the start of tiny.mkv (188,024 bytes) fits; its index and hash reads don't

    def setUp(self):
        watch_the_log(self)
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        shutil.copy(TINY, self.dir)
        self.srv = RangeServer(self.dir)
        self.size = os.path.getsize(TINY)
        # the listing's size is the file's: its first read asks for the file's start only, as for a real video
        patcher = mock.patch.object(sources, "MIN_VIDEO_BYTES", 1)
        patcher.start()
        self.addCleanup(patcher.stop)
        srv = self.srv

        class Aios:
            lists = 0

            def find_row(self, *a):
                return {"url": srv.url("tiny.mkv")}

            def subtitles(self, *a):
                self.lists += 1
                return [{"id": "e1", "url": srv.url("tiny.srt"), "lang": "eng", "release": ""}]
        self.aios = Aios()
        self.store = closed_after(self, Store(os.path.join(self.dir, "store")))
        self.job = Job("movie", "tt1", {"filename": "tiny.mkv", "videoSize": self.size})

    def tearDown(self):
        self.srv.close()
        shutil.rmtree(self.dir)

    def worker(self, **env):
        s = Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC_BASE": "http://subsync:8790",
                           "SUBSYNC_LANGUAGES": "eng", "SUBSYNC_INDEX_CAP_MB": self.CAP_MB}, **env))
        w = Worker(s, self.store, self.aios, Safety(), limits=CountedLimits())
        w._sync_all = lambda *a: None     # the index read is what is tested here, not the sync after it
        return w

    def test_an_index_over_the_cap_is_not_queued_again_and_not_read_again(self):
        w = self.worker()
        with self.assertLogs("subsync.worker", "INFO") as logs:
            run_once(w, self.job)
        read = list(self.srv.log)
        self.assertEqual(read[0], (0, self.size))                # the start was read, then the index stopped at the cap
        self.assertFalse(w.busy_with(self.job.alias))            # not queued again
        self.assertEqual(sum("index larger than the cap" in line for line in logs.output), 1)
        self.assertEqual(self.store.over_cap(self.job.alias), 200_000)
        self.assertEqual(w.limits.started, 1)
        # the next request: no "(Subsync)" entry but its "(original)" one, from the name-ranked guess, and the
        # job it queues reads nothing and counts no file
        app = App(w.s, self.store, w, Safety())
        listed = app.subtitles("movie", "tt1", f"filename=tiny.mkv&videoSize={self.size}")
        self.assertEqual([e for e in listed if str(e.get("id", "")).startswith("subsync-pick-")], [])
        self.assertEqual([(e["label"], e["variantOf"]) for e in listed], [("English (original)", "e1")])
        self.assertTrue(w.busy_with(self.job.alias))             # queued: its subtitle list is asked for again
        with w.cv:
            w.originals.clear()
        lists = self.aios.lists
        run_once(w, Job("movie", "tt1", dict(self.job.hints)))
        self.assertEqual(self.srv.log, read)                     # nothing more read
        self.assertEqual(w.limits.started, 1)                    # no new file counted
        self.assertEqual(self.aios.lists, lists + 1)
        self.assertEqual(w.originals[self.job.alias]["eng"]["id"], "e1")   # its original is still served
        self.assertFalse(w.busy_with(self.job.alias))

    def test_the_same_torrent_file_under_another_name_is_not_read_again(self):
        # found in review: the mark was kept for the asked name only, so the same file of the same torrent
        # asked under another name was read again up to the cap; the torrent's alias is marked too, and the other name
        # once found
        torrent = {"infoHash": "a" * 40, "fileIdx": 2}
        self.aios.find_row = lambda *a: {"url": self.srv.url("tiny.mkv"), "streamData": {"torrent": torrent}}
        w = self.worker()
        run_once(w, self.job)
        read = list(self.srv.log)
        self.assertEqual(self.store.over_cap(f"bt:{'a' * 40}:2"), 200_000)
        other = Job("movie", "tt1", {"filename": "Renamed.Release.mkv", "videoSize": self.size})
        self.assertFalse(w.capped(other))                        # not known by this name before its row is listed
        run_once(w, other)
        self.assertEqual(self.srv.log, read)                     # nothing more read
        self.assertEqual(w.limits.started, 1)                    # no new file counted
        self.assertTrue(w.capped(Job("movie", "tt1", dict(other.hints))))   # its next listing has no "(Subsync)" entry

    def test_a_file_still_asked_for_keeps_its_marks(self):
        # A mark goes 180 days after the last request it met; each request that meets one renews it, so a file
        # still asked for isn't read again (its byte cap here, then its read failures)
        run_once(self.worker(), self.job)
        read = list(self.srv.log)
        self.store.db.execute("UPDATE over_cap SET at=0")
        run_once(self.worker(), Job("movie", "tt1", dict(self.job.hints)))
        self.assertEqual(self.srv.log, read)                     # nothing more read
        at = self.store.db.execute("SELECT at FROM over_cap WHERE alias=?", (self.job.alias,)).fetchone()[0]
        self.assertGreater(at, time.time() - 60)
        self.store.db.execute("DELETE FROM over_cap")
        for _ in range(5):
            self.store.file_failed(self.job.alias)                # 10 min, 1 h, 6 h, 24 h, then never again
        self.store.db.execute("UPDATE files SET read_at=0")
        run_once(self.worker(), Job("movie", "tt1", dict(self.job.hints)))
        self.assertEqual(self.srv.log, read)
        at = self.store.db.execute("SELECT read_at FROM files WHERE file_key=?", (self.job.alias,)).fetchone()[0]
        self.assertGreater(at, time.time() - 60)

    def test_a_raised_cap_reads_it_again(self):
        run_once(self.worker(), self.job)
        read = len(self.srv.log)
        w = self.worker(SUBSYNC_INDEX_CAP_MB="32")
        run_once(w, Job("movie", "tt1", dict(self.job.hints)))
        self.assertGreater(len(self.srv.log), read)
        self.assertIsNotNone(self.store.resolve(self.job.alias))  # its index was read
        self.assertEqual(w.limits.started, 1)

    def test_an_index_over_the_parsers_bound_ends_as_one_over_the_cap(self):
        # Cues over mkv.MAX_CUES were skipped, so the file looked as if it had no subtitle track and went to the
        # pick and the audio route, whatever the cap; it is now marked as an index over the cap is, under the job's cap
        with mock.patch.object(mkv, "MAX_CUES", 64):            # tiny.mkv's Cues are larger
            w = self.worker(SUBSYNC_INDEX_CAP_MB="32")
            run_once(w, self.job)
            read = list(self.srv.log)
            self.assertEqual(self.store.over_cap(self.job.alias), 32 * 2 ** 20)
            self.assertFalse(w.busy_with(self.job.alias))         # not queued again, not waiting for a pick
            self.assertFalse(w.waits_for_pick(self.job.alias))
            self.assertTrue(w.capped(Job("movie", "tt1", dict(self.job.hints))))
            run_once(w, Job("movie", "tt1", dict(self.job.hints)))
            self.assertEqual(self.srv.log, read)                  # not opened again

    def test_an_audio_job_stopped_at_its_audio_cap_leaves_the_index_readable(self):
        # The audio route's own cap (SUBSYNC_FILE_CAP_GB) says nothing about the index: the file isn't marked
        # over the cap, so its next index pass reads it; it waits for another pick, as when the person's day ran out
        w = self.worker(SUBSYNC_AUDIO_ROUTE="on")
        audio = Job("movie", "tt1", dict(self.job.hints))
        audio.audio = True

        def stopped(job):
            raise CapReached(w._cap(job))
        w.process = stopped
        run_once(w, audio, lane="audio")
        self.assertIsNone(self.store.over_cap(self.job.alias))
        self.assertFalse(w.capped(Job("movie", "tt1", dict(self.job.hints))))
        self.assertFalse(w.busy_with(self.job.alias))            # not queued again
        self.assertTrue(w.waits_for_pick(self.job.alias))

    def test_a_stop_for_the_days_budget_is_tried_again(self):
        w = self.worker(SUBSYNC_INDEX_CAP_MB="32", SUBSYNC_DAILY_BUDGET_GB="0.0001")   # 100,000 bytes
        run_once(w, self.job)
        self.assertTrue(w.busy_with(self.job.alias))             # queued again after SUBSYNC_RETRY_STOPPED_S
        self.assertGreater(w.jobs[self.job.alias].not_before, time.time() + 500)
        self.assertIsNone(self.store.over_cap(self.job.alias))

    def test_a_stopped_job_lists_and_counts_nothing_again_while_its_pause_lasts(self):
        """A job stopped by the day's budget (until 00:00 UTC) or the refusal back-off, due again while that
        pause lasts, is stopped again before anything is listed, asked or counted; once reads may go on it runs, and its
        file is counted once in all (README: new files)."""
        listings = []
        find_row = self.aios.find_row
        self.aios.find_row = lambda *a: listings.append(1) or find_row(*a)
        for pause in ("budget", "back-off"):
            with self.subTest(pause=pause):
                listings.clear()
                self.aios.lists = 0
                w = self.worker(SUBSYNC_INDEX_CAP_MB="32", SUBSYNC_DAILY_BUDGET_GB="0.0001" if pause == "budget" else "100")
                if pause == "back-off":
                    w.safety.refused()                           # an earlier refusal: every read waits
                run_once(w, Job("movie", "tt1", dict(self.job.hints)))
                retry = w.jobs[self.job.alias]
                self.assertEqual((len(listings), self.aios.lists, w.limits.started), (1, 1, 1))
                for _ in range(3):                               # due again: the pause is still on
                    retry.not_before = 0
                    run_once(w, retry)
                    self.assertIs(w.jobs.get(self.job.alias), retry)   # queued again
                self.assertEqual((len(listings), self.aios.lists, w.limits.started), (1, 1, 1))
                w.s.daily_budget, retry.budget_until, w.safety.refused_until = 10 ** 12, 0.0, 0.0   # 00:00 UTC came
                retry.not_before = 0
                run_once(w, retry)
                self.assertFalse(w.busy_with(self.job.alias))
                self.assertIsNotNone(self.store.resolve(self.job.alias))     # its index was read
                self.assertEqual((len(listings), w.limits.started), (2, 1))  # counted once in all
                self.store.db.execute("DELETE FROM aliases")


# -- a first read that can't reach the file ------------------------------------------------------------
class Flaky:
    """realdebrid.urlopen with faults, against the local server: the first `faults` requests to the link fail as a
    dropped connection does (URLError); with `moved`, the link answers as one that redirected to another address,
    which never answers."""

    def __init__(self, link, faults=0, moved=False):
        self.real, self.link, self.faults, self.moved = realdebrid.urlopen, link, faults, moved
        self.link_calls = 0

    def __call__(self, request, timeout):
        if request.full_url != self.link:       # the address the link pointed to
            raise urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer"))
        self.link_calls += 1
        if self.faults:
            self.faults -= 1
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        r = self.real(request, timeout)
        if self.moved:
            r.url = self.link + "?moved"
        return r


class BreaksOff:
    """realdebrid.urlopen whose first answer breaks off with a reset after `after` bytes of its body."""

    def __init__(self, after):
        self.real, self.after, self.calls = realdebrid.urlopen, after, 0

    def __call__(self, request, timeout):
        r = self.real(request, timeout)
        self.calls += 1
        if self.calls == 1:
            read, left = r.read, [self.after]

            def broken(n=-1):
                if not left[0]:
                    raise ConnectionResetError(54, "Connection reset by peer")
                data = read(left[0] if n is None or n < 0 else min(n, left[0]))
                left[0] -= len(data)
                return data
            r.read = broken
        return r


class UnreadableFileTest(unittest.TestCase):
    """A file subsync can't use: one neither Matroska nor MP4 (an AVI, a TS) keeps its originals and is never read
    again, since its format can't change (it was read again after 1, 6 and 24 hours), while one whose index is
    damaged backs off an hour, 6 and 24 hours (the worker's handling was untested); a first pass that fails
    once the index is read (damage the parsers let through, met when its fields are used) backs off as a read that
    failed (it was read again on every request). None is read again meanwhile, and the log holds no link."""

    def setUp(self):
        watch_the_log(self)
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(TINY, self.dir)
        with open(os.path.join(self.dir, "clip.avi"), "wb") as f:
            f.write(b"RIFF" + (8184).to_bytes(4, "little") + b"AVI LIST" + bytes(8176))
        self.srv = RangeServer(self.dir)
        self.addCleanup(self.srv.close)
        sized = mock.patch.object(sources, "MIN_VIDEO_BYTES", 1)    # the listing's size is the file's
        sized.start()
        self.addCleanup(sized.stop)
        srv = self.srv

        class Aios:
            name, lists = "tiny.mkv", 0

            def find_row(self, *a):
                return {"url": srv.url(self.name)}

            def subtitles(self, *a):
                self.lists += 1
                return [{"id": "e1", "url": srv.url("tiny.srt"), "lang": "eng", "release": ""}]
        self.aios = Aios()
        self.store = closed_after(self, Store(os.path.join(self.dir, "store")))

    def worker(self):
        s = Settings({"SUBSYNC_DATA": self.dir, "SUBSYNC_LANGUAGES": "eng", "SUBSYNC_EXTRA_SOURCES": ""})
        return Worker(s, self.store, self.aios, Safety(), limits=CountedLimits())

    def job(self, name):
        self.aios.name = name
        return Job("movie", "tt1", {"filename": name, "videoSize": os.path.getsize(os.path.join(self.dir, name))})

    def test_a_file_neither_matroska_nor_mp4_keeps_its_originals_and_is_never_read_again(self):
        w = self.worker()
        job = self.job("clip.avi")
        with self.assertLogs("subsync.worker", "INFO") as logs:
            run_once(w, job)
        self.assertIn("file not readable as Matroska or MP4 movie/tt1", "\n".join(logs.output))
        refused = self.store.db.execute("SELECT fails, retry_at FROM files WHERE file_key=?",
                                        (job.alias + "|refused",)).fetchone()
        self.assertEqual(refused, (1, float("inf")))             # never again, from the first read
        self.assertEqual(self.store.file_retry(job.alias), float("inf"))
        self.assertTrue(w._file_blocked(job.alias))
        self.assertIsNone(self.store.resolve(job.alias))         # nothing of it stored as read
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)
        self.assertEqual(w.done[job.alias], {"eng": None})       # nothing published
        self.assertEqual(w.known_originals(job.alias)["eng"]["id"], "e1")   # its original is served
        read, lists = len(self.srv.log), self.aios.lists
        run_once(w, self.job("clip.avi"))                        # asked again within the hour
        self.assertEqual((len(self.srv.log), self.aios.lists), (read, lists))   # nothing read, no list
        later = time.time() + sum(REFUSED_BACKOFF_S) + 3600     # past every wait a damaged index gets
        self.store.clock = lambda: later
        self.assertTrue(w._file_blocked(job.alias))
        run_once(w, self.job("clip.avi"))
        self.assertEqual((len(self.srv.log), self.aios.lists), (read, lists))   # still nothing read, no list

    def test_a_file_whose_index_is_damaged_backs_off_an_hour_6_and_24_hours(self):
        # Unlike a format, a damaged index is tried again (the file may be served whole another time)
        with open(os.path.join(self.dir, "damaged.mkv"), "wb") as f:
            f.write(bytes.fromhex("1a45dfa3") + bytes(8188))
        w = self.worker()
        job = self.job("damaged.mkv")
        with self.assertLogs("subsync.worker", "INFO") as logs:
            run_once(w, job)
        self.assertIn("file not readable as Matroska or MP4 movie/tt1", "\n".join(logs.output))
        refused = self.store.db.execute("SELECT fails, retry_at FROM files WHERE file_key=?",
                                        (job.alias + "|refused",)).fetchone()
        self.assertEqual(refused[0], 1)                          # under the refusal back-off: an hour first
        self.assertAlmostEqual(refused[1], time.time() + REFUSED_BACKOFF_S[0], delta=60)
        self.assertTrue(w._file_blocked(job.alias))
        read, lists = len(self.srv.log), self.aios.lists
        run_once(w, self.job("damaged.mkv"))                     # asked again within the hour
        self.assertEqual((len(self.srv.log), self.aios.lists), (read, lists))   # nothing read, no list
        later = refused[1] + 1                                   # the hour has passed: read once more
        self.store.clock = lambda: later
        self.assertFalse(w._file_blocked(job.alias))
        run_once(w, self.job("damaged.mkv"))
        self.assertGreater(len(self.srv.log), read)
        self.assertEqual(self.store.db.execute("SELECT fails FROM files WHERE file_key=?",
                                               (job.alias + "|refused",)).fetchone(), (2,))

    def test_a_first_pass_that_fails_once_the_index_is_read_backs_off(self):
        from subsync.store import READ_FAILED_BACKOFF_S
        w = self.worker()
        job = self.job("tiny.mkv")
        # a 2,000-byte TrackNumber opens, then fails here (json.dumps of the tracks: Python's limit of 4,300 digits)
        with mock.patch.object(self.store, "file_read", side_effect=ValueError("Exceeds the limit (4300 digits)")), \
                self.assertLogs("subsync.worker", "WARNING") as logs:
            run_once(w, job)
        self.assertEqual(logs.output, ["WARNING:subsync.worker:failed movie/tt1: ValueError"])
        retry = self.store.file_retry(job.alias)
        self.assertIsNotNone(retry, "nothing marked: read again on every request")
        self.assertAlmostEqual(retry, time.time() + READ_FAILED_BACKOFF_S[0], delta=60)
        read, lists = len(self.srv.log), self.aios.lists
        run_once(w, self.job("tiny.mkv"))                        # asked again within the back-off
        self.assertEqual((len(self.srv.log), self.aios.lists), (read, lists))
        # a failure before the index is read (here the subtitle list's) marks nothing: nothing is known of the file
        self.store.db.execute("DELETE FROM files")
        with mock.patch.object(w, "_candidates", side_effect=RuntimeError("a bug")), \
                self.assertLogs("subsync.worker", "WARNING") as logs:
            run_once(w, self.job("tiny.mkv"))
        self.assertEqual(logs.output, ["WARNING:subsync.worker:failed movie/tt1: RuntimeError"])
        self.assertIsNone(self.store.file_retry(job.alias))


class ConnectionFaultTest(unittest.TestCase):
    """A read that fails for want of a connection says so (ReadFailed.connection); an answer it can't use doesn't."""

    def setUp(self):
        self.srv = RangeServer(FIXTURES)
        self.link = self.srv.url("tiny.mkv")

    def tearDown(self):
        self.srv.close()

    def failed(self, reader, stream=False):
        with self.assertRaises(ReadFailed) as caught:
            b"".join(reader.stream(0, 1000)) if stream else reader.get(0, 1000)
        return caught.exception

    def test_kinds(self):
        self.assertTrue(self.failed(RangeReader(self.link, "t", opener=Flaky(self.link, faults=1))).connection)
        self.assertTrue(self.failed(RangeReader(self.link, "t", opener=Flaky(self.link, faults=1)), True).connection)

        def timed_out(request, timeout):
            raise TimeoutError("timed out")
        self.assertTrue(self.failed(RangeReader(self.link, "t", opener=timed_out)).connection)

        def blocked(request, timeout):
            raise urllib.error.URLError(realdebrid.Blocked("a request to Real-Debrid"))
        self.assertFalse(self.failed(RangeReader(self.link, "t", opener=blocked)).connection)
        self.srv.fault = "403"
        self.assertFalse(self.failed(RangeReader(self.link, "t")).connection)                 # Refused
        self.srv.fault = "wrong-size"
        self.assertFalse(self.failed(RangeReader(self.link, "t", size=1000)).connection)      # size differs
        self.srv.fault = "ignore-range"
        with self.assertRaises(ReadFailed) as caught:
            RangeReader(self.link, "t").get(500, 100)                                         # range ignored
        self.assertFalse(caught.exception.connection)


class FirstReadRetryTest(unittest.TestCase):
    """A first read that can't reach the file is tried once more READ_RETRY_S later in the same job,
    as one of the link's look-ups; then the file backs off 10 minutes, 1, 6 and 24 hours. A refusal, an answer
    of another size and a Usenet file are not tried again."""

    def setUp(self):
        watch_the_log(self)
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        shutil.copy(TINY, self.dir)
        self.srv = RangeServer(self.dir)
        self.link = self.srv.url("tiny.mkv")
        self.size = os.path.getsize(TINY)
        sized = mock.patch.object(sources, "MIN_VIDEO_BYTES", 1)    # the listing's size is the file's
        sized.start()
        self.addCleanup(sized.stop)
        # the wait is recorded, not waited: the worker's own time, whose sleep alone is a stand-in (a patch of
        # subsync.worker.time.sleep replaced time.sleep for every module and thread)
        waits = mock.patch("subsync.worker.time", mock.Mock(wraps=time, sleep=mock.Mock()))
        self.sleep = waits.start().sleep
        self.addCleanup(waits.stop)
        link = self.link

        class Aios:
            def find_row(self, *a):
                return {"url": link}

            def subtitles(self, *a):
                return [{"id": "e1", "url": link + ".srt", "lang": "eng", "release": ""}]
        self.store = closed_after(self, Store(os.path.join(self.dir, "store")))
        s = Settings({"SUBSYNC_DATA": self.dir, "SUBSYNC_LANGUAGES": "eng", "SUBSYNC_SIZE_SLACK_MB": "0"})
        self.w = Worker(s, self.store, Aios(), Safety())
        self.w._sync_all = lambda *a: None    # the reads are what is tested here, not the sync after them
        self.job = Job("movie", "tt1", {"filename": "tiny.mkv", "videoSize": self.size})

    def tearDown(self):
        self.srv.close()
        shutil.rmtree(self.dir)

    def run_with(self, opener=None, usenet=False):
        with mock.patch.object(realdebrid, "urlopen", opener or realdebrid.urlopen):
            if not usenet:
                run_once(self.w, self.job)
                return
            stopped = []
            self.w.usenet_finder = UsenetFinder(None, mock.Mock(stop=stopped.append))   # its gate records the stop
            self.job.usenet = True
            self.w._process_row(self.job, dict(self.job.hints), {"url": self.link}, None)
            return stopped

    def backed_off_for(self):
        retry = self.store.file_retry(self.job.alias)
        return None if retry is None else round(retry - time.time(), -1)

    def test_only_the_workers_wait_is_a_stand_in(self):
        # A sleep anywhere else (another module, another thread) still waits and isn't counted as the worker's
        t0 = time.monotonic()
        time.sleep(0.05)
        self.assertGreaterEqual(time.monotonic() - t0, 0.04)
        self.sleep.assert_not_called()

    def test_one_dropped_connection_is_tried_again_and_the_file_read_in_the_same_job(self):
        flaky = Flaky(self.link, faults=1)
        self.run_with(flaky)
        self.assertEqual(READ_RETRY_S, 10)
        self.sleep.assert_called_once_with(READ_RETRY_S)
        self.assertEqual(self.srv.log[0], (0, self.size))          # the start, read on the second try
        self.assertIsNotNone(self.store.resolve(self.job.alias))   # its index was read
        self.assertFalse(self.store.file_blocked(self.job.alias))

    def test_the_wait_before_the_second_try_holds_no_place(self):
        """The job keeps its claims through the wait before its second try, but gives its place back, so another
        person's file is worked on meanwhile; it takes the place again for the read."""
        turn, inside = Turn(1), []
        self.w.turn = turn
        self.sleep.side_effect = lambda s: inside.append((turn._inside, set(turn._claims)))
        self.run_with(Flaky(self.link, faults=1))
        self.assertEqual(inside, [(0, {self.job.alias})])           # the place free, the file's claim kept
        self.assertIsNotNone(self.store.resolve(self.job.alias))   # read on the second try
        self.assertEqual((turn._inside, turn._claims), (0, set()))

    def test_a_first_read_that_breaks_off_is_counted_and_tried_again(self):
        # What the read got before it broke off is counted (it was not), and the read is still tried once
        # more, as one that got no answer is
        with self.assertLogs("subsync.worker", "INFO") as logs:
            self.run_with(BreaksOff(1000))
        self.sleep.assert_called_once_with(READ_RETRY_S)
        self.assertIsNotNone(self.store.resolve(self.job.alias))   # read on the second try
        self.assertEqual(self.srv.log[0], (0, self.size))          # the read that broke off, after 1000 bytes
        got = 1000 + sum(n for _, n in self.srv.log[1:])
        self.assertEqual(self.store.day_count("read"), got)
        self.assertIn(f" bytes={got} requests={len(self.srv.log)}", [m for m in logs.output if ":done " in m][0])

    def test_two_dropped_connections_back_the_file_off_for_ten_minutes(self):
        flaky = Flaky(self.link, faults=2)
        self.run_with(flaky)
        self.sleep.assert_called_once()
        self.assertEqual(flaky.link_calls, 2)
        self.assertTrue(self.store.file_blocked(self.job.alias))
        self.assertEqual(self.backed_off_for(), 600)

    def test_a_refusal_is_not_tried_again(self):
        self.srv.fault = "403"
        with self.assertLogs("subsync.worker", "WARNING") as logs:
            self.run_with()
        self.assertEqual(logs.output, ["WARNING:subsync.worker:refused movie/tt1 (HTTP 403): no reads for 900 s"])
        self.sleep.assert_not_called()
        self.assertEqual(len(self.srv.headers), 1)
        self.assertEqual(self.backed_off_for(), 3600)             # a refused file waits an hour first, as before

    def test_an_answer_of_another_size_is_not_tried_again(self):
        self.srv.fault = "wrong-size"
        self.run_with()
        self.sleep.assert_not_called()
        self.assertEqual(len(self.srv.headers), 1)
        self.assertEqual(self.backed_off_for(), 600)

    def test_the_link_is_asked_at_most_one_plus_max_re_resolve_times_in_all(self):
        # a file whose index takes four reads (its head, its Cues' header and body, the hash's last 64 KiB): tiny.mkv
        # with 300 KiB after it, as its Cues in the last 64 KiB would come in one read with that tail, and a
        # head read of 128 KiB, as Cues the head holds are taken from it
        patch = mock.patch.object(mp4, "HEAD_BYTES", 128 * 1024)
        patch.start()
        self.addCleanup(patch.stop)
        with open(os.path.join(self.dir, "tiny.mkv"), "ab") as f:
            f.write(bytes(300 * 1024))
        self.job = Job("movie", "tt1", {"filename": "tiny.mkv", "videoSize": self.size + 300 * 1024})
        flaky = Flaky(self.link, faults=1, moved=True)       # then every address it points to drops the connection
        self.run_with(flaky)
        self.sleep.assert_called_once()
        self.assertEqual(flaky.link_calls, 1 + MAX_RE_RESOLVE)
        self.assertTrue(self.store.file_blocked(self.job.alias))

    def test_a_read_that_succeeds_clears_the_failures_before_it(self):
        # The failures of a file added up over its whole life, a refusal's too, so passing faults months
        # apart stopped it for good in the end
        past = time.time() - 2 * 86400
        self.store.clock = lambda: past
        for _ in range(2):
            self.store.file_failed(self.job.alias)                     # 10 min, then 1 h: over by now
        self.store.file_failed(self.job.alias, REFUSED_BACKOFF_S)     # and a refusal's hour
        self.store.clock = time.time
        self.assertFalse(self.store.file_blocked(self.job.alias))
        self.run_with()
        self.assertIsNotNone(self.store.resolve(self.job.alias))     # its index was read
        self.assertIsNone(self.backed_off_for())
        self.assertEqual(round(self.store.file_failed(self.job.alias) - time.time(), -1), 600)   # the first wait again

    def test_a_usenet_file_is_not_tried_again(self):
        flaky = Flaky(self.link, faults=1)
        stopped = self.run_with(flaky, usenet=True)
        self.sleep.assert_not_called()
        self.assertEqual(flaky.link_calls, 1)
        self.assertEqual(stopped, ["the first read of a Usenet file failed"])   # Usenet stops for the day
        self.assertEqual(self.backed_off_for(), 3600)


class SelfTestTest(unittest.TestCase):
    """The installer's one index read (python -m subsync.selftest)."""

    def setUp(self):
        self.srv = RangeServer(FIXTURES)

    def tearDown(self):
        self.srv.close()

    def run_selftest(self, url, *args, **kwargs):
        """selftest.run(url, ...), checked: what it gives (printed for the installer) holds no link, neither the one
        given nor the address it was read from, on every path."""
        out = selftest.run(url, *args, **kwargs)
        printed = json.dumps(out)
        for link in (url, "127.0.0.1", f":{self.srv.port}"):
            self.assertNotIn(link, printed)
        return out

    def test_reads_the_index_within_the_cap(self):
        out = self.run_selftest(self.srv.url("tiny.mkv"), 4 * 1024 * 1024, Settings({}))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["container"], "mkv")
        self.assertNotIn("matroska", out)                  # the container says it
        self.assertEqual(out["size"], os.path.getsize(os.path.join(FIXTURES, "tiny.mkv")))
        self.assertTrue(out["text_tracks"])
        self.assertLessEqual(out["bytes"], 4 * 1024 * 1024)
        self.assertNotIn("url", json.dumps(out).lower().replace("ok", ""))

    def test_refusal_and_cap_are_reported(self):
        self.srv.fault = "403"
        out = self.run_selftest(self.srv.url("tiny.mkv"), 4 * 1024 * 1024, Settings({}))
        self.assertEqual(out["error"], "refused")
        self.srv.fault = None
        self.assertEqual(self.run_selftest(self.srv.url("tiny.mkv"), 1000, Settings({}))["error"], "cap")

    def test_the_cap_is_subsync_s_own_index_cap_unless_given(self):
        # The wizard's 4 MB cap stopped a 7.3 MB index that subsync itself reads (16 MB then, 32 MB)
        out = self.run_selftest(self.srv.url("tiny.mkv"), None, Settings({}))
        self.assertTrue(out["ok"], out)
        out = self.run_selftest(self.srv.url("tiny.mkv"), None, Settings({"SUBSYNC_INDEX_CAP_MB": "0.001"}))
        self.assertEqual(out["error"], "cap")
        self.assertIn("larger than subsync reads", out["detail"])

    def test_an_index_past_the_parsers_own_limit_names_that_limit(self):
        # An index is never read past a fixed limit (32 MiB in MKV, 64 MiB in MP4) whatever the cap; when that
        # limit stopped the read, the sentence still named the cap: with a cap of 64, an MKV index of 40 MB was "larger
        # than subsync reads (64 MB)"
        for name, limit, cap, said in (("tiny.mkv", mkv.MAX_CUES, "64", "32"), ("tiny.mp4", mp4.MAX_MOOV, "100", "64")):
            with self.subTest(name=name), \
                    mock.patch.object(selftest, "open_index", side_effect=IndexTooLarge(limit)):
                out = self.run_selftest(self.srv.url(name), None, Settings({"SUBSYNC_INDEX_CAP_MB": cap}))
                self.assertEqual((out["ok"], out["error"], out["detail"]),
                                 (False, "cap", f"its index is larger than subsync reads ({said} MB): subsync keeps "
                                                "the original subtitles for this file"))
        with mock.patch.object(selftest, "open_index", side_effect=Stopped("cap reached")):   # the cap stopped it
            out = self.run_selftest(self.srv.url("tiny.mkv"), 3 * 1024 * 1024, Settings({}))
        self.assertEqual(out["detail"], "its index is larger than subsync reads (3 MB): subsync keeps the original "
                                        "subtitles for this file")

    def test_an_index_cap_of_0_is_no_cap_as_for_the_worker(self):
        # SUBSYNC_INDEX_CAP_MB 0 (accepted: "a number from 0") is no cap for the worker's JobGuard, but the
        # self-test stopped at its first read and said every file's index was larger than subsync reads (0 MB)
        s = Settings({"SUBSYNC_INDEX_CAP_MB": "0"})
        self.assertEqual(s.index_cap, 0)
        out = self.run_selftest(self.srv.url("tiny.mkv"), None, s)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["container"], "mkv")
        self.assertGreater(out["bytes"], 0)

    def test_a_listed_size_off_by_rounding_is_accepted(self):
        real = os.path.getsize(os.path.join(FIXTURES, "tiny.mkv"))
        self.assertTrue(self.run_selftest(self.srv.url("tiny.mkv"), None, Settings({}), size=real + 1000)["ok"])

    def test_the_container_is_named(self):
        self.assertEqual(self.run_selftest(self.srv.url("tiny.mkv"), None, Settings({}))["container"], "mkv")
        out = self.run_selftest(self.srv.url("tiny.mp4"), None, Settings({}))
        self.assertEqual(out["container"], "mp4")
        self.assertNotIn("matroska", out)
        self.assertTrue(out["text_tracks"])

    def test_a_failed_read_and_another_kind_of_file_are_reported(self):
        out = self.run_selftest(self.srv.url("nope.mkv"), None, Settings({}))
        self.assertEqual((out["ok"], out["error"]), (False, "read"))
        out = self.run_selftest(self.srv.url("tiny.mkv"), None, Settings({"SUBSYNC_SIZE_SLACK_MB": "0"}), size=1000)
        self.assertEqual((out["ok"], out["error"], out["detail"]), (False, "read", "size differs"))
        out = self.run_selftest(self.srv.url("tiny.srt"), None, Settings({}))
        self.assertEqual((out["ok"], out["container"]), (True, None))
        self.assertIn("neither Matroska nor MP4", out["note"])

    def test_an_index_its_parsers_cant_read_is_reported_too(self):
        # An error of the parsers themselves escaped (in 1.6.0 a damaged index raised IndexError, struct.error
        # or OverflowError), nothing was printed, and setup said "no answer from subsync" for a file the service just
        # keeps the originals of, as the worker catches the same errors
        with mock.patch.object(MkvIndex, "text_tracks", side_effect=IndexError("vint past the buffer")):
            out = self.run_selftest(self.srv.url("tiny.mkv"), None, Settings({}))
            self.assertEqual((out["ok"], out["error"], out["detail"]),
                             (False, "unreadable",
                              "its index couldn't be read (IndexError): subsync keeps the original subtitles for this "
                              "file"))
            self.assertGreater(out["bytes"], 0)                # what it read is still said
            code, printed = self.main(self.srv.url("tiny.mkv") + "\n")
        self.assertEqual((code, printed.count("\n"), json.loads(printed)["error"]), (1, 1, "unreadable"))
        self.assertNotIn("127.0.0.1", printed)

    def test_rewrite_applies(self):
        s = Settings({"SUBSYNC_URL_REWRITE": f"https://public.example=http://127.0.0.1:{self.srv.port}"})
        self.assertTrue(self.run_selftest("https://public.example/tiny.mkv", 4 * 1024 * 1024, s)["ok"])

    def test_the_time_taken_is_measured_on_a_steady_clock(self):
        # The system clock stepped back a day during the read doesn't make its time negative
        wall, steady = iter([2_000_000_000.0, 1_999_913_600.0]), iter([100.0, 100.25])
        clock = types.SimpleNamespace(time=lambda: next(wall), monotonic=lambda: next(steady))
        with mock.patch.object(selftest, "time", clock):
            out = self.run_selftest(self.srv.url("tiny.mkv"), None, Settings({}))
        self.assertEqual(out["ms"], 250)

    def main(self, stdin, *argv):
        """python -m subsync.selftest with `stdin`: (exit code, what it printed)."""
        printed = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(stdin)), mock.patch.object(sys, "stdout", printed), \
                mock.patch.object(sys, "argv", ["selftest", *argv]), \
                mock.patch.object(selftest, "Settings", lambda: Settings({})):
            code = selftest.main()
        return code, printed.getvalue()

    def test_main_reads_the_link_from_standard_input_and_prints_one_json_line(self):
        # The installer's only check of a real read; the link goes in on standard input, never out
        link = self.srv.url("tiny.mkv")
        code, printed = self.main(link + "\n", "--size", str(os.path.getsize(os.path.join(FIXTURES, "tiny.mkv"))))
        self.assertEqual(code, 0)
        self.assertEqual(printed.count("\n"), 1)
        out = json.loads(printed)
        self.assertEqual((out["ok"], out["container"]), (True, "mkv"))
        self.assertNotIn(link, printed)
        self.assertNotIn("127.0.0.1", printed)
        self.srv.fault = "403"
        code, printed = self.main(link + "\n")
        self.assertEqual((code, json.loads(printed)["error"]), (1, "refused"))
        self.assertNotIn("127.0.0.1", printed)

    def test_main_without_a_link_says_so_and_never_echoes_what_it_got(self):
        for stdin in ("", "\n", "not a link: ftp://somewhere/file.mkv\n"):
            code, printed = self.main(stdin)
            self.assertEqual(code, 2, stdin)
            self.assertEqual(json.loads(printed), {"ok": False, "error": "no link on standard input"})


class Reply:
    """A 206 from memory as http.client reads one: each read gives what is asked, in a new buffer (`body` None: zero
    bytes, never held here); `fault`, an exception the body's first read raises."""

    status = 206

    def __init__(self, start, size, body=None, fault=None):
        self.body, self.left, self.fault = body, size - start, fault
        self.headers = {"Content-Range": f"bytes {start}-{size - 1}/{size}"}

    def read(self, n=-1):
        if self.fault is not None:
            raise self.fault
        n = self.left if n is None or n < 0 else min(n, self.left)
        self.left -= n
        return bytes(n)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Garbled(Flaky):
    """The link redirected (`moved`) to an address whose replies http.client can't read (a garbled status line)."""

    def __call__(self, request, timeout):
        if request.full_url != self.link:
            raise http.client.BadStatusLine("HTTP/1.1 2OO OK")
        return super().__call__(request, timeout)


class Trickle:
    """A local server that answers a range with 206 and then sends one byte every `gap` seconds."""

    def __init__(self, size, gap):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                a, _, b = self.headers["Range"][6:].partition("-")
                n = int(b) - int(a) + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
                self.send_header("Content-Length", str(n))
                self.end_headers()
                try:
                    for _ in range(n):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        time.sleep(gap)
                except OSError:              # the reader gave up: so does the connection
                    self.close_connection = True
        self.server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/f.mkv"

    def close(self):
        stop_server(self.server)


class ReadTimeLimitTest(unittest.TestCase):
    """The timeout bounds each wait for data only, so a service that sent a byte now and then held a read (and
    its job's sync place) for hours; a read now fails once it has gone on for the timeout plus a second for every
    READ_BYTES_PER_S bytes it asked for."""

    def setUp(self):
        self.srv = Trickle(10_000, 0.1)
        self.addCleanup(self.srv.close)

    def test_a_trickling_read_fails_as_too_slow(self):
        for name, read in ReplyCheckTest.reads():
            with self.subTest(read=name):
                r = RangeReader(self.srv.url, "t", timeout=0.5)
                t0 = time.monotonic()
                with self.assertRaises(ReadFailed) as caught:
                    read(r, 0, 40)                          # 4 s at this pace, each wait 0.1 s
                self.assertLess(time.monotonic() - t0, 2.0)
                self.assertEqual(str(caught.exception), "too slow")
                self.assertTrue(caught.exception.connection)  # a first read is tried once more, as after a timeout

    def test_the_limit_grows_with_the_length_asked(self):
        from subsync import reader
        r = RangeReader(self.srv.url, "t", timeout=0.5)
        with mock.patch.object(reader, "READ_BYTES_PER_S", 8):  # 0.5 s and a second for every 8 bytes: 3 s for 20
            self.assertEqual(r.get(0, 20), b"x" * 20)               # sent in 2 s


class TrickleHead:
    """A local server whose answer's status line and headers come one byte every `gap` seconds, its body then at once
; /hop answers so with a redirect to /fast, which answers at once."""

    def __init__(self, size, gap):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                a, _, b = self.headers["Range"][6:].partition("-")
                n = int(b) - int(a) + 1
                head = (f"HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {a}-{b}/{size}\r\n"
                        f"Content-Length: {n}\r\n\r\n")
                if self.path == "/hop":
                    head, n = "HTTP/1.1 302 Found\r\nLocation: /fast\r\nContent-Length: 0\r\n\r\n", 0
                try:
                    for byte in head.encode():
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if self.path != "/fast":
                            time.sleep(gap)
                    self.wfile.write(b"x" * n)
                except OSError:              # the reader gave up: so does the connection
                    pass
                self.close_connection = True
        self.server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        stop_server(self.server)


class WholeReadLimitTest(unittest.TestCase):
    """The time limit started at the reply's body, so a status line, headers or a redirect sent a byte now and
    then, each within a read's own wait, held the read (and its job's sync place) as long as the server liked; each
    request of a read is now bounded from before it connects."""

    def setUp(self):
        self.srv = TrickleHead(10_000, 0.1)
        self.addCleanup(self.srv.close)

    def test_a_trickled_head_or_redirect_fails_as_too_slow(self):
        for path in ("/f.mkv", "/hop"):
            for name, read in ReplyCheckTest.reads():
                with self.subTest(path=path, read=name):
                    r = RangeReader(self.srv.url + path, "t", timeout=0.5)
                    t0 = time.monotonic()
                    with self.assertRaises(ReadFailed) as caught:
                        read(r, 0, 10)                      # its head alone takes 6 s at this pace
                    self.assertLess(time.monotonic() - t0, 2.0)
                    self.assertEqual(str(caught.exception), "too slow")
                    self.assertTrue(caught.exception.connection)
                    self.assertEqual(r.requests, 1)

    def test_a_first_look_up_has_its_own_longer_wait(self):
        # a Usenet file's first look-up waits longer (the service opens the NZB first): its bound is that wait's
        srv = TrickleHead(10_000, 0.03)                     # the redirect's head in about 1.5 s
        self.addCleanup(srv.close)
        r = RangeReader(srv.url + "/hop", "t", timeout=0.5, first_timeout=10)
        self.assertEqual(r.get(0, 10), b"x" * 10)


class StallAfter:
    """A local server that answers a range with 206, sends its first `sent` bytes and then nothing for `gap` seconds, as
    a service whose connection breaks off does."""

    def __init__(self, size, sent, gap):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                a, _, b = self.headers["Range"][6:].partition("-")
                n = int(b) - int(a) + 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
                self.send_header("Content-Length", str(n))
                self.end_headers()
                try:
                    self.wfile.write(bytes(min(sent, n)))
                    self.wfile.flush()
                    time.sleep(gap)
                except OSError:              # the reader gave up: so does the connection
                    pass
                self.close_connection = True
        self.server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/f.mkv"

    def close(self):
        stop_server(self.server)


class BrokenOffReadTest(unittest.TestCase):
    """The bytes a read got before it broke off were counted nowhere (the reader's count, the job's cap, the
    day's total, the person's daily data): a read that fails counts what arrived, a CHUNK at a time, as a stream did."""

    def test_what_arrived_before_a_read_broke_off_is_counted(self):
        sent, asked = 3 * CHUNK, 4 * CHUNK
        srv = StallAfter(asked, sent, gap=1.5)
        self.addCleanup(srv.close)
        for name, read in ReplyCheckTest.reads():
            with self.subTest(read=name):
                guard = mock.Mock()
                r = RangeReader(srv.url, "t", guard=guard, timeout=0.5)
                with self.assertRaises(ReadFailed) as caught:
                    read(r, 0, asked)
                self.assertTrue(caught.exception.connection)    # a first read is still tried once more
                self.assertEqual((r.bytes, r.requests), (sent, 1))
                guard.after_read.assert_called_once_with(sent, asked)
                self.assertEqual(r.finished, 0)

    def test_a_read_that_gets_all_it_asked_for_is_finished(self):
        n = 3 * CHUNK // 2      # two pieces
        for name, read in ReplyCheckTest.reads():
            with self.subTest(read=name):
                r = RangeReader("http://127.0.0.1:9/f.mkv", "t", opener=lambda request, timeout: Reply(0, n))
                self.assertEqual(read(r, 0, n), bytes(n))
                self.assertEqual((r.bytes, r.requests, r.finished), (n, 1, 1))


class ReplyCheckTest(unittest.TestCase):
    """A reply is checked the same way by get and stream: where its bytes start and its size;
    http.client's own errors are failed reads; the size tolerance is the reader's; and a large read
    is held once."""

    def setUp(self):
        self.srv = RangeServer(FIXTURES)
        self.link = self.srv.url("tiny.mkv")
        with open(TINY, "rb") as f:
            self.data = f.read()

    def tearDown(self):
        self.srv.close()

    @staticmethod
    def reads():
        return (("get", lambda r, start, n: r.get(start, n)),
                ("stream", lambda r, start, n: b"".join(r.stream(start, n))))

    def test_a_reply_that_starts_elsewhere_than_asked_is_refused(self):
        # A proxy answering whole 4096-byte blocks sent the bytes from 4096 for a read from 5000, taken as
        # the bytes from 5000
        self.srv.fault = "aligned"
        for name, read in self.reads():
            with self.subTest(read=name):
                with self.assertRaises(ReadFailed) as caught:
                    read(RangeReader(self.link, "t"), 5000, 64)
                self.assertEqual(str(caught.exception), "range differs")
                self.assertFalse(caught.exception.connection)
                self.assertEqual(read(RangeReader(self.link, "t"), 8192, 64), self.data[8192:8256])   # a block's own

    def test_a_reply_that_fails_its_checks_is_not_read(self):
        # Get read a reply's body (up to the length asked: 32 MiB for the Cues) and charged it to the guard
        # (the day's budget, the job's cap, the person's data) before it checked the status, range and size its
        # headers gave; stream() checked first
        size = len(self.data)
        for fault, start, why in (("aligned", 5000, "range differs"), ("ignore-range", 5000, "range ignored"),
                                  ("wrong-size", 0, "size differs")):
            self.srv.fault = fault
            for name, read in self.reads():
                with self.subTest(fault=fault, read=name):
                    guard = mock.Mock()
                    r = RangeReader(self.link, "t", guard=guard, size=size)
                    with self.assertRaises(ReadFailed) as caught:
                        read(r, start, 64 * 1024)
                    self.assertEqual(str(caught.exception), why)
                    self.assertEqual((r.bytes, r.requests, r.finished), (0, 1, 0))
                    guard.after_read.assert_called_once_with(0, 64 * 1024)

    def test_stream_checks_the_size_as_get_does(self):
        # Stream had its own copy of the checks, without the rule for a size not known yet (a Usenet
        # release) and without a whole file's Content-Length
        for name, read in self.reads():
            with self.subTest(read=name):
                with self.assertRaises(ReadFailed) as caught:
                    read(RangeReader(self.link, "t", size_ok=lambda total: False), 0, 64)
                self.assertEqual(str(caught.exception), "size differs")
                r = RangeReader(self.link, "t", size_ok=lambda total: total == len(self.data))
                self.assertEqual(read(r, 0, 64), self.data[:64])
                self.assertEqual(r.size, len(self.data))
        self.srv.fault = "ignore-range"            # the whole file, its size in Content-Length
        for name, read in self.reads():
            with self.subTest(read=name, whole=True):
                with self.assertRaises(ReadFailed) as caught:
                    read(RangeReader(self.link, "t", size=len(self.data) + 10_000), 0, 64)
                self.assertEqual(str(caught.exception), "size differs")

    def test_http_client_errors_are_failed_reads(self):
        # A chunked reply cut short (IncompleteRead) or a garbled status line (BadStatusLine) is no OSError:
        # it came out as itself, past the worker's back-off, its retry and the Usenet stop
        def cut(request, timeout):
            return Reply(0, 1000, fault=http.client.IncompleteRead(b"", 100))

        def garbled(request, timeout):
            raise http.client.BadStatusLine("HTTP/1.1 2OO OK")

        def malformed(request, timeout):
            raise http.client.InvalidURL("URL can't contain control characters")
        for opener, connection in ((cut, True), (garbled, True), (malformed, False)):
            for name, read in self.reads():
                with self.subTest(opener=opener.__name__, read=name):
                    guard = Guard()
                    with self.assertRaises(ReadFailed) as caught:
                        read(RangeReader("http://127.0.0.1:9/f.mkv", "t", guard, opener=opener), 0, 100)
                    self.assertIs(caught.exception.connection, connection)
                    self.assertEqual(guard.calls, 1)

    def test_a_garbled_reply_at_the_address_the_link_pointed_to_asks_the_link_again(self):
        # As for an address that doesn't answer
        opener = Garbled(self.link, moved=True)
        r = RangeReader(self.link, "t", opener=opener)
        self.assertEqual(r.get(0, 64), self.data[:64])
        self.assertEqual(r.get(64, 64), self.data[64:128])
        self.assertEqual(opener.link_calls, 2)

    def test_the_size_tolerance_is_the_readers(self):
        # The worker and the installer's test read each passed their own 0.0005; now both use the reader's
        listed = len(self.data) + int(len(self.data) * SIZE_TOLERANCE)
        r = RangeReader(self.link, "t", size=listed)
        self.assertEqual(r.get(0, 64), self.data[:64])
        self.assertEqual(r.size, len(self.data))
        with self.assertRaises(ReadFailed):
            RangeReader(self.link, "t", size=listed + 100).get(0, 64)

    def test_a_large_read_is_held_once(self):
        # Its pieces and their join were held together: twice the read (128 MiB for a 64 MiB moov)
        n = 8 << 20
        r = RangeReader("http://127.0.0.1:9/f.mp4", "t", opener=lambda request, timeout: Reply(0, n))
        with traced_memory() as traced:
            data = r.get(0, n)
            peak = traced()[1]
        self.assertEqual(len(data), n)
        self.assertLess(peak, n * 1.25)
