"""Pure pieces: subtitle text, the gate, the per-line snap, the OpenSubtitles hash, language codes, the VAD
timestamp rule."""
import os
import random
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subsync import choose, imageref, langs, pipeline, refine, subs  # noqa: E402
from subsync.mkv import oshash  # noqa: E402
from subsync.relgate import relgate  # noqa: E402
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
