"""Language codes: the full two-letter (ISO 639-1) table to the three-letter bibliographic codes (ISO 639-2/B) that
Matroska tracks use, the terminologic spellings, TMDB's own codes, codes of plain letters only, and the language
families that count as one language when a title's original language is compared with a file's tracks, and where only
the script matters."""
import unittest

from helpers import HAVE_CRYPTOGRAPHY, NEEDS  # first: it puts the package on the import path
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

    def test_a_code_with_letters_outside_a_to_z_names_no_language(self):
        # Any three Unicode letters were taken as a code, so SUBSYNC_LANGUAGES=eng,рус started with a
        # language no source answers for; language codes are plain letters
        for code in ("рус", "ñño", "ÉNG", "eñg", "日本語"):
            self.assertIsNone(langs.known(code), code)
        self.assertEqual(langs.known("RUS"), "rus")


class FamilyTest(unittest.TestCase):
    def test_one_language_for_the_comparison(self):
        for a, b in (("yue", "chi"), ("cmn", "zh"), ("zh-Hant", "chi"), ("nob", "nor"), ("nno", "no"), ("arb", "ara"),
                     ("pes", "per"), ("prs", "fa"), ("zsm", "may"), ("eng", "en-GB"), ("fra", "fre")):
            self.assertTrue(langs.same_language(a, b), (a, b))
        for a, b in (("eng", "spa"), ("chi", "jpn"), ("nor", "dan"), ("und", "eng"), ("", "")):
            self.assertFalse(langs.same_language(a, b), (a, b))

    def test_a_written_form_or_a_variety_as_its_language(self):
        # where only the script matters: a subtitle's old encoding, and the language check
        for code, language in (("yue", "chi"), ("cmn", "chi"), ("nb", "nor"), ("nno", "nor"), ("arb", "ara"),
                               ("pes", "per"), ("prs", "per"), ("zsm", "may"), ("en-GB", "eng"), ("zho", "chi")):
            self.assertEqual(langs.family(code), language, code)
        self.assertEqual(langs.legacy_encodings("yue"), ("gbk", "cp950"))
        self.assertEqual(langs.legacy_encodings("arb"), ("cp1256",))

    def test_the_families_dont_change_lang3(self):
        self.assertEqual(langs.lang3("yue"), "yue")
        self.assertEqual(langs.lang3("nb"), "nob")


class WrittenFormsTest(unittest.TestCase):
    """Wherever a subtitle's language is compared with a track's, a language's written forms count as it
    (Norwegian's nb and nn, the standard forms of Arabic, Persian and Malay); the Chinese varieties stay apart, since
    their written text differs."""

    def test_a_written_form_is_its_language(self):
        for code, language in (("nb", "nor"), ("nb-NO", "nor"), ("nob", "nor"), ("nn", "nor"), ("nno", "nor"),
                               ("no", "nor"), ("arb", "ara"), ("pes", "per"), ("prs", "per"), ("zsm", "may"),
                               ("yue", "yue"), ("cmn", "cmn"), ("en-GB", "eng")):
            self.assertEqual(langs.written(code), language, code)

    def test_a_norwegian_subtitle_takes_a_bokmal_track_as_its_own(self):
        from helpers import realistic
        from subsync import pipeline
        cues = realistic(600, 61)

        class Index:
            def text_tracks(self):
                return [{"number": n, "language": lang, "codec": "S_TEXT/UTF8", "name": "", "forced": False,
                         "cues": cues} for n, lang in ((3, "en"), (4, "nb-NO"))]
        ref = pipeline.choose_reference(Index(), 0, "nor")
        self.assertEqual(ref["number"], 4)                         # it was the English track: nb was another language
        self.assertFalse(pipeline.is_cross(ref, "nor"))            # and judged as its own language's, words included
        self.assertTrue(pipeline.is_cross({"language": "yue"}, "chi"))

    def test_a_fallback_language_takes_its_written_forms_too(self):
        # SUBSYNC_FALLBACK_REFERENCE=nor and a Swedish subtitle on a file with a Norwegian track tagged nb (or
        # nn) and a longer German one: the Norwegian track is the reference, as a track tagged nor is; so for every
        # written form (arb for ara, prs for per, zsm for may), on text and image tracks alike
        from helpers import back_to_back, FakeIndex, pgs_track, realistic
        from subsync import pipeline

        def text(number, lang, n):
            return {"number": number, "language": lang, "codec": "S_TEXT/UTF8", "name": "", "forced": False,
                    "cues": realistic(n, number)}
        for tag, fallback in (("nb", "nor"), ("nn", "no"), ("nor", "nor"), ("arb", "ara"), ("prs", "per"),
                              ("zsm", "may")):
            idx = FakeIndex(text=[text(3, tag, 100), text(4, "de", 120)])
            self.assertEqual(pipeline.choose_reference(idx, 80, "swe", fallback=(fallback,))["number"], 3, tag)
            idx = FakeIndex(image=[pgs_track(back_to_back(300, 5), 7, tag), pgs_track(back_to_back(360, 6), 8, "ger")])
            ref = pipeline.choose_reference(idx, 80, "swe", image_ref=True, fallback=(fallback,))
            self.assertEqual((ref["route"], ref["number"]), ("image", 7), tag)


class DefaultLanguageTest(unittest.TestCase):
    """The language synced when none is chosen is one constant, wherever a default language is written."""

    def test_every_default_language_is_the_one_constant(self):
        from subsync import explain, ui
        from subsync.settings import Settings
        self.assertEqual(langs.DEFAULT_LANGUAGE, "eng")
        self.assertEqual(Settings({}).languages, [langs.DEFAULT_LANGUAGE])
        self.assertEqual(explain.SETTINGS["SUBSYNC_LANGUAGES"][0], langs.DEFAULT_LANGUAGE)
        self.assertEqual(explain.QUESTIONS["LANGUAGES"][0], "English")
        slots = ui.language_slots([("English", "eng"), ("Russian", "rus")], [], 2, "q", "h")
        self.assertIn('<option value="eng" selected>English</option>', slots)

    @unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
    def test_the_sign_up_takes_the_one_constant_too(self):
        # public mode's sign-up needs the cryptography package (its records), so it is checked on its own
        from subsync import signup
        self.assertEqual(signup._languages(""), [langs.DEFAULT_LANGUAGE])

    def test_a_list_of_codes(self):
        # lang_list moved here from the worker: a setting's comma-separated codes, in their order
        self.assertEqual(langs.lang_list(" en , ja,,rus "), ["eng", "jpn", "rus"])
        self.assertEqual(langs.lang_list(""), [])
        self.assertEqual(langs.lang_list(None), [])
        # each once (en,eng was English twice)
        self.assertEqual(langs.lang_list("en,eng, English ,ja,jpn"), ["eng", "jpn"])


if __name__ == "__main__":
    unittest.main()
