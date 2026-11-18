"""The language check: a subtitle whose text isn't in the language it is listed under is refused; a short one,
or one the check can't tell, never is."""
import random
import re
import time
import unittest

from helpers import DIALOGUE  # noqa: E402
from subsync import langcheck  # noqa: E402

ENGLISH, SPANISH, VIETNAMESE, RUSSIAN = (DIALOGUE[code] for code in ("eng", "spa", "vie", "rus"))


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
            self.assertEqual(bool(langcheck._DRAWING.search(text)), bool(old.search(text)), text)
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
        # a change to what passes: stored verdicts are judged again
        from subsync import RULE_REVISION
        self.assertGreaterEqual(RULE_REVISION, 8)


if __name__ == "__main__":
    unittest.main()
