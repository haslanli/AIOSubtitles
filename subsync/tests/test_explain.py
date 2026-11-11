"""Every setting and fixed rule has its one explanation, and every explanation matches what the code does."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subsync import explain  # noqa: E402
from subsync.settings import Settings  # noqa: E402

CODE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "subsync")
REGISTER_NOTE = re.compile(r"\b(?:AD\d{1,3}|S4-\d+|P-S4-[A-Z0-9-]+)\b")


def settings_read_by_the_code():
    names = set()
    for name in os.listdir(CODE):
        if name.endswith(".py") and name != "explain.py":
            with open(os.path.join(CODE, name), encoding="utf-8") as f:
                names |= set(re.findall(r"""["'](SUBSYNC_[A-Z0-9_]+)["']""", f.read()))
    return names


class ExplainTest(unittest.TestCase):
    def test_every_setting_the_code_reads_has_one_explanation(self):
        self.assertEqual(set(explain.SETTINGS), settings_read_by_the_code())

    def test_each_setting_is_in_one_group(self):
        names = [name for _, group in explain.GROUPS for name in group]
        self.assertEqual(len(names), len(set(names)))

    def test_every_documented_default_is_the_codes_default(self):
        defaults = vars(Settings({}))
        for name, (default, _) in explain.SETTINGS.items():
            if default is None or name == "SUBSYNC_VAD_MODEL":   # required (no default), or read outside Settings
                continue
            with self.subTest(name=name):
                self.assertEqual(vars(Settings({name: default})), defaults)

    def test_required_settings_have_no_default(self):
        required = {name for name, (default, _) in explain.SETTINGS.items() if default is None}
        self.assertEqual(required, set(Settings({}).missing()))

    def texts(self):
        yield from ((name, text) for name, (_, text) in explain.SETTINGS.items())
        yield from ((name, text) for name, (_, text) in explain.QUESTIONS.items())
        yield from explain.RULES

    def test_each_explanation_is_one_short_plain_paragraph(self):
        for name, text in self.texts():
            with self.subTest(name=name):
                self.assertNotIn("\n", text)
                self.assertEqual(text, text.strip())
                self.assertTrue(text.endswith((".", ")")), text[-30:])
                self.assertLessEqual(len(text.split()), 160 if name in dict(explain.RULES) else 125)
                self.assertIsNone(REGISTER_NOTE.search(text))

    def test_the_real_debrid_rule_gives_the_reason_the_setup_stops_with(self):
        rules = dict(explain.RULES)
        self.assertIn("Remote Traffic", rules["Real-Debrid is never used"])
        self.assertIn("Usenet is never read", rules)


if __name__ == "__main__":
    unittest.main()
