"""The language check: a subtitle whose text isn't in the language it is listed under is refused; a short one,
or one the check can't tell, never is."""
import hashlib
import json
import random
import re
import time
import unittest

from helpers import DIALOGUE
from subsync import langcheck, RULE_REVISION

ENGLISH, SPANISH, VIETNAMESE, RUSSIAN = (DIALOGUE[code] for code in ("eng", "spa", "vie", "rus"))
# a few lines of Chinese, Japanese and Korean dialogue, each with its English (bilingual subtitles show both)
EN = ("I don't know what you're talking about.", "Let's leave tomorrow morning.", "Why didn't you call me back?",
      "This has nothing to do with you.", "Hurry up, we're going to be late!", "Did he come home last night?")
CJK = {"chi": ("我不知道你在说什么。", "我们明天早上再走吧。", "你为什么一直不回我电话？", "这件事跟你没有关系。",
               "快点，我们要迟到了！", "他昨天晚上回家了吗？"),
       "jpn": ("何を言っているのか分からない。", "明日の朝に出発しましょう。", "どうして電話してくれなかったの？",
               "これはあなたには関係ない。", "急いで、遅れちゃうよ！", "彼は昨日の夜に帰ってきたの？"),
       "kor": ("무슨 말을 하는지 모르겠어.", "내일 아침에 떠나자.", "왜 나한테 다시 전화 안 했어?", "이건 너랑 상관없어.",
               "서둘러, 늦겠어!", "그 사람 어젯밤에 집에 왔어?")}

# What passes the check, as one fingerprint (check_rules), and the RULE_REVISION it has been judged under since it took
# this form. A change to it must bump RULE_REVISION, so stored verdicts are judged again: pin the new fingerprint here
# with the new revision
CHECK_RULES = "8fe862e37499fa0567bb0662e19d32a180512058f861348f8023a613e2ce96b4"
CHECK_REVISION = 11


def check_rules():
    """The fingerprint of what the check decides by: its thresholds, the scripts' letters and each language's scripts
    (and own script), the common-word lists, and how words and vector drawings are found."""
    rules = {"thresholds": [langcheck.MIN_WORDS, langcheck.MIN_SCRIPT_SHARE, langcheck.MIN_COMMON_SHARE,
                            langcheck.MIN_OWN_SCRIPT_SHARE],
             "blocks": langcheck._BLOCKS, "scripts": langcheck.SCRIPTS, "own": langcheck.OWN_SCRIPT,
             "characters": langcheck.CHARACTER_SCRIPTS,
             "words": {code: sorted(words) for code, words in langcheck.WORDS.items()},
             "patterns": [langcheck._TOKEN.pattern, langcheck.DRAWING.pattern]}
    return hashlib.sha256(json.dumps(rules, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def ts(t):
    return f"{t // 3600:02d}:{t // 60 % 60:02d}:{t % 60:02d},000"


def srt(lines, words=600):
    """An SRT of `lines` in turn, with <i> tags on some, until it has at least `words` words."""
    out, n, i = [], 0, 0
    while n < words:
        text = lines[i % len(lines)]
        if i % 4 == 0:
            text = f"<i>{text}</i>"
        out.append(f"{i + 1}\n{ts(2 * i + 1)} --> {ts(2 * i + 2)}\n{text}\n")
        n += len(lines[i % len(lines)].split())
        i += 1
    return "\n".join(out)


def ass(lines, words=600):
    """An ASS whose dialogue lines carry long styling tags (more Latin letters than the lines themselves) and a vector
    drawing, until it has at least `words` words of dialogue."""
    head = ("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
            "Style: Default,Arial,48\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    tag = r"{\fnArial Black\fs48\c&H00FFFFFF&\3c&H00000000&\bord2\shad0\blur0.6\pos(960,1000)\fad(120,240)}"
    out, n, i = [], 0, 0
    while n < words:
        line = lines[i % len(lines)]
        out.append(f"Dialogue: 0,0:{i // 60:02d}:{i % 60:02d}.00,0:{i // 60:02d}:{i % 60:02d}.90,Default,,0,0,0,,"
                   f"{tag}{line}\\N{{\\i1}}{line}{{\\i0}}")
        out.append(f"Dialogue: 0,0:{i // 60:02d}:{i % 60:02d}.00,0:{i // 60:02d}:{i % 60:02d}.90,Default,,0,0,0,,"
                   r"{\p1}m 0 0 l 100 0 100 100 0 100 b 10 20 30 40 50 60{\p0}")
        n += 2 * len(line.split())
        i += 1
    return head + "\n".join(out) + "\n"


class LanguageCheckTest(unittest.TestCase):
    def test_english_passes(self):
        self.assertEqual(langcheck.check(srt(ENGLISH), "eng"), langcheck.PASS)
        self.assertEqual(langcheck.check(srt(ENGLISH), "en"), langcheck.PASS)          # the service's codes, mapped

    def test_spanish_listed_as_english_fails(self):
        # Friends S01E01's "English" original: mostly Spanish common words
        self.assertEqual(langcheck.check(srt(SPANISH), "eng"), langcheck.FAIL)
        self.assertTrue(langcheck.refuses(srt(SPANISH), "eng"))
        self.assertEqual(langcheck.check(srt(SPANISH), "spa"), langcheck.PASS)

    def test_cyrillic_listed_as_english_fails(self):
        self.assertEqual(langcheck.check(srt(RUSSIAN), "eng"), langcheck.FAIL)

    def test_russian_listed_as_russian_passes(self):
        self.assertEqual(langcheck.check(srt(RUSSIAN), "rus"), langcheck.PASS)       # the script check alone

    def test_vietnamese_listed_as_english_fails(self):
        # the same Latin letters: the common words tell them apart
        self.assertEqual(langcheck.check(srt(VIETNAMESE), "eng"), langcheck.FAIL)
        self.assertEqual(langcheck.check(srt(VIETNAMESE), "vie"), langcheck.PASS)

    def test_no_verdict_under_about_300_words(self):
        short = srt(SPANISH, words=250)
        self.assertLess(langcheck.measure(short, "eng")["words"], langcheck.MIN_WORDS)
        self.assertEqual(langcheck.check(short, "eng"), langcheck.NO_VERDICT)
        self.assertFalse(langcheck.refuses(short, "eng"))
        self.assertEqual(langcheck.check("", "eng"), langcheck.NO_VERDICT)
        self.assertEqual(langcheck.check("not a subtitle at all", "eng"), langcheck.NO_VERDICT)

    def test_a_bilingual_subtitle_is_not_refused(self):
        both = [line for pair in zip(ENGLISH, SPANISH) for line in pair]   # each English line, then its Spanish
        self.assertFalse(langcheck.refuses(srt(both), "eng"))
        self.assertFalse(langcheck.refuses(srt(both), "spa"))

    def test_a_bilingual_chinese_japanese_or_korean_subtitle_is_in_its_language(self):
        # The script share counted letters, a Chinese character against the four or five letters of an English
        # word, so a subtitle with an English line under each line was refused as not its language (0.24 for Chinese);
        # each Chinese, kana or Hangul character now counts one unit against one word
        for lang, lines in CJK.items():
            with self.subTest(lang=lang):
                both = srt([f"{own}\n{en}" for own, en in zip(lines, EN)])
                self.assertGreater(langcheck.measure(both, lang)["script"], langcheck.MIN_SCRIPT_SHARE)
                self.assertEqual(langcheck.check(both, lang), langcheck.PASS)
                self.assertEqual(langcheck.check(srt(lines, words=900), lang), langcheck.PASS)
        # still refused: Japanese listed as Chinese (its kanji alone), and English listed as Chinese
        self.assertEqual(langcheck.check(srt(CJK["jpn"], words=900), "chi"), langcheck.FAIL)
        self.assertEqual(langcheck.check(srt(EN), "chi"), langcheck.FAIL)

    def test_chinese_listed_as_japanese_or_korean_fails(self):
        # Han counted as Japanese and as Korean script, so a Chinese subtitle passed as either (script 1.0);
        # Japanese needs kana, and Korean Hangul, in at least MIN_OWN_SCRIPT_SHARE of its units, and Chinese has none
        chinese = srt(CJK["chi"], words=900)
        for lang in ("jpn", "kor"):
            with self.subTest(lang=lang):
                m = langcheck.measure(chinese, lang)
                self.assertEqual((m["script"], m["own_script"]), (1.0, 0.0))
                self.assertEqual(langcheck.check(chinese, lang), langcheck.FAIL)
        self.assertEqual(langcheck.check(chinese, "chi"), langcheck.PASS)
        self.assertIsNone(langcheck.measure(chinese, "chi")["own_script"])         # Chinese has no floor of its own
        # Japanese and Korean stay far above the floor, with an English line under each line too, and Korean with a
        # word in Chinese characters (Hanja) on each line passes
        for lang in ("jpn", "kor"):
            with self.subTest(lang=lang):
                both = srt([f"{own}\n{en}" for own, en in zip(CJK[lang], EN)])
                self.assertGreater(langcheck.measure(both, lang)["own_script"], 4 * langcheck.MIN_OWN_SCRIPT_SHARE)
        hanja = srt([f"{line} 韓國人" for line in CJK["kor"]], words=900)
        self.assertEqual(langcheck.check(hanja, "kor"), langcheck.PASS)

    def test_a_languages_written_forms_and_varieties_get_its_check(self):
        # The scripts and words were looked up by the code as given, so a language set as one of its written
        # forms or varieties (nb, nn, arb, pes, prs, zsm, yue, cmn) got no verdict whatever its subtitle's text
        for code, language in (("nob", "nor"), ("nb", "nor"), ("nno", "nor"), ("nn", "nor"), ("arb", "ara"),
                               ("pes", "per"), ("prs", "per"), ("zsm", "may"), ("yue", "chi"), ("cmn", "chi")):
            texts = {"English": srt(ENGLISH), "Russian": srt(RUSSIAN), "Chinese": srt(CJK["chi"], words=900)}
            for name, text in texts.items():
                with self.subTest(code=code, text=name):
                    self.assertNotEqual(langcheck.check(text, code), langcheck.NO_VERDICT)
                    self.assertEqual(langcheck.check(text, code), langcheck.check(text, language))
        self.assertEqual(langcheck.check(srt(RUSSIAN), "arb"), langcheck.FAIL)
        self.assertEqual(langcheck.check(srt(CJK["chi"], words=900), "yue"), langcheck.PASS)

    def test_webvtt_character_references_are_their_characters(self):
        # WebVTT writes "&", "<" and a hard space as "&amp;", "&lt;" and "&nbsp;", which were read as the
        # words "amp", "lt" and "nbsp"; read as the SRT copy writes them, the same subtitle reads the same
        cues = (("Tom &amp; Jerry&nbsp;now", "Tom & Jerry\xa0now"), ("1 &lt; 2, R&amp;D", "1 < 2, R&D"))
        vtt = "WEBVTT\n\n" + "\n".join(f"00:00:0{i}.000 --> 00:00:0{i}.800\n{v}\n" for i, (v, _) in enumerate(cues))
        plain = "\n".join(f"{i + 1}\n00:00:0{i},000 --> 00:00:0{i},800\n{s}\n" for i, (_, s) in enumerate(cues))
        self.assertEqual(langcheck.plain_text(vtt), "tom & jerry now\n1 < 2, r&d")
        self.assertEqual(langcheck.plain_text(vtt), langcheck.plain_text(plain))
        self.assertEqual(langcheck.measure(vtt, "eng")["words"], 5)                  # tom, jerry, now, r, d

    def test_ass_styling_tags_and_drawings_are_not_text(self):
        text = ass(RUSSIAN)
        self.assertEqual(langcheck.check(text, "rus"), langcheck.PASS)
        self.assertEqual(langcheck.check(text, "eng"), langcheck.FAIL)
        m = langcheck.measure(text, "rus")
        self.assertEqual(m["script"], 1.0)                                     # no letter of a tag or drawing counted
        self.assertEqual(langcheck.check(ass(ENGLISH), "eng"), langcheck.PASS)

    def test_a_drawing_is_found_as_before_and_unclosed_braces_are_read_in_linear_time(self):
        old = re.compile(r"\{[^}]*\\p[1-9]")
        rng = random.Random(5)
        for _ in range(4000):
            text = "".join(rng.choice("{}\\p1a ") for _ in range(rng.randrange(0, 14)))
            self.assertEqual(bool(langcheck.DRAWING.search(text)), bool(old.search(text)), text)
        cue = "1\n00:00:01,000 --> 00:00:02,000\n{}" + " ".join(ENGLISH[:3]) + "\n"
        t0 = time.monotonic()   # 100,000 braces with no closer after them took about 3 s a pass, and there are two
        braces = langcheck.measure(cue.format("{" * 100000), "eng")
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual(braces["words"], langcheck.measure(cue.format(""), "eng")["words"])

    def test_a_language_without_a_word_list_gets_the_script_check_only(self):
        self.assertNotIn("pol", langcheck.WORDS)
        self.assertEqual(langcheck.check(srt(ENGLISH), "pol"), langcheck.PASS)      # Latin letters: nothing to refuse
        self.assertEqual(langcheck.check(srt(RUSSIAN), "pol"), langcheck.FAIL)
        self.assertEqual(langcheck.check(srt(ENGLISH), "xyz"), langcheck.NO_VERDICT)  # nothing known about it

    def test_the_scripts_and_word_lists_it_knows(self):
        for code in ("eng", "spa", "fre", "ger", "ita", "por", "dut"):
            with self.subTest(code=code):
                self.assertTrue(30 <= len(langcheck.WORDS[code]) <= 60, len(langcheck.WORDS[code]))
        for code, script in (("eng", "latin"), ("rus", "cyrillic"), ("gre", "greek"), ("ara", "arabic"),
                             ("heb", "hebrew"), ("chi", "han"), ("jpn", "kana"), ("kor", "hangul"), ("tha", "thai"),
                             ("hin", "devanagari")):
            with self.subTest(code=code):
                self.assertIn(script, langcheck.SCRIPTS[code])
        self.assertEqual(langcheck.script_of("ж"), "cyrillic")
        self.assertEqual(langcheck.script_of("ệ"), "latin")
        self.assertEqual(langcheck.script_of("漢"), "han")
        self.assertEqual(langcheck.script_of("か"), "kana")

    def test_the_rule_revision_changed_with_it(self):
        # a change to what passes: stored verdicts are judged again. A lower bound alone stayed true whatever
        # changed; the rules' fingerprint tells
        self.assertEqual(check_rules(), CHECK_RULES, "the language check's rules changed: bump RULE_REVISION and pin "
                                                     "the new fingerprint with it (CHECK_RULES, CHECK_REVISION)")
        self.assertGreaterEqual(RULE_REVISION, CHECK_REVISION)
        self.assertGreaterEqual(RULE_REVISION, 8)                 # the check came in revision 8


if __name__ == "__main__":
    unittest.main()
