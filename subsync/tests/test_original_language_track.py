""", as amended: every subtitle is timed to the title's original-language track when the file has one
(Game of Thrones S01E01: Spanish to the English track, not to its own es-ES or es-419, which run 167–250 ms early against
it): a plain text track first, else an image track (Kingdom of Heaven DC: Russian to the English PGS track), else a CC or
SDH-only track, but only for a subtitle whose own language has no plain track (Dan Da Dan: its English and Spanish keep
their own); with the original language unknown, or no such track, the reference is chosen as before."""
import os
import random
import unittest
from unittest import mock

from helpers import closed_after, pgs_track, realistic, TempDir
from subsync import choose, pipeline
from subsync.settings import Settings
from subsync.store import Store
from subsync.worker import Job, Safety, Worker


def shifted(cues, by, keep=1.0, seed=1):
    r = random.Random(seed)
    return [(s + by + r.gauss(0, 0.02), d) for s, d in cues if r.random() < keep]


class Index:
    def __init__(self, tracks, original=None, images=()):
        self.tracks, self.images = tracks, list(images)
        if original:
            self.original_language = original

    def text_tracks(self):
        return [dict(t) for t in self.tracks]

    def image_tracks(self):
        return [dict(t) for t in self.images]


def track(number, language, cues, name="", forced=False):
    return {"number": number, "language": language, "codec": "S_TEXT/UTF8", "name": name, "forced": forced, "cues": cues}


class OriginalLanguageTrackTest(unittest.TestCase):
    def setUp(self):
        ref = realistic(600, 61)
        early = -0.167
        self.tracks = [track(5, "en-US", shifted(ref, 0.0, 0.85, 2)),
                       track(6, "en-US", shifted(ref, 0.3, 0.99, 9), "SDH"),
                       track(11, "es-419", shifted(ref, -0.25, 0.92, 3), "Latin American"),
                       track(12, "es-ES", shifted(ref, early, 0.96, 4), "Castilian"),
                       track(30, "ru-RU", shifted(ref, early, 0.95, 5))]
        self.tracks += [track(20 + k, lang, shifted(ref, early, 0.9, 10 + k))
                        for k, lang in enumerate(("bg-BG", "cs-CZ", "da-DK", "el-GR", "fi-FI", "pl-PL"))]

    def test_unknown_original_language_changes_nothing(self):
        self.assertEqual(pipeline.choose_reference(Index(self.tracks), 0, "spa")["number"], 12)
        self.assertEqual(pipeline.choose_reference(Index(self.tracks), 0, "rus")["number"], 30)

    def test_every_language_is_timed_to_the_original_track(self):
        idx = Index(self.tracks, "eng")
        for lang in ("spa", "rus", "eng"):
            self.assertEqual(pipeline.choose_reference(idx, 0, lang)["number"], 5, lang)   # the plain one, not SDH

    def test_no_track_in_the_original_language_keeps_todays_rule(self):
        idx = Index(self.tracks, "jpn")
        self.assertEqual(pipeline.choose_reference(idx, 0, "spa")["number"], 12)

    def test_a_forced_original_track_is_no_reference(self):
        tracks = [dict(t, forced=True) if t["language"].startswith("en") else t for t in self.tracks]
        self.assertEqual(pipeline.choose_reference(Index(tracks, "eng"), 0, "spa")["number"], 12)

    def test_the_original_track_is_judged_as_another_languages_for_a_translation(self):
        ref = pipeline.choose_reference(Index(self.tracks, "eng"), 0, "spa")
        self.assertTrue(pipeline.is_cross(ref, "spa"))


class LineCountSignalTest(unittest.TestCase):
    """"as many lines as the release's own track, so made from this release" (choose.order) is counted against
    the track in the subtitle's own language, as before 1.6.0; its timing is still judged against the title's original
    language. An English film with an English track of 900 lines and a Spanish one of 760: a Spanish subtitle of 760
    lines ripped from this release is the one tried first."""

    def setUp(self):
        self.index = Index([track(3, "eng", [(30.0 + 3 * i, 2.0) for i in range(900)]),
                            track(4, "spa", [(30.5 + 3.5 * i, 2.0) for i in range(760)])], "eng")

    def test_the_subtitles_own_language_track_without_the_original_rule(self):
        self.assertEqual(pipeline.choose_reference(self.index, 0, "spa")["number"], 3)                  # timing
        self.assertEqual(pipeline.choose_reference(self.index, 0, "spa", by_original=False)["number"], 4)

    def test_a_job_ranks_by_its_languages_own_track(self):
        tmp = TempDir()
        folder = tmp.__enter__()
        self.addCleanup(tmp.__exit__, None, None, None)
        s = Settings({"SUBSYNC_DATA": folder, "SUBSYNC_LANGUAGES": "eng,spa", "SUBSYNC_EXTRA_SOURCES": ""})
        w = Worker(s, closed_after(self, Store(os.path.join(folder, "store"))), None, Safety())
        with mock.patch.object(choose, "order", wraps=choose.order) as order:
            w._sync_language(Job("movie", "tt1"), self.index, "f-1", "spa", [], "abcd", "Movie.mkv")
        self.assertEqual(order.call_args.kwargs["ref_cues"], 760)
        cand = {"id": "tsdb-1", "lang": "spa", "release": "Some.Other.Name", "cues": 760}
        self.assertEqual(choose.tier_of(3, cand, "Movie.2020.1080p.BluRay-GRP.mkv", None, 760), 1)

    def test_a_language_with_no_track_of_its_own_counts_no_lines(self):
        # The count came from another language's track (choose_reference's fallback), so a Russian subtitle
        # about as long as the English track ranked as made from this release, above a release-group match
        tmp = TempDir()
        folder = tmp.__enter__()
        self.addCleanup(tmp.__exit__, None, None, None)
        s = Settings({"SUBSYNC_DATA": folder, "SUBSYNC_LANGUAGES": "rus", "SUBSYNC_EXTRA_SOURCES": ""})
        w = Worker(s, closed_after(self, Store(os.path.join(folder, "store"))), None, Safety())
        index = Index([track(3, "eng", [(30.0 + 3 * i, 2.0) for i in range(820)])], "eng")
        self.assertEqual(pipeline.choose_reference(index, 0, "rus", by_original=False)["number"], 3)   # the fallback
        with mock.patch.object(choose, "order", wraps=choose.order) as order:
            w._sync_language(Job("movie", "tt1"), index, "f-1", "rus", [], "abcd", "Movie.mkv")
        self.assertIsNone(order.call_args.kwargs["ref_cues"])


class OriginalImageAndCaptionTrackTest(unittest.TestCase):
    """."""

    def setUp(self):
        self.ref = realistic(500, 71)

    def test_an_original_image_track_is_the_reference_for_another_language(self):
        idx = Index([track(9, "ru-RU", shifted(self.ref, -0.2, 0.95, 3))], "eng", [pgs_track(self.ref, 3, "eng")])
        got = pipeline.choose_reference(idx, 0, "rus", image_ref=True)
        self.assertEqual((got["number"], got["route"]), (3, "image"))

    def test_without_image_references_an_original_image_track_changes_nothing(self):
        idx = Index([track(9, "ru-RU", shifted(self.ref, -0.2, 0.95, 3))], "eng", [pgs_track(self.ref, 3, "eng")])
        self.assertEqual(pipeline.choose_reference(idx, 0, "rus", image_ref=False)["number"], 9)

    def test_a_caption_only_original_track_leaves_a_plain_own_track_alone(self):
        tracks = [track(4, "ja-JP", shifted(self.ref, 0.0, 0.99, 2), "Japanese(CC)"),
                  track(5, "en-US", shifted(self.ref, -0.15, 0.9, 4))]
        self.assertEqual(pipeline.choose_reference(Index(tracks, "jpn"), 0, "eng")["number"], 5)

    def test_a_caption_only_original_track_is_the_reference_without_one(self):
        tracks = [track(4, "ja-JP", shifted(self.ref, 0.0, 0.99, 2), "Japanese(CC)"),
                  track(5, "en-US", shifted(self.ref, -0.15, 0.9, 4))]
        self.assertEqual(pipeline.choose_reference(Index(tracks, "jpn"), 0, "spa")["number"], 4)


if __name__ == "__main__":
    unittest.main()
