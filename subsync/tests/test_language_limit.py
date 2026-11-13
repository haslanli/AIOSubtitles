"""The language limit is an instance setting, 1 to 4, default 4: the setting, its explanation, the
languages explanation and the too-many error take its number, and the sign-up and the settings save enforce it."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subsync import explain  # noqa: E402
from subsync.aiosconfig import parse_languages  # noqa: E402
from subsync.settings import Settings  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTOGRAPHY = True
except ImportError:
    HAVE_CRYPTOGRAPHY = False

T1 = ("Public mode only: how many languages each person may choose, from 1 to 4. Each language is a separate sync for "
      "every file they play, so a lower number means less work for the service. 4 is the most the pages offer.")
TODAY = explain.PUBLIC_FIELDS["languages"]
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
        self.assertEqual(explain.SETTINGS["SUBSYNC_MAX_LANGUAGES"], ("4", T1))
        group = dict(explain.GROUPS)["Public mode (one service for many people)"]
        self.assertIn("SUBSYNC_MAX_LANGUAGES", group)


class ExplanationTest(unittest.TestCase):
    def test_four_is_todays_text(self):
        self.assertEqual(explain.languages_help(4), TODAY)
        self.assertIn("up to four,", TODAY)

    def test_two_and_three_change_only_the_number(self):
        self.assertEqual(explain.languages_help(3), TODAY.replace("up to four,", "up to three,"))
        self.assertEqual(explain.languages_help(2), TODAY.replace("up to four,", "up to two,"))

    def test_one(self):
        self.assertEqual(explain.languages_help(1), ONE)


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


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
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
