"""The audio route on a file with no usable subtitle track fits the sound in the title's original language first (a
track named as a commentary skipped), then the default track, then the second track in another language as before;
an untagged track counts only for an English original and only after a tagged one; without an original language the
order is exactly as before."""
import random
import unittest

from helpers import AUDIO_BETTER, FIXTURES, rhythm
from subsync import audiofit  # noqa: E402
from subsync.mp4 import open_index  # noqa: E402
from subsync.worker import audio_facts  # noqa: E402


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
        self.assertEqual(self.order(tracks, "eng")[:2], [2, 0])

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
        import numpy as np
        r = random.Random(5)
        speech = {follows: [(s + r.gauss(0, 0.08), s + d) for s, d in self.truth],
                  1 - follows: [(s + r.gauss(0, 0.08), s + d) for s, d in self.dub]}
        end = max(self.truth[-1][0] + self.truth[-1][1], self.dub[-1][0] + self.dub[-1][1]) + 30
        index = Index([track(0, "eng", True), track(1, "jpn")], original=original, end=end)

        class TruthFit(audiofit.AudioFit):
            def _read_window(self, start, length):
                self.windows_read += 1
                self.audio_s += length
                n = int(round(length * 100))
                mask, onsets = np.zeros(n, dtype=bool), []
                for a, b in speech[self.track or 0]:
                    if b <= start or a >= start + length:
                        continue
                    mask[max(0, int((a - start) * 100)):min(n, int((b - start) * 100))] = True
                    if a >= start:
                        onsets.append(a - self.lead)
                return {"start": start, "len": length, "mask": mask, "onsets": np.array(onsets)}
        rr = random.Random(1)
        starts = [s - 9.0 + rr.gauss(0, 0.15) for s, _ in self.truth]
        lines = [{"s": a, "e": a + d, "text": "x", "kind": "dialogue"} for a, (_, d) in zip(starts, self.truth)]
        return audiofit.fit_tracks(index, lines, vad=object(), better=AUDIO_BETTER, fit_class=TruthFit,
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
            self.assertEqual(set(t), {"index", "language", "default", "name", "tagged"})
            self.assertIsInstance(t["tagged"], bool)
            self.assertIsInstance(t["name"], str)


if __name__ == "__main__":
    unittest.main()
