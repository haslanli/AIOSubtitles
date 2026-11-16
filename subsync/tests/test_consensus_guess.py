"""The first guess for a file with no subtitle track of its own: a guess by name that sits seconds away from what most of
the language's subtitles agree on is replaced by that agreement (Primer's YTS MP4: the guess was 9.3 s off while eight
other English subtitles agreed within 100 ms, and AutoSync's first was right)."""
import os
import unittest

from helpers import FakeDownloads, RangeServer, TempDir
from subsync import pipeline
from subsync.settings import Settings
from subsync.store import Store
from subsync.worker import Job, Safety, Worker
from test_io import realistic, srt_of, translated

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class ConsensusGuessTest(unittest.TestCase):
    def setUp(self):
        self.ref = realistic(400, 41)
        self.agreeing = [srt_of(self.ref, jitter=0.04, seed=k) for k in range(6)]

    def test_a_guess_seconds_away_from_a_large_agreement_is_replaced(self):
        guess = srt_of(self.ref, shift=9.3, jitter=0.04, seed=99)
        texts = [srt_of(realistic(400, 7))] + self.agreeing
        got = pipeline.consensus_guess(guess, texts)
        self.assertEqual(got["index"], 1)                      # the first of the agreement, in try order
        self.assertGreaterEqual(got["group"], pipeline.CONSENSUS_MIN)
        self.assertAlmostEqual(abs(got["offset_s"]), 9.3, delta=0.2)

    def test_a_small_agreement_replaces_nothing(self):
        guess = srt_of(self.ref, shift=2.4, jitter=0.04, seed=99)
        self.assertIsNone(pipeline.consensus_guess(guess, self.agreeing[:2] + [srt_of(realistic(400, 8))]))

    def test_a_guess_a_little_off_another_split_is_kept(self):
        # Leviathan: the guess pairs with none of them at 60 % (another split of the lines) but sits only 130 ms off
        guess = translated(self.ref, 3, shift=0.13, sigma=0.05)
        self.assertIsNone(pipeline.consensus_guess(guess, self.agreeing))

    def test_a_guess_inside_the_agreement_is_kept(self):
        self.assertIsNone(pipeline.consensus_guess(srt_of(self.ref, jitter=0.04, seed=50), self.agreeing))

    def test_a_guess_that_lines_up_with_nothing_is_kept(self):
        self.assertIsNone(pipeline.consensus_guess(srt_of(realistic(400, 77)), self.agreeing))


class WorkerConsensusGuessTest(unittest.TestCase):
    """The first pass of a file with no track downloads the main language's first subtitles beside its guess, and a
    consensus replaces the guess before the other languages are lined up with it."""

    def setUp(self):
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)
        self.srv = RangeServer(FIXTURES)
        self.addCleanup(self.srv.close)
        self.size = os.path.getsize(os.path.join(FIXTURES, "tiny-nosub.mp4"))
        self.hints = {"filename": "tiny-nosub.mp4", "videoSize": self.size}
        ref = realistic(300, 43)
        files, subs = {}, []
        far = "http://subs.test/far.srt"
        files[far] = srt_of(ref, shift=9.3, jitter=0.04, seed=1)
        subs.append({"id": "far", "lang": "eng", "url": far, "release": "tiny-nosub"})     # the guess by name
        for k in range(6):
            url = f"http://subs.test/e{k}.srt"
            files[url] = srt_of(ref, jitter=0.04, seed=10 + k)
            subs.append({"id": f"e{k}", "lang": "eng", "url": url, "release": f"Other.Release.{k}"})
        rus = "http://subs.test/r.srt"
        files[rus] = translated(ref, 5, shift=4.0, sigma=0.05)
        subs.append({"id": "r", "lang": "rus", "url": rus, "release": ""})
        self.ref, self.downloads = ref, FakeDownloads(files)
        srv = self.srv

        class Listing:
            def find_row(self, *a):
                return {"url": srv.url("tiny-nosub.mp4")}

            def subtitles(self, *a):
                return [dict(c) for c in subs]
        self.listing = Listing()

    def worker(self):
        s = Settings({"SUBSYNC_DATA": self.dir, "SUBSYNC_AUDIO_ROUTE": "on", "SUBSYNC_LANGUAGES": "eng,rus",
                      "SUBSYNC_EXTRA_SOURCES": ""})
        return Worker(s, Store(self.dir), self.listing, Safety())

    def test_the_consensus_is_served_and_anchors_the_other_languages(self):
        w = self.worker()
        job = Job("movie", "tt1", self.hints)
        with self.downloads.patch():
            w.process(job)
        self.assertEqual(w.originals[job.alias]["eng"]["id"], "e0")
        from subsync.subs import parse
        starts = sorted(l["s"] for l in parse(w.provisional[job.alias]["rus"]))
        refs = [s for s, _ in self.ref]
        errs = sorted(min(abs(s - r) for r in refs) for s in starts)
        self.assertLess(errs[len(errs) // 2], 0.3)             # lined up with the agreement, not with the 9.3 s guess


if __name__ == "__main__":
    unittest.main()
