"""Language codes: the full two-letter (ISO 639-1) table to the three-letter bibliographic codes (ISO 639-2/B) that
Matroska tracks use, the terminologic spellings, TMDB's own codes, and the language families that count as one language
when a title's original language is compared with a file's tracks."""
import unittest

from subsync import langs

# the 32 two-letter codes the table knew before it was completed: each must map exactly as before
BEFORE = {"en": "eng", "ru": "rus", "ja": "jpn", "fr": "fre", "de": "ger", "es": "spa", "it": "ita", "pt": "por",
          "ko": "kor", "zh": "chi", "uk": "ukr", "pl": "pol", "nl": "dut", "sv": "swe", "tr": "tur", "ar": "ara",
          "he": "heb", "cs": "cze", "hu": "hun", "el": "gre", "fi": "fin", "da": "dan", "no": "nor", "ro": "rum",
          "bg": "bul", "hr": "hrv", "sr": "srp", "id": "ind", "th": "tha", "vi": "vie", "hi": "hin", "fa": "per"}
NAMES_BEFORE = {"eng": "English", "rus": "Russian", "jpn": "Japanese", "fre": "French", "ger": "German",
                "spa": "Spanish", "ita": "Italian", "por": "Portuguese", "kor": "Korean", "chi": "Chinese",
                "ukr": "Ukrainian", "pol": "Polish", "dut": "Dutch", "swe": "Swedish", "nor": "Norwegian",
                "dan": "Danish", "fin": "Finnish", "cze": "Czech", "hun": "Hungarian", "rum": "Romanian",
                "bul": "Bulgarian", "hrv": "Croatian", "srp": "Serbian", "gre": "Greek", "tur": "Turkish",
                "ara": "Arabic", "heb": "Hebrew", "per": "Persian", "hin": "Hindi", "ind": "Indonesian", "tha": "Thai",
                "vie": "Vietnamese"}
# ISO 639-2's twenty languages with a bibliographic and a terminologic code
B_T = {"alb": "sqi", "arm": "hye", "baq": "eus", "bur": "mya", "chi": "zho", "cze": "ces", "dut": "nld", "fre": "fra",
       "geo": "kat", "ger": "deu", "gre": "ell", "ice": "isl", "mac": "mkd", "mao": "mri", "may": "msa", "per": "fas",
       "rum": "ron", "slo": "slk", "tib": "bod", "wel": "cym"}


class TableTest(unittest.TestCase):
    def test_every_two_letter_code_is_known(self):
        self.assertEqual(len(langs.TWO), 183)
        for two, three in langs.TWO.items():
            self.assertRegex(two, r"^[a-z]{2}$")
            self.assertRegex(three, r"^[a-z]{3}$")
            self.assertNotIn(three, langs.TERMINOLOGIC, f"{two}: the bibliographic code, not the terminologic one")

    def test_the_codes_known_before_map_as_before(self):
        for two, three in BEFORE.items():
            self.assertEqual(langs.lang3(two), three)
        for three, name in NAMES_BEFORE.items():
            self.assertEqual(langs.name(three), name)
            self.assertEqual(langs.lang3(name), three)
        self.assertEqual(langs.lang3("pob"), "por")     # OpenSubtitles' Brazilian Portuguese, as before
        self.assertEqual(langs.lang3(""), "und")
        self.assertEqual(langs.lang3(None), "und")

    def test_codes_unknown_before(self):
        for two, three in {"ta": "tam", "te": "tel", "ml": "mal", "bn": "ben", "kn": "kan", "mr": "mar", "ur": "urd",
                           "pa": "pan", "is": "ice", "ka": "geo", "sq": "alb", "hy": "arm", "eu": "baq", "my": "bur",
                           "mk": "mac", "mi": "mao", "ms": "may", "sk": "slo", "bo": "tib", "cy": "wel", "sl": "slv",
                           "et": "est", "lv": "lav", "lt": "lit", "ca": "cat", "gl": "glg", "nb": "nob", "nn": "nno",
                           "tl": "tgl", "sw": "swa", "zu": "zul", "af": "afr", "yi": "yid", "la": "lat"}.items():
            self.assertEqual(langs.lang3(two), three, two)
        self.assertEqual(langs.lang3("ta-IN"), "tam")
        self.assertEqual(langs.name("tam"), "Tamil")
        self.assertEqual(langs.name("pan"), "Panjabi")     # the first of ISO 639-2's names
        self.assertEqual(langs.lang3("Tamil"), "tam")

    def test_every_terminologic_code_gives_the_bibliographic_one(self):
        for b, t in B_T.items():
            self.assertEqual(langs.lang3(t), b, t)
            self.assertEqual(langs.lang3(b), b, b)

    def test_the_terminologic_table_is_iso_639_2s_and_a_sources_own_codes_are_apart(self):
        # OpenSubtitles' "pob" sat in the ISO table as a twenty-first entry
        self.assertEqual(langs.TERMINOLOGIC, {t: b for b, t in B_T.items()})
        self.assertEqual(langs.SOURCE_CODES, {"pob": "por"})

    def test_two_letter_codes(self):
        # From the ISO 639-1 table, never cut from a three-letter code
        for two, three in BEFORE.items():
            self.assertEqual(langs.lang2(three), two, three)
        for three, two in {"est": "et", "slo": "sk", "arm": "hy", "ben": "bn", "kaz": "kk", "lit": "lt", "lav": "lv",
                           "bos": "bs", "baq": "eu", "alb": "sq", "ice": "is", "mac": "mk", "may": "ms", "nob": "nb",
                           "fra": "fr", "pob": "pt", "en-US": "en", "Estonian": "et"}.items():
            self.assertEqual(langs.lang2(three), two, three)
        for code in ("haw", "yue", "und", "", None):
            self.assertIsNone(langs.lang2(code), code)

    def test_every_language_of_the_table_has_a_name(self):
        for three in langs.TWO.values():
            self.assertIn(three, langs.NAMES)
            self.assertNotIn(";", langs.NAMES[three])
            self.assertNotIn("(", langs.NAMES[three])


class TmdbCodeTest(unittest.TestCase):
    def test_tmdb_codes(self):
        self.assertEqual(langs.from_tmdb("en"), "eng")
        self.assertEqual(langs.from_tmdb("cn"), "chi")      # TMDB's Cantonese; Matroska tracks say chi for it
        self.assertEqual(langs.from_tmdb("zh"), "chi")
        self.assertEqual(langs.from_tmdb("ta"), "tam")
        self.assertIsNone(langs.from_tmdb("xx"))            # TMDB's "no language"
        self.assertIsNone(langs.from_tmdb("sh"))            # not a code of the table: no language rather than a guess
        self.assertIsNone(langs.from_tmdb(""))
        self.assertIsNone(langs.from_tmdb(None))


class KnownTest(unittest.TestCase):
    def test_a_code_as_a_language_or_nothing(self):
        self.assertEqual(langs.known("en-US"), "eng")       # an IETF tag (a dialect item on Wikidata)
        self.assertEqual(langs.known("fra"), "fre")
        self.assertEqual(langs.known("yue"), "yue")         # a three-letter code stays itself
        for code in ("", None, "und", "mul", "zxx", "mis", "xx", "q", "toolong", "e1"):
            self.assertIsNone(langs.known(code), code)


class FamilyTest(unittest.TestCase):
    def test_one_language_for_the_comparison(self):
        for a, b in (("yue", "chi"), ("cmn", "zh"), ("zh-Hant", "chi"), ("nob", "nor"), ("nno", "no"), ("arb", "ara"),
                     ("pes", "per"), ("prs", "fa"), ("zsm", "may"), ("eng", "en-GB"), ("fra", "fre")):
            self.assertTrue(langs.same_language(a, b), (a, b))
        for a, b in (("eng", "spa"), ("chi", "jpn"), ("nor", "dan"), ("und", "eng"), ("", "")):
            self.assertFalse(langs.same_language(a, b), (a, b))

    def test_the_families_dont_change_lang3(self):
        self.assertEqual(langs.lang3("yue"), "yue")
        self.assertEqual(langs.lang3("nb"), "nob")


if __name__ == "__main__":
    unittest.main()
