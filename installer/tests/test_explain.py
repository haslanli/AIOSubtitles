"""Every ./setup question has its one explanation, and the README's settings section and docs/answers.example are the
texts of subsync/subsync/explain.py, written by installer/docs.py."""
import ast
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import docs, hosting, limits, plan, screens, ui, web
from subsync import explain, version

REPO = Path(__file__).resolve().parent.parent.parent
ASKS = {'text', 'yesno', 'choose'}
# The only questions whose texts may name a command, each its own, once: REMOVE's and RESTORE's texts are asked
# only in the terminal, never shown on a setup page (test_screens.TerminalOnly, test_web's removal pages)
OWN_COMMAND = {'REMOVE': './setup --uninstall', 'RESTORE': './setup --restore FILE'}


def terminal_words(key, text):
    """Each ./setup a question's text names besides "./setup again": a question's text is also its field's explanation
    on the setup page. REMOVE's and RESTORE's may name their own command once: that one occurrence is taken out
    first, and only where it is the whole command (not the start of a longer one); the same check then applies."""
    if key in OWN_COMMAND:
        text = re.sub(re.escape(OWN_COMMAND[key]) + r'(?![\w-])', '', text, count=1)
    return re.findall(r'\./setup(?! again)', text)


def wizard_tree():
    """Every installer module (the questions are asked in engine.py and the hosting route's modules)."""
    body = []
    for path in sorted((REPO / 'installer').glob('*.py')):
        body += ast.parse(path.read_text()).body
    return ast.Module(body=body, type_ignores=[])


def asked_by_name():
    """The key of every question asked by its name: the first argument of each ask call that is written out."""
    return {node.args[0].value for node in ast.walk(wizard_tree())
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ASKS
            and node.args and isinstance(node.args[0], ast.Constant)}


class Questions(unittest.TestCase):
    def test_every_question_asked_has_an_explanation(self):
        asked = asked_by_name()
        self.assertTrue(asked)
        self.assertLessEqual(asked, set(explain.QUESTIONS) | set(explain.SETTINGS))   # a limit: its setting's text

    def test_every_explanation_is_of_a_question_the_wizard_asks(self):
        # asked, not only named: every key is also a string outside the ask calls (labels, the page's fields), so any
        # string in installer/ let a question nobody asks any more keep its explanation
        asked = asked_by_name() | set(web.PERSONAL_KEYS + web.HOST_KEYS)    # the optional keys, by engine.optional_key
        self.assertLessEqual(set(explain.QUESTIONS), asked)

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
        # it said removing and restoring "also ask": they asked CONFIRM itself; it named neither the Usenet
        # config nor the configs saving deletes when no longer used
        self.assertEqual(explain.QUESTIONS['CONFIRM'][1],
                         "Saves the changes shown here: subsync added to your config (after a backup copy), and "
                         "subsync's own config and its Usenet config created, updated or, when no longer used, "
                         "deleted. Until then nothing is changed: go back to change an answer, or leave and run "
                         "./setup again any time. Removing subsync and restoring a saved copy ask their own questions "
                         "before they change anything.")
        self.assertEqual(explain.QUESTIONS['CONFIRM'][0], 'yes')

    def test_removing_and_restoring_ask_their_own_yes(self):
        # CONFIRM answered "Save these changes?", "Remove subsync?" and the restore question alike, and the
        # example's CONFIRM=yes removed subsync; each now has its own key, its approved text and the example "no".
        # On a public instance the database copies made before updates, which hold everyone's records, stay
        self.assertEqual(explain.QUESTIONS['REMOVE'],
                         ('no', "Asked by ./setup --uninstall before anything is removed. Yes takes subsync out of your "
                                "config, deletes its own config and removes its container with its stored copies; on a "
                                "public instance, everyone's records go too, except in the database copies made before "
                                "updates, which stay in state/backups/. No changes nothing. It is kept apart from "
                                "CONFIRM, so an answers file made for installing never removes anything."))
        self.assertEqual(explain.QUESTIONS['RESTORE'],
                         ('no', "Asked by ./setup --restore FILE before the saved copy is put back into your config. "
                                "Yes replaces your config with that copy, after saving the current one. No changes "
                                "nothing."))
        engine_source = (REPO / 'installer' / 'engine.py').read_text()
        wizard_source = (REPO / 'installer' / 'wizard.py').read_text()
        self.assertEqual(engine_source.count("self.ask.yesno('CONFIRM', "), 1)          # Save these changes? only
        self.assertIn("self.ask.yesno('CONFIRM', 'Save these changes?', True)", engine_source)
        self.assertIn("self.ask.yesno('REMOVE', 'Remove subsync?', False)", engine_source)
        self.assertIn("self.ask.yesno('REMOVE', screens.DELETE_RECORDS, False)", engine_source)
        self.assertNotIn("'CONFIRM'", wizard_source)
        self.assertIn("e.ask.yesno('RESTORE', f'Put the copy from {Path(path).name} back into config {short(uuid)}?', "
                      "False)", wizard_source)

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
        # It is also asked when setup finds the container but can't reach it (engine.reach_aios)
        self.assertTrue(explain.QUESTIONS['AIOS_URL'][1].startswith(
            "Asked only when setup can't find or can't reach AIOStreams running in Docker on this machine: "))
        self.assertIn("setup takes the ID out of it", explain.QUESTIONS['AIOS_CONFIG_ID'][1])
        self.assertIn("You can paste the whole install link, or its alias link (…/stremio/u/<name>/…): setup takes the "
                      "ID out of it", explain.QUESTIONS['AIOS_CONFIG_ID'][1])
        self.assertIn("(setup shows it)", explain.QUESTIONS['PUBLIC_ADDRESS'][1])

    def test_no_question_speaks_of_the_terminal(self):
        for key, (_, text) in explain.QUESTIONS.items():
            with self.subTest(key=key):
                self.assertNotIn('Enter with nothing typed', text)
                self.assertEqual(terminal_words(key, text), [])

    def test_only_remove_and_restore_may_name_a_command_their_own_once(self):
        # The exemption is exactly "./setup --uninstall" in REMOVE and "./setup --restore FILE" in RESTORE
        remove, restore = explain.QUESTIONS['REMOVE'][1], explain.QUESTIONS['RESTORE'][1]
        self.assertEqual(terminal_words('REMOVE', remove), [])
        self.assertEqual(terminal_words('RESTORE', restore), [])
        refused = [
            ('REMOVE', remove + ' Then run ./setup --uninstall.'),                         # its own command twice
            ('REMOVE', remove + ' ./setup --restore FILE puts a copy back.'),               # the other's command
            ('REMOVE', remove + ' ./setup --status shows it.'),                             # any other command
            ('REMOVE', remove.replace('./setup --uninstall', './setup --uninstall-finish')),    # a longer command
            ('REMOVE', remove.replace('./setup --uninstall', './setup')),
            ('RESTORE', restore + ' ./setup --restore FILE again.'),
            ('RESTORE', restore + ' ./setup --uninstall takes it out.'),
            ('RESTORE', restore.replace('./setup --restore FILE', './setup --restore FILES')),
            ('RESTORE', restore.replace('./setup --restore FILE', './setup --restore')),
            ('CONFIRM', explain.QUESTIONS['CONFIRM'][1] + ' ./setup --uninstall removes it.'),
            ('REMOVE_OPENSUBTITLES', explain.QUESTIONS['REMOVE_OPENSUBTITLES'][1] + ' Asked by ./setup --uninstall.'),
            ('AIOS_CONFIG_PASSWORD', explain.QUESTIONS['AIOS_CONFIG_PASSWORD'][1] + ' ./setup --restore FILE asks it.')]
        for key, text in refused:
            with self.subTest(key=key, text=text):
                self.assertNotEqual(terminal_words(key, text), [])
        for key in ('CONFIRM', 'REMOVE', 'RESTORE'):                # "./setup again" stays allowed in every question
            self.assertEqual(terminal_words(key, 'Run ./setup again any time.'), [])


class Documents(unittest.TestCase):
    def test_the_readme_settings_section_is_current(self):
        readme = (REPO / 'README.md').read_text(encoding='utf-8')
        self.assertIn(docs.readme_section(), readme, 'run: python3 -I installer/docs.py')
        self.assertEqual(readme.count(docs.START), 1)

    def test_the_readme_escapes_each_placeholder_and_the_example_keeps_it(self):
        # The texts went into README as written, and Markdown took "(…/stremio/<ID>/…)" for an HTML tag,
        # which GitHub drops; docs/answers.example is plain text and shows them as written
        self.assertEqual(docs.markdown('a "<Language> (synced)" entry'), 'a "\\<Language> (synced)" entry')
        section = docs.readme_section()
        self.assertEqual(re.findall(r'(?<!\\)<[A-Za-z]', section), [])
        self.assertIn('(…/stremio/\\<ID>/…)', section)
        self.assertIn('as "\\<Language> (original)"', section)
        self.assertIn('(…/stremio/<ID>/…)', docs.answers_example())

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
        self.assertEqual((REPO / 'docs' / 'answers.example').read_text(encoding='utf-8'), docs.answers_example(),
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
        """docs/answers.example as [(heading, [(key, explanation, value, commented out)])], the route's question under
        heading None; a key's line written commented out ("# KEY=value", docs.COMMENTED_OUT) is a key's line too."""
        parts, heading, comment = [(None, [])], None, []
        for line in docs.answers_example().split('\n')[1:]:
            commented = re.fullmatch(r'# ([A-Z][A-Z0-9_]*)=(\S*)', line)
            if line.startswith('# ====='):
                heading = ''
            elif commented and commented[1] in explain.QUESTIONS.keys() | explain.SETTINGS.keys():
                parts[-1][1].append((commented[1], ' '.join(comment), commented[2], True))
                comment = []
            elif line.startswith('# '):
                comment.append(line[2:])
            elif '=' in line:
                key, value = line.split('=', 1)
                parts[-1][1].append((key, ' '.join(comment), value, False))
                comment = []
            elif heading == '' and comment:                     # a part's heading ends at its blank line
                parts.append((' '.join(comment), []))
                heading, comment = None, []
            else:
                comment = [] if not line else comment
        return parts

    def test_the_answers_example_has_its_four_parts_each_key_with_its_explanation(self):
        # one list mixed both routes and removal, and left out the keys asked under a setting's name;
        # removing asked CONFIRM, and no part said when restoring asks its keys (the headings); the
        # public part wrote the two limits again, and removing's and restoring's headings left out the
        # AIOStreams sign-in and, after an uninstall, AIOStreams' container or address
        parts = self.example_parts()
        self.assertEqual([heading for heading, _ in parts], [
            None, 'Install it for yourself (ROUTE=yourself)',
            'Host a public instance (ROUTE=host). SUBDL_KEY, TMDB_KEY, SUBSYNC_USENET_LISTINGS_PER_DAY and '
            'SUBSYNC_DAILY_BUDGET_GB above are asked here too.',
            'Removing it (./setup --uninstall --answers FILE). AIOS_CONFIG_PASSWORD above is asked here too, and '
            'AIOS_USERNAME and AIOS_PASSWORD when your AIOStreams wants a sign-in to save configs; on a public '
            'instance only REMOVE.',
            'Restoring a saved copy (./setup --restore FILE --answers FILE). AIOS_CONFIG_PASSWORD above is asked here '
            'too, AIOS_USERNAME and AIOS_PASSWORD when your AIOStreams wants a sign-in to save configs, and after '
            './setup --uninstall AIOS_CONTAINER or AIOS_URL, as when installing.'])
        keys = [[key for key, _, _, _ in lines] for _, lines in parts]
        self.assertEqual(keys[0], ['ROUTE'])
        self.assertEqual(keys[1], ['AIOS_CONTAINER', 'AIOS_URL', 'AIOS_CONFIG_ID', 'AIOS_CONFIG_PASSWORD', 'LANGUAGES',
                                   'AUDIO', 'USENET', 'AUDIO_BUDGET', 'SUBSYNC_DAILY_BUDGET_GB',
                                   'SUBSYNC_USENET_LISTINGS_PER_DAY', 'SUBDL_KEY', 'WYZIE_KEY', 'TMDB_KEY',
                                   'ADD_OPENSUBTITLES', 'PUBLIC_ADDRESS', 'CACHE', 'AIOS_USERNAME', 'AIOS_PASSWORD',
                                   'CONFIRM', 'TEST_READ'])
        self.assertEqual(keys[2][:7], ['HOST_ADDRESS', 'SUBSYNC_TRUSTED_PROXY', 'SUBSYNC_PUBLIC_INVITE',
                                       'SUBSYNC_SUBSOURCE_KEY', 'SUBSYNC_TMDB_PERSONAL', 'SUBSYNC_USENET',
                                       'SUBSYNC_AUDIO_ROUTE'])
        self.assertEqual(keys[2][7:], [lim.setting for lim in limits.hosting(usenet=True, audio=True)
                                       if lim.setting not in keys[1]])
        self.assertEqual(keys[3], ['REMOVE', 'REMOVE_OPENSUBTITLES'])
        self.assertEqual(keys[4], ['RESTORE'])
        self.assertEqual(len(keys), 5)
        notes = {'AUDIO': 'In an answers file: no, pick or open.',
                 'SUBSYNC_PUBLIC_INVITE': 'In an answers file: leave this line out to keep the current code (on a '
                                          'first setup, the one setup makes); an empty line lets anyone sign up.'}
        commented_out = {'SUBDL_KEY', 'WYZIE_KEY', 'TMDB_KEY', 'SUBSYNC_SUBSOURCE_KEY', 'SUBSYNC_PUBLIC_INVITE'}
        for _, lines in parts:
            for key, text, value, commented in lines:
                with self.subTest(key=key):
                    question = explain.QUESTIONS.get(key)
                    explanation = question[1] if question else explain.SETTINGS[key][1]
                    if key in notes:                        # with its lines for answers files
                        explanation += ' ' + notes[key]
                    self.assertEqual(text, explanation)
                    self.assertEqual(commented, key in commented_out)
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
        asked = asked_by_name() | set(web.PERSONAL_KEYS + web.HOST_KEYS)   # the optional keys, by engine.optional_key
        asked |= {lim.setting for lim in limits.personal(audio=False, usenet=True) + limits.hosting(True, True)}
        self.assertIn('SUBSYNC_PUBLIC_INVITE', asked_by_name())      # asked like every question
        in_example = {key for _, lines in self.example_parts() for key, _, _, _ in lines}
        self.assertEqual(in_example, asked)

    def test_each_key_has_one_line_and_the_public_part_names_the_ones_above(self):
        # The public part wrote the daily limit and the Usenet searches again, and setup takes a key's last
        # line: the public part's 100 GB silently won over a lower limit set in your own install's part
        lines = [line.removeprefix('# ') for line in docs.answers_example().splitlines()]
        keys = [line.split('=', 1)[0] for line in lines if re.fullmatch(r'[A-Z][A-Z0-9_]*=\S*', line)]
        self.assertEqual(sorted(key for key in set(keys) if keys.count(key) > 1), [])
        example = docs.answers_example()
        # your own install's part's line, lowered as a person would
        lowered = example.replace('\nSUBSYNC_DAILY_BUDGET_GB=100\n', '\nSUBSYNC_DAILY_BUDGET_GB=20\n', 1)
        self.assertNotEqual(lowered, example)
        self.assertEqual(plan.parse_env(lowered)['SUBSYNC_DAILY_BUDGET_GB'], '20')
        # the heading names each key a public instance asks that is written above it, and only those
        parts = self.example_parts()
        heading = parts[2][0]
        written = {key for key, _, _, _ in parts[2][1]}
        host_asked = {'HOST_ADDRESS', *docs.HOSTING_SETTINGS, *web.HOST_KEYS,
                      *limits.names(limits.hosting(usenet=True, audio=True))}
        named = set(re.findall(r'\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b', heading))
        self.assertEqual(named, host_asked - written)
        self.assertLessEqual(named, {key for key, _, _, _ in parts[1][1]})

    def test_the_example_used_as_it_is_removes_nothing_setup_keeps(self):
        # Its empty key lines (SUBDL_KEY= ...) removed the saved keys, and its empty
        # SUBSYNC_PUBLIC_INVITE= opened a public instance to anyone; they are written commented out, as a line left out
        example = docs.answers_example()
        for key in ('SUBDL_KEY', 'WYZIE_KEY', 'TMDB_KEY', 'SUBSYNC_SUBSOURCE_KEY', 'SUBSYNC_PUBLIC_INVITE'):
            with self.subTest(key=key):
                self.assertEqual(example.count(f'\n# {key}=\n'), 1)
                self.assertNotIn(f'\n{key}=', example)
        self.assertEqual(set(plan.parse_env(example)) & set(docs.COMMENTED_OUT), set())
        note = ('In an answers file: leave this line out to keep the current code (on a first setup, the one setup '
                'makes); an empty line lets anyone sign up.')
        self.assertIn('\n'.join(docs.comment(explain.SETTINGS['SUBSYNC_PUBLIC_INVITE'][1]) + docs.comment(note))
                      + '\n# SUBSYNC_PUBLIC_INVITE=\n', example)
        # they are the keys whose empty line removes what setup keeps: every answer asked as optional
        optional = {node.args[0].value for node in ast.walk(wizard_tree())
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'text'
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and any(k.arg == 'optional' and getattr(k.value, 'value', None) is True for k in node.keywords)}
        self.assertEqual(set(docs.COMMENTED_OUT), optional | set(web.PERSONAL_KEYS + web.HOST_KEYS))

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
            self.assertEqual(readme.read_text(encoding='utf-8'), f'# Title\n{docs.readme_section()}\n## After\n')
            self.assertEqual((folder / 'docs' / 'answers.example').read_text(encoding='utf-8'), docs.answers_example())
            self.assertEqual(sorted(p.name for p in folder.rglob('*')), ['README.md', 'answers.example', 'docs'])

    def test_both_files_are_utf8_whatever_the_locale(self):
        # They were read and written in the locale's encoding, README.md in place: under a locale that can't
        # write its "…" README.md was left empty, and docs/answers.example wasn't written
        self.assertFalse(docs.readme_section().isascii())
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'docs').mkdir()
            (folder / 'README.md').write_bytes(f'# Title\n{docs.START}\nold\n{docs.END}\n## After\n'.encode())
            script = ('import locale, sys\nfrom pathlib import Path\nfrom unittest import mock\n'
                      'from installer import docs\nprint(locale.getencoding())\n'
                      'with mock.patch.object(docs, "REPO", Path(sys.argv[1])):\n    docs.write()\n')
            env = dict(PATH=os.environ.get('PATH', '/usr/bin:/bin'), LC_ALL='C', PYTHONCOERCECLOCALE='0',
                       PYTHONDONTWRITEBYTECODE='1')
            done = subprocess.run([sys.executable, '-X', 'utf8=0', '-c', script, tmp], cwd=REPO, env=env, timeout=60,
                                  capture_output=True, text=True, stdin=subprocess.DEVNULL)
            if done.stdout.strip().lower().replace('-', '') in ('utf8', ''):
                self.skipTest(f'no locale here writes other than UTF-8 ({done.stdout.strip()} {done.stderr[-200:]})')
            self.assertEqual(done.returncode, 0, done.stderr[-600:])
            self.assertEqual((folder / 'README.md').read_bytes().decode('utf-8'),
                             f'# Title\n{docs.readme_section()}\n## After\n')
            self.assertEqual((folder / 'docs' / 'answers.example').read_bytes().decode('utf-8'), docs.answers_example())

    def test_a_failure_leaves_both_files_as_they_were(self):
        # README.md was rewritten before docs/answers.example's text was built
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'docs').mkdir()
            readme, example = folder / 'README.md', folder / 'docs' / 'answers.example'
            readme.write_text(f'# Title\n{docs.START}\nold\n{docs.END}\n## After\n', encoding='utf-8')
            example.write_text('KEY=old\n', encoding='utf-8')
            with mock.patch.object(docs, 'REPO', folder), \
                    mock.patch.object(docs, 'answers_example', side_effect=RuntimeError('stopped')), \
                    self.assertRaises(RuntimeError):
                docs.write()
            self.assertEqual(readme.read_text(encoding='utf-8'), f'# Title\n{docs.START}\nold\n{docs.END}\n## After\n')
            self.assertEqual(example.read_text(encoding='utf-8'), 'KEY=old\n')
            with mock.patch.object(docs, 'REPO', folder), \
                    mock.patch.object(docs.os, 'replace', side_effect=OSError('disk full')), self.assertRaises(OSError):
                docs.write()
            self.assertEqual(readme.read_text(encoding='utf-8'), f'# Title\n{docs.START}\nold\n{docs.END}\n## After\n')
            self.assertEqual(sorted(p.name for p in folder.rglob('*')), ['README.md', 'answers.example', 'docs'])

    def test_the_projects_address_is_written_once(self):
        # README's link to the example and the setup pages' Questions link wrote the repository's address
        # again; a move of the repository changed version.PROJECT only
        self.assertEqual(docs.EXAMPLE_ON_GITHUB, f'{version.PROJECT}/blob/main/docs/answers.example')
        self.assertEqual(explain.CONTACT, f'{version.PROJECT}/issues')
        modules = sorted((REPO / 'installer').glob('*.py')) + sorted((REPO / 'subsync' / 'subsync').glob('*.py'))
        written = [path.relative_to(REPO).as_posix() for path in modules
                   if 'github.com/haslanli/AIOSubtitles' in path.read_text(encoding='utf-8')]
        self.assertEqual(written, ['subsync/subsync/version.py'])

    def test_comments_keep_the_whole_text(self):
        text = explain.SETTINGS['SUBSYNC_DAILY_BUDGET_GB'][1]
        lines = docs.comment(text)
        self.assertTrue(all(line.startswith('# ') and len(line) <= 120 for line in lines))
        self.assertEqual(' '.join(line[2:] for line in lines), text)


if __name__ == '__main__':
    unittest.main()
