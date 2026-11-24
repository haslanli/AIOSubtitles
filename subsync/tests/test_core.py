"""Pure pieces: subtitle text, the gate, the per-line snap, the OpenSubtitles hash, language codes, the VAD
timestamp rule."""
import random
import statistics
import struct
import time
import unittest

from helpers import (AUDIO_BETTER, FakeIndex, pgs_left_out, realistic, rhythm, rules, timeline_fit,
                     traced_memory)
from subsync import choose, imageref, langs, pipeline, refine, shift, subs
from subsync import relgate as relgate_mod
from subsync.addon import as_srt
from subsync.mkv import oshash
from subsync.relgate import aligned, clearly_better, relgate
from subsync.vad import FRAME, timestamps

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


# a line in each language and the Windows encoding it is usually saved in (subs.decode)
LEGACY_TEXTS = (("eng", "cp1252", "It’s “fine”… — €5"), ("fre", "cp1252", "Ça va? « Oui », c’est l’été."),
                ("ita", "cp1252", "Perché? Così è la città."),                   # any language not in the table
                ("pol", "cp1250", "Cześć, jak się masz? Źle. Łódź"), ("cze", "cp1250", "Příliš žluťoučký kůň úpěl"),
                ("hun", "cp1250", "Árvíztűrő tükörfúrógép"), ("hrv", "cp1250", "Šta želiš? Čekaj, ćao."),
                ("rum", "cp1250", "Ştiu că ţara şi îmi pâine"), ("rus", "cp1251", "Привет, как дела?"),
                ("bul", "cp1251", "Здравей, как си?"), ("mac", "cp1251", "Здраво, како си?"),
                ("gre", "cp1253", "Γεια σου, τι κάνεις;"), ("tur", "cp1254", "Şimdi gitmeliyiz, ağabey"),
                ("heb", "cp1255", "שלום, מה שלומך?"), ("ara", "cp1256", "مرحبا، كيف حالك؟"),
                ("per", "cp1256", "سلام، حال شما چطور است؟"), ("lit", "cp1257", "Labas, kaip sekasi? Ačiū"),
                ("vie", "cp1258", "Đi đâu? Tôi không chào"), ("tha", "cp874", "สวัสดีครับ"),
                ("jpn", "cp932", "何を言っているのか分からない。"), ("kor", "cp949", "무슨 말을 하는지 모르겠어."),
                ("srp", "cp1250", "Šta želiš? Čekaj, ćao, đak."),                 # Serbian in Latin letters
                ("srp", "cp1251", "Шта желиш? Чекај, ћао, ђак."),                 # and in Cyrillic
                ("chi", "gbk", "我不知道你在说什么，我们明天再走吧。"),                   # GBK
                ("chi", "cp950", "我不知道你在說什麼，我們明天再走吧。"))                 # Big5

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
        srt = subs.to_srt(lines, "ass")
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

    def test_timing_srt_sorts_the_cues_and_gives_each_at_least_the_minimum_length(self):
        self.assertEqual(subs.MIN_REFERENCE_CUE_S, 0.5)
        self.assertEqual(subs.timing_srt([(5.0, 2.0), (1.0, 0.0), (3.0, 0.25)]),
                         "1\n00:00:01,000 --> 00:00:01,500\nx\n\n2\n00:00:03,000 --> 00:00:03,500\nx\n\n"
                         "3\n00:00:05,000 --> 00:00:07,000\nx\n")

    def test_decode(self):
        self.assertEqual(subs.decode("﻿Hi".encode("utf-8")), "Hi")
        self.assertEqual(subs.decode("Привет".encode("cp1251"), "rus"), "Привет")

    def test_a_subtitle_that_isnt_unicode_is_read_in_its_languages_usual_encoding(self):
        # Anything but UTF-8 was read as Latin-1 (only Cyrillic languages by their first two letters,:
        # so not Bulgarian or Macedonian, and Romanian and Latin Serbian wrongly): garbled, or refused as another
        # language
        for lang, enc, text in LEGACY_TEXTS:
            with self.subTest(lang=lang, enc=enc):
                self.assertEqual(subs.decode(text.encode(enc), lang), text)
        self.assertEqual(subs.decode(b"caf\xe9 \x81", "eng"), "caf\xe9 \x81")       # Latin-1 last (0x81: no cp1252)

    def test_unicode_is_read_by_its_byte_order_mark_or_without_one(self):
        # UTF-16 ("Unicode" in Windows tools) was read with a zero between the characters, and parsed to
        # nothing
        srt = "1\n00:00:01,000 --> 00:00:02,000\nHello there, Привет\n"
        for enc in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-8-sig"):
            with self.subTest(enc=enc):
                text = subs.decode(srt.encode(enc), "eng")
                self.assertEqual(text, srt)
                self.assertEqual([l["text"] for l in subs.parse(text)], ["Hello there, Привет"])

    def test_chinese_japanese_and_korean_utf16_without_a_mark_is_read(self):
        # A character such as 一 (U+4E00), 가 (U+AC00) or the ideographic space (U+3000) puts a zero in the
        # half of the bytes that had to have none, so such a file was read with a zero between the characters: no line
        for lang, line in (("chi", "我们一起去吧"), ("jpn", "こんにちは\u3000世界"), ("kor", "내가 가자"),
                           ("chi", "我知道\u3000但是\u3000你不能\u3000一个人去")):   # a few zeros in every cue
            srt = "".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\n{line}\n\n" for i in range(1, 30))
            for enc in ("utf-16-le", "utf-16-be"):
                with self.subTest(line=line, enc=enc):
                    text = subs.decode(srt.encode(enc), lang)
                    self.assertEqual(text, srt)
                    self.assertEqual([l["text"] for l in subs.parse(text)], [line] * 29)

    def test_a_few_bytes_its_encoding_cant_read_leave_a_subtitle_in_its_language(self):
        # One byte the language's encoding doesn't define, or a character cut off at the end, made the whole
        # file Western (cp1252, else Latin-1): garbled, or refused by the language check
        for lang, enc, line, bad in (("rus", "cp1251", "Привет, как дела?", b"\x98"),     # 0x98: none in cp1251
                                     ("gre", "cp1253", "Γεια σου, τι κάνεις;", b"\xaa"),
                                     ("jpn", "cp932", "何を言っているのか分からない。", b"\x82"),   # half a character
                                     ("eng", "cp1252", "It’s “fine”… — €5", b"\x81")):   # 0x81: none in cp1252
            first = f"1\n00:00:01,000 --> 00:00:01,500\n{line}\n\n"
            rest = "".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\n{line}\n\n" for i in range(2, 60))
            with self.subTest(lang=lang, where="inside"):
                text = subs.decode(first.encode(enc) + bad + rest.encode(enc), lang)
                self.assertEqual(text, first + "\ufffd" + rest)
            with self.subTest(lang=lang, where="at the end"):
                self.assertEqual(subs.decode((first + rest).encode(enc) + bad, lang), first + rest + "\ufffd")
        # beyond BAD_BYTE_SHARE of the file, Western (and Latin-1 last, test above)
        raw = "Привет".encode("cp1251") + b"\x98"
        self.assertEqual(subs.decode(raw, "rus"), raw.decode("cp1252"))

    def test_cr_cr_lf_line_ends_are_one_line_end(self):
        # CR CR LF (a CR LF text saved again in text mode on Windows) was read as a line end and a blank line,
        # so every cue lost its text, and the copy served had every cue blank
        srt = ("1\n00:00:01,000 --> 00:00:02,000\nHello there,\nhow are you?\n\n"
               "2\n00:00:03,000 --> 00:00:04,000\nFine.\n")
        lines = subs.parse(srt)
        crcrlf = srt.replace("\n", "\r\r\n")
        self.assertEqual(subs.parse(crcrlf), lines)
        self.assertEqual(subs.write_like(crcrlf, subs.parse(crcrlf)), subs.write_like(srt, lines))
        self.assertEqual(subs.fingerprint(crcrlf), subs.fingerprint(srt))
        ass = ASS.replace("\n", "\r\r\n")
        self.assertEqual(subs.write_like(ass, subs.parse(ass)), subs.write_like(ASS, subs.parse(ASS)))
        # a CR alone is one line end (old Mac files), so two of them are a blank line
        self.assertEqual(subs.parse(srt.replace("\n", "\r")), lines)
        self.assertEqual([l["text"] for l in subs.parse("1\r00:00:01,000 --> 00:00:02,000\rA\r\rB\r")], ["A"])
        t0 = time.monotonic()   # a long run of CRs is read in linear time
        self.assertEqual(subs.parse(srt + "\r" * 200000 + "x"), lines)
        self.assertEqual(subs.parse(srt + "\r" * 200000 + "\nx"), lines)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_a_utf8_subtitle_with_a_stray_byte_stays_utf8(self):
        # One bad byte made the whole file Latin-1 ("CafÃ©"); a file in cp1252 with a few curly quotes and no
        # other character beyond ASCII is no UTF-8 file
        good = "Café naïve, déjà vu — " * 40
        text = subs.decode(good.encode("utf-8") + b"\x92" + good.encode("utf-8"), "fre")
        self.assertEqual(text, good + "\ufffd" + good)
        body = "Hello there, how are you doing today? " * 60 + "It’s fine."
        self.assertEqual(subs.decode(body.encode("cp1252"), "eng"), body)

    def test_one_subtitle_in_two_encodings_is_one(self):
        # The README's "another text encoding ... doesn't make it a different subtitle"
        srt = "".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\nIt’s “fine” — {i}…\n\n" for i in range(1, 30))
        self.assertEqual(subs.fingerprint(subs.decode(srt.encode("utf-8"), "eng")),
                         subs.fingerprint(subs.decode(srt.encode("cp1252"), "eng")))

    def test_a_dialogue_line_whose_time_cant_be_read_is_kept_as_it_is_and_not_counted(self):
        bad = "Dialogue: 0,0:00:0x.00,0:00:04.00,Default,,0,0,0,,unreadable"
        text = ASS.replace("Comment: ", bad + "\nComment: ")
        lines = subs.parse(text)
        self.assertEqual([l["text"] for l in lines], ["Hello, there", "{\\pos(10,10)}A SIGN", "la la"])
        out = subs.write_like(text, [dict(l, s=l["s"] + 1, e=l["e"] + 1) for l in lines])
        self.assertIn(bad, out)
        self.assertEqual([round(l["s"], 2) for l in subs.parse(out)], [2.0, 4.0, 66.25])

    def test_a_long_run_of_digits_is_no_time_and_is_read_in_linear_time(self):
        digits = "1" * 30000   # each search took about 3 s when a time could start at any of them
        srt = f"1\n{digits} --> 00:00:02,000\nx\n\n2\n00:00:03,000 --> 00:00:04,000\ny\n"
        ass = ASS.replace("0:00:01.00,0:00:02.50", f"{digits},0:00:02.50")
        t0 = time.monotonic()
        self.assertEqual([l["text"] for l in subs.parse(srt)], ["y"])
        lines = subs.parse(ass)
        self.assertEqual([l["text"] for l in lines], ["{\\pos(10,10)}A SIGN", "la la"])
        self.assertIn(f"Dialogue: 0,{digits},0:00:02.50,Default", subs.write_like(ass, lines))   # left as it is
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_an_hour_too_long_to_be_one_is_no_time(self):
        for digits in (300, 400):   # 300 was read as 3.6e303 s; 308 and more raised OverflowError
            srt = "1\n00:00:01,000 --> 00:00:02,000\nHi\n\n2\n" + "9" * digits + ":00:00,000 --> 00:00:05,000\nx\n"
            self.assertEqual([l["text"] for l in subs.parse(srt)], ["Hi"])
            self.assertEqual(subs.fingerprint(srt), subs.fingerprint(srt.split("\n\n2\n")[0]))
        long = "1\n123:00:01,000 --> 123:00:02,500\nHi\n\n2\n1:02:03,4 --> 1:02:04.5\nThere\n"
        self.assertEqual([(l["s"], l["e"]) for l in subs.parse(long)],
                         [(442801.0, 442802.5), (3723.4, 3724.5)])   # ordinary times read as before

    def test_tags_are_stripped_as_the_old_pattern_did(self):
        import re
        rng = random.Random(7)
        for _ in range(4000):
            text = "".join(rng.choice("{}<>a \\N") for _ in range(rng.randrange(0, 14)))
            for html, pattern in ((True, r"\{[^}]*\}|<[^>]*>"), (False, r"\{[^}]*\}")):
                for repl in ("", " "):
                    self.assertEqual(subs.strip_tags(text, repl, html), re.sub(pattern, repl, text), (text, html))
        self.assertEqual(subs.strip_tags("{a{\\i1}text"), "text")             # as libass reads it
        self.assertEqual(subs.strip_tags("{{a}} hello"), "} hello")
        self.assertEqual(subs.strip_tags("<<b>x I <3"), "x I <3")

    def test_unclosed_tags_are_read_in_linear_time(self):
        n = 64000   # one cue of them took about 8 s through a fingerprint, 1.2 s through each other pass
        for opener in "{<":
            text = f"1\n00:00:01,000 --> 00:00:02,000\n{opener * n}\n"
            t0 = time.monotonic()
            subs.fingerprint(text)
            self.assertEqual(subs.plain_line("<i>A</i> {\\i1}b{\\i0} " + opener * n), "a b " + opener * n)
            self.assertEqual(subs.to_srt(subs.parse(text)), text)
            self.assertIsNotNone(subs.timing_recipe(text, text.replace("00:00:01,000", "00:00:01,500")))
            self.assertLess(time.monotonic() - t0, 2.0, opener)

    def test_song_and_sign_styles_are_told_by_their_names_whole_words(self):
        # "OP_Romaji", "Opening", "Karaoke" and the like were dialogue (only names starting "op "/"op-" or
        # "song" were songs), "TS" and "Title" too, and any name with "sign" in it ("Design") was a sign
        kinds = {"song": ("OP_Romaji", "ED_TL", "OPRomaji", "Opening", "Ending", "Karaoke", "Kara", "Lyrics",
                          "Insert Song", "OP Romaji", "OP-Romaji", "OP", "ed", "ED-English", "Song_JP", "Songs"),
                 "sign": ("TS", "Title", "Typeset", "Sign", "signs", "Main_Signs"),
                 "dialogue": ("Default", "Design", "Assignment", "Main_Top", "Italics", "Speaker", "")}
        for kind, styles in kinds.items():
            for style in styles:
                with self.subTest(style=style):
                    self.assertEqual(subs.kind_of_style(style), kind)
        ass = ASS.replace(",Song,", ",OP_Romaji,").replace(",Sign,,", ",TS,,")
        self.assertEqual([l["kind"] for l in subs.parse(ass)], ["dialogue", "sign", "song"])

    def test_srt_cues_without_a_blank_line_between_them_are_read_apart(self):
        # The second cue was read as the first one's text, so it was in no measure, and the copy showed its
        # index and its timing line as dialogue
        srt = ("1\n00:00:01,000 --> 00:00:02,000\nHello\n2\n00:00:03,000 --> 00:00:04,000\nWorld\nagain\n\n"
               "3\n00:00:05,000 --> 00:00:06,000\n42\n")
        lines = subs.parse(srt)
        self.assertEqual([(l["s"], l["text"]) for l in lines], [(1.0, "Hello"), (3.0, "World\nagain"), (5.0, "42")])
        vtt = "WEBVTT\n\n" + srt.replace(",", ".")
        self.assertEqual([(l["s"], l["text"]) for l in subs.parse(vtt)], [(l["s"], l["text"]) for l in lines])
        copy = subs.write_like(srt, [dict(l, s=l["s"] + 10, e=l["e"] + 10) for l in lines])
        self.assertEqual(copy, "1\n00:00:11,000 --> 00:00:12,000\nHello\n\n2\n00:00:13,000 --> 00:00:14,000\n"
                               "World\nagain\n\n3\n00:00:15,000 --> 00:00:16,000\n42\n")

    def test_a_superscript_before_a_timing_line_is_text_not_an_index(self):
        # Isdigit takes "²", which no index is: a cue whose last line was it lost that line
        srt = "1\n00:00:01,000 --> 00:00:02,000\nE = mc\n\u00b2\n00:00:03,000 --> 00:00:04,000\nWorld\n"
        self.assertEqual([(l["s"], l["text"]) for l in subs.parse(srt)], [(1.0, "E = mc\n\u00b2"), (3.0, "World")])

    def test_ass_is_written_as_srt_without_drawings_with_whole_lines_and_their_styles(self):
        # A drawing's commands were shown as text, an empty line inside a line ended the cue (the rest was
        # lost), and italics, bold and underline were dropped; {\anN} (shown at the top) went too
        events = ("Dialogue: 0,0:00:01.00,0:00:02.00,Sign,,0,0,0,,"
                  "{\\an7\\pos(0,0)\\p1}m 0 0 l 100 0 100 100 0 100{\\p0}\n"
                  "Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,Hello\\N\\Nworld\n"
                  "Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,"
                  "{\\i1}Ital{\\i0}, {\\b1}bold{\\b0} and {\\u1}under\n"
                  "Dialogue: 0,0:00:07.00,0:00:08.00,Default,,0,0,0,,"
                  "{\\an8\\fad(100,100)\\t(\\i1)}On top\\hhere{\\r}\n")
        ass = ASS.split("Dialogue:")[0] + events
        srt = ("1\n00:00:03,000 --> 00:00:04,000\nHello\nworld\n\n"
               "2\n00:00:05,000 --> 00:00:06,000\n<i>Ital</i>, <b>bold</b> and <u>under</u>\n\n"
               "3\n00:00:07,000 --> 00:00:08,000\n{\\an8}On top here\n")
        self.assertEqual(as_srt(ass), srt)                                    # its "(original)" entry
        self.assertEqual(subs.to_srt(subs.parse(ass), "ass"), srt)            # a copy, for a player that asks for SRT
        self.assertEqual([l["text"] for l in subs.parse(srt)][0], "Hello\nworld")
        self.assertEqual(subs.write_like(ass, subs.parse(ass)), ass)          # the ASS copy itself is unchanged

    def test_webvtt_is_written_as_srt_with_its_codes_as_characters(self):
        # "Tom &amp; Jerry", "<v Bob>", "<c.yellow>", "&nbsp;" and "&lrm;" showed as they were
        vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Bob>Tom &amp; Jerry</v> <c.yellow>hi</c>&nbsp;there\n\n"
               "00:00:03.000 --> 00:00:04.000\n"
               "<i>It's</i> <lang fr>bon</lang> <b.loud>now</b> <00:00:03.500><u>go</u>\n\n"
               "00:00:05.000 --> 00:00:06.000\n&lrm;שלום&rlm;\n&nbsp;\n")
        srt = ("1\n00:00:01,000 --> 00:00:02,000\nTom & Jerry hi\xa0there\n\n"
               "2\n00:00:03,000 --> 00:00:04,000\n<i>It's</i> bon <b>now</b> <u>go</u>\n\n"
               "3\n00:00:05,000 --> 00:00:06,000\n‎שלום‏\n")
        self.assertEqual(as_srt(vtt), srt)                                    # its "(original)" entry
        lines = subs.parse(vtt)
        copy = subs.write_like(vtt, [dict(l, s=l["s"] + 1, e=l["e"] + 1) for l in lines])
        self.assertEqual([l["text"] for l in subs.parse(copy)], [l["text"] for l in subs.parse(srt)])
        self.assertEqual(subs.apply_recipe(vtt, subs.timing_recipe(vtt, copy)), copy)   # public mode still rebuilds it

    def test_an_srt_copy_keeps_its_originals_text_as_it_is(self):
        # Every {...} was taken out of an SRT's lines, so a line marked {\an8} moved from the top of the
        # picture to the bottom in its copy
        srt = ("1\n00:00:01,000 --> 00:00:02,000\n{\\an8}A sign at the top\n\n"
               "2\n00:00:03,000 --> 00:00:04,000\n<i>Hello</i> {\\i1}there{\\i0}\n- Hi\n")
        copy = subs.write_like(srt, [dict(l, s=l["s"] + 1, e=l["e"] + 1) for l in subs.parse(srt)])
        self.assertEqual(copy, srt.replace("00:00:01,000 --> 00:00:02,000", "00:00:02,000 --> 00:00:03,000")
                                  .replace("00:00:03,000 --> 00:00:04,000", "00:00:04,000 --> 00:00:05,000"))
        self.assertEqual(subs.apply_recipe(srt, subs.timing_recipe(srt, copy)), copy)

    def test_the_srt_writers_read_unclosed_tags_in_linear_time(self):
        n = 64000
        t0 = time.monotonic()
        for fmt, text in (("ass", "{" * n), ("ass", "{\\i1" * n), ("ass", "{\\i1}x" * n), ("vtt", "<" * n),
                          ("vtt", "<a" * n)):
            subs.to_srt([{"s": 1.0, "e": 2.0, "text": text, "kind": "dialogue"}], fmt)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(subs.to_srt([{"s": 1.0, "e": 2.0, "text": "a {b", "kind": "dialogue"}], "ass"),
                         "1\n00:00:01,000 --> 00:00:02,000\na {b\n")    # an opener with no closer is text

    def test_a_webvtt_tag_opener_before_a_long_word_is_read_in_linear_time(self):
        # The tag's name and the rest after it could both take the same letters, so a '<' with a long run
        # and no '>' after it took time by the square of the run (a 64,000-letter cue: about 17 s, holding every
        # thread), on the .srt routes (as_srt), in a copy (write_like) and in public mode's recipe
        n = 64000
        t0 = time.monotonic()
        for text in ("<" + "a" * n, "</" + "a" * n, "<a." + "b" * n, "<a " + "b" * n):
            self.assertEqual(subs._vtt_srt_text(text), text)            # an opener with no closer is text
        vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<" + "x" * n + "\n"
        self.assertEqual(as_srt(vtt), "1\n00:00:01,000 --> 00:00:02,000\n<" + "x" * n + "\n")
        copy = subs.write_like(vtt, [dict(l, s=l["s"] + 1, e=l["e"] + 1) for l in subs.parse(vtt)])
        self.assertEqual(subs.apply_recipe(vtt, subs.timing_recipe(vtt, copy)), copy)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(subs._vtt_srt_text("<c.a.b>x</c> <v Tom Lee>hi</v> <i.loud>y</i> </b/>"),
                         "x hi <i>y</i> </b>")                        # names, classes and annotations as before


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

    def test_an_error_of_exactly_the_bar_counts_as_within_it(self):
        # Millisecond times exactly 200 ms (100 ms) apart come out a hair either side of it as floats
        ms = [600_000 + 3_217 * i for i in range(200)]
        ref = [t / 1000 for t in ms]
        for off in (200, -200):
            r = relgate(ref, [(t + off) / 1000 for t in ms])
            self.assertEqual((r["within200"], r["weak_tenths"], r["weak_tenths_500"]), (1.0, [], []), off)
        self.assertTrue(relgate(ref, [(t + 100) / 1000 for t in ms])["pass"])          # the median bar: 100 ms
        ends = relgate(ref, ref, [1.0] * len(ref), cand_ends=[(t + 1300) / 1000 for t in ms])
        self.assertEqual(ends["end_within300"], 1.0)


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

    @staticmethod
    def line(s, e, text="n", **kw):
        return dict({"s": s, "e": e, "text": text, "kind": "dialogue"}, **kw)

    @staticmethod
    def dense(seed, split=False):
        """Dense random dialogue against its release track: (lines, release cues); with `split`, about a third of the
        lines followed by a split half that starts inside the same release line."""
        r = random.Random(seed)
        ref, t = [], 10.0
        for _ in range(r.randint(50, 150)):
            d = r.uniform(0.3, 2.0)
            ref.append((t, d))
            t += r.uniform(0.1, 0.8) if r.random() < 0.5 else r.uniform(0.8, 3.0)
        cand = []
        for s, d in ref:
            if r.random() < 0.7:
                s2 = s + 0.2 + r.gauss(0, 0.2)
                cand.append(RefineTest.line(s2, s2 + d))
                if split and r.random() < 0.3:
                    s3 = s2 + r.uniform(0.05, d)
                    cand.append(RefineTest.line(s3, s3 + d / 2))
        return cand, ref

    def assert_what_the_snap_guarantees(self, cand, ref, seed):
        out, _ = refine.snap(cand, ref)
        idx = sorted(range(len(cand)), key=lambda i: cand[i]["s"])
        self.assertEqual([out[i]["s"] for i in idx], sorted(out[i]["s"] for i in idx), seed)
        for i in idx:
            need = min(refine.MIN_DISPLAY, cand[i]["e"] - cand[i]["s"])
            self.assertGreaterEqual(round(out[i]["e"] - out[i]["s"], 9), round(need, 9), seed)
        for x, y in zip(idx, idx[1:]):
            if cand[x]["e"] <= cand[y]["s"] and out[x]["e"] > out[y]["s"] + 1e-9:
                need = min(refine.MIN_DISPLAY, cand[x]["e"] - cand[x]["s"])
                self.assertAlmostEqual(out[x]["e"], out[x]["s"] + need, places=9, msg=seed)

    def test_what_the_snap_guarantees(self):
        # Order kept, the minimum display kept, and a line that didn't overlap the next one doesn't after,
        # unless the minimum display is what makes it (it wins), over dense random dialogue
        for seed in range(300):
            self.assert_what_the_snap_guarantees(*self.dense(seed), seed)

    def test_the_order_is_kept_through_split_continuations(self):
        # P snaps to a long release line (40.0, 4 s), c is its split continuation and keeps p's shift (41.1),
        # y snaps to a second voice starting inside it (41.05); x, between them, was first raised to c, then lowered to
        # y by an order fix that never looked back, so c came after x. The same guarantees with split halves.
        around = [(t, 2.0) for t in (28.0, 31.0, 34.0, 37.0)]
        after = [(t, 2.0) for t in (46.0, 49.0, 52.0, 55.0)]
        lines = [self.line(t, t + 2.0) for t, _ in around]
        lines += [self.line(39.9, 40.8, "p"), self.line(41.0, 41.5, "c"), self.line(41.03, 41.5, "x"),
                  self.line(41.06, 41.9, "y")]
        lines += [self.line(t, t + 2.0) for t, _ in after]
        out, st = refine.snap(lines, around + [(40.0, 4.0), (41.05, 1.0)] + after)
        self.assertEqual(st["continuations"], 1)
        self.assertEqual([(o["text"], round(o["s"], 9)) for o in out[4:8]],
                         [("p", 40.0), ("c", 41.05), ("x", 41.05), ("y", 41.05)])
        for seed in range(3000):
            self.assert_what_the_snap_guarantees(*self.dense(seed, split=True), seed)

    def test_lines_a_sectioned_fit_left_out_take_no_part(self):
        # A dropped line (a scene the release cut) takes no release line and counts in no stat
        ref = [(10.0 + 3 * i, 2.0) for i in range(10)]
        lines = [self.line(s + 0.1, s + 1.5) for s, _ in ref]
        lines[4] = self.line(22.2, 23.5, "kept")
        lines.insert(4, self.line(22.0, 23.0, "cut", drop=True))
        out, st = refine.snap(lines, ref)
        self.assertAlmostEqual(out[5]["s"], 22.0, places=9)         # the kept line gets its release line
        self.assertAlmostEqual(out[4]["s"], 21.806, places=3)       # the dropped one moves by the shift around it
        self.assertEqual((st["dialogue"], st["snapped"], st["continuations"]), (10, 10, 0))

    def test_an_end_lands_only_on_a_release_line_of_known_duration(self):
        # 400 ms off its neighbours, the line is kept only when its end lands on its release line's end;
        # a duration unknown in the index (0) gives no end to land on
        around = (10.0, 13.0, 16.0, 19.0, 25.0, 28.0, 31.0, 34.0)
        lines = [self.line(t, t + 2.0) for t in around] + [self.line(21.6, 21.8, "x")]
        known = [(t, 2.0) for t in around] + [(22.0, 0.2)]
        out, st = refine.snap(lines, known)
        self.assertEqual((round(out[-1]["s"], 9), st["snapped"]), (22.0, 9))
        out, st = refine.snap(lines, known[:-1] + [(22.0, 0.0)])
        self.assertEqual((round(out[-1]["s"], 9), st["snapped"]), (21.6, 8))

    def test_a_leftover_line_never_crosses_a_snapped_one(self):
        # X's second pass took 78.399, past y's own 78.295, and the
        # order fix then moved y onto x's start
        ref = [(77.59, 1.736), (77.742, 0.502), (78.295, 1.266), (78.399, 0.452), (80.013, 0.809), (80.779, 0.702)]
        lines = [self.line(78.06, 79.796, "x"), self.line(78.186, 78.688, "y"), self.line(80.742, 81.551, "z")]
        out, st = refine.snap(lines, ref)
        self.assertAlmostEqual(out[1]["s"], 78.295, places=9)
        self.assertLess(out[0]["s"], out[1]["s"])
        self.assertEqual(st["snapped"], 2)

    def test_two_leftover_lines_wanting_one_release_line_the_nearer_gets_it(self):
        # The local shift (+0.45) puts a at 24.45 and b at 24.6, both near the unused 24.7; b is nearer
        around = (10.0, 13.0, 16.0, 19.0, 30.0, 33.0, 36.0, 39.0)
        ref = [(t + 0.45, 1.0) for t in around] + [(24.7, 1.0)]
        lines = [self.line(t, t + 1.0) for t in around] + [self.line(24.0, 25.0, "a"), self.line(24.15, 25.15, "b")]
        out, st = refine.snap(lines, ref)
        self.assertAlmostEqual(out[-1]["s"], 24.7, places=9)
        self.assertLess(out[-2]["s"], 24.7)
        self.assertEqual(st["continuations"], 0)

    def test_a_continuation_follows_the_release_line_its_predecessor_snapped_to(self):
        # Two release lines start at 40.0 (0.5 s and 3.0 s); p snaps to the long one, z to the short one,
        # and c, 1 s into the long one, follows p
        around = [(t, 2.0) for t in (28.0, 31.0, 34.0, 37.0)]
        ref = around + [(40.0, 0.5), (40.0, 3.0)] + [(t + 0.2, 2.0) for t in (45.8, 48.8, 51.8, 54.8)]
        lines = [self.line(t, t + 2.0) for t, _ in around]
        lines += [self.line(39.9, 39.95, "z"), self.line(40.0, 40.9, "p"), self.line(41.0, 42.5, "c")]
        lines += [self.line(t, t + 2.0) for t in (45.8, 48.8, 51.8, 54.8)]
        out, st = refine.snap(lines, ref)
        self.assertEqual(st["continuations"], 1)
        self.assertAlmostEqual(out[6]["s"], 41.0, places=9)


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

    def test_the_guess_when_nothing_can_be_checked_yet(self):
        # Detectorists S01E01 WEBRip (no subtitle track): the viewer's first, a DVD rip, was 41.7 s off; the WEB one fits
        # the speech as it is and is AutoSync's pick by name. The guess is order's first with no place of the
        # viewer's own (Worker._name_order)
        cands = [{"release": "Detectorists S01E01 Episode 1.1.DVDRip.NonHI.en.ACORN"},
                 {"release": "Detectorists.S01E01.WEB"}, {"release": "Detectorists.S01E01.1080p.WEB-DL.DD+2.0.H.264-SbR"}]
        name = "Detectorists.S01E01.WEBRip.x264-XEN0N.mkv"
        self.assertIs(choose.order(cands, name, viewer_first=False)[0][1], cands[1])
        hashed = cands + [{"release": "x", "hash": "ab12"}]
        self.assertIs(choose.order(hashed, name, "AB12", viewer_first=False)[0][1], hashed[3])   # made for this very file
        self.assertIs(choose.order(cands, None, viewer_first=False)[0][1], cands[0])   # nothing known: the viewer's first
        self.assertEqual(choose.order([{"release": "x", "ai": True}], name, skip=True, viewer_first=False), [])
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
        self.assertIn("DIRECTOR'S.CUT", choose.order(self.KOH, self.KOH_FILE, viewer_first=False)[0][1]["release"])
        self.assertEqual(choose.tier_of(1, self.KOH[1], self.KOH_FILE, viewer_first=False), 2)     # bluray: 3, up one
        self.assertLess(choose.tier_of(2, self.KOH[2], self.KOH_FILE, viewer_first=False), 2)      # group: 2, up

    def test_no_edition_in_the_file_name_changes_nothing(self):
        name = self.KOH_FILE.replace(".DC", "")
        self.assertEqual([r for r, _ in choose.order(self.KOH, name, viewer_first=False)], [0, 2, 1])
        self.assertIs(choose.order(self.KOH, name, viewer_first=False)[0][1], self.KOH[0])

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

    @staticmethod
    def track(number, name, n, lang="en"):
        return {"number": number, "language": lang, "codec": "S_TEXT/UTF8", "name": name, "forced": False,
                "cues": [(10.0 + 3 * i, 2.0) for i in range(n)]}

    def test_the_plain_track_wins_over_a_longer_sdh_one(self):
        for name in ("SDH", "English (SDH)", "English CC", "Hearing Impaired", "English [HoH]"):
            idx = FakeIndex(text=[self.track(6, name, 665), self.track(5, "", 498)])
            self.assertEqual(pipeline.choose_reference(idx, 600, "eng")["number"], 5, name)

    def test_a_track_for_the_dub_comes_after_the_one_for_the_original_audio(self):
        # Spirited Away (dual audio): "For Dub Audio" has more lines, but its timing follows the English dub
        for name in ("For Dub Audio", "English (Dubtitles)", "SDH For Dub", "Dub"):
            idx = FakeIndex(text=[self.track(5, name, 1216), self.track(4, "For Japanese Audio", 1074)])
            self.assertEqual(pipeline.choose_reference(idx, 1000, "eng")["number"], 4, name)

    def test_an_sdh_track_is_used_when_the_plain_one_is_too_short(self):
        idx = FakeIndex(text=[self.track(6, "SDH", 665), self.track(5, "English", 40)])
        self.assertEqual(pipeline.choose_reference(idx, 600, "eng")["number"], 6)

    def test_the_language_still_comes_first(self):
        idx = FakeIndex(text=[self.track(6, "SDH", 665, "en"), self.track(5, "", 600, "ru")])
        self.assertEqual(pipeline.choose_reference(idx, 600, "eng")["number"], 6)
        self.assertEqual(pipeline.choose_reference(idx, 600, "rus")["number"], 5)


class AgreeingTrackReferenceTest(unittest.TestCase):
    """With several tracks in the subtitle's language, the reference is the one whose timing agrees with
    the file's other plain tracks, not the one with the most lines (Game of Thrones S01E01: es-ES, 576 lines, runs about
    167 ms early against English and es-419, 552 lines)."""

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
        idx = FakeIndex(text=[self.es_es, self.es_419] + self.others)
        self.assertEqual(pipeline.choose_reference(idx, 540, "spa")["number"], 11)
        self.assertEqual(pipeline.choose_reference(idx, 540, "es")["number"], 11)

    def test_one_track_in_the_language_is_unchanged(self):
        idx = FakeIndex(text=[self.es_es] + self.others)
        self.assertEqual(pipeline.choose_reference(idx, 540, "spa")["number"], 12)

    def test_with_no_other_track_to_compare_with_the_most_lines_win(self):
        idx = FakeIndex(text=[self.es_es, self.es_419])
        self.assertEqual(pipeline.choose_reference(idx, 540, "spa")["number"], 12)

    def test_other_languages_keep_their_reference(self):
        idx = FakeIndex(text=[self.es_es, self.es_419] + self.others)
        self.assertEqual(pipeline.choose_reference(idx, 540, "rus")["number"], 2)      # the fallback, English
        self.assertEqual(pipeline.choose_reference(idx, 540, "fre")["number"], 3)

    def test_a_plain_track_still_beats_an_sdh_one_that_agrees(self):
        sdh = dict(self.es_419, name="SDH")
        idx = FakeIndex(text=[self.es_es, sdh] + self.others)
        self.assertEqual(pipeline.choose_reference(idx, 540, "spa")["number"], 12)

    def test_the_agreement_is_worked_out_once_per_file(self):
        idx = FakeIndex(text=[self.es_es, self.es_419] + self.others)
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

    def test_a_file_with_many_text_tracks_is_compared_within_bounds(self):
        # Each track of the language was compared with every other plain track (T x (T - 1) relgate calls:
        # about 2 minutes of CPU for 256 tracks of 1,000 lines); now at most AGREE_CANDIDATES of the language, each
        # against at most AGREE_OTHERS plain tracks, those with the most lines
        base = rhythm(120, 12)
        spanish = [self.track(100 + k, "es", base, 100 + k, keep=0.9) for k in range(12)]
        french = [self.track(200 + k, "fr", base, 200 + k, keep=0.8) for k in range(80)]
        compared = []
        real = pipeline.relgate
        pipeline.relgate = lambda ref, cand, *a, **kw: compared.append((len(ref), len(cand))) or real(ref, cand, *a, **kw)
        try:
            pipeline.choose_reference(FakeIndex(text=spanish + french), 100, "spa")
        finally:
            pipeline.relgate = real
        self.assertEqual(len(compared), pipeline.AGREE_CANDIDATES * pipeline.AGREE_OTHERS)   # 12 x 91 before
        # the 11 other Spanish tracks (the longest) and the 53 longest French ones: no shorter track is compared
        lengths = sorted((len(t["cues"]) for t in spanish[1:] + french), reverse=True)
        self.assertGreaterEqual(min(n for n, _ in compared), lengths[pipeline.AGREE_OTHERS])


class TypesettingTrackReferenceTest(unittest.TestCase):
    """A fansub ASS track can hold tens of thousands of typesetting frames (Frieren S01E01's Portuguese track: 55,735 cues
    in 24 minutes). Against such a track any subtitle pairs by chance and the snap moves its lines onto sign frames, so a
    track is a reference only through its dialogue lines, and only when they are as dense as dialogue can be."""

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
        idx = FakeIndex(text=[self.typesetting(4, "pt", signs=3000), self.dialogue(2, "en")])
        ref = pipeline.choose_reference(idx, 290, "rus")
        self.assertEqual(ref["number"], 2)
        self.assertEqual(len(ref["cues"]), 300)

    def test_a_track_of_typesetting_only_is_no_reference(self):
        idx = FakeIndex(text=[self.typesetting(4, "pt", signs=3000)])
        self.assertIsNone(pipeline.choose_reference(idx, 290, "rus"))
        self.assertFalse(pipeline.own_track(idx, 290))

    def test_the_dialogue_lines_are_worked_out_once_per_file(self):
        idx = FakeIndex(text=[self.typesetting(4, "pt", signs=3000), self.dialogue(2, "en")])
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
        refs = pipeline.text_references(FakeIndex(text=[self.typesetting(4, "pt", signs=3000)]))
        self.assertEqual(pipeline.image_check(refs, [image]), [])

    def test_a_dialogue_track_with_its_signs_is_still_a_reference(self):
        track = self.dialogue(2, "en")
        track["cues"] = sorted(track["cues"] + [(31.0 + 9 * i, 0.042) for i in range(2000)]
                               + [(33.3 + 20 * i, 4.0) for i in range(60)])
        ref = pipeline.choose_reference(FakeIndex(text=[track]), 290, "eng")
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

    def test_the_settings_defaults_are_the_gates(self):
        # The defaults of this rule and of the cross-language one are kept in one place
        from subsync.settings import Settings
        self.assertEqual(Settings({}).better, relgate_mod.BETTER)
        self.assertIsNone(Settings({"SUBSYNC_BETTER": "off"}).better)

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

    def test_another_films_subtitle_is_refused_at_the_shift_the_shift_path_finds(self):
        # 40 unrelated subtitles, each moved as the one-shift path moves a subtitle (pipeline.shift_sync): by the scale
        # and offset shift.best_shift finds for it, any offset within MAX_OFFSET_S, the frame-rate scales too
        for seed in range(40):
            other = [s for s, _ in rhythm(900, 1000 + seed)]
            scale, offset, _ = shift.best_shift(other, self.ref)
            before = relgate(self.ref, other, self.dur, min_span=0.7)
            after = relgate(self.ref, [c * scale + offset for c in other], self.dur, min_span=0.7)
            with self.subTest(seed=seed):
                self.assertFalse(clearly_better(before, after, min_span=0.7)[0])

    def test_the_numbers_are_settings(self):
        before, after = self.gates(styled(self.cues, 1, shift=9.0), styled(self.cues, 1))
        self.assertFalse(clearly_better(before, after, {"median_ms": 50}, min_span=0.7)[0])
        self.assertFalse(clearly_better(before, after, {"paired": 1.01}, min_span=0.7)[0])
        self.assertFalse(clearly_better(before, after, {"ratio": 0.01}, min_span=0.7)[0])


class BestShiftTest(unittest.TestCase):
    """One steady offset (and a frame-rate scale) from a histogram of start differences, in milliseconds: most subtitles
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

    def test_a_24_fps_subtitle_of_a_23_976_fps_release(self):
        # 7 s of drift over two hours; the audio route tried this pair, the one-shift path didn't
        film = 24000 / 1001
        for scale in (24 / film, film / 24, 25 / 24, 24 / 25):
            with self.subTest(scale=scale):
                cand = [s / scale + 2.0 for s in self.ref]
                got, offset, votes = shift.best_shift(cand, self.ref)
                self.assertAlmostEqual(got, scale, places=6)
                self.assertAlmostEqual(offset, -2.0 * scale, delta=0.05)
                self.assertEqual(votes, len(self.ref))

    def test_one_list_of_frame_rate_scales_for_both_paths(self):
        from subsync import audiofit
        self.assertIs(audiofit.SCALES, shift.SCALES)
        self.assertEqual((audiofit.END_WEIGHT, audiofit.DT), (shift.END_WEIGHT, shift.BIN_S))
        self.assertEqual(shift.SCALES[0], 1.0)
        self.assertEqual(len(set(shift.SCALES)), 7)

    def test_another_films_subtitle_gets_few_votes(self):
        _, _, votes = shift.best_shift([s for s, _ in rhythm(900, 1001)], self.ref)
        self.assertLess(votes, 0.3 * 900)

    def test_nothing_to_go_on(self):
        self.assertEqual(shift.best_shift([], self.ref), (1.0, 0.0, 0))

    def test_of_equal_peaks_the_smallest_offset_and_then_the_earlier(self):
        # One peak picker with ends and without
        self.assertEqual(shift._offset([10.0], [8.0, 13.0], 120.0), -2.0)
        self.assertEqual(shift._offset([10.0], [8.0, 12.0], 120.0), -2.0)
        self.assertEqual(shift._offset([10.0], [8.0, 12.0], 120.0, ([11.0], [9.0, 13.0])), -2.0)
        self.assertEqual(shift._offset([10.0], [8.0, 12.0], 120.0, ([11.0], [13.0])), 2.0)     # the end decides
        self.assertEqual(shift._offset([10.0], [200.0], 120.0), 0.0)                          # nothing in reach


class StartWindowTest(unittest.TestCase):
    """The start-difference histogram is taken a block at a time, against only the reference starts a block can
    reach: the same bins as every pair at once, in memory that doesn't grow with the line counts."""

    @staticmethod
    def every_pair(cand, ref, max_offset):
        """The histogram as it was made from the whole |ref| x |cand| difference matrix."""
        import numpy as np
        diffs = np.subtract.outer(np.asarray(ref), np.asarray(cand)).ravel()
        diffs = diffs[np.abs(diffs) <= max_offset]
        if not diffs.size:
            return None
        n = int(round(max_offset / shift.BIN_S))
        counts = np.bincount(np.rint(diffs / shift.BIN_S).astype(np.int64) + n, minlength=2 * n + 1)
        return np.convolve(counts, np.ones(3, dtype=np.int64), mode="same").tolist(), n

    def cases(self):
        rng = random.Random(5)
        yield [], [1.0, 2.0]
        yield [1.0, 2.0], []
        yield [0.0], [120.0, -120.0, 120.000001, 600.0, -600.0]           # exactly at the window's edges, and past
        yield [10.0], [10.025, 10.075, 9.975, 12.525, 129.975, 129.9749]  # half bins
        for _ in range(40):
            ref = [round(rng.uniform(0, 3600), rng.choice([3, 6])) for _ in range(rng.randint(1, 400))]
            off = rng.choice([0.0, 1.43, -60.0, 119.975, 300.0])
            cand = [r - off + rng.gauss(0, 0.05) for r in ref if rng.random() < 0.7]
            cand += [rng.uniform(0, 3600) for _ in range(rng.randint(0, 200))]
            cand += [rng.choice(ref) - rng.choice([120.0, -120.0, 600.0, 0.025, -2.525]) for _ in range(5)]
            rng.shuffle(cand)
            yield cand, ref

    def test_the_bins_are_those_of_every_pair_whatever_the_blocks(self):
        from unittest import mock
        cases = list(self.cases())
        # blocks of one on the first 6 cases only (the four edge cases and two random tracks; ~11 s on all 44): (7, 50)
        # already reaches every other branch, several blocks, a partial last one, several sub-steps a block
        blocks = ((shift.STARTS_AT_ONCE, shift.PAIRS_AT_ONCE, cases), (7, 50, cases), (1, 1, cases[:6]))
        for starts, pairs, chosen in blocks:
            with mock.patch.object(shift, "STARTS_AT_ONCE", starts), mock.patch.object(shift, "PAIRS_AT_ONCE", pairs):
                for i, (cand, ref) in enumerate(chosen):
                    for max_offset in (120.0, 600.0):
                        with self.subTest(blocks=(starts, pairs), case=i, max_offset=max_offset):
                            got = shift._window(cand, sorted(ref), max_offset)
                            want = self.every_pair(cand, ref, max_offset)
                            self.assertEqual(None if got is None else (got[0].tolist(), got[1]), want)

    def test_a_long_or_dense_track_takes_little_memory(self):
        # every pair at once: 1,700 x 8,000 starts peaked at ~220 MiB; a crafted track of 20,000 cues within five
        # minutes against 1,000 lines at ~380 MiB. Blocked: a few arrays of PAIRS_AT_ONCE (2^20) differences at most
        rng = random.Random(9)
        long_ = (sorted(rng.uniform(0, 7200) for _ in range(8000)), sorted(rng.uniform(0, 7200) for _ in range(1700)))
        dense = (sorted(rng.uniform(900, 1400) for _ in range(1000)),
                 sorted([rng.uniform(1000, 1300) for _ in range(20000)] + [7000.0]))
        for name, (cand, ref) in (("long", long_), ("dense", dense)):
            with self.subTest(track=name):
                with traced_memory() as traced:
                    self.assertIsNotNone(shift._window(cand, ref, 120.0))
                    peak = traced()[1]
                self.assertLess(peak, 32 * 2 ** 20)


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

    def test_a_credits_song_the_subtitle_lacks_is_no_missing_scene(self):
        # the file's track transcribes a 200 s closing song (40 lines) after the dialogue; the subtitle stops before it
        song = [self.ref[-1] + self.dur[-1] + 5.0 + 5.0 * k for k in range(40)]
        r = random.Random(1)
        g = relgate(self.ref + song, sorted(s + r.gauss(0, 0.04) for s in self.ref), self.dur + [3.0] * 40, min_span=0.7)
        self.assertFalse(g["missing_scene"])
        self.assertTrue(g["pass"])

    def test_a_subtitle_that_stops_long_before_the_end_is_missing_it(self):
        # The last 28 % of the film (over 17 minutes, 244 lines) has no line; its span (72 %) passed, and
        # every line it has pairs; across languages, as it is or moved by one shift, it isn't excused (never
        # beyond the subtitle's ends)
        r = random.Random(1)
        cand = [s + r.gauss(0, 0.04) for s in self.ref if s < self.ref[0] + 0.72 * (self.ref[-1] - self.ref[0])]
        g = self.gate(cand)
        self.assertGreaterEqual(g["span"], 0.7)
        self.assertEqual((g["missing_scene"], g["missing_at_end"], g["missing_lines"], g["missing_s"], g["pass"]),
                         (True, True, 244, 1026.5, False))
        self.assertEqual(clearly_better({"median_ms": 5000.0}, g, min_span=0.7),
                         (False, "a scene of the release is missing (another cut)"))
        self.assertEqual(aligned(g, min_span=0.7, one_shift=True), (False, "a scene of the release is missing (another cut)"))
        late = self.gate(self.without(self.ref, 0.0, 1100.0))       # the same at the start: 18 minutes, 268 lines
        self.assertEqual((late["missing_scene"], late["missing_lines"], late["pass"]), (True, 268, False))
        short = self.gate(self.without(self.ref, 3300.0, 4000.0))   # a credits song's length: 400 s, 94 lines
        self.assertEqual((short["missing_scene"], short["pass"]), (False, True))

    def test_each_stretch_is_judged_on_its_own_against_the_subtitles_own_language(self):
        # A scene the subtitle lacks (22 lines over 210 s) was refused alone, but with a denser stretch it
        # also leaves out (an untranslated opening song, 30 lines over 80 s) the song was judged in its place and the
        # copy passed. Against a track in the subtitle's own language each stretch is judged on its own (the record: the
        # worst one found, else the longest); against another language's track, where an untranslated stretch looks
        # the same, the stretch with the most lines still is
        def gap(ref, cand, a, z, n):
            keep = [s for s in ref if not a - 1.5 <= s <= z + 1.5]
            return sorted(keep + [a + (z - a) * k / (n - 1) for k in range(n)]), [s for s in cand if not a - 1.5 <= s <= z + 1.5]
        line = [2.0 * k for k in range(1501)]
        ref, cand = gap(line, line, 2000.0, 2210.0, 22)               # the scene alone
        for cross in (False, True):
            g = relgate(ref, cand, cross=cross)
            self.assertEqual((g["missing_scene"], g["missing_lines"], g["missing_s"], g["pass"]), (True, 22, 210.0, False))
        ref, cand = gap(ref, cand, 500.0, 580.0, 30)                  # and the song
        g = relgate(ref, cand)
        self.assertEqual((g["missing_scene"], g["missing_lines"], g["missing_s"], g["pass"]), (True, 22, 210.0, False))
        g = relgate(ref, cand, cross=True)
        self.assertEqual((g["missing_scene"], g["missing_lines"], g["missing_s"], g["pass"]), (False, 30, 80.0, True))
        ref, cand = gap(*gap(line, line, 500.0, 580.0, 30), 2000.0, 2140.0, 15)    # none qualifies: the song, 15 over 140 s
        g = relgate(ref, cand)
        self.assertEqual((g["missing_scene"], g["missing_lines"], g["missing_s"], g["pass"]), (False, 15, 140.0, True))
        g = relgate(ref, cand, cross=True)
        self.assertEqual((g["missing_scene"], g["missing_lines"], g["missing_s"], g["pass"]), (False, 30, 80.0, True))


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
        return FakeIndex(text=[{"number": 2, "language": "en", "codec": "S_TEXT/UTF8", "name": "", "forced": False,
                                "cues": list(self.cues)}])

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
        r = pipeline.shift_sync(self.index(), text, "rus", rules(better={}))
        self.assertEqual(r["outcome"], "synced", r.get("reason"))
        self.assertTrue(r["reason"].startswith("aligned to another language's track (one shift)"), r["reason"])
        r = pipeline.shift_sync(self.index(), text, "eng", rules(better={}))     # against its own language's track
        self.assertEqual(r["outcome"], "keep_original")
        self.assertTrue(r["gate_synced"]["missing_scene"])

    def test_an_original_in_place_is_in_sync_across_languages_only(self):
        text = self.srt(self.translation(0.05))
        rule = dict(relgate_mod.BETTER)
        self.assertTrue(pipeline.check_original(self.index(), text, "rus", rules(snap=False, better=rule))["in_sync"])
        self.assertFalse(pipeline.check_original(self.index(), text, "eng", rules(snap=False, better=rule))["in_sync"])

    def test_a_scene_a_denser_stretch_hid_is_refused_against_the_subtitles_own_language_only(self):
        # through the shift path: the track has an untranslated song (30 lines over 80 s) and a scene (22 lines
        # over 210 s) the subtitle lacks. Against a track in its own language the scene refuses the copy, snapped or not
        # (it passed snapped, and was "clearly better" unsnapped, with the song judged in the scene's place); against
        # another language's track the stretch with the most lines is judged, so the copy stays offered (Dune: Part
        # Two's Russian and Spanish one-shift copies against the English image track keep theirs)
        song, scene = (800.0, 880.0), (2000.0, 2210.0)
        r = random.Random(5)
        sub = [(s + r.gauss(0, 0.15), d) for s, d in self.cues if not any(a - 1.5 <= s <= z + 1.5 for a, z in (song, scene))]
        cues = sorted([c for c in self.cues if not any(a - 1.5 <= c[0] <= z + 1.5 for a, z in (song, scene))]
                      + [(song[0] + 80.0 * k / 29, 2.0) for k in range(30)] + [(scene[0] + 10.0 * k, 3.0) for k in range(22)])
        index = FakeIndex(text=[{"number": 2, "language": "en", "codec": "S_TEXT/UTF8", "name": "", "forced": False,
                                 "cues": cues}])
        text = self.srt(sub, shift_s=5.0)
        for snap in (True, False):
            r = pipeline.shift_sync(index, text, "eng", rules(snap=snap, better={}))
            self.assertEqual(r["outcome"], "keep_original", r.get("reason"))
            self.assertEqual(r["better"]["why"], "a scene of the release is missing (another cut)")
            self.assertEqual((r["gate_synced"]["missing_scene"], r["gate_synced"]["missing_lines"]), (True, 22))
        r = pipeline.shift_sync(index, text, "rus", rules(better={}))
        self.assertEqual((r["outcome"], r["reason"]), ("synced", "passes against the release track (one shift) (snapped)"))
        r = pipeline.shift_sync(index, text, "rus", rules(snap=False, better={}))
        self.assertEqual((r["outcome"], r["reason"]), ("synced", "aligned to another language's track (one shift)"))
        self.assertEqual((r["gate_synced"]["missing_scene"], r["gate_synced"]["missing_lines"]), (False, 30))

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
            res = pipeline.shift_sync(self.index(), self.srt(theatrical, shift_s), "rus", rules(better={}))
            self.assertEqual(res["outcome"], "keep_original", res.get("reason"))
            self.assertLess(res["gate_synced"]["paired"], relgate_mod.ONE_SHIFT_PAIRED)   # one shift: the cut breaks it
        self.assertFalse(pipeline.check_original(self.index(), self.srt(theatrical), "rus",
                                                 rules(snap=False, better=dict(relgate_mod.BETTER)))["in_sync"])

    def test_one_shift_excuses_a_stretch_only_when_half_of_the_lines_after_it_pair_near_the_shift(self):
        # Under one shift, a missing stretch inside the subtitle's span is excused only when at least half of
        # the subtitle's lines after it still pair near the shift, with no minimum count: after an untranslated song
        # the lines line up again, after a cut they are off by its length. A track line every 4 s; the subtitle leaves
        # out the 49 lines from 3,400 to 3,600 s and has 4 lines after them, its last ones 2 s off: 2 of 4 near the
        # shift excuse it, 1 of 4 doesn't; with one line after the stretch, which pairs, it is excused ('s MobLand
        # S02E08 Russian: its untranslated stretch ends 10 s before its last line, 1 of 1)
        ref = [20.0 + 4.0 * k for k in range(900)]
        for z, off, excused in ((3600.0, 2, True), (3600.0, 3, False), (3612.0, 0, True)):
            cand = [s for s in ref if not 3400.0 <= s <= z]
            cand = cand[:len(cand) - off] + [s + 2.0 for s in cand[len(cand) - off:]]
            g = relgate(ref, cand, min_span=0.7, cross=True)
            self.assertEqual((g["missing_scene"], g.get("missing_at_end", False)), (True, False))
            self.assertGreaterEqual(g["paired"], relgate_mod.ONE_SHIFT_PAIRED)
            self.assertEqual(aligned(g, min_span=0.7, one_shift=True)[0], excused, (z, off))
            self.assertEqual(aligned(g, min_span=0.7), (False, "a scene of the release is missing (another cut)"))

    def test_a_late_extra_scene_inside_the_span_is_not_excused_by_one_shift(self):
        # A 400 s extra scene at 85 % of the film, then a sparse finale of 8 lines moved back by its length:
        # the stretch the scene leaves uncovered is inside the subtitle's span, and the subtitle still pairs 99.5 % of
        # its lines under one shift, but none of its 8 lines after the stretch pair near the shift: refused
        r = random.Random(3)
        rel, t = [], 20.0
        while t < 5100:
            rel.append(t)
            t += r.uniform(3, 8)
        scene = []
        while t < 5500:
            scene.append(t)
            t += r.uniform(5, 8)
        post = []
        while t < 6000:
            post.append(t)
            t += r.uniform(50, 70)
        ref = sorted(rel + scene + post)
        cand = sorted([x + r.gauss(0, 0.04) for x in rel] + [x - 400.0 + r.gauss(0, 0.04) for x in post])
        g = relgate(ref, cand, [1.5] * len(ref), min_span=0.7, cross=True)
        self.assertEqual((len(post), g["missing_scene"], g.get("missing_at_end", False), g["paired"]), (8, True, False, 0.995))
        self.assertEqual(aligned(g, min_span=0.7, one_shift=True), (False, "a scene of the release is missing (another cut)"))


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


class AudioFitFixture(unittest.TestCase):
    """The audio route on a synthetic timeline: each release line (self.truth) is spoken a little off its start (sigma
    80 ms); fit() fits a subtitle's lines to that speech. No tests of its own: each class below has its own
."""

    def setUp(self):
        self.truth = rhythm(700, 11)

    def fit(self, starts, better=AUDIO_BETTER, durations=None):
        r = random.Random(5)
        speech = [(s + r.gauss(0, 0.08), s + d) for s, d in self.truth]
        end = self.truth[-1][0] + self.truth[-1][1] + 30

        class Index:
            def duration(self):
                return end
        durations = durations or [d for _, d in self.truth]
        lines = [{"s": s, "e": s + d, "text": "x", "kind": "dialogue"} for s, d in zip(starts, durations)]
        return timeline_fit(speech)(Index(), lines, vad=object(), better=better).run()   # windows without speech ends

    def styled(self, seed, shift, sigma=0.15):
        r = random.Random(seed)
        return [s + shift + r.gauss(0, sigma) for s, _ in self.truth]


class AudioClearlyBetterTest(AudioFitFixture):
    """The audio route's second way to pass. Synthetic speech: each release line is spoken a little off its start
    (sigma 80 ms); a third-party subtitle has its own timing style (sigma 150 ms) and is 9 s off. Like Primer's YTS
    MP4, the re-timed copy then fails the speech gate (about 70 % within 200 ms, needs 90 %) and passes this
    rule."""

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
        # windows without speech-run ends, a shape production never makes: 8 films keep it covered (R3-3,
        #); test_matcher's CloseCallTest sweeps 40 with production's windows, both verdicts
        accepted = []
        for seed in range(8):
            other = rhythm(700, 3000 + seed)
            fit = self.fit([s for s, _ in other], durations=[d for _, d in other])
            if fit["decision"] != "keep_original":
                accepted.append((seed, fit["decision"], fit.get("why"), fit["gate_new"]))
        self.assertEqual(accepted, [])


class SparseDialogueTest(AudioFitFixture):
    """A film with little dialogue (Upstream Color: 10 lines heard in the windows) gives the audio route too
    few lines when its windows are spread evenly; placed where the subtitle has its lines, the same windows hear enough."""

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
        lines = [{"s": s, "e": s + d, "text": "x", "kind": "dialogue"} for s, (_, d) in zip(starts, self.truth)]
        return audiofit.fit_tracks(Index(), lines, vad=object(), better=AUDIO_BETTER, fit_class=timeline_fit(speech))

    def test_the_other_track_is_tried_when_the_default_fails(self):
        r = random.Random(1)
        fit = self.fit([s - 9.0 + r.gauss(0, 0.15) for s, _ in self.truth])
        self.assertEqual((fit["decision"], fit.get("audio_track")), ("synced", 1))
        self.assertAlmostEqual(fit["model"]["offset"], 9.0, delta=0.3)

    def test_the_default_track_is_kept_when_it_fits(self):
        r = random.Random(1)
        fit = self.fit([s - 9.0 + r.gauss(0, 0.15) for s, _ in self.truth], follows=0)   # the default is the right one
        self.assertEqual((fit["decision"], fit.get("audio_track")), ("synced", None))

    def test_when_every_track_keeps_the_original_the_first_answer_names_the_other(self):
        # The answer when both tracks tried keep the original had no test: the first track's, with the
        # windows of both and the other track's own reason
        from subsync import audiofit

        class Keeps:
            def __init__(self, index, lines, track=None, **kw):
                self.track = track

            def run(self):
                return {"decision": "keep_original", "why": f"no fit on track {self.track}", "windows": 3,
                        "audio_s": 30.0}

        class Index:
            def audio_tracks(self):
                return [{"index": 0, "language": "eng", "default": True},
                        {"index": 1, "language": "jpn", "default": False}]
        self.assertEqual(audiofit.fit_tracks(Index(), [], fit_class=Keeps),
                         {"decision": "keep_original", "why": "no fit on track None", "windows": 6, "audio_s": 60.0,
                          "other_track": {"index": 1, "language": "jpn", "why": "no fit on track 1"}})


class AudioBudgetTest(unittest.TestCase):
    """A file's audio windows fit its audio budget (default 1 GB): on a high-bitrate file the windows get shorter;
    when they would have to be shorter than the minimum, the original is kept without reading any audio."""

    def setUp(self):
        self.truth = rhythm(700, 11)

    def fit(self, rate, budget, starts=None, spent=0):
        truth = self.truth
        r = random.Random(5)
        speech = [(s + r.gauss(0, 0.08), s + d) for s, d in truth]
        end = truth[-1][0] + truth[-1][1] + 30

        class Index:
            size = int(rate * end)
            audio_bytes = spent

            def duration(self):
                return end
        rr = random.Random(1)
        starts = starts or [s - 9.0 + rr.gauss(0, 0.15) for s, _ in truth]
        lines = [{"s": s, "e": s + d, "text": "x", "kind": "dialogue"} for s, (_, d) in zip(starts, truth)]
        idx = Index()
        fit = timeline_fit(speech, rate=rate)(idx, lines, vad=object(), better=AUDIO_BETTER, budget=budget)
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
        t0 = time.monotonic()
        fit.prefetch(starts, 30.0)
        took = time.monotonic() - t0
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
        failed = []

        def prefetch(fit):
            try:
                fit.prefetch(starts, 30.0)
            except BaseException as e:          # an error in a thread was lost to threading's hook
                failed.append(repr(e))
        ts = [th.Thread(target=prefetch, args=(f,), daemon=True) for f in fits]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)                          # two fits waiting on each other fail, never hang the run
        self.assertFalse(any(t.is_alive() for t in ts), "a fit still reads after 10 s")
        self.assertEqual(failed, [])
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


class ImageRefTest(unittest.TestCase):
    def setUp(self):
        self.lines = rhythm(400, 11)

    def test_pgs_with_every_clear_gives_the_line_starts(self):
        lines = realistic(400, 5)       # Blu-ray style: every line has its clear (2-frame gaps, pauses between scenes)
        ref = imageref.reference(pgs_left_out(lines))
        self.assertTrue(ref["plausible"])
        got = [round(s, 3) for s, _ in ref["cues"]]
        truth = {round(s, 3) for s, _ in lines}
        self.assertTrue(all(x in truth for x in got))
        self.assertGreater(len(got) / len(lines), 0.75)                        # 82 % here; 83-95 % on real timings

    def test_no_pauses_and_no_short_gaps_means_no_guessing(self):
        ref = imageref.reference(pgs_left_out(self.lines))   # nothing pins the labels
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
        ref = imageref.reference(pgs_left_out(lines))
        truth = {round(s, 3) for s, _ in lines}
        got = [round(s, 3) for s, _ in ref["cues"]]
        self.assertTrue(ref["plausible"])
        self.assertGreater(sum(x in truth for x in got) / len(got), 0.95)   # precision
        self.assertGreater(len(got) / len(lines), 0.4)                         # recall

    def test_a_display_set_cued_twice_counts_once(self):
        # The labelling takes times within SAME_EVENT_S as one event, but the share of sure starts was divided
        # by every cue, so a track whose Cues list each display set twice lost half its share (and its plausibility)
        events = pgs_left_out(realistic(400, 5))["cues"]
        once = imageref.reference({"codec": "S_HDMV/PGS", "cues": events})
        twice = imageref.reference({"codec": "S_HDMV/PGS", "cues": events + [(t + 0.004, 0.0) for t, _ in events]})
        self.assertTrue(once["plausible"])
        self.assertEqual(twice, once)

    def test_vobsub_starts_and_noise_is_not_plausible(self):
        r = imageref.reference({"codec": "S_VOBSUB", "cues": [(s, 0.0) for s, _ in self.lines]})
        self.assertEqual(len(r["cues"]), len(self.lines))
        self.assertTrue(r["plausible"])
        rnd = random.Random(3)
        noise = sorted((rnd.uniform(0, 5000), 0.0) for _ in range(60))   # sparse random events: long "shown" times
        self.assertFalse(imageref.reference({"codec": "S_HDMV/PGS", "cues": noise})["plausible"])

    def test_calibration_compares_an_image_track_with_the_text_track(self):
        # An image track whose starts can't be told (one line kept of 400) no longer passes against the text
        # track: the 399 lines after its only line are left uncovered; a Blu-ray style track (82 % sure) does pass
        for lines, plausible in ((self.lines, False), (realistic(400, 5), True)):
            text = [{"number": 3, "language": "eng", "codec": "S_TEXT/ASS", "forced": False, "cues": lines}]
            image = [dict(pgs_left_out(lines), language="en")]
            got = pipeline.image_check(text, image)
            self.assertEqual(len(got), 1)
            self.assertEqual((got[0]["plausible"], got[0]["vs_text"]["pass"]), (plausible, plausible))
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
