"""Range reads and the Matroska index against a local range server; the pipeline on the tiny fixture."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import FIXTURES, RangeServer, have  # noqa: E402
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
import shutil  # noqa: E402
import tempfile  # noqa: E402

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
                "SUBSYNC_IMAGE_REF": "off"}   # every switch stated, so each test turns on only what it tests
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
        self.assertIsNone(w.done[job.alias]["rus"])              # the Russian original is in sync: nothing to list


class GuardCapTest(unittest.TestCase):
    def test_a_job_stops_at_its_byte_cap(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d)
            JobGuard(Safety(), store, 10 ** 9, 1000).before_read(500)
            g = JobGuard(Safety(), store, 10 ** 9, 1000)
            g.after_read(900)
            with self.assertRaises(GuardStopped):
                g.before_read(500)                               # over the job's cap
            self.assertEqual(Settings({}).index_cap, 16 * 1024 * 1024)
            self.assertFalse(Settings({}).audio_route)           # off unless asked for


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

    def test_rewrite_applies(self):
        from subsync.selftest import run
        from subsync.settings import Settings
        s = Settings({"SUBSYNC_URL_REWRITE": f"https://public.example=http://127.0.0.1:{self.srv.port}"})
        self.assertTrue(run("https://public.example/tiny.mkv", 4 * 1024 * 1024, s)["ok"])
