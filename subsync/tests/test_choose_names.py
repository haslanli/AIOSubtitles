"""Release names as the ranking reads them: one season/episode parser for choose and the built-in sources (names with
underscores, four-digit episodes, several episodes in one tag), every video extension left out before a trailing
release group, source tags written with a hyphen, "Non-Forced" subtitles, and AI-made, machine-translated and forced
subtitles last when they are not skipped."""
import time
import unittest

from helpers import SourcesHarness  # (first: it puts the package on the import path)
from subsync import choose, extra


def cand(cid, release, **kw):
    return dict({"id": cid, "url": f"http://subs.test/{cid}", "lang": "eng", "release": release}, **kw)


class EpisodeTagTest(unittest.TestCase):
    def test_names_choose_and_the_built_in_sources_read_alike(self):
        for name, tags in (("Show_S01E05_720p", [(1, 5, 5)]), ("Show.S01E05x264", [(1, 5, 5)]),
                           ("Show.S01E1000.720p", [(1, 1000, 1000)]), ("Show.S01E05E06.mkv", [(1, 5, 5), (1, 6, 6)]),
                           ("Show.S01E01-E10.1080p", [(1, 1, 1), (1, 1, 10)]), ("Show.S01E01-10.mkv", [(1, 1, 1), (1, 1, 10)]),
                           ("Show.S01E10-E05", [(1, 10, 10), (1, 5, 10)]),
                           ("Show.S01E01-1080p", [(1, 1, 1)]), ("Show.S01E01-10bit", [(1, 1, 1)]),
                           ("Show.S01.COMPLETE", []), ("Movie.2010.1080p", [])):
            self.assertEqual(choose.episodes(name), tags, name)
        self.assertEqual(choose.season_episode("Show.S01E1000.720p"), (1, 1000))
        self.assertEqual(choose.season_episode("Show_S01E05_720p"), (1, 5))

    def test_the_built_in_sources_see_an_underscored_release_of_another_episode(self):
        self.assertTrue(extra.other_episode("Show_S01E05_720p", 1, 3))
        self.assertTrue(extra.other_episode("Show.S01E05E06.mkv", 1, 3))
        self.assertFalse(extra.other_episode("Show_S01E03_720p", 1, 3))
        self.assertFalse(extra.other_episode("Show.S02.COMPLETE", 1, 3))      # a season pack: theirs to judge later

    def test_a_release_of_several_episodes_including_the_files_is_not_another(self):
        ep = (1, 2)
        for release in ("Show.S01E01-E02.1080p.WEB-DL-GRP", "Show.S01E01E02E03.1080p.WEB-DL-GRP",
                        "Show.S01E01-E10.1080p.WEB-DL-GRP", "Show.S01E01-10.1080p.WEB-DL-GRP"):
            self.assertFalse(choose.other_episode(release, ep), release)
            self.assertFalse(extra.other_episode(release, *ep), release)
        for release in ("Show.S01E03-E04.1080p.WEB-DL-GRP", "Show.S01E05E06", "Show.S02E01-E10"):
            self.assertTrue(choose.other_episode(release, ep), release)
            self.assertTrue(extra.other_episode(release, *ep), release)

    def test_a_multi_episode_release_of_the_files_group_keeps_its_credit(self):
        name = "Show.S01E02.1080p.WEB-DL-GRP.mkv"
        self.assertEqual(choose.tier_of(3, cand("a", "Show.S01E01-E02.1080p.WEB-DL-GRP"), name), 2)
        self.assertEqual(choose.tier_of(3, cand("b", "Show.S01E01E02E03.1080p.WEB-DL-GRP"), name), 2)
        self.assertEqual(choose.tier_of(3, cand("c", "Show.S01E03E04.1080p.WEB-DL-GRP"), name), 4)


class VideoExtensionTest(unittest.TestCase):
    def test_the_release_group_comes_before_any_video_extension(self):
        for ext in ("mkv", "mp4", "m4v", "mov", "webm", "wmv", "M4V"):
            self.assertEqual(choose.groups(f"Movie.2020.1080p.WEB-DL.DDP5.1.H.264-NTb.{ext}"), {"ntb"}, ext)

    def test_a_m4v_file_ranks_its_groups_subtitles_first(self):
        cands = [cand("other", "Movie.2020.1080p.WEB-DL-OTHER"), cand("own", "Movie.2020.720p.WEB-DL-NTb")]
        name = "Movie.2020.1080p.WEB-DL.DDP5.1.H.264-NTb.m4v"
        self.assertEqual(choose.order(cands, name, viewer_first=False)[0][1]["id"], "own")


class TheSubtitleDBNameTest(SourcesHarness, unittest.TestCase):
    def test_the_release_name_asked_has_no_video_extension(self):
        for ext in ("m4v", "wmv", "mkv"):
            self.fake.calls.clear()
            self.extra(SUBSYNC_EXTRA_SOURCES="tsdb").search(
                "movie", "tt1", hints={"filename": f"Movie.2010.1080p.WEB-DL-GRP.{ext}"})
            asked = [q.get("release") for p, q in self.fake.calls if p == "/tsdb/v1/by-releasename"]
            self.assertEqual(asked, ["Movie.2010.1080p.WEB-DL-GRP"], ext)


class HyphenatedTagTest(unittest.TestCase):
    def test_a_hyphenated_source_tag_is_no_release_group(self):
        for name in ("Movie.2020.1080p.WEB-DL.mkv", "Show.S01E01.1080p.WEB-DL.H.264.mkv", "Show.S01E01.WEB-DL.DDP5.1.mkv",
                     "Movie.2010.1080p-WEBRip.x264.mkv", "Movie.2010.Blu-ray.mkv", "Movie.DVD-R.x264.mkv",
                     "Movie.2020.WEB-DLRip.avi",
                     "Movie (2010) Bluray-1080p.mkv", "Show - S01E01 - Pilot WEBDL-1080p.mkv"):
            self.assertEqual(choose.groups(name), set(), name)

    def test_real_groups_are_still_found(self):
        self.assertEqual(choose.groups("Movie.2010.1080p.WEB-DL.x264-GROUP.mkv"), {"group"})
        self.assertEqual(choose.groups("Movie.2010.1080p.Blu-ray.Remux.AVC-FraMeSToR.mkv"), {"framestor"})
        self.assertEqual(choose.groups("[Erai-raws] Show - 01 [1080p].mkv"), {"erai raws"})
        self.assertEqual(choose.groups("Movie.2019.1080p.WEB-RARBG.mkv"), {"rarbg"})
        self.assertEqual(choose.groups("Movie.2020.WEB-DLRip.x264-GRP.avi"), {"grp"})

    def test_radarr_and_sonarr_names_give_their_source(self):
        self.assertEqual(choose.source_kind("Movie (2010) Bluray-1080p.mkv"), "bluray")
        self.assertEqual(choose.source_kind("Show - S01E01 - Pilot WEBDL-1080p.mkv"), "web")
        self.assertEqual(choose.source_kind("Movie.2010.1080p-WEBRip.x264.mkv"), "web")
        self.assertEqual(choose.source_kind("Movie.DVD-R.mkv"), "dvd")
        self.assertEqual(choose.source_kind("Show.S01E01.1080p.AMZN.WEB-DL.DDP5.1"), "web")

    def test_a_group_less_web_dl_file_gives_no_group_credit(self):
        name = "Show.S01E01.1080p.WEB-DL.mkv"
        self.assertEqual(choose.tier_of(3, cand("a", "Show.S01E01.1080p.WEB-DL.DD5.1-NTb"), name), 3)
        self.assertEqual(choose.tier_of(3, cand("b", "Show.S01E01.1080p.WEBRip.x264-GRP"), name), 3)
        self.assertEqual(choose.tier_of(3, cand("c", "Show.S01E01.720p.HDTV-LOL"), name), 4)


class UnwantedTest(unittest.TestCase):
    def test_a_non_forced_subtitle_is_a_full_one(self):
        for release in ("Movie.2019.1080p.BluRay.English.Non-Forced", "Movie non.forced", "Movie (Non Forced)",
                        "Movie.Not.Forced", "Movie.No-Forced"):
            self.assertFalse(choose.unwanted(cand("x", release)), release)
        for release in ("Movie.2019.English.Forced", "Movie.2001.FORCED.srt", "Movie (Forced)"):
            self.assertTrue(choose.unwanted(cand("x", release)), release)
        cands = [cand("full", "Movie.2019.1080p.BluRay.English.Non-Forced"), cand("forced", "Movie.2019.Forced")]
        self.assertEqual([c["id"] for _, c in choose.order(cands, None, None, skip=True)], ["full"])

    def test_with_skip_off_unwanted_subtitles_go_last(self):
        name = "Movie.2010.1080p.BluRay.x264-OTHER.mkv"
        for release in ("Movie.2010.BluRay.Machine.Translated", "Movie.2010.BluRay.x264-OTHER.Forced",
                        "Movie.2010.BluRay.AI-translated"):
            self.assertEqual(choose.tier_of(2, cand("x", release), name), 9, release)
        cands = [cand("first", "A"), cand("mt", "Movie.2010.BluRay.x264-OTHER.machine.translated"),
                 cand("forced", "Movie.2010.BluRay.Forced"), cand("plain", "Movie.2010.WEB"),
                 cand("ai", "Movie.2010.BluRay", ai=True)]
        self.assertEqual([c["id"] for _, c in choose.order(cands, name)], ["first", "plain", "mt", "forced", "ai"])
        self.assertEqual(choose.order(cands[1:], name, viewer_first=False)[0][1]["id"], "plain")


class NameReadingTest(unittest.TestCase):
    """How a file's or a release's name is read: its groups in linear time, a hyphenated title's end, a pack of several
    seasons, a tag written with a hyphen, a title's own words."""

    def test_a_name_of_many_unclosed_brackets_is_read_in_linear_time(self):
        # From every "[" with no "]" after it, the bracket patterns read to the end of the name (a request's
        # filename hint of 40,000 "[" took 8.7 s of CPU, holding the interpreter)
        for opener in ("[", "("):
            t0 = time.process_time()
            self.assertEqual(choose.groups(opener * 60_000), set())
            self.assertLess(time.process_time() - t0, 1.0, opener)
        self.assertEqual(choose.groups("Movie [a[Group]] (x(y)z).2010.1080p-GRP.mkv"), {"group", "grp"})

    def test_the_end_of_a_hyphenated_title_is_no_release_group(self):
        # "Ant-Man.mkv" named the group "man", so every subtitle naming Spider-Man or Iron Man was tier 2
        for name in ("Ant-Man.mkv", "X-Men.2000.mkv", "Frieren-01.mkv", "Spider-Man.2002.1080p.mkv",
                     "Ant-Man.and.the.Wasp.2018.mkv"):
            self.assertEqual(choose.groups(name), set(), name)
        self.assertEqual(choose.groups("[Judas] Kaguya-sama.mkv"), {"judas"})
        self.assertEqual(choose.groups("Spider-Man.Homecoming.2017.1080p.BluRay.x264-SPARKS.mkv"), {"sparks"})
        self.assertEqual(choose.groups("Movie.2019.1080p.BluRay-YTS.MX.mp4"), {"yts mx"})
        self.assertEqual(choose.groups("Show.S01E01-GRP.mkv"), {"grp"})              # an episode tag is a release's
        self.assertEqual(choose.tier_of(3, cand("a", "Spider-Man.Homecoming.2017.1080p.WEB"), "Ant-Man.mkv"), 4)
        cands = [cand("other", "[Other] Kaguya-sama - 01"), cand("own", "[Judas] Kaguya-sama - 01")]
        self.assertEqual(choose.order(cands, "[Judas] Kaguya-sama.mkv", viewer_first=False)[0][1]["id"], "own")

    def test_a_pack_of_several_seasons_the_files_among_them_is_not_another(self):
        # Only a pack's first season was read, so S01-S03 was "another season" for an S02 file
        ep = (2, 5)
        for release in ("Show.S01-S03.Complete", "Show.S01.S02.S03.Pack", "Show.Season.1-3", "Show.S02.Complete"):
            self.assertFalse(choose.other_episode(release, ep), release)
        for release in ("Show.S03-S04.Complete", "Show.S01.S03.Pack", "Show.Season.3-4", "Show.S01.Complete"):
            self.assertTrue(choose.other_episode(release, ep), release)
        self.assertEqual(choose.seasons("Show.S03-S01"), {1, 2, 3})
        self.assertEqual(choose.seasons("Movie.2010.1080p"), set())
        self.assertEqual(choose.season_episode("Show.S01-S03.Complete"), (1, None))   # its first season, as before
        name = "Show.S02E05.1080p.WEB-DL-GRP.mkv"
        self.assertEqual(choose.tier_of(3, cand("pack", "Show.S01-S03.Complete.1080p.WEB-DL-GRP"), name), 2)

    def test_a_tag_written_with_a_hyphen_gives_its_kind(self):
        # "HD-TV" split into "hd-tv", "hd" and "tv", none of them a TV tag
        self.assertEqual(choose.source_kind("Show.S01E01.HD-TV.x264-LOL"), "tv")
        self.assertEqual(choose.tier_of(3, cand("a", "Show.S01E01.720p.HDTV.x264-OTHER"), "Show.S01E01.HD-TV.x264-LOL.mkv"),
                         3)
        for name, kind in (("Movie.DVD-R.mkv", "dvd"), ("Movie.2020.WEB-DLRip.x264", "web"),
                           ("Movie.2010.Blu-ray.mkv", "bluray")):
            self.assertEqual(choose.source_kind(name), kind, name)

    def test_a_titles_own_words_are_no_source_tags(self):
        # "Charlottes.Web.2006.DVDRip" was a web release, so its WEB-DL subtitles got the same-source credit
        for name, kind in (("Charlottes.Web.2006.DVDRip.XviD-GRP", "dvd"), ("The.Dark.Web.2019.DVDRip", "dvd"),
                           ("Web.of.Lies.S01E01.HDTV.x264", "tv"), ("Charlottes.Web.2006.1080p.WEB-DL", "web"),
                           ("Spirited Away (2001) JAPANESE.Bluray.REMUX.H264", "remux"),
                           ("[Group] Show - 01 [BD 1080p HEVC]", "bluray")):
            self.assertEqual(choose.source_kind(name), kind, name)
        cands = [cand("web", "Charlottes.Web.2006.1080p.WEB-DL.x264-OTHER"),
                 cand("dvd", "Charlottes.Web.2006.DVDRip.x264-OTHER")]
        self.assertEqual([c["id"] for _, c in choose.order(cands, "Charlottes.Web.2006.DVDRip.XviD-GRP.mkv",
                                                           viewer_first=False)], ["dvd", "web"])


if __name__ == "__main__":
    unittest.main()
