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
    return ast.parse((REPO / 'installer' / 'wizard.py').read_text())


class Questions(unittest.TestCase):
    def test_every_question_asked_has_an_explanation(self):
        asked = set()
        for node in ast.walk(wizard_tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ASKS \
                    and node.args and isinstance(node.args[0], ast.Constant):
                asked.add(node.args[0].value)
        self.assertTrue(asked)
        self.assertLessEqual(asked, set(explain.QUESTIONS))

    def test_every_explanation_is_of_a_question_the_wizard_asks(self):
        constants = {node.value for node in ast.walk(wizard_tree()) if isinstance(node, ast.Constant)}
        self.assertLessEqual(set(explain.QUESTIONS), constants)

    def test_a_question_without_an_explanation_is_refused(self):
        with self.assertRaises(KeyError):
            ui.Asker({'NOT_A_QUESTION': 'x'}).text('NOT_A_QUESTION', 'Anything?')


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
