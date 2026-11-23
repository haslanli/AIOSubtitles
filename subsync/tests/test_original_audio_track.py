"""The audio route on a file with no usable subtitle track fits the sound in the title's original language first (a
commentary or an audio description never fitted, nor counted), then the default track, then the second track in another
language as before; an untagged track counts only for an English original and only after a tagged one; without an
original language the order is exactly as before."""
import itertools
import random
import threading
import unittest

from helpers import AUDIO_BETTER, FIXTURES, rhythm, timeline_fit
from subsync import audiofit
from subsync.langs import lang3
from subsync.mp4 import open_index
from subsync.worker import audio_facts


def track(i, lang, default=False, name="", tagged=True):
    return {"index": i, "language": lang, "default": default, "name": name, "tagged": tagged}


class Index:
    def __init__(self, tracks, original=None, end=600.0):
        self.tracks, self.end, self.size = tracks, end, 600_000_000
        if original:
            self.original_language = original

    def audio_tracks(self):
        return [dict(t) for t in self.tracks]

    def duration(self):
        return self.end


class OrderTest(unittest.TestCase):
    def order(self, tracks, original):
        return [t["index"] for t in audiofit.track_order(Index(tracks), original)]

    def test_the_original_language_first(self):
        haibane = [track(0, "eng", True), track(1, "jpn")]          # an English dub as the default
        self.assertEqual(self.order(haibane, "jpn"), [1, 0])
        self.assertEqual(self.order([track(0, "rus", True), track(1, "ukr"), track(2, "eng")], "eng"), [2, 0, 1])
        self.assertEqual(self.order([track(0, "jpn", True), track(1, "eng")], "jpn"), [0, 1])   # it is the default
        self.assertEqual(self.order([track(0, "ger", True), track(1, "zh-Hant")], "chi"), [1, 0])

    def test_a_commentary_is_skipped(self):
        tracks = [track(0, "rus", True), track(1, "eng", name="Director's Commentary"), track(2, "eng")]
        self.assertEqual(self.order(tracks, "eng"), [2, 0])

    def test_a_commentary_or_audio_description_is_never_fitted_nor_counted(self):
        # The fallback track in another language took a commentary too, and the room estimate counted it
        shapes = (
            [track(0, "eng", True, "English"), track(1, "und", name="Commentary", tagged=False)],   # a remux's und one
            [track(0, "fre", True), track(1, "eng", name="Commentary with the director")],          # Criterion's layout
            [track(0, "eng", True), track(1, "spa", name="Audio Description")],
        )
        for tracks in shapes:
            with self.subTest(tracks=tracks):
                main = tracks[0]["language"]
                for original in (None, main, "kor"):
                    self.assertEqual(self.order(tracks, original), [0])
                self.assertIsNone(audiofit.second_track(Index(tracks)))
                self.assertEqual(audiofit.most_tracks(Index(tracks)), 1)
                self.assertEqual(audio_facts(Index(tracks))["tracks"], 1)
        dub = [track(0, "eng", True), track(1, "jpn", name="Commentary"), track(2, "jpn")]
        self.assertEqual(self.order(dub, None), [0, 2])                # the dialogue track in another language

    def test_an_untagged_track_counts_only_for_english_and_after_a_tagged_one(self):
        untagged = track(1, "eng", tagged=False)                  # no Language element: Matroska's default, English
        self.assertEqual(self.order([track(0, "ger", True), untagged], "eng"), [1, 0])
        self.assertEqual(self.order([track(0, "ger", True), untagged, track(2, "eng")], "eng"), [2, 0, 1])
        self.assertEqual(self.order([track(0, "ger", True), track(1, "und")], "eng"), [1, 0])
        self.assertEqual(self.order([track(0, "ger", True), untagged], "jpn"), [0, 1])           # as before

    def test_without_an_original_language_as_before(self):
        for tracks in ([track(0, "eng", True), track(1, "jpn")], [track(0, "jpn", True)],
                       [track(0, "rus", True), track(1, "rus"), track(2, "eng")]):
            want = [0] + ([audiofit.second_track(Index(tracks))["index"]] if audiofit.second_track(Index(tracks)) else [])
            self.assertEqual(self.order(tracks, None), want)
            self.assertEqual(self.order(tracks, "kor"), want)       # no track in it: as before too

    def test_an_mp4_offers_no_tracks(self):
        class Mp4Like:
            def duration(self):
                return 600.0
        self.assertEqual(audiofit.track_order(Mp4Like(), "jpn"), [])

    def test_the_estimate_is_the_most_any_language_among_the_tracks_would_make(self):
        # most_tracks tries only the languages that can put a track first; the answer is that of every
        # language of the file's tracks tried in turn (the reference)
        def every_language(index):
            languages = {lang3(t.get("language")) for t in index.audio_tracks()} | {None}
            return max([1] + [len(audiofit.track_order(index, c)) for c in languages])
        pool = ["eng", "en", "English", "jpn", "ja", "chi", "yue", "cmn", "zh-Hant", "und", "mul", "qaa", "x1", "",
                "nor", "nob", "fre", "ger", "deu", "kor"]
        r = random.Random(7)
        for _ in range(1500):
            tracks = [dict(track(i, r.choice(pool), r.random() < 0.3, r.choice(["", "", "Commentary", "Main"]),
                                 r.random() < 0.8), commentary=r.random() < 0.1)
                      for i in range(r.choice([0, 1, 2, 3, 4, 6, 9]))]
            self.assertEqual(audiofit.most_tracks(Index(tracks)), every_language(Index(tracks)), tracks)

    def test_a_file_with_thousands_of_tracks_is_counted_at_once(self):
        # Every language's order, each reading every track twice: 4 s of CPU for 1,000 tracks, growing with the
        # square of their number (a crafted Tracks element holds hundreds of thousands)
        codes = ["".join(c) for c in itertools.product("abcdefghijklmnopqrstuvwxyz", repeat=3)][:17_000]
        shapes = {"each in its own language": ([track(i, c, i == 0) for i, c in enumerate(codes)], 3),
                  "all commentaries but the default": ([track(i, c, i == 0, "" if i == 0 else "Commentary")
                                                        for i, c in enumerate(codes)], 1),
                  "languages that name none": ([track(i, f"x{i}", i == 0) for i in range(17_000)], 2)}
        for shape, (tracks, want) in shapes.items():
            index, reads, done = Index(tracks), [], []
            listed = index.audio_tracks

            def counted():
                reads.append(1)
                return listed()
            index.audio_tracks = counted
            t = threading.Thread(target=lambda: done.append(audiofit.most_tracks(index)), daemon=True)
            t.start()
            t.join(10)
            self.assertEqual(done, [want], f"{shape}: most_tracks still running after 10 s")
            self.assertEqual(len(reads), 1, shape)                     # the tracks read once

    def test_the_read_estimate_counts_the_tracks_tried(self):
        tracks = [track(0, "rus", True), track(1, "ukr"), track(2, "eng")]
        # the original language may come after the file is first read: the estimate is the most the order could try
        self.assertEqual(audio_facts(Index(tracks))["tracks"], 3)
        self.assertEqual(audio_facts(Index([track(0, "eng", True), track(1, "jpn")]))["tracks"], 2)
        self.assertEqual(audio_facts(Index(tracks, original="eng"))["tracks"], 3)
        self.assertEqual(audio_facts(Index(tracks[:1], original="eng"))["tracks"], 1)


class FitTest(unittest.TestCase):
    """The fit on the original-language track first: on a dual-audio file whose default is a dub, one fit instead of
    two (the dub's fit read for nothing)."""

    def setUp(self):
        self.truth = rhythm(700, 11)
        self.dub = rhythm(700, 4242)

    def fit(self, original, follows=1):
        r = random.Random(5)
        speech = {follows: [(s + r.gauss(0, 0.08), s + d) for s, d in self.truth],
                  1 - follows: [(s + r.gauss(0, 0.08), s + d) for s, d in self.dub]}
        end = max(self.truth[-1][0] + self.truth[-1][1], self.dub[-1][0] + self.dub[-1][1]) + 30
        index = Index([track(0, "eng", True), track(1, "jpn")], original=original, end=end)
        rr = random.Random(1)
        starts = [s - 9.0 + rr.gauss(0, 0.15) for s, _ in self.truth]
        lines = [{"s": a, "e": a + d, "text": "x", "kind": "dialogue"} for a, (_, d) in zip(starts, self.truth)]
        return audiofit.fit_tracks(index, lines, vad=object(), better=AUDIO_BETTER, fit_class=timeline_fit(speech),
                                   original=original)

    def test_the_original_track_first_saves_the_dubs_fit(self):
        before = self.fit(None)                      # today: the dub (default) first, then Japanese
        now = self.fit("jpn")
        self.assertEqual((before["decision"], before.get("audio_track")), ("synced", 1))
        self.assertEqual((now["decision"], now.get("audio_track"), now.get("audio_language")), ("synced", 1, "jpn"))
        self.assertLess(now["windows"], before["windows"])
        self.assertAlmostEqual(now["model"]["offset"], 9.0, delta=0.3)

    def test_the_default_still_tried_when_the_original_doesnt_fit(self):
        got = self.fit("jpn", follows=0)             # the subtitles follow the default track after all
        self.assertEqual((got["decision"], got.get("audio_track")), ("synced", None))


class MatroskaTest(unittest.TestCase):
    def test_audio_tracks_carry_their_name_and_whether_they_were_tagged(self):
        from subsync.reader import RangeReader
        from helpers import RangeServer
        srv = RangeServer(FIXTURES)
        self.addCleanup(srv.close)
        idx = open_index(RangeReader(srv.url("tiny.mkv"), "test"))
        tracks = idx.audio_tracks()
        self.assertTrue(tracks)
        for t in tracks:
            # and the file's commentary and audio-description flags
            self.assertEqual(set(t), {"index", "language", "default", "name", "tagged", "commentary", "descriptive"})
            self.assertIsInstance(t["tagged"], bool)
            self.assertIsInstance(t["name"], str)
            self.assertIs(t["commentary"], False)
            self.assertIs(t["descriptive"], False)


if __name__ == "__main__":
    unittest.main()
