"""A release naming another season or episode than the file gets no release-group or same-source credit when the
first guess is chosen (The Bear S01E01: 'The.Bear.S03.COMPLETE...-PGW', another season's subtitle, was the guess by its
release group alone); a hash or line-count match keeps its place, and a pack of the file's own season keeps its credit."""
import unittest

import helpers  # noqa: F401  (first: it puts the package on the import path)
from subsync import choose

FILE = "The.Bear.S01E01.1080p.WEB.x265-PGW.mkv"


def cand(cid, release, **kw):
    return dict({"id": cid, "url": f"http://subs.test/{cid}", "lang": "eng", "release": release}, **kw)


class EpisodeCreditTest(unittest.TestCase):
    def test_another_seasons_release_group_gives_no_credit(self):
        cands = [cand("s3", "The.Bear.S03.COMPLETE.1080p.WEB.x265-PGW"), cand("e1", "The.Bear.S01E01.720p.WEB.h264-GRP")]
        self.assertEqual(choose.best_guess(cands, FILE)["id"], "e1")

    def test_another_episode_of_the_season_gives_no_credit(self):
        cands = [cand("e2", "The.Bear.S01E02.1080p.WEB.x265-PGW"), cand("e1", "The.Bear.S01E01.720p.WEB.h264-GRP")]
        self.assertEqual(choose.best_guess(cands, FILE)["id"], "e1")

    def test_a_pack_of_the_files_own_season_keeps_its_credit(self):
        cands = [cand("x", "The.Bear.S01E01.720p.HDTV-OTHER"), cand("pack", "The.Bear.S01.COMPLETE.1080p.WEB.x265-PGW")]
        self.assertEqual(choose.best_guess(cands, FILE)["id"], "pack")

    def test_a_hash_match_keeps_its_place(self):
        cands = [cand("e1", "The.Bear.S01E01.720p.WEB.h264-GRP"), cand("s3", "The.Bear.S03.COMPLETE-PGW", hash_match=True)]
        self.assertEqual(choose.best_guess(cands, FILE)["id"], "s3")

    def test_a_film_is_unaffected(self):
        cands = [cand("a", "Primer.2004.720p.BluRay.x264-OTHER"), cand("b", "Primer.2004.1080p.BluRay.x264-YTS")]
        self.assertEqual(choose.best_guess(cands, "Primer.2004.1080p.BluRay.x264-YTS.mp4")["id"], "b")

    def test_tiers(self):
        self.assertEqual(choose.tier_of(3, cand("s3", "The.Bear.S03.COMPLETE.1080p.WEB.x265-PGW"), FILE), 4)
        self.assertEqual(choose.tier_of(3, cand("e1", "The.Bear.S01E01.1080p.WEB.x265-PGW"), FILE), 2)


if __name__ == "__main__":
    unittest.main()
