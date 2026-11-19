"""Every ./setup question has its one explanation, and the README's settings section and docs/answers.example are the
texts of subsync/subsync/explain.py, written by installer/docs.py."""
import ast
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import docs, hosting, limits, plan, screens, ui, web
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
        self.assertIn("You can paste the whole install link, or its alias link (…/stremio/u/<name>/…): setup takes the "
                      "ID out of it", explain.QUESTIONS['AIOS_CONFIG_ID'][1])
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

    def test_the_settings_intro_says_what_setup_writes_and_how_a_change_is_applied(self):
        # it said only the first group is rewritten, and that a change needs docker compose on the machine
        section = ' '.join(docs.readme_section().split())
        self.assertIn("### subsync's settings ./setup writes the first group into `state/subsync.env` from your "
                      'answers, each line with its explanation above it, and the limits it asks; for a public '
                      'instance, also the public mode settings it asks. A limit it stops asking (its Usenet or audio '
                      'route turned off) goes back to its default. The others keep the defaults shown. To change one, '
                      'add `NAME=value` to `state/subsync.env` and run ./setup again: Change your answers, then go '
                      'through to the end, and subsync restarts with it (where Docker Compose is installed on this '
                      'machine, `docker compose -f state/compose.yaml up -d` does the same). Running ./setup again '
                      "keeps every line it doesn't write. ####", section)
        self.assertIn('Change your answers', screens.INSTALLER_WORDS)       # the page after setup's button

    def test_the_answers_example_is_current(self):
        self.assertEqual((REPO / 'docs' / 'answers.example').read_text(), docs.answers_example(),
                         'run: python3 -I installer/docs.py')

    def test_the_answers_examples_header_says_where_the_file_goes(self):
        # in state/: both routes mount it (the source route only it); and a " #" inside quotes is kept (plan.parse_env)
        header = docs.answers_example().split('\n\n', 1)[0]
        self.assertEqual(' '.join(line.removeprefix('# ') for line in header.splitlines()),
                         'Answers for ./setup --answers state/FILE (add --yes to ask nothing). Leave a line out to be '
                         "asked, or to keep the current value on a re-run; an optional key's empty line (TMDB_KEY=) "
                         'removes the saved key. A " #" starts a comment, except inside quotes. Keep this file private '
                         'if it holds a password.')

    def test_the_readme_names_the_keys_asked_under_a_settings_name_and_links_the_example(self):
        # it listed only the main questions, and pointed image-route users at a file the image doesn't have
        section = ' '.join(docs.readme_section().split())
        self.assertIn('Each question shows its explanation as it is asked. In an answers file (`./setup --answers FILE`) '
                      'each one is a line `KEY=value`; see [docs/answers.example](https://github.com/haslanli/'
                      'AIOSubtitles/blob/main/docs/answers.example). Also asked, under their setting names explained '
                      'below: for your own install SUBSYNC_DAILY_BUDGET_GB and, with Usenet on, '
                      'SUBSYNC_USENET_LISTINGS_PER_DAY; for a public instance SUBSYNC_TRUSTED_PROXY, '
                      'SUBSYNC_PUBLIC_INVITE, SUBSYNC_SUBSOURCE_KEY, SUBSYNC_TMDB_PERSONAL, SUBSYNC_USENET, '
                      'SUBSYNC_AUDIO_ROUTE and its limits.', section)

    def test_the_readme_explains_each_key_once(self):
        # SUBSYNC_AUDIO_ROUTE was explained twice, as a question and as a setting, in other words; the host's
        # question keeps its own words on setup's screen and in the example, and no other key is in both lists
        self.assertEqual(set(explain.QUESTIONS) & set(explain.SETTINGS), {'SUBSYNC_AUDIO_ROUTE'})
        section = docs.readme_section()
        questions = section.split('### The questions ./setup asks', 1)[1].split("### subsync's settings", 1)[0]
        self.assertEqual([key for key in explain.SETTINGS if f'- **`{key}`**' in questions], [])
        self.assertEqual(section.count('- **`SUBSYNC_AUDIO_ROUTE`** '), 1)
        self.assertIn('- **`SUBSYNC_AUDIO_ROUTE`** (default `off`): ' + explain.SETTINGS['SUBSYNC_AUDIO_ROUTE'][1],
                      section)
        self.assertIn('\n'.join(docs.comment(explain.QUESTIONS['SUBSYNC_AUDIO_ROUTE'][1]))
                      + '\nSUBSYNC_AUDIO_ROUTE=no\n', docs.answers_example())

    @staticmethod
    def example_parts():
        """docs/answers.example as [(heading, [(key, explanation, value)])], the route's question under heading None."""
        parts, heading, comment = [(None, [])], None, []
        for line in docs.answers_example().split('\n')[1:]:
            if line.startswith('# ====='):
                heading = ''
            elif line.startswith('# '):
                comment.append(line[2:])
            elif '=' in line:
                key, value = line.split('=', 1)
                parts[-1][1].append((key, ' '.join(comment), value))
                comment = []
            elif heading == '' and comment:                     # a part's heading ends at its blank line
                parts.append((' '.join(comment), []))
                heading, comment = None, []
            else:
                comment = [] if not line else comment
        return parts

    def test_the_answers_example_has_its_three_parts_each_key_with_its_explanation(self):
        # one list mixed both routes and removal, and left out the keys asked under a setting's name
        parts = self.example_parts()
        self.assertEqual([heading for heading, _ in parts], [
            None, 'Install it for yourself (ROUTE=yourself)',
            'Host a public instance (ROUTE=host). SUBDL_KEY and TMDB_KEY above are asked here too.',
            'Removing it (./setup --uninstall --answers FILE). AIOS_CONFIG_PASSWORD and CONFIRM above are asked here '
            'too; on a public instance only CONFIRM.'])
        keys = [[key for key, _, _ in lines] for _, lines in parts]
        self.assertEqual(keys[0], ['ROUTE'])
        self.assertEqual(keys[1], ['AIOS_CONTAINER', 'AIOS_URL', 'AIOS_CONFIG_ID', 'AIOS_CONFIG_PASSWORD', 'LANGUAGES',
                                   'AUDIO', 'USENET', 'AUDIO_BUDGET', 'SUBSYNC_DAILY_BUDGET_GB',
                                   'SUBSYNC_USENET_LISTINGS_PER_DAY', 'SUBDL_KEY', 'WYZIE_KEY', 'TMDB_KEY',
                                   'ADD_OPENSUBTITLES', 'PUBLIC_ADDRESS', 'CACHE', 'AIOS_USERNAME', 'AIOS_PASSWORD',
                                   'CONFIRM', 'TEST_READ'])
        self.assertEqual(keys[2][:7], ['HOST_ADDRESS', 'SUBSYNC_TRUSTED_PROXY', 'SUBSYNC_PUBLIC_INVITE',
                                       'SUBSYNC_SUBSOURCE_KEY', 'SUBSYNC_TMDB_PERSONAL', 'SUBSYNC_USENET',
                                       'SUBSYNC_AUDIO_ROUTE'])
        self.assertEqual(keys[2][7:], [lim.setting for lim in limits.hosting(usenet=True, audio=True)])
        self.assertEqual(keys[3], ['REMOVE_OPENSUBTITLES'])
        for _, lines in parts:
            for key, text, value in lines:
                with self.subTest(key=key):
                    question = explain.QUESTIONS.get(key)
                    explanation = question[1] if question else explain.SETTINGS[key][1]
                    if key == 'AUDIO':                      # with its line for answers files
                        explanation += ' In an answers file: no, pick or open.'
                    self.assertEqual(text, explanation)
                    if question:
                        self.assertEqual(value, question[0])
                    elif key == 'SUBSYNC_TRUSTED_PROXY':          # setup's own answer when none is given
                        self.assertEqual(value, hosting.TRUSTED_PROXY)
                    else:
                        self.assertEqual(value, explain.SETTINGS[key][0] or '')
        self.assertEqual(hosting.TRUSTED_PROXY, 'gateway')

    def test_the_audio_answers_are_named_under_its_explanation_in_the_example(self):
        # The answers an answers file takes for AUDIO, as one comment line under its explanation; the
        # question keeps its text on setup's screen and in the README
        note = 'In an answers file: no, pick or open.'
        example = docs.answers_example()
        self.assertIn('\n\n' + '\n'.join(docs.comment(explain.QUESTIONS['AUDIO'][1])) + '\n# ' + note + '\nAUDIO=no\n',
                      example)
        self.assertEqual(example.count(note), 1)
        self.assertNotIn(note, explain.QUESTIONS['AUDIO'][1])
        self.assertNotIn(note, docs.readme_section())
        self.assertEqual([value for value, _ in plan.AUDIO_ANSWERS], ['no', 'pick', 'open'])   # the answers it names

    def test_the_answers_example_has_every_key_setup_reads_from_an_answers_file(self):
        asked = set()
        for node in ast.walk(wizard_tree()):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ASKS \
                    and node.args and isinstance(node.args[0], ast.Constant):
                asked.add(node.args[0].value)
        asked |= set(web.PERSONAL_KEYS + web.HOST_KEYS)            # the optional keys, asked by engine.optional_key
        asked |= {lim.setting for lim in limits.personal(audio=False, usenet=True) + limits.hosting(True, True)}
        engine_source = (REPO / 'installer' / 'engine.py').read_text()
        self.assertIn("if 'SUBSYNC_PUBLIC_INVITE' in self.answers:", engine_source)   # read, not asked
        asked.add('SUBSYNC_PUBLIC_INVITE')
        in_example = {key for _, lines in self.example_parts() for key, _, _ in lines}
        self.assertEqual(in_example, asked)

    def test_every_setting_and_rule_is_in_the_readme(self):
        section = docs.readme_section()
        for name in list(explain.QUESTIONS) + list(explain.SETTINGS):
            self.assertIn(f'`{name}`', section)
        for title, _ in explain.RULES:
            self.assertIn(title, section)

    def test_writing_needs_both_markers_and_rewrites_only_between_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'docs').mkdir()
            readme = folder / 'README.md'
            with mock.patch.object(docs, 'REPO', folder):
                for text, missing in (('# Title\n', docs.START), (f'# Title\n{docs.START}\nold\n', docs.END),
                                      (f'# Title\n{docs.END}\n{docs.START}\nold\n', docs.END)):
                    with self.subTest(text=text):
                        readme.write_text(text)
                        with self.assertRaises(SystemExit) as caught:
                            docs.write()
                        self.assertIn(f'README.md has no line "{missing}"', str(caught.exception))
                        self.assertEqual(readme.read_text(), text)                  # nothing written
                        self.assertFalse((folder / 'docs' / 'answers.example').exists())
                readme.write_text(f'# Title\n{docs.START}\nold\n{docs.END}\n## After\n')
                docs.write()
            self.assertEqual(readme.read_text(), f'# Title\n{docs.readme_section()}\n## After\n')
            self.assertEqual((folder / 'docs' / 'answers.example').read_text(), docs.answers_example())

    def test_comments_keep_the_whole_text(self):
        text = explain.SETTINGS['SUBSYNC_DAILY_BUDGET_GB'][1]
        lines = docs.comment(text)
        self.assertTrue(all(line.startswith('# ') and len(line) <= 120 for line in lines))
        self.assertEqual(' '.join(line[2:] for line in lines), text)


if __name__ == '__main__':
    unittest.main()
