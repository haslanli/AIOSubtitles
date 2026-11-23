"""The language limit is an instance setting, 1 to 4, default 4: the setting, its explanation, the
languages explanation and the too-many error take its number, and the sign-up and the settings save enforce it."""
import unittest

from helpers import HAVE_CRYPTOGRAPHY, NEEDS  # (first: it puts the package on the import path)
from subsync import explain
from subsync.aiosconfig import parse_languages
from subsync.settings import Settings

# the approved text, rewritten with the installer plan
MAX_LANGUAGES_TEXT = (
    "How many languages each person may choose, from 1 to 4. Each language is a separate sync for every file they "
    "play: its own subtitle downloads and a few seconds of work, while the file itself is read once. Recommended: 4, "
    "the most the pages offer; lower it only when the server can't keep up. A lower limit applies when people next "
    "save their languages; until then they keep the ones they have.")
# the approved text, as the pages show it with today's limit of four
TODAY = ("The languages you want synced subtitles in, up to four, like English, Spanish. Each one is a separate sync "
         "for every file you play, so fewer is faster. The first one gets up to three synced choices, the others one "
         "each. You can change them later; a change applies from the next file you play.")
ONE = ("The language you want synced subtitles in, like English. It gets up to three synced choices for every file you "
       "play. You can change it later; a change applies from the next file you play.")


class SettingTest(unittest.TestCase):
    def test_default_four(self):
        self.assertEqual(Settings({}).max_languages, 4)

    def test_one_to_four(self):
        for n in "1234":
            with self.subTest(n=n):
                s = Settings({"SUBSYNC_MAX_LANGUAGES": n})
                self.assertEqual(s.max_languages, int(n))
                self.assertEqual(s.conflicts(), [])

    def test_anything_else_stops_the_start(self):
        for value in ("0", "5", "-1", "two", "", "3.5"):
            with self.subTest(value=value):
                s = Settings({"SUBSYNC_MAX_LANGUAGES": value})
                self.assertIsNone(s.max_languages)
                self.assertIn("SUBSYNC_MAX_LANGUAGES must be a whole number from 1 to 4.", s.conflicts())

    def test_explanation_t1(self):
        self.assertEqual(explain.SETTINGS["SUBSYNC_MAX_LANGUAGES"], ("4", MAX_LANGUAGES_TEXT))
        group = dict(explain.GROUPS)["Public mode (one service for many people)"]
        self.assertIn("SUBSYNC_MAX_LANGUAGES", group)


class CeilingTest(unittest.TestCase):
    """The most languages anyone may choose is one value, aiosconfig.MAX_LANGUAGES, and every place that names
    the languages one by one covers it."""

    def test_one_ceiling_for_the_setting_the_parser_and_the_pages(self):
        from subsync import aiosconfig, ui
        top = aiosconfig.MAX_LANGUAGES
        self.assertEqual(top, 4)
        self.assertEqual(Settings({"SUBSYNC_MAX_LANGUAGES": str(top)}).max_languages, top)
        over = Settings({"SUBSYNC_MAX_LANGUAGES": str(top + 1)})
        self.assertIsNone(over.max_languages)
        self.assertIn(f"SUBSYNC_MAX_LANGUAGES must be a whole number from 1 to {top}.", over.conflicts())
        self.assertIn(f"from 1 to {top}.", explain.SETTINGS["SUBSYNC_MAX_LANGUAGES"][1])
        codes = [three for _, _, three in aiosconfig.LANGUAGES[:top + 1]]
        self.assertEqual(len(parse_languages(", ".join(codes[:top]))[0]), top)     # the parser's default: the ceiling
        self.assertEqual(parse_languages(", ".join(codes))[0], [])
        for n in range(1, top + 1):
            with self.subTest(n=n):
                self.assertIn(n, aiosconfig.LIMIT_WORDS)
                self.assertIn(n, explain.NUMBER_WORDS)
        self.assertGreaterEqual(len(ui.ORDINALS), top)


def limit(n, **env):
    return Settings(dict({"SUBSYNC_MAX_LANGUAGES": str(n)}, **env))


class ExplanationTest(unittest.TestCase):
    def test_four_is_todays_text(self):
        self.assertEqual(explain.languages_help(Settings({})), TODAY)
        self.assertEqual(explain.languages_help(limit(4)), TODAY)
        self.assertIn("up to four,", TODAY)

    def test_two_and_three_change_only_the_number(self):
        self.assertEqual(explain.languages_help(limit(3)), TODAY.replace("up to four,", "up to three,"))
        self.assertEqual(explain.languages_help(limit(2)), TODAY.replace("up to four,", "up to two,"))

    def test_one(self):
        self.assertEqual(explain.languages_help(limit(1)), ONE)

    def test_the_synced_choices_are_the_instances(self):
        # The first language gets SUBSYNC_MAIN_ALTERNATIVES copies with best fit on, one with it off, and the
        # text says so (in words up to four); at the defaults the words are today's
        several = "The first one gets up to three synced choices, the others one each."
        one = "It gets up to three synced choices for every file you play."
        self.assertIn(" " + several + " ", TODAY)
        self.assertIn(" " + one + " ", ONE)
        for env, first, only in (
                ({"SUBSYNC_MAIN_ALTERNATIVES": "2"},
                 "The first one gets up to two synced choices, the others one each.",
                 "It gets up to two synced choices for every file you play."),
                ({"SUBSYNC_MAIN_ALTERNATIVES": "5"},
                 "The first one gets up to 5 synced choices, the others one each.",
                 "It gets up to 5 synced choices for every file you play."),
                ({"SUBSYNC_MAIN_ALTERNATIVES": "1"}, "Each gets one synced choice.",
                 "It gets one synced choice for every file you play."),
                ({"SUBSYNC_BEST_FIT": "off"}, "Each gets one synced choice.",
                 "It gets one synced choice for every file you play.")):
            with self.subTest(env=env):
                self.assertEqual(explain.languages_help(limit(4, **env)), TODAY.replace(several, first))
                self.assertEqual(explain.languages_help(limit(2, **env)),
                                 TODAY.replace(several, first).replace("up to four,", "up to two,"))
                self.assertEqual(explain.languages_help(limit(1, **env)), ONE.replace(one, only))


class ParseTest(unittest.TestCase):
    FIVE = "English, Spanish, French, German, Italian"

    def test_four_by_default_with_todays_words(self):
        self.assertEqual(parse_languages(self.FIVE),
                         ([], "Four languages at most (each one is a separate sync for every file)."))
        self.assertEqual(len(parse_languages("English, Spanish, French, German")[0]), 4)

    def test_the_limit_names_its_number(self):
        self.assertEqual(parse_languages("English, Spanish, French, German", limit=3),
                         ([], "Three languages at most (each one is a separate sync for every file)."))
        self.assertEqual(parse_languages("English, Spanish, French", limit=2),
                         ([], "Two languages at most (each one is a separate sync for every file)."))
        self.assertEqual(parse_languages("English, Spanish", limit=1), ([], "One language at most."))
        self.assertEqual(len(parse_languages("English, Spanish, French", limit=3)[0]), 3)
        self.assertEqual(len(parse_languages("English", limit=1)[0]), 1)


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class EnforcedTest(unittest.TestCase):
    """The sign-up's and the settings save's checks (signup._languages) take the instance's limit."""

    def test_signup_languages_take_the_limit(self):
        from subsync.signup import SignupError, _languages
        self.assertEqual(_languages("English, Spanish, French", 3), ["eng", "spa", "fre"])
        with self.assertRaises(SignupError) as caught:
            _languages("English, Spanish, French, German", 3)
        self.assertEqual(caught.exception.field, "languages")
        self.assertEqual(str(caught.exception.message),
                         "Three languages at most (each one is a separate sync for every file).")
        self.assertEqual(_languages("", 1), ["eng"])


if __name__ == "__main__":
    unittest.main()
