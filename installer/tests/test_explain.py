"""Every ./setup question has its one explanation, and the README's settings section and docs/answers.example are the
texts of subsync/subsync/explain.py, written by installer/docs.py."""
import ast
import unittest
from pathlib import Path

from installer import docs, ui
from subsync import explain

REPO = Path(__file__).resolve().parent.parent.parent
ASKS = {'text', 'yesno', 'choose'}


def wizard_tree():
    """Every installer module (the questions are asked in engine.py and the hosting route's modules)."""
    body = []
    for path in sorted((REPO / 'installer').glob('*.py')):
        body += ast.parse(path.read_text()).body
    return ast.Module(body=body, type_ignores=[])


class Questions(unittest.TestCase):
    def test_every_question_asked_has_an_explanation(self):
        asked = set()
        for node in ast.walk(wizard_tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ASKS \
                    and node.args and isinstance(node.args[0], ast.Constant):
                asked.add(node.args[0].value)
        self.assertTrue(asked)
        self.assertLessEqual(asked, set(explain.QUESTIONS) | set(explain.SETTINGS))   # a limit: its setting's text

    def test_every_explanation_is_of_a_question_the_wizard_asks(self):
        constants = {node.value for node in ast.walk(wizard_tree()) if isinstance(node, ast.Constant)}
        self.assertLessEqual(set(explain.QUESTIONS), constants)

    def test_a_question_without_an_explanation_is_refused(self):
        with self.assertRaises(KeyError):
            ui.Asker({'NOT_A_QUESTION': 'x'}).text('NOT_A_QUESTION', 'Anything?')


class QuestionTexts(unittest.TestCase):
    """The question texts for the setup page as the owner approved them."""

    def test_the_password_text(self):
        self.assertEqual(explain.QUESTIONS['AIOS_CONFIG_PASSWORD'][1],
                         "The password you chose when you saved that config. Setup uses it to read the config and save "
                         "the change, and doesn't store it; subsync gets its own config with its own random password. "
                         "Removing subsync and restoring a saved copy ask for it again. If you put it in an answers "
                         "file, keep that file private.")

    def test_the_confirm_text(self):
        self.assertEqual(explain.QUESTIONS['CONFIRM'][1],
                         "Saves the changes shown here: subsync added to your config (after a backup copy) and "
                         "subsync's own config created or updated. Until then nothing is changed: go back to change an "
                         "answer, or leave and run ./setup again any time. Removing subsync and restoring a saved copy "
                         "also ask before they change anything.")

    def test_the_languages_text_starts_with_the_slots(self):
        text = explain.QUESTIONS['LANGUAGES'][1]
        self.assertTrue(text.startswith(
            "The subtitle languages to sync, up to four, the first one first; English if you choose none. In an "
            "answers file, names or codes separated by commas, like English, Spanish, ru. For each language and each "
            "file you play, "), text[:200])

    def test_removing_opensubtitles_is_asked_when_you_remove_subsync(self):
        self.assertTrue(explain.QUESTIONS['REMOVE_OPENSUBTITLES'][1].startswith(
            "Asked when you remove subsync, if setup had added OpenSubtitles to your config. Yes takes it out again"))

    def test_setup_names_the_program_without_the_command(self):
        self.assertIn("Asked only when setup can't find AIOStreams", explain.QUESTIONS['AIOS_URL'][1])
        self.assertIn("setup takes the ID out of it", explain.QUESTIONS['AIOS_CONFIG_ID'][1])
        self.assertIn("(setup shows it)", explain.QUESTIONS['PUBLIC_ADDRESS'][1])

    def test_no_question_speaks_of_the_terminal(self):
        import re
        for key, (_, text) in explain.QUESTIONS.items():
            with self.subTest(key=key):
                self.assertNotIn('Enter with nothing typed', text)
                self.assertEqual(re.findall(r'\./setup(?! again)', text), [])


class Documents(unittest.TestCase):
    def test_the_readme_settings_section_is_current(self):
        readme = (REPO / 'README.md').read_text()
        self.assertIn(docs.readme_section(), readme, 'run: python3 -I installer/docs.py')
        self.assertEqual(readme.count(docs.START), 1)

    def test_the_answers_example_is_current(self):
        self.assertEqual((REPO / 'docs' / 'answers.example').read_text(), docs.answers_example(),
                         'run: python3 -I installer/docs.py')

    def test_every_setting_and_rule_is_in_the_readme(self):
        section = docs.readme_section()
        for name in list(explain.QUESTIONS) + list(explain.SETTINGS):
            self.assertIn(f'`{name}`', section)
        for title, _ in explain.RULES:
            self.assertIn(title, section)

    def test_comments_keep_the_whole_text(self):
        text = explain.SETTINGS['SUBSYNC_DAILY_BUDGET_GB'][1]
        lines = docs.comment(text)
        self.assertTrue(all(line.startswith('# ') and len(line) <= 120 for line in lines))
        self.assertEqual(' '.join(line[2:] for line in lines), text)


if __name__ == '__main__':
    unittest.main()
