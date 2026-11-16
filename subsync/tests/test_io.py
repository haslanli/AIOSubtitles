"""Range reads and the Matroska index against a local range server; the pipeline on the tiny fixture."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import FIXTURES, RangeServer, TempDir, have  # noqa: E402
from subsync import pipeline  # noqa: E402
from subsync.mkv import MkvIndex, NotMatroska, oshash  # noqa: E402
from subsync.mp4 import Mp4Index, open_index  # noqa: E402
from subsync.reader import RangeReader, ReadFailed, Refused, Stopped  # noqa: E402
from subsync.subs import parse  # noqa: E402

TINY = os.path.join(FIXTURES, "tiny.mkv")


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

    def after_read(self, n):
        self.got += n


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

    @unittest.skipUnless(have("ffmpeg"), "ffmpeg not installed")
    def test_window_pcm_decodes(self):
        r = RangeReader(self.srv.url("tiny.mkv"), "test")
        idx = MkvIndex(r).read_head().read_cues()
        pcm = idx.window_pcm(30.0, 10.0)
        self.assertAlmostEqual(len(pcm) / 2 / 16000, 10.0, delta=0.1)


class PipeUntilTest(unittest.TestCase):
    """An audio window is fed to the decoder from one streamed read, and the read stops once the decoder's output holds
    the whole window (a Matroska window's byte range runs to the next seek point, which can be far past the window)."""

    def test_feeding_stops_once_the_output_is_complete(self):
        from subsync.mkv import pipe_until
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
        from subsync.mkv import pipe_until
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
        import array
        import statistics
        for name in ("tiny.mp4", "tiny-nosub.mp4"):
            idx = self.index(name)
            a, b = idx.byte_range(20.0, 10.0)
            self.assertLess(b - a, idx.size // 3)
            pcm = idx.window_pcm(20.0, 40.0)
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


@unittest.skipUnless(have(os.environ.get("SUBSYNC_ALASS", "alass")), "alass not installed")
class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.srv = RangeServer(FIXTURES)
        self.idx = MkvIndex(RangeReader(self.srv.url("tiny.mkv"), "test")).read_head().read_cues()

    def tearDown(self):
        self.srv.close()

    def run_one(self, name):
        text = read_text(name)
        return pipeline.sync_subtitle(self.idx, text, "srt", "eng", alass=os.environ.get("SUBSYNC_ALASS", "alass"),
                                      audio=False)

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
            r = pipeline.sync_subtitle(self.idx, text, "srt", "eng", alass=os.environ.get("SUBSYNC_ALASS", "alass"),
                                       audio=False)
            self.assertNotIn("alass_error", r)
            self.assertEqual(r["outcome"], "synced")

    def styled_offset(self):
        """tiny-offset.srt in another author's timing: lines up to 280 ms earlier or later, a fixed pattern."""
        pattern = (0, 120, -150, 260, -90, 30, -280, 180, 60, -210)
        lines, out, n = parse(read_text("tiny-offset.srt")), [], 0

        def stamp(x):
            ms = int(round(x * 1000))
            return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)
        for i, l in enumerate(lines):
            d = pattern[i % len(pattern)] / 1000
            n += 1
            out.append("%d\n%s --> %s\n%s\n" % (n, stamp(l["s"] + d), stamp(l["e"] + d), l["text"]))
        return "\n".join(out)

    def test_a_copy_clearly_better_than_the_original_is_offered_only_with_the_rule_on(self):
        text = self.styled_offset()
        kw = dict(alass=os.environ.get("SUBSYNC_ALASS", "alass"), audio=False, snap=False)
        off = pipeline.sync_subtitle(self.idx, text, "srt", "eng", **kw)
        on = pipeline.sync_subtitle(self.idx, text, "srt", "eng", better={}, **kw)
        self.assertFalse(off["gate_synced"]["pass"])
        self.assertEqual(off["outcome"], "keep_original")
        self.assertNotIn("better", off)
        self.assertEqual(on["outcome"], "synced", on.get("better"))
        self.assertEqual(on["reason"], "clearly better than the original")
        self.assertEqual(len(parse(on["body"])), len(parse(text)))

    def test_an_offset_subtitle_is_synced_against_an_mp4s_text_track(self):
        idx = open_index(RangeReader(self.srv.url("tiny.mp4"), "test"))
        r = pipeline.sync_subtitle(idx, read_text("tiny-offset.srt"), "srt", "eng",
                                   alass=os.environ.get("SUBSYNC_ALASS", "alass"), audio=False)
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
import random  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402

from subsync import imageref  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.store import Store  # noqa: E402
from subsync.subs import to_srt  # noqa: E402
from subsync.worker import Job, Worker  # noqa: E402

ALASS = os.environ.get("SUBSYNC_ALASS", "alass")


class FakeIndex:
    """An index with given tracks: no reads."""

    def __init__(self, text=(), image=()):
        self._t, self._i, self.size = list(text), list(image), 1

    def text_tracks(self):
        return self._t

    def image_tracks(self):
        return self._i


def realistic(n, seed, start=30.0):
    """(start, duration) lines with real subtitle gaps (2-frame gaps, short, mid and long pauses)."""
    r = random.Random(seed)
    t, out = start, []
    for _ in range(n):
        d = r.uniform(1.0, 4.5)
        out.append((t, d))
        x = r.random()
        t += d + (0.083 if x < 0.45 else r.uniform(0.25, 1) if x < 0.7 else r.uniform(1, 8) if x < 0.9 else r.uniform(8, 40))
    return out


def pgs_track(lines, number=7, lang="eng"):
    ev = []
    for s, d in lines:
        ev += [(s, 0.0), (s + d, 0.0)]
    return {"number": number, "language": lang, "codec": "S_HDMV/PGS", "name": "", "forced": False, "cues": ev}


def srt_of(lines, shift=0.0, jitter=0.0, seed=1):
    r = random.Random(seed)
    out = []
    for s, d in lines:
        e = max(-0.12, min(0.12, r.gauss(0, jitter))) if jitter else 0.0
        out.append({"s": s + shift + e, "e": s + d + shift + e, "text": "line", "kind": "dialogue"})
    return to_srt(out)


@unittest.skipUnless(have(ALASS), "alass not installed")
class ImageRoutePipelineTest(unittest.TestCase):
    def setUp(self):
        self.ref = realistic(300, 7)
        self.idx = FakeIndex(image=[pgs_track(self.ref)])

    def run_one(self, text, image_ref=True):
        return pipeline.sync_subtitle(self.idx, text, "srt", "eng", alass=ALASS, audio=False, image_ref=image_ref)

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


def back_to_back(n, seed, start=30.0):
    """Lines as a busy film has them: a fifth follow the previous one with no gap, so their PGS clears are left out and
    the image reference keeps only its sure starts (about half here; 73 % on Kingdom of Heaven's director's cut)."""
    r = random.Random(seed)
    t, out = start, []
    for _ in range(n):
        d = r.uniform(1.0, 4.5)
        out.append((t, d))
        x = r.random()
        t += d + (0.0 if x < 0.22 else 0.083 if x < 0.39 else r.uniform(0.25, 1) if x < 0.75 else
                  r.uniform(1, 8) if x < 0.93 else r.uniform(8, 40))
    return out


def pgs_left_out(lines, number=7):
    ev = []
    for k, (s, d) in enumerate(lines):
        ev.append((s, 0.0))
        if k + 1 == len(lines) or lines[k + 1][0] - (s + d) > 0.001:   # a back-to-back line replaces it: no clear
            ev.append((s + d, 0.0))
    return {"number": number, "language": "eng", "codec": "S_HDMV/PGS", "name": "", "forced": False, "cues": ev}


def translated(lines, seed, shift, cut=None, sigma=0.12, resplit=0.2, merge=0.25):
    """Another language's subtitle of the same film: a quarter of the line pairs merged into one line, each start in its
    author's timing style, a fifth split elsewhere (0.3 to 0.8 s off), all shifted; with `cut` (a, b) the lines of that
    stretch left out (a subtitle for another cut)."""
    r = random.Random(seed)
    out, k = [], 0
    while k < len(lines):
        s, d = lines[k]
        if k + 1 < len(lines) and r.random() < merge:
            d = lines[k + 1][0] + lines[k + 1][1] - s
            k += 1
        k += 1
        if cut and cut[0] <= s <= cut[1]:
            continue
        e = r.gauss(0, sigma)
        if r.random() < resplit:
            e += r.choice((-1, 1)) * r.uniform(0.3, 0.8)
        out.append({"s": s + shift + e, "e": s + d + shift + e, "text": "строка", "kind": "dialogue"})
    return to_srt(out)


@unittest.skipUnless(have(ALASS), "alass not installed")
class ImageTranslationTest(unittest.TestCase):
    """Kingdom of Heaven's director's cut has only image tracks; its Russian director's-cut subtitles were
    all "below the bar" because the gate expected every line to pair, while the track pins only its sure starts. With the
    gate scaled to them, a translation for this cut passes; one for another cut stays refused by its missing scene."""

    def setUp(self):
        self.ref = back_to_back(1100, 31)
        self.idx = FakeIndex(image=[pgs_left_out(self.ref)])

    def run_one(self, text):
        return pipeline.sync_subtitle(self.idx, text, "srt", "rus", alass=ALASS, audio=False, image_ref=True,
                                      better={"paired": 0.85, "median_ms": 200, "ratio": 0.5})

    def test_the_track_pins_only_its_sure_starts(self):
        share = imageref.reference(self.idx.image_tracks()[0])["stats"]["sure_share"]
        self.assertLess(share, 0.85)

    def test_a_translation_for_this_cut_passes(self):
        for seed in (4, 5, 6):
            r = self.run_one(translated(self.ref, seed, shift=3.4))
            self.assertEqual((r["route"], r["outcome"]), ("image", "synced"), (seed, r.get("gate_synced")))
            self.assertIn("aligned to another language", r["reason"])   # Russian against the English image track

    def test_a_translation_for_another_cut_is_kept_original(self):
        mid = self.ref[len(self.ref) // 2][0]
        r = self.run_one(translated(self.ref, 4, shift=3.4, cut=(mid, mid + 420.0)))
        self.assertEqual((r["route"], r["outcome"]), ("image", "keep_original"))
        self.assertTrue(r["gate_synced"]["missing_scene"])


@unittest.skipUnless(have(ALASS), "alass not installed")
class WorkerChoiceTest(unittest.TestCase):
    """The order subtitles are tried in, and an in-sync original further down listed unchanged."""

    def setUp(self):
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
        store = Store(os.path.join(self.dir, "store"))
        base = {"SUBSYNC_ALASS": ALASS, "SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_MAX_TRIES": "3", "SUBSYNC_RANK_BY_RELEASE": "off",
                "SUBSYNC_LIST_IN_SYNC": "off", "SUBSYNC_FAST_PATH": "off", "SUBSYNC_PARALLEL_LANGUAGES": "off",
                "SUBSYNC_IMAGE_REF": "off", "SUBSYNC_BEST_FIT": "off"}   # every switch stated, so each test turns on only what it tests
        s = Settings(dict(base, **env))
        return Worker(s, store, None, None), store

    def sync(self, w, filename="[GroupX] Movie.mkv"):
        return w._sync_language(Job("movie", "tt1"), self.idx, "f-1", "eng", self.cands, "abcd", filename)

    def test_viewer_order_stops_at_the_first_pass(self):
        w, store = self.worker()
        key = self.sync(w)
        r = store.get(key)
        self.assertEqual(r["candidate_id"], "os-offset")      # the broken one is rejected early, the next is synced
        self.assertEqual(store.get(key)["outcome"], "synced")
        self.assertIn("broken timing", next(x for x in store.db.execute("SELECT reason FROM results WHERE candidate_id='os-broken'"))[0])

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


@unittest.skipUnless(have(ALASS), "alass not installed")
class BestFitTest(WorkerChoiceTest):
    """Every candidate is judged and the best fit is offered first, not the first that passes; an in-sync
    original (a subtitle made for this release) beats any re-timed copy."""
    test_viewer_order_stops_at_the_first_pass = test_release_match_first_and_in_sync_listed_unchanged = None
    test_in_sync_further_down_lists_nothing_by_default = test_switching_list_in_sync_on_lists_a_stored_in_sync_result = None

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
        w._publish = lambda alias, lang, key, final=False: seen.append((key, final))
        best = self.sync(w)
        self.assertEqual([store.get(k)["candidate_id"] for k, _ in seen], ["os-offset", "os-insync"])
        self.assertEqual(seen[-1], (best, False))                 # the final publish is the job's, after every language


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

@unittest.skipUnless(have(ALASS), "alass not installed")
class FrameRateTest(unittest.TestCase):
    """A subtitle timed for 25 fps on a 23.976 fps release is corrected on the text route."""

    def test_a_pal_subtitle_is_synced(self):
        ref = realistic(300, 9)
        idx = FakeIndex(text=[{"number": 3, "language": "eng", "codec": "S_TEXT/UTF8", "name": "", "forced": False,
                               "cues": ref}])
        k = 23.976 / 25
        pal = to_srt([{"s": s * k + 1.5, "e": (s + d) * k + 1.5, "text": "line", "kind": "dialogue"} for s, d in ref])
        r = pipeline.sync_subtitle(idx, pal, "srt", "eng", alass=ALASS, audio=False, better={})
        self.assertEqual(r["outcome"], "synced", (r.get("reason"), r.get("gate_synced")))
        out = parse(r["body"])
        err = sorted(abs(o["s"] - s) for o, (s, _) in zip(out, ref))
        self.assertLess(err[len(err) // 2], 0.1)


# -- fast path, languages in parallel, read caps ---------------------------------------------------------------------
from subsync.worker import JobGuard, Safety  # noqa: E402
from subsync.reader import Stopped as GuardStopped  # noqa: E402


class FastPathTest(WorkerChoiceTest):
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

    def test_none_in_sync_falls_back_to_re_timing(self):
        self.cands = self.cands[:2]                             # broken + offset: no original is in sync
        w, store = self.worker(SUBSYNC_FAST_PATH="on", SUBSYNC_LIST_IN_SYNC="on")
        key = self.sync(w)
        r = store.get(key)
        self.assertEqual((r["candidate_id"], r["outcome"]), ("os-offset", "synced"))
        self.assertFalse(r["detail"].get("unchanged"))

    def test_no_reference_and_audio_route_off_stores_nothing(self):
        # no text or image track and the audio route off: nothing is stored, so turning the route on later syncs it
        w, store = self.worker(SUBSYNC_FAST_PATH="on", SUBSYNC_AUDIO_ROUTE="off")
        self.idx = FakeIndex()
        self.cands = [self.cands[1]]
        self.assertIsNone(w._sync_language(Job("movie", "tt1"), self.idx, "f-1", "eng", self.cands, "abcd", None))
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)


@unittest.skipUnless(have(ALASS), "alass not installed")
class MagnetTest(WorkerChoiceTest):
    """An original in sync as it is still starts its lines a little off the release's own (Patriot S01E01's English:
    43 ms early throughout; Burning: an author's stray line). Like AutoSync's "magnet", each line is snapped to the release
    line it clearly belongs to, and that copy is listed in place of the unchanged original."""
    test_viewer_order_stops_at_the_first_pass = test_release_match_first_and_in_sync_listed_unchanged = None
    test_in_sync_further_down_lists_nothing_by_default = test_switching_list_in_sync_on_lists_a_stored_in_sync_result = None

    def early(self):
        return srt_of(self.ref, shift=-0.043)

    def assert_on_the_release_lines(self, body):
        starts = [l["s"] for l in parse(body)]
        self.assertEqual(len(starts), len(self.ref))
        self.assertLess(max(abs(s - r) for s, (r, _) in zip(starts, self.ref)), 0.002)

    def test_a_steady_small_offset_is_snapped_away(self):
        chk = pipeline.check_original(self.idx, self.early(), "eng", snap_within=0.95)
        self.assertTrue(chk["in_sync"])
        self.assert_on_the_release_lines(chk["body"])
        self.assertGreater(chk["magnet"]["snapped"], 0.9 * len(self.ref))

    def test_a_rip_of_the_release_track_is_left_as_it_is(self):
        chk = pipeline.check_original(self.idx, srt_of(self.ref), "eng", snap_within=0.95)
        self.assertTrue(chk["in_sync"])
        self.assertNotIn("body", chk)

    def test_without_the_snap_bar_nothing_is_snapped(self):
        chk = pipeline.check_original(self.idx, self.early(), "eng")
        self.assertTrue(chk["in_sync"])
        self.assertNotIn("body", chk)

    def test_the_re_timing_route_snaps_an_in_sync_original_too(self):
        r = pipeline.sync_subtitle(self.idx, self.early(), "srt", "eng", alass=ALASS, audio=False)
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
    BETTER = {"paired": 0.85, "median_ms": 200, "ratio": 0.5, "cross_median_ms": 300, "cross_paired": 0.75}

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
        chk = pipeline.check_original(self.idx, text, "rus", snap_within=0.95, better=self.BETTER)
        self.assertTrue(chk["in_sync"], chk["gate_original"])
        self.assertTrue(chk["cross"])                                  # recorded as judged against another language
        self.assertLess(self.error_ms(chk["body"]), self.error_ms(text))

    @unittest.skipUnless(have(ALASS), "alass not installed")
    def test_an_offset_translation_is_aligned_and_snapped(self):
        text = translated(self.ref, 6, shift=3.4, sigma=0.25, resplit=0.1)
        r = pipeline.sync_subtitle(self.idx, text, "srt", "rus", alass=ALASS, audio=False, better=self.BETTER)
        self.assertEqual(r["outcome"], "synced", (r.get("reason"), r.get("gate_synced")))
        self.assertIn("another language", r["reason"])
        self.assertLess(self.error_ms(r["body"]), 120)

    @unittest.skipUnless(have(ALASS), "alass not installed")
    def test_a_loosely_timed_translation_is_aligned_too(self):
        # Breaking Bad S01E02's Russian: alass aligns it (86-94 % paired) but its timing style leaves ~250 ms median, and some
        # tenths of the runtime have under half their lines within 500 ms; every tenth still pairs
        text = translated(self.ref, 8, shift=2.2, sigma=0.32, resplit=0.15)
        r = pipeline.sync_subtitle(self.idx, text, "srt", "rus", alass=ALASS, audio=False, better=self.BETTER)
        self.assertEqual(r["outcome"], "synced", (r.get("reason"), r.get("gate_synced"), r.get("aligned")))

    @unittest.skipUnless(have(ALASS), "alass not installed")
    def test_another_films_translation_is_kept(self):
        text = translated(realistic(600, 77), 6, shift=3.4)
        r = pipeline.sync_subtitle(self.idx, text, "srt", "rus", alass=ALASS, audio=False, better=self.BETTER)
        self.assertEqual(r["outcome"], "keep_original")

    @unittest.skipUnless(have(ALASS), "alass not installed")
    def test_another_cuts_translation_is_kept(self):
        mid = self.ref[len(self.ref) // 2][0]
        text = translated(self.ref, 6, shift=3.4, sigma=0.25, resplit=0.1, cut=(mid, mid + 400))
        r = pipeline.sync_subtitle(self.idx, text, "srt", "rus", alass=ALASS, audio=False, better=self.BETTER)
        self.assertEqual(r["outcome"], "keep_original")

    def test_without_the_rule_a_translation_is_judged_as_before(self):
        text = translated(self.ref, 5, shift=0.0, sigma=0.15, resplit=0.1)
        self.assertFalse(pipeline.check_original(self.idx, text, "rus")["in_sync"])

    def test_english_is_the_preferred_other_language(self):
        pt = {"number": 6, "language": "pt", "codec": "S_TEXT/ASS", "name": "", "forced": False, "cues": realistic(620, 9)}
        idx = FakeIndex(text=[pt, self.idx.text_tracks()[0]])
        self.assertEqual(pipeline.choose_reference(idx, 500, "rus")["number"], 3)
        self.assertEqual(pipeline.choose_reference(idx, 500, "rus", fallback=())["number"], 6)


class HasReferenceTest(unittest.TestCase):
    """A file with no text or image track a subtitle could be judged against hands over to the audio route at once."""

    def test_tracks(self):
        from subsync.store import Store
        with TempDir() as d:
            w = Worker(Settings({"SUBSYNC_DATA": d, "SUBSYNC_IMAGE_REF": "on"}), Store(d), None, None)
            self.assertFalse(w._has_reference(FakeIndex()))
            lines = realistic(300, 4)
            self.assertTrue(w._has_reference(FakeIndex(image=[pgs_track(lines)])))
            text = {"number": 3, "language": "eng", "codec": "S_TEXT/UTF8", "name": "", "forced": False,
                    "cues": [(s, d) for s, d in lines]}
            self.assertTrue(w._has_reference(FakeIndex(text=[text])))
            self.assertFalse(w._has_reference(FakeIndex(text=[dict(text, forced=True)])))
            self.assertFalse(w._has_reference(object()))                          # not an index this module reads


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
                                  gate, recall, snap=False, better={"paired": 0.85, "median_ms": 200, "ratio": 0.5})
        self.assertEqual(out["outcome"], "synced")
        self.assertLess(abs(out["gate_synced"]["signed_median_ms"]), 15)
        self.assertGreater(abs(out["recentred_ms"]), 40)


class StreamedJudgingTest(WorkerChoiceTest):
    """With best fit, each download is judged as it arrives: Kingdom of Heaven's first passes (one shift, 0.1 s each) came
    18 s after the request because 20 downloads per language were waited for first, and the pick's 14 s ran out."""
    test_viewer_order_stops_at_the_first_pass = test_release_match_first_and_in_sync_listed_unchanged = None
    test_in_sync_further_down_lists_nothing_by_default = test_switching_list_in_sync_on_lists_a_stored_in_sync_result = None

    def test_a_pass_is_published_before_a_slow_download_ends(self):
        w, store = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_FAST_PATH="on", SUBSYNC_LIST_IN_SYNC="on")
        fetch = w._fetch_one
        slow = {"os-broken"}

        def fetch_one(job, lang, rank, cand):
            if cand["id"] in slow:
                time.sleep(1.5)
            return fetch(job, lang, rank, cand)
        w._fetch_one = fetch_one
        t0, seen = time.time(), []
        w._publish = lambda alias, lang, key, final=False: seen.append(time.time() - t0)
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
        t0 = time.time()
        stream.close()                                  # judging raised: the 8 downloads not started are dropped
        self.assertLess(time.time() - t0, 0.3)


class ShiftPathTest(WorkerChoiceTest):
    """A candidate off by one steady offset is synced by that shift at once, without alass (Finding Nemo's first download was
    an original because alass judged one candidate at a time and the right ones came 26 s after the request)."""
    test_viewer_order_stops_at_the_first_pass = test_release_match_first_and_in_sync_listed_unchanged = None
    test_in_sync_further_down_lists_nothing_by_default = test_switching_list_in_sync_on_lists_a_stored_in_sync_result = None

    def test_the_shift_alone_syncs_it(self):
        r = pipeline.shift_sync(self.idx, srt_of(self.ref, shift=-1.43, jitter=0.05), "srt", "eng")
        self.assertEqual(r["outcome"], "synced")
        self.assertIn("one shift", r["reason"])
        starts = [l["s"] for l in parse(r["body"])]
        self.assertLess(statistics.median(abs(a - b) for a, (b, _) in zip(starts, self.ref)), 0.06)

    def test_another_films_subtitle_is_not_shifted_into_place(self):
        r = pipeline.shift_sync(self.idx, srt_of(realistic(200, 99)), "srt", "eng")
        self.assertEqual(r["outcome"], "keep_original")

    def test_another_cut_is_not_shifted_into_place(self):
        cut = [c for c in self.ref if not 500 <= c[0] <= 800]
        r = pipeline.shift_sync(self.idx, srt_of(cut, shift=-2.0), "srt", "eng")
        self.assertNotEqual(r["outcome"], "synced")

    def test_the_worker_needs_no_alass_for_it(self):
        self.cands = [self.cands[1]]                                  # the offset one only
        w, store = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_FAST_PATH="on", SUBSYNC_ALASS="/nonexistent/alass")
        key = self.sync(w)
        r = store.get(key)
        self.assertEqual((r["candidate_id"], r["outcome"]), ("os-offset", "synced"))
        self.assertTrue(r["detail"].get("shift_fit"))

    def test_off_means_alass_as_before(self):
        self.cands = [self.cands[1]]
        w, store = self.worker(SUBSYNC_BEST_FIT="on", SUBSYNC_FAST_PATH="on", SUBSYNC_SHIFT_PATH="off")
        r = store.get(self.sync(w))
        self.assertFalse(r["detail"].get("shift_fit"))


class OwnTrackTest(WorkerChoiceTest):
    """A candidate with more than twice the lines of the file's own track finds no reference track, but the
    file has one: the candidate is kept, never sent to the audio route (Inception: 3,753 lines against 1,738; the audio
    route started and read 979 MB). Only a file with no usable text or image track needs the audio route."""
    test_viewer_order_stops_at_the_first_pass = test_release_match_first_and_in_sync_listed_unchanged = None
    test_in_sync_further_down_lists_nothing_by_default = test_switching_list_in_sync_on_lists_a_stored_in_sync_result = None

    def long_text(self):
        return srt_of(realistic(450, 5))                      # 2.25 times the 200 lines of the file's track

    def test_a_far_longer_candidate_is_kept_without_the_audio_route(self):
        r = pipeline.sync_subtitle(self.idx, self.long_text(), "srt", "eng", alass=ALASS, audio=True, vad=object())
        self.assertEqual((r["outcome"], r["route"]), ("keep_original", None))
        self.assertIn("more than twice the lines of the file's own track", r["reason"])

    def test_a_track_of_a_few_lines_is_not_the_files_own(self):
        # a signs-only track (30 lines) against a full subtitle: the file still needs the audio route
        idx = FakeIndex(text=[{"number": 3, "language": "eng", "codec": "S_TEXT/UTF8", "name": "Signs", "forced": False,
                               "cues": self.ref[:30]}])
        r = pipeline.sync_subtitle(idx, srt_of(self.ref), "srt", "eng", alass=ALASS, audio=False)
        self.assertIn("audio route is off", r["reason"])

    @unittest.skipUnless(have(ALASS), "alass not installed")
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


class ParallelLanguagesTest(unittest.TestCase):
    """process() with both languages at once: each is published, results as without parallelism."""

    def setUp(self):
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

    @unittest.skipUnless(have(ALASS), "alass not installed")
    def test_both_languages_published(self):
        store = Store(os.path.join(self.dir, "store"))
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


class PlayedFileHashTest(ParallelLanguagesTest):
    """A request that names the played file's hash (Stremio sends it) is checked against the file read; a
    listed row that turns out to be another file is not synced, so its copies never reach this file."""
    test_both_languages_published = None

    def run_job(self, video_hash):
        store = Store(os.path.join(self.dir, "store"))
        s = Settings({"SUBSYNC_ALASS": ALASS, "SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_LANGUAGES": "eng,rus"})
        w = Worker(s, store, self.aios, Safety())
        job = Job("movie", "tt1", {"filename": "tiny.mkv", "videoHash": video_hash})
        w.process(job)
        return w, store

    def test_another_file_is_not_synced(self):
        w, store = self.run_job("0123456789abcdef")
        self.assertEqual(store.db.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)
        self.assertTrue(w.recently_unlisted("name:tiny.mkv|None"))   # the listing has no such file: not looked for again

    @unittest.skipUnless(have(ALASS), "alass not installed")
    def test_the_played_file_is_synced(self):
        with open(TINY, "rb") as f:
            data = f.read()
        w, store = self.run_job(oshash(len(data), data[:65536], data[-65536:]).upper())
        self.assertGreater(store.db.execute("SELECT COUNT(*) FROM results").fetchone()[0], 0)


class GuardCapTest(unittest.TestCase):
    def test_a_job_stops_at_its_byte_cap(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            JobGuard(Safety(), store, 10 ** 9, 1000).before_read(500)
            g = JobGuard(Safety(), store, 10 ** 9, 1000)
            g.after_read(900)
            with self.assertRaises(GuardStopped):
                g.before_read(500)                               # over the job's cap
            self.assertEqual(Settings({}).index_cap, 32 * 1024 * 1024)
            self.assertFalse(Settings({}).audio_route)           # off unless asked for

    def test_only_the_jobs_own_cap_is_a_cap_reached(self):
        # A stop at the job's own byte cap says which cap; the day's budget is a plain stop
        from subsync.reader import CapReached
        self.assertTrue(issubclass(CapReached, GuardStopped))
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            g = JobGuard(Safety(), store, 10 ** 9, 1000)
            g.after_read(900)
            with self.assertRaises(CapReached) as caught:
                g.before_read(500)
            self.assertEqual((caught.exception.cap, str(caught.exception)), (1000, "file byte cap"))
            budget = JobGuard(Safety(), store, 1000, 10 ** 9)
            with self.assertRaises(GuardStopped) as caught:
                budget.before_read(1001)
            self.assertNotIsInstance(caught.exception, CapReached)


class CountedLimits:
    """A person's limits that allow everything and count the files started."""
    who = None

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

    def after_read(self, n):
        pass


def run_once(w, job):
    """The job through one turn of the worker's loop, in this thread: what _loop does with how it ends (queued again
    or not), then the loop stops."""
    w.submit(job)
    real = w.process

    def process(j):
        try:
            real(j)
        finally:
            w.stop_flag = True
    w.process = process
    try:
        w._loop("main")
    finally:
        w.process, w.stop_flag = real, False


class CapReachedTest(unittest.TestCase):
    """A file whose index is larger than the cap stops there for good under that cap: the job isn't
    queued again, a new request reads nothing of the file and counts no file, its subtitle list and originals are still
    set, and no "(Subsync)" entry is listed for it; a raised cap reads it again. Other stops are tried again as before."""
    CAP_MB = str(200_000 / 2 ** 20)   # the start of tiny.mkv (188,024 bytes) fits; its index and hash reads don't

    def setUp(self):
        from unittest import mock
        from subsync import sources
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
        self.store = Store(os.path.join(self.dir, "store"))
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
        from subsync.addon import App
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

    def test_a_raised_cap_reads_it_again(self):
        run_once(self.worker(), self.job)
        read = len(self.srv.log)
        w = self.worker(SUBSYNC_INDEX_CAP_MB="32")
        run_once(w, Job("movie", "tt1", dict(self.job.hints)))
        self.assertGreater(len(self.srv.log), read)
        self.assertIsNotNone(self.store.resolve(self.job.alias))  # its index was read
        self.assertEqual(w.limits.started, 1)

    def test_an_audio_job_stopped_at_its_audio_cap_leaves_the_index_readable(self):
        # The audio route's own cap (SUBSYNC_FILE_CAP_GB) says nothing about the index: the file isn't marked
        # over the cap, so its next index pass reads it; it waits for another pick, as when the person's day ran out
        from subsync.reader import CapReached
        w = self.worker(SUBSYNC_AUDIO_ROUTE="on")
        audio = Job("movie", "tt1", dict(self.job.hints))
        audio.audio = True

        def stopped(job):
            w.stop_flag = True                                   # one turn of the audio lane, then the loop ends
            raise CapReached(w._cap(job))
        w.process = stopped
        w.submit(audio)
        w._loop("audio")
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


# -- a first read that can't reach the file ------------------------------------------------------------
import urllib.error  # noqa: E402
from unittest import mock  # noqa: E402

from subsync import realdebrid, sources  # noqa: E402
from subsync.reader import MAX_RE_RESOLVE  # noqa: E402


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
        self.dir = tempfile.mkdtemp(prefix="subsync-test-")
        shutil.copy(TINY, self.dir)
        self.srv = RangeServer(self.dir)
        self.link = self.srv.url("tiny.mkv")
        self.size = os.path.getsize(TINY)
        sized = mock.patch.object(sources, "MIN_VIDEO_BYTES", 1)    # the listing's size is the file's
        sized.start()
        self.addCleanup(sized.stop)
        waits = mock.patch("subsync.worker.time.sleep")             # the wait is recorded, not waited
        self.sleep = waits.start()
        self.addCleanup(waits.stop)
        link = self.link

        class Aios:
            def find_row(self, *a):
                return {"url": link}

            def subtitles(self, *a):
                return [{"id": "e1", "url": link + ".srt", "lang": "eng", "release": ""}]
        self.store = Store(os.path.join(self.dir, "store"))
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
            self.w.usenet_gate = mock.Mock(stop=stopped.append)
            self.job.usenet = True
            self.w._process_row(self.job, dict(self.job.hints), {"url": self.link}, None)
            return stopped

    def backed_off_for(self):
        row = self.store.db.execute("SELECT retry_at FROM files WHERE file_key=?", (self.job.alias,)).fetchone()
        return None if row is None else round(row[0] - time.time(), -1)

    def test_one_dropped_connection_is_tried_again_and_the_file_read_in_the_same_job(self):
        from subsync.worker import READ_RETRY_S
        flaky = Flaky(self.link, faults=1)
        self.run_with(flaky)
        self.assertEqual(READ_RETRY_S, 10)
        self.sleep.assert_called_once_with(READ_RETRY_S)
        self.assertEqual(self.srv.log[0], (0, self.size))          # the start, read on the second try
        self.assertIsNotNone(self.store.resolve(self.job.alias))   # its index was read
        self.assertFalse(self.store.file_blocked(self.job.alias))

    def test_two_dropped_connections_back_the_file_off_for_ten_minutes(self):
        flaky = Flaky(self.link, faults=2)
        self.run_with(flaky)
        self.sleep.assert_called_once()
        self.assertEqual(flaky.link_calls, 2)
        self.assertTrue(self.store.file_blocked(self.job.alias))
        self.assertEqual(self.backed_off_for(), 600)

    def test_a_refusal_is_not_tried_again(self):
        self.srv.fault = "403"
        self.run_with()
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
        flaky = Flaky(self.link, faults=1, moved=True)       # then every address it points to drops the connection
        self.run_with(flaky)
        self.sleep.assert_called_once()
        self.assertEqual(flaky.link_calls, 1 + MAX_RE_RESOLVE)
        self.assertTrue(self.store.file_blocked(self.job.alias))

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

    def test_reads_the_index_within_the_cap(self):
        from subsync.selftest import run
        from subsync.settings import Settings
        out = run(self.srv.url("tiny.mkv"), 4 * 1024 * 1024, Settings({}))
        self.assertTrue(out["ok"] and out["matroska"], out)
        self.assertEqual(out["size"], os.path.getsize(os.path.join(FIXTURES, "tiny.mkv")))
        self.assertTrue(out["text_tracks"])
        self.assertLessEqual(out["bytes"], 4 * 1024 * 1024)
        self.assertNotIn("url", json.dumps(out).lower().replace("ok", ""))

    def test_refusal_and_cap_are_reported(self):
        from subsync.selftest import run
        from subsync.settings import Settings
        self.srv.fault = "403"
        self.assertEqual(run(self.srv.url("tiny.mkv"), 4 * 1024 * 1024, Settings({}))["error"], "refused")
        self.srv.fault = None
        self.assertEqual(run(self.srv.url("tiny.mkv"), 1000, Settings({}))["error"], "cap")

    def test_the_cap_is_subsync_s_own_index_cap_unless_given(self):
        # The wizard's 4 MB cap stopped a 7.3 MB index that subsync itself reads (16 MB then, 32 MB)
        from subsync.selftest import run
        from subsync.settings import Settings
        out = run(self.srv.url("tiny.mkv"), None, Settings({}))
        self.assertTrue(out["ok"], out)
        out = run(self.srv.url("tiny.mkv"), None, Settings({"SUBSYNC_INDEX_CAP_MB": "0.001"}))
        self.assertEqual(out["error"], "cap")
        self.assertIn("larger than subsync reads", out["detail"])

    def test_a_listed_size_off_by_rounding_is_accepted(self):
        from subsync.selftest import run
        from subsync.settings import Settings
        real = os.path.getsize(os.path.join(FIXTURES, "tiny.mkv"))
        self.assertTrue(run(self.srv.url("tiny.mkv"), None, Settings({}), size=real + 1000)["ok"])

    def test_the_container_is_named(self):
        from subsync.selftest import run
        from subsync.settings import Settings
        self.assertEqual(run(self.srv.url("tiny.mkv"), None, Settings({}))["container"], "mkv")
        out = run(self.srv.url("tiny.mp4"), None, Settings({}))
        self.assertEqual((out["container"], out["matroska"]), ("mp4", False))
        self.assertTrue(out["text_tracks"])

    def test_rewrite_applies(self):
        from subsync.selftest import run
        from subsync.settings import Settings
        s = Settings({"SUBSYNC_URL_REWRITE": f"https://public.example=http://127.0.0.1:{self.srv.port}"})
        self.assertTrue(run("https://public.example/tiny.mkv", 4 * 1024 * 1024, s)["ok"])
