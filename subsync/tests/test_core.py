"""Pure pieces: subtitle text, the gate, the per-line snap, the OpenSubtitles hash, language codes, the VAD
timestamp rule."""
import os
import random
import statistics
import struct
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subsync import choose, imageref, langs, pipeline, refine, shift, subs  # noqa: E402
from subsync import relgate as relgate_mod  # noqa: E402
from subsync.mkv import oshash  # noqa: E402
from subsync.relgate import aligned, clearly_better, relgate  # noqa: E402
from subsync.vad import FRAME, timestamps  # noqa: E402

ASS = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,Arial,64
Style: Sign,Arial,40

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Hello, there
Dialogue: 0,0:00:03.00,0:00:04.00,Sign,,0,0,0,,{\\pos(10,10)}A SIGN
Comment: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,not a line
Dialogue: 0,0:01:05.25,0:01:07.00,Song,,0,0,0,,la la
"""


def starts(n, step=4.0, start=10.0):
    return [start + i * step for i in range(n)]


class SubsTest(unittest.TestCase):
    def test_parse_srt_and_vtt_times(self):
        srt = "1\n00:00:01,000 --> 00:00:02,500\nHi\n\n2\n00:01:00.250 --> 00:01:01.000\nThere\n"
        lines = subs.parse(srt)
        self.assertEqual([round(l["s"], 3) for l in lines], [1.0, 60.25])
        self.assertTrue(all(l["kind"] == "dialogue" for l in lines))

    def test_parse_ass_kinds_and_rewrite_keeps_fields(self):
        lines = subs.parse(ASS)
        self.assertEqual([l["kind"] for l in lines], ["dialogue", "sign", "song"])
        moved = [dict(l, s=l["s"] + 1.5, e=l["e"] + 1.5) for l in lines]
        out = subs.write_like(ASS, moved)
        self.assertIn("Dialogue: 0,0:00:02.50,0:00:04.00,Default,,0,0,0,,Hello, there", out)
        self.assertIn("{\\pos(10,10)}A SIGN", out)
        self.assertIn("Comment: 0,0:00:05.00", out)          # untouched
        self.assertIn("Style: Sign,Arial,40", out)
        self.assertEqual([round(l["s"], 2) for l in subs.parse(out)], [2.5, 4.5, 66.75])

    def test_drop_and_srt_out(self):
        lines = subs.parse(ASS)
        lines[1]["drop"] = True
        srt = subs.to_srt(lines)
        self.assertNotIn("SIGN", srt)
        self.assertIn("Hello, there", srt)
        self.assertEqual(len(subs.parse(srt)), 2)

    def test_plain_srt_keeps_every_line_in_order(self):
        vtt = "WEBVTT\n\nheader\n00:00:05.000 --> 00:00:06.000\nB\n\n00:00:01.000 --> 00:00:01.000\n\n\n" \
              "00:00:02.000 --> 00:00:03.000\n<i>C</i>\n"
        lines = subs.parse(vtt)
        back = subs.parse(subs.plain_srt(lines))
        self.assertEqual(len(back), len(lines))
        self.assertEqual([round(l["s"], 3) for l in back], [round(l["s"], 3) for l in lines])
        self.assertTrue(all(b["e"] > b["s"] for b in back))

    def test_decode(self):
        self.assertEqual(subs.decode("﻿Hi".encode("utf-8")), "Hi")
        self.assertEqual(subs.decode("Привет".encode("cp1251"), "rus"), "Привет")


class RelgateTest(unittest.TestCase):
    def test_identical_passes(self):
        s = starts(100)
        self.assertTrue(relgate(s, s)["pass"])

    def test_offset_fails(self):
        s = starts(100)
        self.assertFalse(relgate(s, [x + 2.7 for x in s])["pass"])

    def test_small_jitter_passes_and_bias_fails(self):
        s = starts(100)
        self.assertTrue(relgate(s, [x + (0.05 if i % 2 else -0.05) for i, x in enumerate(s)])["pass"])
        self.assertFalse(relgate(s, [x + 0.15 for x in s])["pass"])

    def test_cut_part_cannot_hide_among_unpaired_lines(self):
        # the last third is 60 s off: unpaired, but the coverage rule fails those tenths
        ref = starts(150)
        cand = [x if i < 100 else x + 60 for i, x in enumerate(ref)]
        r = relgate(ref, cand)
        self.assertFalse(r["pass"])
        self.assertTrue(r["weak_tenths"])

    def test_different_splits_still_pass(self):
        ref = starts(100)
        cand = list(ref) + [x + 2.0 for x in ref[::5]]  # extra half-lines between reference lines: unpaired
        self.assertTrue(relgate(ref, sorted(cand))["pass"])

    def test_too_few_paired_fails(self):
        ref = starts(100)
        cand = sorted(ref[:50] + [x + 2.0 for x in ref[50:]])
        self.assertFalse(relgate(ref, cand)["pass"])

    def test_empty(self):
        self.assertFalse(relgate([], [1.0])["pass"])
        self.assertFalse(relgate([1.0], [])["pass"])


class RelgateSplitTest(unittest.TestCase):
    def setUp(self):
        self.ref = starts(120, step=4.0)
        self.dur = [3.0] * len(self.ref)

    def split_cand(self):
        # every third release line split in two by the author: the second half starts 1.4 s in
        cand = []
        for i, x in enumerate(self.ref):
            cand.append(x + (0.04 if i % 2 else -0.04))
            if i % 3 == 0:
                cand.append(x + 1.4)
        return cand

    def test_split_halves_count_as_paired_not_as_errors(self):
        cand = self.split_cand()
        plain = relgate(self.ref, cand)
        r = relgate(self.ref, cand, self.dur)
        self.assertEqual(r["splits"], 40)
        self.assertTrue(r["pass"])
        self.assertGreater(r["paired"], plain["paired"])

    def test_splits_open_no_hole_for_an_offset_or_a_cut(self):
        cand = self.split_cand()
        self.assertFalse(relgate(self.ref, [x + 2.7 for x in cand], self.dur)["pass"])
        cut = [x if x < 300 else x + 60 for x in cand]
        r = relgate(self.ref, cut, self.dur)
        self.assertFalse(r["pass"])
        self.assertTrue(r["weak_tenths"])

    def test_stricter_share_for_snapped_copies(self):
        cand = [x + (0.3 if i % 12 == 0 else 0.02) for i, x in enumerate(self.ref)]  # ~92 % within 200 ms
        self.assertTrue(relgate(self.ref, cand, self.dur)["pass"])
        self.assertFalse(relgate(self.ref, cand, self.dur, within_share=0.95)["pass"])


def rhythm(n, seed):
    r = random.Random(seed)
    t, out = 20.0, []
    for _ in range(n):
        d = r.uniform(1.0, 4.0)
        out.append((t, d))
        t += d + r.uniform(0.3, 3.0)
    return out


def lines_from(cues, jitter=0.0, seed=1):
    r = random.Random(seed)
    out = []
    for s, d in cues:
        e = max(-0.4, min(0.4, r.gauss(0, jitter))) if jitter else 0.0
        out.append({"s": s + e, "e": s + d + e + 0.2, "text": "x", "kind": "dialogue"})
    return out


class RefineTest(unittest.TestCase):
    def test_author_scatter_snaps_to_the_release_lines(self):
        ref = rhythm(300, 1)
        cand = lines_from(ref, jitter=0.18, seed=2)
        out, st = refine.snap(cand, ref)
        err = [abs(o["s"] - s) for o, (s, _) in zip(out, ref)]
        self.assertGreater(st["snapped_share"], 0.9)
        self.assertGreater(sum(x <= 0.01 for x in err) / len(err), 0.9)
        self.assertTrue(relgate([s for s, _ in ref], [o["s"] for o in out], [d for _, d in ref], 0.95)["pass"])

    def test_order_and_minimum_display_kept(self):
        ref = rhythm(200, 3)
        cand = lines_from(ref, jitter=0.25, seed=4)
        out, _ = refine.snap(cand, ref)
        self.assertEqual([o["s"] for o in out], sorted(o["s"] for o in out))
        for a, o in zip(cand, out):
            self.assertGreaterEqual(round(o["e"] - o["s"], 6), round(min(0.5, a["e"] - a["s"]), 6))
        for (a, b), (x, y) in zip(zip(cand, cand[1:]), zip(out, out[1:])):
            if a["e"] <= b["s"]:
                self.assertLessEqual(x["e"], y["s"] + 1e-9)

    def test_a_wrong_episode_does_not_pass_after_snapping(self):
        ref = rhythm(300, 5)
        other = lines_from(rhythm(300, 6))   # same kind of rhythm, different speech
        out, _ = refine.snap(other, ref)
        self.assertFalse(relgate([s for s, _ in ref], [o["s"] for o in out], [d for _, d in ref], 0.95)["pass"])

    def test_split_half_follows_its_first_half_and_signs_follow_the_shift(self):
        ref = [(10.0 + 5 * i, 4.0) for i in range(40)]
        cand = []
        for i, (s, d) in enumerate(ref):
            cand.append({"s": s + 0.12, "e": s + 1.8, "text": "a", "kind": "dialogue"})
            if i == 10:
                cand.append({"s": s + 2.0 + 0.12, "e": s + 3.9, "text": "b", "kind": "dialogue"})
        cand.append({"s": 100.12, "e": 101.0, "text": "SIGN", "kind": "sign"})
        out, st = refine.snap(cand, ref)
        self.assertEqual(st["continuations"], 1)
        half = next(o for o in out if o["text"] == "b")
        self.assertAlmostEqual(half["s"], ref[10][0] + 2.0, places=3)
        sign = next(o for o in out if o["kind"] == "sign")
        self.assertAlmostEqual(sign["s"], 100.0, places=3)

    def test_monotone_pairing_never_crosses(self):
        ref = [1.0, 1.4, 1.8, 5.0]
        got = refine.pair_monotone([1.35, 1.38, 1.9, 5.1], ref, 0.5)
        js = [got[i] for i in sorted(got)]
        self.assertEqual(js, sorted(set(js)))


SA_FILE = "[RedDeadProject] Spirited Away.mkv"
SA_LIST = [   # the real English list for that file, in the viewer's order: release names only
    {"release": "Spirited Away HDTV"}, {"release": "Spirited Away - DVDRip"},
    {"release": "Spirited.Away.2001.DUBBED.1080p.BluRay.x265-RARBG"},
    {"release": "Spirited.Away.2001.1080p.BluRay.X264-AMIABLE", "ai": True},
    {"release": "Spirited Away (2001) JAPANESE.Bluray.REMUX.H264.6.1[RedDeadProject]"},
    {"release": "Spirited.Away.2001.NF.1080p"},
]


class ChooseTest(unittest.TestCase):
    def test_groups_from_real_file_names(self):
        self.assertEqual(choose.groups(SA_FILE), {"reddeadproject"})
        self.assertEqual(choose.groups("Spirited.Away.2001.1080p.BluRay.x264-YTS.mkv"), {"yts"})
        self.assertEqual(choose.groups("Jujutsu Kaisen - S01E01 v2 (BD 1080p HEVC) [Vodes].mkv"), {"vodes"})
        self.assertEqual(choose.groups("[Erai-raws] Jujutsu Kaisen 2nd Season - 01 [1080p][Multiple Subtitle][D4D202DA].mkv"),
                         {"erai raws"})
        self.assertEqual(choose.groups("Kikoushi Enma - 01 [DVD] [Iznjie Biznjie] [0A1B2C3D].mkv"), {"iznjie biznjie"})
        self.assertEqual(choose.groups("Interstellar.mkv"), set())
        self.assertEqual(choose.groups(None), set())

    def test_source_kinds(self):
        self.assertEqual(choose.source_kind("Spirited.Away.2001.1080p.BluRay.REMUX.AVC"), "remux")
        self.assertEqual(choose.source_kind("Jujutsu Kaisen - S01E01 v2 (BD 1080p HEVC) [Vodes]"), "bluray")
        self.assertEqual(choose.source_kind("Show.S01E01.1080p.AMZN.WEB-DL.DDP5.1"), "web")
        self.assertIsNone(choose.source_kind(SA_FILE))

    def test_order_first_then_hash_group_kind_rest_ai_last(self):
        got = [r for r, _ in choose.order(SA_LIST, SA_FILE)]
        self.assertEqual(got, [0, 4, 1, 2, 5, 3])            # the viewer's first, the [RedDeadProject] one, the rest, AI last
        cands = [{"release": "A"}, {"release": "B", "hash": "ab12"}, {"release": "Movie.2001.BluRay.REMUX-X"}]
        self.assertEqual([r for r, _ in choose.order(cands, "Movie.2001.2160p.BluRay.REMUX-Y.mkv", "AB12")], [0, 1, 2])
        self.assertEqual([r for r, _ in choose.order(cands, None, None)], [0, 1, 2])   # nothing known: viewer order

    def test_best_guess_when_nothing_can_be_checked_yet(self):
        # Detectorists S01E01 WEBRip (no subtitle track): the viewer's first, a DVD rip, was 41.7 s off; the WEB one fits
        # the speech as it is and is AutoSync's pick by name
        cands = [{"release": "Detectorists S01E01 Episode 1.1.DVDRip.NonHI.en.ACORN"},
                 {"release": "Detectorists.S01E01.WEB"}, {"release": "Detectorists.S01E01.1080p.WEB-DL.DD+2.0.H.264-SbR"}]
        name = "Detectorists.S01E01.WEBRip.x264-XEN0N.mkv"
        self.assertIs(choose.best_guess(cands, name), cands[1])
        hashed = cands + [{"release": "x", "hash": "ab12"}]
        self.assertIs(choose.best_guess(hashed, name, "AB12"), hashed[3])          # made for this very file
        self.assertIs(choose.best_guess(cands, None), cands[0])                    # nothing known: the viewer's first
        self.assertIsNone(choose.best_guess([{"release": "x", "ai": True}], name, skip=True))
        self.assertEqual([r for r, _ in choose.order(cands, name)], [0, 1, 2])      # tries still start with the viewer's

    KOH_FILE = "Kingdom.Of.Heaven.2005.DC.2160p.4K.BluRay.x265.10bit.AAC5.1-[YTS.MX].mkv"
    KOH = [{"release": "Kingdom.Of.Heaven.2005.2160p.4K.WEB.x265.10bit.AAC5.1-[YTS.MX]"},
           {"release": "kingdom.of.heaven.2005.directors.cut.roadshow.version.uhd.bluray.2160p"},
           {"release": "Kingdom.Of.Heaven.2005.DIRECTOR'S.CUT.ROADSHOW.BLURAY.1080p.BluRay.x264.AAC5.1-[YTS.MX]"}]

    def test_editions_named_in_release_names(self):
        self.assertEqual(choose.editions(self.KOH_FILE), {"dc"})
        self.assertEqual(choose.editions(self.KOH[0]["release"]), set())
        self.assertEqual(choose.editions(self.KOH[1]["release"]), {"dc", "roadshow"})
        self.assertEqual(choose.editions(self.KOH[2]["release"]), {"dc", "roadshow"})
        self.assertEqual(choose.editions("Blade Runner 1982 Directors Cut 1080p"), {"dc"})
        self.assertEqual(choose.editions("Aliens.1986.Special.Edition.EXTENDED.1080p"), {"extended"})
        self.assertEqual(choose.editions("American.Pie.1999.UNRATED.DVDRip"), {"unrated"})
        self.assertEqual(choose.editions("The.Lord.of.the.Rings.2001.Theatrical.Cut.720p"), {"theatrical"})
        for name in ("Movie.2001.DDC.1080p", "Movie.2001.AAC5.1-DCX", "Movie.2001.DCP.Rip", "Theatricality.2001", None):
            self.assertEqual(choose.editions(name), set(), name)

    def test_a_subtitle_of_the_files_edition_is_the_best_guess(self):
        # Kingdom of Heaven DC: the theatrical WEB subtitle won by its release group alone
        self.assertEqual([r for r, _ in choose.order(self.KOH, self.KOH_FILE, viewer_first=False)], [2, 0, 1])
        self.assertIn("DIRECTOR'S.CUT", choose.best_guess(self.KOH, self.KOH_FILE)["release"])
        self.assertEqual(choose.tier_of(1, self.KOH[1], self.KOH_FILE, viewer_first=False), 2)     # bluray: 3, up one
        self.assertLess(choose.tier_of(2, self.KOH[2], self.KOH_FILE, viewer_first=False), 2)      # group: 2, up

    def test_no_edition_in_the_file_name_changes_nothing(self):
        name = self.KOH_FILE.replace(".DC", "")
        self.assertEqual([r for r, _ in choose.order(self.KOH, name, viewer_first=False)], [0, 2, 1])
        self.assertIs(choose.best_guess(self.KOH, name), self.KOH[0])

    def test_a_subtitle_of_another_edition_goes_last(self):
        name = "The.Lord.Of.The.Rings.2001.EXTENDED.1080p.BluRay.x264-GRP.mkv"
        cands = [{"release": "LOTR.2001.Theatrical.1080p.BluRay.x264-GRP"}, {"release": "LOTR.2001.720p.WEB"},
                 {"release": "LOTR.2001.DVDRip", "ai": True}, {"release": "LOTR.2001.Extended.Edition.WEB"},
                 {"release": "LOTR.2001.HDTV"}]
        self.assertEqual([r for r, _ in choose.order(cands, name, viewer_first=False)], [3, 1, 4, 0, 2])
        self.assertEqual([r for r, _ in choose.order(cands, name)], [0, 3, 1, 4, 2])   # the viewer's first stays first
        self.assertEqual([r for r, _ in choose.order(cands[1:], name.replace("EXTENDED.", ""))], [0, 2, 3, 1])

    def test_a_hash_or_line_count_match_stays_first_whatever_its_name(self):
        cands = [{"release": "Kingdom.Of.Heaven.2005.DC.BluRay-[YTS.MX]"},
                 {"release": "Kingdom.Of.Heaven.2005.Theatrical", "hash": "ab12"}, {"release": "Kingdom.Of.Heaven.2005"}]
        self.assertEqual([r for r, _ in choose.order(cands, self.KOH_FILE, "AB12", viewer_first=False)], [1, 0, 2])
        cands[1] = {"release": "Kingdom.Of.Heaven.2005.Theatrical", "cues": 1100}
        self.assertEqual([r for r, _ in choose.order(cands, self.KOH_FILE, ref_cues=1099, viewer_first=False)], [1, 0, 2])

    def test_official_opensubtitles_entries(self):
        # Stremio's official OpenSubtitles v3 names the release in movieReleaseName and says how it matched in m
        from subsync.sources import release_info
        v3 = {"id": "9790226", "lang": "eng", "m": "i", "subtitleFileName": "The.Holdovers.2023.1080p.MA.WEB-DL.DDP5.1.Atmos.H.264.srt",
              "movieReleaseName": "The.Holdovers.2023.1080p.MA.WEB-DL.DDP5.1.Atmos.H.264", "releaseGroup": "MA"}
        info = release_info(v3)
        self.assertEqual(info["release"], "The.Holdovers.2023.1080p.MA.WEB-DL.DDP5.1.Atmos.H.264")
        self.assertFalse(info["hash_match"])
        self.assertTrue(release_info(dict(v3, m="h"))["hash_match"])
        self.assertEqual(release_info({"subtitleFileName": "A.Film.2001.BluRay-X.srt"})["release"], "A.Film.2001.BluRay-X")
        self.assertEqual(release_info({"title": "T", "movieReleaseName": "M"})["release"], "T")     # V3+ keeps its title
        cands = [{"release": "A"}, {"release": "Other.WEB-DL"}, {"release": "B", "hash_match": True},
                 {"release": "The.Holdovers.2023.1080p.BluRay.x264-GRP"}]
        self.assertEqual([r for r, _ in choose.order(cands, "The.Holdovers.2023.1080p.BluRay.x264-GRP.mkv", None)], [0, 2, 3, 1])


class PlainTrackReferenceTest(unittest.TestCase):
    """The plain track of the language is the reference, not the SDH one (on Game of Thrones S01E01 the SDH track
    trails the speech by about 0.3 s), unless the plain one is too short to pin the subtitle."""

    class Index:
        def __init__(self, tracks):
            self.tracks = tracks

        def text_tracks(self):
            return self.tracks

        def image_tracks(self):
            return []

    @staticmethod
    def track(number, name, n, lang="en"):
        return {"number": number, "language": lang, "codec": "S_TEXT/UTF8", "name": name, "forced": False,
                "cues": [(10.0 + 3 * i, 2.0) for i in range(n)]}

    def test_the_plain_track_wins_over_a_longer_sdh_one(self):
        for name in ("SDH", "English (SDH)", "English CC", "Hearing Impaired", "English [HoH]"):
            idx = self.Index([self.track(6, name, 665), self.track(5, "", 498)])
            self.assertEqual(pipeline.choose_reference(idx, 600, "eng")["number"], 5, name)

    def test_a_track_for_the_dub_comes_after_the_one_for_the_original_audio(self):
        # Spirited Away (dual audio): "For Dub Audio" has more lines, but its timing follows the English dub
        for name in ("For Dub Audio", "English (Dubtitles)", "SDH For Dub", "Dub"):
            idx = self.Index([self.track(5, name, 1216), self.track(4, "For Japanese Audio", 1074)])
            self.assertEqual(pipeline.choose_reference(idx, 1000, "eng")["number"], 4, name)

    def test_an_sdh_track_is_used_when_the_plain_one_is_too_short(self):
        idx = self.Index([self.track(6, "SDH", 665), self.track(5, "English", 40)])
        self.assertEqual(pipeline.choose_reference(idx, 600, "eng")["number"], 6)

    def test_the_language_still_comes_first(self):
        idx = self.Index([self.track(6, "SDH", 665, "en"), self.track(5, "", 600, "ru")])
        self.assertEqual(pipeline.choose_reference(idx, 600, "eng")["number"], 6)
        self.assertEqual(pipeline.choose_reference(idx, 600, "rus")["number"], 5)


class AgreeingTrackReferenceTest(unittest.TestCase):
    """With several tracks in the subtitle's language, the reference is the one whose timing agrees with
    the file's other plain tracks, not the one with the most lines (Game of Thrones S01E01: es-ES, 576 lines, runs about
    167 ms early against English and es-419, 552 lines)."""

    Index = PlainTrackReferenceTest.Index

    @staticmethod
    def track(number, lang, base, seed, lead=0.0, keep=1.0, name=""):
        """The film's lines (base) in another author's timing: each start a little off, some lines left out (keep), all
        of them `lead` seconds early."""
        r = random.Random(seed)
        cues = [(s - lead + r.gauss(0, 0.05), d) for s, d in base if r.random() < keep]
        return {"number": number, "language": lang, "codec": "S_TEXT/UTF8", "name": name, "forced": False,
                "cues": sorted(cues)}

    def setUp(self):
        base = rhythm(700, 11)
        self.es_es = self.track(12, "es-ES", base, 1, lead=0.167, keep=0.83)
        self.es_419 = self.track(11, "es-419", base, 2, keep=0.79)
        self.others = [self.track(n, lang, base, n) for n, lang in ((2, "en"), (3, "fr"), (4, "de"))]
        self.assertGreater(len(self.es_es["cues"]), len(self.es_419["cues"]))          # today's order: es-ES

    def test_the_track_that_agrees_with_the_others_wins(self):
        idx = self.Index([self.es_es, self.es_419] + self.others)
        self.assertEqual(pipeline.choose_reference(idx, 540, "spa")["number"], 11)
        self.assertEqual(pipeline.choose_reference(idx, 540, "es")["number"], 11)

    def test_one_track_in_the_language_is_unchanged(self):
        idx = self.Index([self.es_es] + self.others)
        self.assertEqual(pipeline.choose_reference(idx, 540, "spa")["number"], 12)

    def test_with_no_other_track_to_compare_with_the_most_lines_win(self):
        idx = self.Index([self.es_es, self.es_419])
        self.assertEqual(pipeline.choose_reference(idx, 540, "spa")["number"], 12)

    def test_other_languages_keep_their_reference(self):
        idx = self.Index([self.es_es, self.es_419] + self.others)
        self.assertEqual(pipeline.choose_reference(idx, 540, "rus")["number"], 2)      # the fallback, English
        self.assertEqual(pipeline.choose_reference(idx, 540, "fre")["number"], 3)

    def test_a_plain_track_still_beats_an_sdh_one_that_agrees(self):
        sdh = dict(self.es_419, name="SDH")
        idx = self.Index([self.es_es, sdh] + self.others)
        self.assertEqual(pipeline.choose_reference(idx, 540, "spa")["number"], 12)

    def test_the_agreement_is_worked_out_once_per_file(self):
        idx = self.Index([self.es_es, self.es_419] + self.others)
        calls = []
        real = pipeline.relgate
        pipeline.relgate = lambda *a, **kw: calls.append(1) or real(*a, **kw)
        try:
            pipeline.choose_reference(idx, 540, "spa")
            first = len(calls)
            for _ in range(5):
                pipeline.choose_reference(idx, 540, "spa")
        finally:
            pipeline.relgate = real
        self.assertEqual(first, 8)                                      # each Spanish track against the 4 others
        self.assertEqual(len(calls), first)


class TypesettingTrackReferenceTest(unittest.TestCase):
    """A fansub ASS track can hold tens of thousands of typesetting frames (Frieren S01E01's Portuguese track: 55,735 cues
    in 24 minutes). Against such a track any subtitle pairs by chance and the snap moves its lines onto sign frames, so a
    track is a reference only through its dialogue lines, and only when they are as dense as dialogue can be."""

    Index = PlainTrackReferenceTest.Index

    @staticmethod
    def dialogue(number, lang, n=300, step=4.5):
        return {"number": number, "language": lang, "codec": "S_TEXT/ASS", "name": "", "forced": False,
                "cues": [(30.0 + step * i, 2.2) for i in range(n)]}

    @staticmethod
    def typesetting(number, lang, frames=30000, signs=0):
        r = random.Random(number)
        cues = [(r.uniform(0, 1440), 0.042) for _ in range(frames)]                      # one cue per video frame
        cues += [(r.uniform(0, 1440), 3.0) for _ in range(signs)]                        # signs shown for a while
        cues += [(t, d) for t, _ in cues[:frames // 3] for d in (0.042,)]                # layers starting together
        return {"number": number, "language": lang, "codec": "S_TEXT/ASS", "name": "", "forced": False,
                "cues": sorted(cues)}

    def test_typesetting_frames_and_layers_are_not_lines(self):
        got = pipeline.dialogue_cues([(10.0, 2.0), (10.005, 2.5), (11.0, 0.04), (15.0, 0.0), (20.0, 1.5)])
        self.assertEqual(got, [(10.0, 2.5), (15.0, 0.0), (20.0, 1.5)])

    def test_another_languages_dialogue_track_beats_a_typesetting_track(self):
        idx = self.Index([self.typesetting(4, "pt", signs=3000), self.dialogue(2, "en")])
        ref = pipeline.choose_reference(idx, 290, "rus")
        self.assertEqual(ref["number"], 2)
        self.assertEqual(len(ref["cues"]), 300)

    def test_a_track_of_typesetting_only_is_no_reference(self):
        idx = self.Index([self.typesetting(4, "pt", signs=3000)])
        self.assertIsNone(pipeline.choose_reference(idx, 290, "rus"))
        self.assertFalse(pipeline.own_track(idx, 290))

    def test_the_dialogue_lines_are_worked_out_once_per_file(self):
        idx = self.Index([self.typesetting(4, "pt", signs=3000), self.dialogue(2, "en")])
        calls = []
        real = pipeline.dialogue_cues
        pipeline.dialogue_cues = lambda cues: calls.append(1) or real(cues)
        try:
            for _ in range(5):
                pipeline.choose_reference(idx, 290, "rus")
        finally:
            pipeline.dialogue_cues = real
        self.assertEqual(len(calls), 2)                                 # once per track, not per candidate

    def test_calibration_skips_typesetting_tracks(self):
        image = {"number": 7, "language": "pt", "codec": "S_HDMV/PGS", "forced": False, "cues": [(30.0 + 4.5 * i, 0.0) for i in range(300)]}
        self.assertEqual(pipeline.image_check(pipeline.text_references(self.Index([self.typesetting(4, "pt", signs=3000)])), [image]), [])

    def test_a_dialogue_track_with_its_signs_is_still_a_reference(self):
        track = self.dialogue(2, "en")
        track["cues"] = sorted(track["cues"] + [(31.0 + 9 * i, 0.042) for i in range(2000)]
                               + [(33.3 + 20 * i, 4.0) for i in range(60)])
        ref = pipeline.choose_reference(self.Index([track]), 290, "eng")
        self.assertEqual(ref["number"], 2)
        self.assertEqual(len(ref["cues"]), 360)


class SkipUnwantedTest(unittest.TestCase):
    """AI-made, machine-translated and forced subtitles are never tried (not even the viewer's first)."""

    def test_flagged_and_named_ones_are_left_out(self):
        cands = [{"release": "Movie.2001.BluRay", "ai": True}, {"release": "Movie 2001 machine translated"},
                 {"release": "Movie.2001.FORCED.srt"}, {"release": "Movie.2001.Foreign.Parts.Only"},
                 {"release": "Movie 2001 AI-translated"}, {"release": "Movie.2001.WEB-DL"}, {"release": "Movie Google Translate"}]
        self.assertEqual([r for r, _ in choose.order(cands, None, None, skip=True)], [5])
        self.assertEqual(len(choose.order(cands, None, None)), 7)            # off: as before (AI last)

    def test_ordinary_names_are_kept(self):
        cands = [{"release": "Forcedentry.2019.1080p"}, {"release": "Mt.Everest.Documentary"}, {"release": "Aimee.2020"}]
        self.assertEqual([r for r, _ in choose.order(cands, None, None, skip=True)], [0, 1, 2])


class EarlyRejectTest(unittest.TestCase):
    def test_broken_timing(self):
        broken = [{"s": 0.021 + i * 0.001, "e": 0.023 + i * 0.001, "kind": "dialogue", "text": "x"} for i in range(50)]
        self.assertIn("one minute", pipeline.broken_timing(broken))
        zero = [{"s": 10.0 * i, "e": 10.0 * i, "kind": "dialogue", "text": "x"} for i in range(50)]
        self.assertIn("no duration", pipeline.broken_timing(zero))
        fine = [{"s": 10.0 * i, "e": 10.0 * i + 2, "kind": "dialogue", "text": "x"} for i in range(50)]
        self.assertIsNone(pipeline.broken_timing(fine))

    def test_half_a_film_fails_the_span_rule(self):
        ref = starts(200)
        half = ref[100:]                       # a CD2 subtitle, in time for the half it has
        self.assertTrue(relgate(ref, half)["pass"])
        r = relgate(ref, half, min_span=pipeline.MIN_SPAN)
        self.assertFalse(r["pass"])
        self.assertLess(r["span"], 0.6)
        self.assertTrue(relgate(ref, ref, min_span=pipeline.MIN_SPAN)["pass"])


def styled(cues, seed, sigma=0.17, shift=0.0):
    """Another author's timing of the same lines: each start a little earlier or later (not clipped)."""
    r = random.Random(seed)
    return sorted(s + shift + r.gauss(0, sigma) for s, _ in cues)


class ClearlyBetterTest(unittest.TestCase):
    """The second way to pass: a third-party subtitle seconds off, re-timed to within its author's own timing style."""

    def setUp(self):
        self.cues = rhythm(900, 7)
        self.ref = [s for s, _ in self.cues]
        self.dur = [d for _, d in self.cues]

    def gates(self, original, copy):
        return (relgate(self.ref, original, self.dur, min_span=0.7), relgate(self.ref, copy, self.dur, min_span=0.7))

    def test_a_shifted_subtitle_in_another_timing_style_passes_only_the_second_way(self):
        before, after = self.gates(styled(self.cues, 1, shift=9.0), styled(self.cues, 1))
        self.assertFalse(before["pass"])
        self.assertFalse(after["pass"])                       # about 76 % within 200 ms: under the main bar
        self.assertLess(after["within200"], 0.90)
        self.assertEqual(after["weak_tenths_500"], [])
        ok, why = clearly_better(before, after, min_span=0.7)
        self.assertTrue(ok, why)

    def test_a_copy_no_closer_than_the_original_fails(self):
        before, after = self.gates(styled(self.cues, 2, sigma=0.20), styled(self.cues, 3, sigma=0.17))
        self.assertEqual(clearly_better(before, after, min_span=0.7), (False, "not clearly better than the original"))

    def test_a_loose_copy_fails_on_its_median(self):
        before, after = self.gates(styled(self.cues, 4, shift=9.0), styled(self.cues, 4, sigma=0.45))
        self.assertEqual(clearly_better(before, after, min_span=0.7)[1], "median error too large")

    def test_a_part_that_does_not_fit_fails(self):
        cut = styled(self.cues, 5)
        third = len(cut) // 3
        cut = sorted(cut[:2 * third] + [c + 3.0 for c in cut[2 * third:]])   # the last third belongs to another cut
        before, after = self.gates(styled(self.cues, 5, shift=9.0), cut)
        self.assertTrue(after["weak_tenths_500"])
        self.assertFalse(clearly_better(before, after, min_span=0.7)[0])

    def test_half_a_film_fails_on_span(self):
        half = styled(self.cues[:len(self.cues) // 2], 6)
        before, after = self.gates(styled(self.cues, 6, shift=9.0), half)
        self.assertEqual(clearly_better(before, after, min_span=0.7)[1], "covers too little of the runtime")

    def test_another_films_subtitle_is_never_accepted_at_any_shift(self):
        # 40 unrelated subtitles, each tried at its best constant shift (what a re-timer would find)
        accepted = 0
        for seed in range(40):
            other = [s for s, _ in rhythm(900, 1000 + seed)]
            before = relgate(self.ref, other, self.dur, min_span=0.7)
            best = None
            for step in range(-40, 41):
                g = relgate(self.ref, [c + step * 0.05 for c in other], self.dur, min_span=0.7)
                if "median_ms" in g and (best is None or g["median_ms"] < best["median_ms"]):
                    best = g
            accepted += clearly_better(before, best, min_span=0.7)[0]
        self.assertEqual(accepted, 0)

    def test_the_numbers_are_settings(self):
        before, after = self.gates(styled(self.cues, 1, shift=9.0), styled(self.cues, 1))
        self.assertFalse(clearly_better(before, after, {"median_ms": 50}, min_span=0.7)[0])
        self.assertFalse(clearly_better(before, after, {"paired": 1.01}, min_span=0.7)[0])
        self.assertFalse(clearly_better(before, after, {"ratio": 0.01}, min_span=0.7)[0])


class BestShiftTest(unittest.TestCase):
    """One steady offset (and a 24/25 fps scale) from a histogram of start differences, in milliseconds: most candidates
    differ from the release only by that (Finding Nemo's English: +1.43 s throughout), so they need no alass."""

    def setUp(self):
        self.ref = [s for s, _ in rhythm(900, 7)]

    def test_a_steady_offset_in_another_timing_style(self):
        scale, offset, votes = shift.best_shift(styled(rhythm(900, 7), 2, sigma=0.08, shift=-1.43), self.ref)
        self.assertEqual(scale, 1.0)
        self.assertAlmostEqual(offset, 1.43, delta=0.03)
        self.assertGreater(votes, 0.7 * 900)                             # sigma 80 ms: ~79 % land within 100 ms

    def test_a_pal_speed_up(self):
        pal = [s * (24000 / 1001) / 25 + 3.0 for s in self.ref]          # a 25 fps subtitle of a 23.976 fps release
        scale, offset, _ = shift.best_shift(pal, self.ref)
        self.assertAlmostEqual(scale, 25 / (24000 / 1001), places=6)
        self.assertAlmostEqual(offset, -3.0 * 25 / (24000 / 1001), delta=0.05)

    def test_another_films_subtitle_gets_few_votes(self):
        _, _, votes = shift.best_shift([s for s, _ in rhythm(900, 1001)], self.ref)
        self.assertLess(votes, 0.3 * 900)

    def test_nothing_to_go_on(self):
        self.assertEqual(shift.best_shift([], self.ref), (1.0, 0.0, 0))


class MissingSceneTest(unittest.TestCase):
    """A subtitle for another cut lacks the scenes this release adds: a stretch of the release's lines with no candidate
    line. Lines it does have can all pair, so nothing on the candidate's side shows it (Kingdom of Heaven's director's cut:
    a theatrical subtitle aligned perfectly leaves 68 consecutive release lines over 477 s uncovered; every director's-cut
    subtitle at most 11)."""

    def setUp(self):
        self.cues = rhythm(900, 7)
        self.ref = [s for s, _ in self.cues]
        self.dur = [d for _, d in self.cues]

    def without(self, starts, a, b):
        return [s for s in starts if not a <= s <= b]

    def gate(self, cand):
        return relgate(self.ref, cand, self.dur, min_span=0.7)

    def test_a_whole_subtitle_misses_no_scene(self):
        g = self.gate(styled(self.cues, 1))
        self.assertFalse(g["missing_scene"])
        self.assertLess(g["missing_lines"], 5)

    def test_a_missing_scene_fails_even_a_perfect_copy(self):
        g = self.gate(self.without(self.ref, 1500.0, 1800.0))
        self.assertTrue(g["missing_scene"])
        self.assertGreaterEqual(g["missing_lines"], 60)
        self.assertFalse(g["pass"])

    def test_a_missing_scene_is_never_clearly_better(self):
        before = self.gate(styled(self.cues, 1, shift=9.0))
        ok, why = clearly_better(before, self.gate(self.without(styled(self.cues, 1), 1500.0, 1800.0)), min_span=0.7)
        self.assertFalse(ok)
        self.assertIn("scene", why)

    def test_an_opening_song_left_out_is_not_a_missing_scene(self):
        g = self.gate(self.without(self.ref, 1500.0, 1590.0))      # 90 s, about 20 lines: a song the release subtitles
        self.assertFalse(g["missing_scene"])
        self.assertTrue(g["pass"])

    def test_only_the_candidates_own_span_counts(self):
        g = self.gate(self.without(self.ref, 0.0, 400.0))           # a late start is the span rule's business
        self.assertFalse(g["missing_scene"])


class AlignedTest(unittest.TestCase):
    """relgate.aligned: a translation against another language's track must pair in every part of the runtime, but its
    lines need not be within 500 ms there (Breaking Bad S01E02's Russian: ~250 ms median, a few tenths under half within
    500 ms, every tenth paired)."""

    def setUp(self):
        self.cues = rhythm(900, 7)
        self.ref = [s for s, _ in self.cues]
        self.dur = [d for _, d in self.cues]

    def gate_with_tenth_off(self, lo, hi):
        r = random.Random(4)
        third = self.ref[len(self.ref) // 10 * 3]
        fourth = self.ref[len(self.ref) // 10 * 4]
        cand = [s + (r.uniform(lo, hi) if third <= s < fourth else r.gauss(0, 0.15)) for s in self.ref]
        return relgate(self.ref, cand, self.dur, min_span=0.7)

    def test_a_loosely_timed_tenth_still_pairs(self):
        g = self.gate_with_tenth_off(0.55, 0.9)
        self.assertTrue(g["weak_tenths_500"])
        self.assertTrue(aligned(g, min_span=0.7)[0], g)

    def test_a_translation_with_its_own_line_splits(self):
        # Amelie's Russian against the English track (a French film): 81-82 % of its lines pair after alass, 257-289 ms
        # median, every tenth pairing; translations of a third language split lines differently
        g = {"paired": 0.82, "median_ms": 260.0, "unpaired_tenths": [], "missing_scene": False, "span": 1.0}
        self.assertTrue(aligned(g, min_span=0.7)[0])
        self.assertEqual(aligned(dict(g, paired=0.70), min_span=0.7), (False, "too few lines pair"))

    def test_a_tenth_that_does_not_pair_is_not_aligned(self):
        g = self.gate_with_tenth_off(2.0, 4.0)
        ok, why = aligned(g, min_span=0.7)
        self.assertFalse(ok)
        self.assertIn("does not fit", why)


class CrossMissingSceneTest(unittest.TestCase):
    """Against another language's track an untranslated stretch (a song, background talk, on-screen
    text) looks like a missing scene, while a subtitle for another cut can't pair 90 % of its lines under one shift (the
    lines after the extra scene are off by its length). MobLand S02E08's Russian against the en-GB track: one shift of
    60 ms, 97 % of its 624 lines paired, refused only for "a scene of the release is missing"."""

    def setUp(self):
        self.cues = rhythm(900, 7)
        self.ref = [s for s, _ in self.cues]
        self.dur = [d for _, d in self.cues]

    def translation(self, extra, gap=(1500.0, 1660.0), seed=3):
        """Another language's subtitle (start, duration): its author's own timing, a 160 s stretch left untranslated,
        and `extra` (a share of its lines) lines of its own that pair with no line of the track."""
        r = random.Random(seed)
        out = [(s + r.gauss(0, 0.15), d) for s, d in self.cues if not gap[0] <= s <= gap[1]]
        # after a line ends and over 1 s from any start: not the second half of a split line
        room = [max(a + d, a + 1.0) + 0.1 for (a, d), (b, _) in zip(self.cues, self.cues[1:])
                if b - max(a + d, a + 1.0) > 1.2 and not gap[0] - 2 <= a <= gap[1] + 2]
        out += [(m, 0.2) for m in r.sample(room, int(extra / (1 - extra) * len(out)))]
        return sorted(out)

    def gate(self, cand):
        return relgate(self.ref, [s for s, _ in cand], self.dur, min_span=0.7)

    @staticmethod
    def srt(cand, shift_s=0.0):
        return "\n".join(f"{i}\n{subs.ts(s + shift_s)} --> {subs.ts(s + d + shift_s)}\nline {i}\n"
                         for i, (s, d) in enumerate(cand, 1))

    def index(self):
        return PlainTrackReferenceTest.Index([{"number": 2, "language": "en", "codec": "S_TEXT/UTF8", "name": "",
                                               "forced": False, "cues": list(self.cues)}])

    def test_an_untranslated_stretch_under_one_shift_is_aligned(self):
        g = self.gate(self.translation(0.05))
        self.assertTrue(g["missing_scene"])
        self.assertGreaterEqual(g["paired"], 0.9)
        self.assertEqual(aligned(g, min_span=0.7), (False, "a scene of the release is missing (another cut)"))
        self.assertTrue(aligned(g, min_span=0.7, one_shift=True)[0])     # moved by one shift, or as it is

    def test_a_subtitle_that_pairs_less_under_one_shift_is_still_refused(self):
        g = self.gate(self.translation(0.25))
        self.assertTrue(g["missing_scene"])
        self.assertLessEqual(g["paired"], 0.8)
        self.assertEqual(aligned(g, min_span=0.7, one_shift=True), (False, "a scene of the release is missing (another cut)"))

    def test_the_shift_path_offers_it_across_languages_only(self):
        text = self.srt(self.translation(0.05), shift_s=5.0)
        r = pipeline.shift_sync(self.index(), text, "rus", better={})
        self.assertEqual(r["outcome"], "synced", r.get("reason"))
        self.assertTrue(r["reason"].startswith("aligned to another language's track (one shift)"), r["reason"])
        r = pipeline.shift_sync(self.index(), text, "eng", better={})       # against its own language's track
        self.assertEqual(r["outcome"], "keep_original")
        self.assertTrue(r["gate_synced"]["missing_scene"])

    def test_an_original_in_place_is_in_sync_across_languages_only(self):
        text = self.srt(self.translation(0.05))
        rule = dict(relgate_mod.BETTER)
        self.assertTrue(pipeline.check_original(self.index(), text, "rus", better=rule)["in_sync"])
        self.assertFalse(pipeline.check_original(self.index(), text, "eng", better=rule)["in_sync"])

    def test_a_theatrical_subtitle_against_an_extended_cut_is_still_refused(self):
        r = random.Random(17)
        t, film = 20.0, []
        for _ in range(600):                                                   # about 10 lines a minute
            d = r.uniform(1.0, 3.5)
            film.append((t, d))
            t += d + r.uniform(1.5, 6.0)
        self.cues, self.ref = film, [s for s, _ in film]
        theatrical = self.translation(0.0, gap=(0.0, -1.0))                   # every line translated
        cut = self.ref[400]                                                    # the release adds a 300 s scene here
        scene = [(cut + 1.0 + 6.0 * k, 2.0) for k in range(50)]
        self.cues = sorted([(s + (300.0 if s >= cut else 0.0), d) for s, d in film] + scene)
        self.ref = [s for s, _ in self.cues]
        for shift_s in (5.0, 0.0):
            res = pipeline.shift_sync(self.index(), self.srt(theatrical, shift_s), "rus", better={})
            self.assertEqual(res["outcome"], "keep_original", res.get("reason"))
            self.assertLess(res["gate_synced"]["paired"], relgate_mod.ONE_SHIFT_PAIRED)   # one shift: the cut breaks it
        self.assertFalse(pipeline.check_original(self.index(), self.srt(theatrical), "rus",
                                                 better=dict(relgate_mod.BETTER))["in_sync"])


class ImageRecallTest(unittest.TestCase):
    """An image reference holds only its sure starts (Kingdom of Heaven's PGS track: 1,099 of about 1,500 lines, 73 %),
    so a perfect candidate pairs at most that share of its lines, also when it has fewer lines than the reference."""

    def ref(self, n, sure_share):
        return {"route": "image", "cues": [(10.0 + 3 * i, 2.0) for i in range(n)], "image": {"sure_share": sure_share}}

    def test_recall_is_the_share_of_lines_the_track_pins(self):
        self.assertEqual(pipeline.reference_gate(self.ref(1099, 0.732), 1034)[1], 0.732)

    def test_a_longer_candidate_still_pairs_at_most_the_references_lines(self):
        self.assertAlmostEqual(pipeline.reference_gate(self.ref(500, 0.9), 1000)[1], 0.5)

    def test_a_text_reference_pins_every_line(self):
        self.assertEqual(pipeline.reference_gate({"route": "cues", "cues": [(1.0, 1.0)] * 10}, 1000)[1], 1.0)


AUDIO_BETTER = {"matched": 25, "miss": 0.30, "median_ms": 150.0, "within": 0.55, "min_shift_s": 1.0}


class AudioClearlyBetterTest(unittest.TestCase):
    """The audio route's second way to pass. Synthetic speech: each release line is spoken a little off its start
    (sigma 80 ms); a third-party subtitle has its own timing style (sigma 150 ms) and is 9 s off. Like Primer's YTS
    MP4, the re-timed copy then fails the speech gate (about 70 % within 200 ms, needs 90 %) and passes this
    rule."""

    def setUp(self):
        self.truth = rhythm(700, 11)

    def fit(self, starts, better=AUDIO_BETTER, durations=None):
        import numpy as np
        from subsync.audiofit import AudioFit
        r = random.Random(5)
        speech = [(s + r.gauss(0, 0.08), s + d) for s, d in self.truth]
        end = self.truth[-1][0] + self.truth[-1][1] + 30

        class Index:
            def duration(self):
                return end

        class TruthFit(AudioFit):
            def _read_window(self, start, length):   # speech straight from the timeline: no audio, no VAD
                self.windows_read += 1
                self.audio_s += length
                n = int(round(length * 100))
                mask, onsets = np.zeros(n, dtype=bool), []
                for a, b in speech:
                    if b <= start or a >= start + length:
                        continue
                    mask[max(0, int((a - start) * 100)):min(n, int((b - start) * 100))] = True
                    if a >= start:
                        onsets.append(a - self.lead)
                return {"start": start, "len": length, "mask": mask, "onsets": np.array(onsets)}
        durations = durations or [d for _, d in self.truth]
        lines = [{"s": s, "e": s + d, "text": "x", "kind": "dialogue"} for s, d in zip(starts, durations)]
        return TruthFit(Index(), lines, vad=object(), better=better).run()

    def styled(self, seed, shift, sigma=0.15):
        r = random.Random(seed)
        return [s + shift + r.gauss(0, sigma) for s, _ in self.truth]

    def test_a_subtitle_9_s_off_in_another_timing_style_passes_only_the_second_way(self):
        fit = self.fit(self.styled(1, -9.0))
        self.assertFalse(fit["gate_new"]["pass"])                 # the speech gate alone refuses it
        self.assertLess(fit["gate_new"]["within200"], 0.90)
        self.assertEqual((fit["decision"], fit["why"]), ("synced", "clearly better than the original"))
        self.assertAlmostEqual(fit["model"]["offset"], 9.0, delta=0.3)
        moved = statistics.median(n["s"] - o for n, o in zip(fit["lines"], self.styled(1, -9.0)))
        self.assertAlmostEqual(moved, 9.0, delta=0.15)

    def test_a_right_fit_whose_windows_do_not_agree_still_passes(self):
        # On a real film most fit windows give no label (music, silence) and a silent one may give a wrong one;
        # the held-out windows, never used for the fit, are the confirmation
        end = self.truth[-1][0] + self.truth[-1][1] + 30
        margin, span = 0.03 * end, (end - 0.06 * end) / 8
        quiet = [(margin + (i + 0.5) * span - 40, margin + (i + 0.5) * span + 40) for i in (0, 2, 4, 6)]
        self.truth = [(s, d) for s, d in self.truth if not any(lo <= s <= hi for lo, hi in quiet)]   # no one speaks there
        fit = self.fit(self.styled(1, -9.0))
        self.assertEqual((fit["decision"], fit["why"]), ("synced", "clearly better than the original"))
        self.assertAlmostEqual(fit["model"]["offset"], 9.0, delta=0.3)

    def test_without_the_rule_it_is_kept(self):
        fit = self.fit(self.styled(1, -9.0), better=None)
        self.assertEqual(fit["decision"], "keep_original")

    def test_a_shift_under_one_second_is_not_clearly_better(self):
        fit = self.fit(self.styled(2, -0.5))
        self.assertEqual(fit["decision"], "keep_original")

    def test_a_subtitle_made_for_this_release_fits_as_it_is(self):
        # Rips of Primer's YTS release fit its speech at a 52-58 ms median with 81 % of lines within
        # 200 ms, short of the speech gate, and the fit would move them by under 100 ms: listed as they are
        fit = self.fit(self.styled(3, 0.05))
        self.assertFalse(fit["gate_old"]["pass"])                 # the speech gate alone refuses the original
        self.assertEqual((fit["decision"], fit["why"]), ("in_sync", "fits the speech as it is"))
        self.assertIsNone(fit["lines"])                           # nothing re-timed: the original is listed

    def test_fits_as_it_is_needs_the_rule(self):
        self.assertEqual(self.fit(self.styled(3, 0.05), better=None)["decision"], "keep_original")
        self.assertEqual(self.fit(self.styled(3, 0.05), better=dict(AUDIO_BETTER, median_ms=50.0))["decision"],
                         "keep_original")

    def test_the_numbers_are_settings(self):
        fit = self.fit(self.styled(1, -9.0), better=dict(AUDIO_BETTER, median_ms=50.0))
        self.assertEqual(fit["decision"], "keep_original")
        fit = self.fit(self.styled(1, -9.0), better=dict(AUDIO_BETTER, within=0.95))
        self.assertEqual(fit["decision"], "keep_original")

    def test_another_films_subtitle_is_never_accepted(self):
        accepted = []
        for seed in range(40):
            other = rhythm(700, 3000 + seed)
            fit = self.fit([s for s, _ in other], durations=[d for _, d in other])
            if fit["decision"] != "keep_original":
                accepted.append((seed, fit["decision"], fit.get("why"), fit["gate_new"]))
        self.assertEqual(accepted, [])


class SparseDialogueTest(AudioClearlyBetterTest):
    """A film with little dialogue (Upstream Color: 10 lines heard in the windows) gives the audio route too
    few lines when its windows are spread evenly; placed where the subtitle has its lines, the same windows hear enough."""
    test_a_subtitle_9_s_off_in_another_timing_style_passes_only_the_second_way = None
    test_a_right_fit_whose_windows_do_not_agree_still_passes = test_without_the_rule_it_is_kept = None
    test_a_shift_under_one_second_is_not_clearly_better = test_a_subtitle_made_for_this_release_fits_as_it_is = None
    test_fits_as_it_is_needs_the_rule = test_the_numbers_are_settings = test_another_films_subtitle_is_never_accepted = None

    def setUp(self):
        r = random.Random(17)
        self.truth = []
        for k in range(14):                     # 14 short scenes of talk in 96 minutes, silence between them
            t = 200.0 + k * 400.0 + r.uniform(0, 150)
            for _ in range(18):
                d = r.uniform(1.0, 3.0)
                self.truth.append((t, d))
                t += d + r.uniform(0.3, 1.5)

    def test_windows_go_where_the_lines_are(self):
        fit = self.fit(self.styled(3, -6.0, sigma=0.12))
        self.assertGreaterEqual(fit["gate_new"]["matched"], 25, fit["gate_new"])
        self.assertEqual(fit["decision"], "synced")
        self.assertAlmostEqual(fit["model"]["offset"], 6.0, delta=0.3)

    def test_fit_and_check_windows_never_overlap(self):
        fit = self.fit(self.styled(3, -6.0, sigma=0.12))
        fits, checks = fit["plan"]["fit"], fit["plan"]["check"]
        for a in checks:
            self.assertTrue(all(abs(a - b) >= fit["plan"]["window_s"] for b in fits), (a, fits))


class OtherAudioTrackTest(unittest.TestCase):
    """On a dual-audio file the default track may be a dub whose speech does not follow the subtitles (Haibane
    Renmei: English dub default, the subtitles follow the Japanese). When the fit on the default track fails and the file
    has an audio track in another language, the fit runs on that one too (same rule)."""

    def setUp(self):
        self.truth = rhythm(700, 11)
        self.dub = rhythm(700, 4242)                               # the dub: speech at other times

    def fit(self, starts, follows=1):
        import numpy as np
        from subsync import audiofit
        r = random.Random(5)
        end = max(self.truth[-1][0] + self.truth[-1][1], self.dub[-1][0] + self.dub[-1][1]) + 30
        speech = {follows: [(s + r.gauss(0, 0.08), s + d) for s, d in self.truth],      # the track the subtitle follows
                  1 - follows: [(s + r.gauss(0, 0.08), s + d) for s, d in self.dub]}

        class Index:
            def duration(self):
                return end

            def audio_tracks(self):
                return [{"index": 0, "language": "eng", "default": True}, {"index": 1, "language": "jpn", "default": False}]

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
        lines = [{"s": s, "e": s + d, "text": "x", "kind": "dialogue"} for s, (_, d) in zip(starts, self.truth)]
        return audiofit.fit_tracks(Index(), lines, vad=object(), better=AUDIO_BETTER, fit_class=TruthFit)

    def test_the_other_track_is_tried_when_the_default_fails(self):
        r = random.Random(1)
        fit = self.fit([s - 9.0 + r.gauss(0, 0.15) for s, _ in self.truth])
        self.assertEqual((fit["decision"], fit.get("audio_track")), ("synced", 1))
        self.assertAlmostEqual(fit["model"]["offset"], 9.0, delta=0.3)

    def test_the_default_track_is_kept_when_it_fits(self):
        r = random.Random(1)
        fit = self.fit([s - 9.0 + r.gauss(0, 0.15) for s, _ in self.truth], follows=0)   # the default is the right one
        self.assertEqual((fit["decision"], fit.get("audio_track")), ("synced", None))


class AudioBudgetTest(unittest.TestCase):
    """A file's audio windows fit its audio budget (default 1 GB): on a high-bitrate file the windows get shorter;
    when they would have to be shorter than the minimum, the original is kept without reading any audio."""

    def setUp(self):
        self.truth = rhythm(700, 11)

    def fit(self, rate, budget, starts=None, spent=0):
        import numpy as np
        from subsync.audiofit import AudioFit
        truth = self.truth
        r = random.Random(5)
        speech = [(s + r.gauss(0, 0.08), s + d) for s, d in truth]
        end = truth[-1][0] + truth[-1][1] + 30

        class Index:
            size = int(rate * end)
            audio_bytes = spent

            def duration(self):
                return end

        class Fit(AudioFit):
            def _read_window(self, start, length):
                self.index.audio_bytes += int(length * rate)     # what a read of this window costs
                self.windows_read += 1
                n = int(round(length * 100))
                mask, onsets = np.zeros(n, dtype=bool), []
                for a, b in speech:
                    if b <= start or a >= start + length:
                        continue
                    mask[max(0, int((a - start) * 100)):min(n, int((b - start) * 100))] = True
                    if a >= start:
                        onsets.append(a - self.lead)
                return {"start": start, "len": length, "mask": mask, "onsets": np.array(onsets)}
        rr = random.Random(1)
        starts = starts or [s - 9.0 + rr.gauss(0, 0.15) for s, _ in truth]
        lines = [{"s": s, "e": s + d, "text": "x", "kind": "dialogue"} for s, (_, d) in zip(starts, truth)]
        idx = Index()
        fit = Fit(idx, lines, vad=object(), better=AUDIO_BETTER, budget=budget)
        return fit, fit.run(), idx

    def test_a_low_bitrate_file_keeps_its_30_s_windows(self):
        fit, out, idx = self.fit(rate=200_000, budget=10 ** 9)
        self.assertEqual(fit.W, 30.0)
        self.assertEqual(out["decision"], "synced")

    def test_a_high_bitrate_file_gets_shorter_windows_within_the_budget(self):
        fit, out, idx = self.fit(rate=2_500_000, budget=10 ** 9)   # 20 Mbit/s: 30 s windows would need about 1.2 GB
        self.assertLess(fit.W, 30.0)
        self.assertLessEqual(idx.audio_bytes, 10 ** 9)
        self.assertEqual(out["decision"], "synced")
        self.assertAlmostEqual(out["model"]["offset"], 9.0, delta=0.3)

    def test_too_high_a_bitrate_keeps_the_original_without_reading_audio(self):
        fit, out, idx = self.fit(rate=20_000_000, budget=10 ** 9)  # 160 Mbit/s
        self.assertEqual(out["decision"], "keep_original")
        self.assertIn("audio budget", out["why"])
        self.assertEqual((fit.windows_read, idx.audio_bytes), (0, 0))

    def test_a_window_that_would_pass_the_budget_is_not_read(self):
        fit, out, idx = self.fit(rate=200_000, budget=10 ** 9, spent=10 ** 9 - 1_000_000)   # windows of other subtitles
        self.assertEqual(out["decision"], "keep_original")
        self.assertIn("audio budget", out["why"])
        self.assertLessEqual(idx.audio_bytes, 10 ** 9)


class AudioWindowReuseTest(unittest.TestCase):
    def test_a_second_subtitle_of_the_same_file_reuses_the_windows_already_read(self):
        from subsync.audiofit import AudioFit

        class Index:
            def __init__(self):
                self.reads = 0

            def duration(self):
                return 6000.0

            def window_pcm(self, start, length, sr, ffmpeg):
                self.reads += 1
                return b"\0\0" * int(sr * length)

        class Silent:
            def speech(self, audio):
                return []
        lines = [{"s": 1.0, "e": 2.0, "text": "x", "kind": "dialogue"}]
        idx, other = Index(), Index()
        a, b = AudioFit(idx, lines, vad=Silent()), AudioFit(idx, lines, vad=Silent())
        a.read_window(10.0, 30.0)
        a.read_window(10.0, 30.0)
        b.read_window(10.0, 30.0)
        b.read_window(50.0, 30.0)
        self.assertEqual(idx.reads, 2)
        AudioFit(other, lines, vad=Silent()).read_window(10.0, 30.0)   # another file reads its own
        self.assertEqual(other.reads, 1)


class AudioPrefetchTest(unittest.TestCase):
    """12 Angry Men (YIFY MP4, no subtitle track): the first audio verdict came 27 s after the pick, 14 windows read one
    after another (about 1 to 1.5 s each), so the pick (it waits 14 s) got the unchecked original. The planned windows are
    now read up to 4 at once: the same windows and bytes, sooner."""

    def index(self, delay=0.2):
        import threading as th

        class Index:
            size, audio_bytes = 10 ** 9, 0

            def __init__(self):
                self.reads, self.busy, self.peak, self.lock = [], 0, 0, th.Lock()

            def duration(self):
                return 6000.0

            def window_pcm(self, start, length, sr, ffmpeg):
                with self.lock:
                    self.busy += 1
                    self.peak = max(self.peak, self.busy)
                time.sleep(delay)
                with self.lock:
                    self.busy -= 1
                    self.reads.append(start)
                return b"\0\0" * int(sr * length)
        return Index()

    class Silent:
        def speech(self, audio):
            return []

    def test_planned_windows_are_read_four_at_a_time_and_kept(self):
        from subsync.audiofit import AudioFit
        idx = self.index()
        fit = AudioFit(idx, [{"s": 1.0, "e": 2.0, "text": "x", "kind": "dialogue"}], vad=self.Silent())
        starts = [100.0 * k for k in range(1, 13)]
        t0 = time.time()
        fit.prefetch(starts, 30.0)
        took = time.time() - t0
        self.assertEqual(sorted(idx.reads), starts)
        self.assertEqual(idx.peak, 4)
        self.assertLess(took, 1.2)                             # 12 reads of 0.2 s: 0.6 s at 4 at once, 2.4 s one by one
        self.assertEqual((fit.windows_read, fit.audio_s), (12, 360.0))
        fit.read_window(100.0, 30.0)
        fit.prefetch(starts, 30.0)
        self.assertEqual(len(idx.reads), 12)                   # kept: nothing is read twice

    def test_two_fits_of_one_file_at_once_read_each_window_once(self):
        import threading as th
        from subsync.audiofit import AudioFit
        idx = self.index(delay=0.05)
        lines = [{"s": 1.0, "e": 2.0, "text": "x", "kind": "dialogue"}]
        fits = [AudioFit(idx, lines, vad=self.Silent()) for _ in range(2)]   # English and Russian, same windows
        starts = [100.0 * k for k in range(1, 9)]
        ts = [th.Thread(target=f.prefetch, args=(starts, 30.0)) for f in fits]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(idx.reads), starts)

    def test_a_failed_read_is_left_to_the_ordered_read(self):
        from subsync.audiofit import AudioFit
        from subsync.reader import ReadFailed
        idx = self.index(delay=0.0)
        plain = idx.window_pcm

        def flaky(start, length, sr, ffmpeg):
            if start == 300.0:
                raise ReadFailed("short reply")
            return plain(start, length, sr, ffmpeg)
        idx.window_pcm = flaky
        fit = AudioFit(idx, [{"s": 1.0, "e": 2.0, "text": "x", "kind": "dialogue"}], vad=self.Silent())
        fit.prefetch([100.0, 200.0, 300.0, 400.0], 30.0)       # no error here
        self.assertEqual(sorted(idx.reads), [100.0, 200.0, 400.0])
        with self.assertRaises(ReadFailed):
            fit.read_window(300.0, 30.0)                       # raised where the fit reads it

    def test_a_refusal_stops_the_prefetch(self):
        from subsync.audiofit import AudioFit
        from subsync.reader import Refused
        idx = self.index(delay=0.05)
        plain = idx.window_pcm

        def refusing(start, length, sr, ffmpeg):
            if start == 100.0:
                raise Refused("HTTP 429")
            return plain(start, length, sr, ffmpeg)
        idx.window_pcm = refusing
        fit = AudioFit(idx, [{"s": 1.0, "e": 2.0, "text": "x", "kind": "dialogue"}], vad=self.Silent())
        with self.assertRaises(Refused):
            fit.prefetch([100.0 * k for k in range(1, 13)], 30.0)
        self.assertLessEqual(len(idx.reads), 4)                # those already running end; the rest are not asked

    def test_prefetch_stops_at_the_audio_budget(self):
        from subsync.audiofit import AudioFit
        idx = self.index(delay=0.0)                            # 10**9 bytes over 6000 s: 166,667 B/s
        per = 1.25 * 30.0 * idx.size / 6000.0
        fit = AudioFit(idx, [{"s": 1.0, "e": 2.0, "text": "x", "kind": "dialogue"}], vad=self.Silent(), budget=3.5 * per)
        fit.prefetch([100.0 * k for k in range(1, 9)], 30.0)
        self.assertEqual(len(idx.reads), 3)                    # the fourth would pass the budget: left to read_window


def pgs_events(lines, merge_every=0):
    """PGS cue times for lines (start, duration): show and clear, the clear left out when the next line starts at the
    same moment (every merge_every-th line made contiguous)."""
    ev = []
    for k, (s, d) in enumerate(lines):
        ev.append(s)
        nxt = lines[k + 1][0] if k + 1 < len(lines) else None
        if nxt is None or nxt - (s + d) > 0.01:
            ev.append(s + d)
    return [(t, 0.0) for t in ev]


def contiguous(lines, every):
    out = list(lines)
    for k in range(0, len(out) - 1, every):
        s, _ = out[k]
        out[k] = (s, out[k + 1][0] - s)      # this line ends exactly when the next starts: no clear event
    return out


class ImageRefTest(unittest.TestCase):
    def setUp(self):
        self.lines = rhythm(400, 11)

    def starts_found(self, ref_lines, got):
        g = relgate([s for s, _ in ref_lines], [s for s, _ in got], [d for _, d in ref_lines])
        return g

    def test_pgs_with_every_clear_gives_the_line_starts(self):
        # Blu-ray style: every line has its clear (2-frame gaps in dialogue, pauses between scenes)
        r = random.Random(5)
        t, lines = 30.0, []
        for _ in range(400):
            d = r.uniform(1.0, 4.5)
            lines.append((t, d))
            x = r.random()
            t += d + (0.083 if x < 0.45 else r.uniform(0.25, 1) if x < 0.7 else r.uniform(1, 8) if x < 0.9 else r.uniform(8, 40))
        ref = imageref.reference({"codec": "S_HDMV/PGS", "cues": pgs_events(lines)})
        self.assertTrue(ref["plausible"])
        got = [round(s, 3) for s, _ in ref["cues"]]
        truth = {round(s, 3) for s, _ in lines}
        self.assertTrue(all(x in truth for x in got))
        self.assertGreater(len(got) / len(lines), 0.75)                        # 82 % here; 83-95 % on real timings

    def test_no_pauses_and_no_short_gaps_means_no_guessing(self):
        ref = imageref.reference({"codec": "S_HDMV/PGS", "cues": pgs_events(self.lines)})   # nothing pins the labels
        self.assertFalse(ref["plausible"])

    def test_left_out_clears_keep_only_sure_starts(self):
        # gaps as in real subtitles (22-36 % back to back, 13-67 % under 0.25 s, 3-15 % over 8 s); a back-to-back
        # line has no clear event, so plain alternation slips; the sure starts must be right, even if fewer
        r = random.Random(21)
        t, lines = 30.0, []
        for _ in range(600):
            d = r.uniform(1.0, 4.5)
            lines.append((t, d))
            x = r.random()
            gap = 0.0 if x < 0.30 else r.uniform(0.04, 0.2) if x < 0.45 else r.uniform(0.25, 1) if x < 0.70 else \
                r.uniform(1, 8) if x < 0.90 else r.uniform(8, 40)
            t += d + gap
        ref = imageref.reference({"codec": "S_HDMV/PGS", "cues": pgs_events(lines)})
        truth = {round(s, 3) for s, _ in lines}
        got = [round(s, 3) for s, _ in ref["cues"]]
        self.assertTrue(ref["plausible"])
        self.assertGreater(sum(x in truth for x in got) / len(got), 0.95)   # precision
        self.assertGreater(len(got) / len(lines), 0.4)                         # recall

    def test_vobsub_starts_and_noise_is_not_plausible(self):
        r = imageref.reference({"codec": "S_VOBSUB", "cues": [(s, 0.0) for s, _ in self.lines]})
        self.assertEqual(len(r["cues"]), len(self.lines))
        self.assertTrue(r["plausible"])
        rnd = random.Random(3)
        noise = sorted((rnd.uniform(0, 5000), 0.0) for _ in range(60))   # sparse random events: long "shown" times
        self.assertFalse(imageref.reference({"codec": "S_HDMV/PGS", "cues": noise})["plausible"])

    def test_calibration_compares_an_image_track_with_the_text_track(self):
        text = [{"number": 3, "language": "eng", "codec": "S_TEXT/ASS", "forced": False, "cues": self.lines}]
        image = [{"number": 7, "language": "en", "codec": "S_HDMV/PGS", "forced": False, "cues": pgs_events(self.lines)}]
        got = pipeline.image_check(text, image)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]["vs_text"]["pass"])
        self.assertEqual(pipeline.image_check(text, [dict(image[0], language="ger")]), [])


class HashTest(unittest.TestCase):
    def test_oshash_matches_reference_formula(self):
        data = bytes(range(256)) * 1024  # 256 KiB
        size = len(data)
        h = size
        for off in (0, size - 65536):
            for i in range(0, 65536, 8):
                h = (h + struct.unpack("<Q", data[off + i: off + i + 8])[0]) & 0xFFFFFFFFFFFFFFFF
        self.assertEqual(oshash(size, data[:65536], data[-65536:]), f"{h:016x}")


class LangTest(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(langs.lang3("en"), "eng")
        self.assertEqual(langs.lang3("ru-RU"), "rus")
        self.assertEqual(langs.lang3("English"), "eng")
        self.assertEqual(langs.lang3("fra"), "fre")
        self.assertEqual(langs.name("rus"), "Russian")

    def test_every_sign_up_language_has_its_name(self):
        # Entry labels in players read "<name> (synced)": a language offered at sign-up must never show as its code.
        from subsync import aiosconfig
        for name, two, three in aiosconfig.LANGUAGES:
            with self.subTest(three=three):
                self.assertEqual(langs.name(three), name)
                self.assertEqual(langs.lang3(name), three)
                self.assertEqual(langs.lang3(two), three)


class VadRuleTest(unittest.TestCase):
    def test_timestamps_min_speech_and_silence(self):
        p = [0.0] * 10 + [0.9] * 10 + [0.0] * 3 + [0.9] * 10 + [0.0] * 2 + [0.9] * 5 + [0.0] * 10 + [0.9] * 2 + [0.0] * 10
        segs = timestamps(p, len(p) * FRAME)
        # a 3-frame dip (96 ms) splits (min silence 60 ms counts from the dip's first frame); a 2-frame dip doesn't;
        # a 2-frame blip (64 ms) is under the 100 ms minimum
        self.assertEqual(segs, [(10 * FRAME, 20 * FRAME), (23 * FRAME, 40 * FRAME)])

    def test_trailing_speech(self):
        p = [0.0] * 5 + [0.9] * 20
        self.assertEqual(timestamps(p, len(p) * FRAME), [(5 * FRAME, 25 * FRAME)])


if __name__ == "__main__":
    unittest.main()
