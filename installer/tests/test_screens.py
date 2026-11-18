"""The setup page's screens: approved words only, the parts' structure, every value
escaped, no inline style, no way forward while installing or on a stop (but one about the config, whose Continue reads
it again), and the secret never on a page."""
import re
import unittest
from html.parser import HTMLParser
from unittest import mock

from installer import dockerenv, limits, plan, screens, update
from installer.aios import Aios
from installer.tests import samples
import html as htmllib

from subsync import explain, pages, realdebrid, ui
from subsync.aiosconfig import LANGUAGES

HOSTILE = '"><script>alert(1)</script><b>x</b>'


class Tree(HTMLParser):
    """Every visible text and aria-label, the tags with their attributes, and the h1s."""

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.texts, self.tags, self.skip = [], [], 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag in ('script', 'style', 'title', 'pre', 'textarea', 'option'):
            self.skip += 1
        if attrs.get('aria-label'):
            self.texts.append(attrs['aria-label'])

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'title', 'pre', 'textarea', 'option'):
            self.skip -= 1

    def handle_data(self, data):
        text = ' '.join(data.split())
        if text and not self.skip:
            self.texts.append(text)


def approved():
    texts = set(screens.INSTALLER_WORDS) | set(pages.UI_WORDS) | {realdebrid.REAL_DEBRID_ONLY}
    texts |= {spoken for _, spoken in ui.CHECK_MARKS.values()} | {spoken for _, spoken in ui.PROGRESS_MARKS.values()}
    texts |= {option for lim in limits.personal(audio=True) + limits.hosting(audio=True) for option in lim.options}
    texts |= {text for _, text in explain.QUESTIONS.values()} | {text for _, text in explain.SETTINGS.values()}
    return texts


class Words(unittest.TestCase):
    def test_every_text_is_an_approved_text(self):
        corpus = approved()
        slots = {f'Remove: {name}' for name, _, _ in LANGUAGES} | {f'Remove: {o}' for o in ui.ORDINALS}
        for name, (html, values) in samples.every_state().items():
            values = {' '.join(v.split()) for v in values}
            with self.subTest(screen=name):
                for text in Tree(html).texts:
                    self.assertTrue(text in corpus or text in values or text in slots, f'{name}: "{text}"')

    def test_the_installer_words_are_the_approved_ones(self):
        # the start screen and the labels, word for word
        for words in ('Set up AIOSubtitles', 'How do you want to use it?', 'Install it for yourself',
                      'Host a public instance', 'Save these changes', 'Web address', 'Trusted proxy', 'Secret key',
                      'SubSource API key (optional)', 'Updates', 'Update', 'Your answers', 'Change your answers',
                      'Remove subsync', 'Data a day in total', 'Audio per file', 'Languages per person',
                      'New files per person a day', 'Data per person a day', 'People signed up',
                      'Sign-up attempts per address an hour', 'Files synced at a time', 'Workers at once',
                      'Requests at once', 'The whole service', 'This server', 'Its web address', 'Sign-ups',
                      'Installing AIOSubtitles', 'Limits', 'Check again', 'Remove AIOSubtitles',
                      "Delete everyone's records and stored copies",
                      'Run ./setup again to see its status, update it, change your answers or remove it.',
                      'This setup page has ended. Run ./setup again for a new address.',
                      # the audio route: the questions, the answers, the hosting route's limits
                      'Sync files that have no subtitles of their own, from their audio?', 'No',
                      'Yes, when I pick the file', 'Yes, as soon as a file is opened',
                      'Offer syncing files that have no subtitles of their own, from their audio?',
                      'Audio syncs per person a day', 'Of those, as soon as a file is opened', 'Audio syncs at a time'):
            self.assertIn(words, screens.INSTALLER_WORDS)

    def test_every_limit_label_is_an_approved_word(self):
        # the hosting route's audio-only limits too: the list once left them out
        for lim in limits.personal(audio=True, usenet=True) + limits.hosting(usenet=True, audio=True):
            with self.subTest(label=lim.label):
                self.assertIn(lim.label, screens.INSTALLER_WORDS)
        for old in ('Also sync files that carry no subtitles of their own, by listening to the audio?',
                    'Let people sync files that carry no subtitles of their own, by listening to the audio, when they '
                    'pick them?'):
            self.assertNotIn(old, screens.INSTALLER_WORDS)


class Samples(unittest.TestCase):
    """The samples' stops and problems are setup's own words (hand-copied ones had drifted from them), and the values a
    sample passes reach its screen."""

    def texts(self, name):
        return Tree(samples.every_state()[name][0]).texts

    def test_no_docker_is_docker_problems_stop(self):
        with mock.patch.object(dockerenv.shutil, 'which', lambda name: None):
            problem, fix = dockerenv.docker_problem()
        self.assertIn(problem, self.texts('machine-stop'))
        self.assertIn(fix, self.texts('machine-stop'))

    def test_an_unknown_language_is_the_language_checks_problem(self):
        self.assertIn(plan.parse_languages('Klingon')[1], self.texts('choices-problem'))

    def test_an_address_nothing_answers_at_is_the_clients_words_and_fix(self):
        # engine.Engine._check_aios outside setup's container: the client's message, then its fix
        refused = samples.aios_error('http://127.0.0.1:3001', Aios.status, samples.Offline())
        self.assertIn(f'{refused.message} {refused.fix}', self.texts('aios-problem'))

    def test_a_wrong_password_is_the_clients_words(self):
        refusal = samples.Offline(401, {'error': {'code': 'USER_INVALID_PASSWORD'}})
        wrong = samples.aios_error(samples.PUBLIC, lambda aios: aios.get_config(samples.CONFIG_ID, 'x'), refusal)
        self.assertIn(wrong.message, self.texts('config-wrong-password'))

    def test_a_limit_problem_is_the_limit_checks_words(self):
        self.assertIn(limits.check(limits.DAILY, '0')[1], self.texts('limits-problem'))

    def test_the_audio_limit_shows_the_samples_value(self):
        # it once passed the answers file's key (AUDIO_BUDGET), which the screen doesn't read: the default showed
        html, _ = samples.every_state()['limits-audio']
        checked = [attrs['value'] for tag, attrs in Tree(html).tags
                   if tag == 'input' and attrs.get('name') == limits.AUDIO.setting and 'checked' in attrs]
        self.assertEqual(checked, ['2'])
        self.assertNotEqual(limits.AUDIO.default, '2')


class TmdbQuestion(unittest.TestCase):
    def test_the_key_fields_with_tmdbs_notice(self):
        notice = explain.TMDB_NOTICE
        for name, label in (('choices', 'TMDB API key (optional)'), ('host-choices', 'TMDB API key for everyone (optional)')):
            html, _ = samples.every_state()[name]
            with self.subTest(screen=name):
                self.assertIn(label, html)
                self.assertIn('name="TMDB_KEY"', html)
                self.assertIn(htmllib.escape(notice, quote=False), html)
                self.assertIn('class="tmdb-logo"', html)
        html, _ = samples.every_state()['host-choices']
        self.assertIn('Let each person add their own TMDB key?', html)
        self.assertIn(htmllib.escape(explain.SETTINGS['SUBSYNC_TMDB_PERSONAL'][1], quote=True), html)


class AudioQuestion(unittest.TestCase):
    """The personal route's audio question, one of three answers, as the setup page asks it."""

    def radios(self, html):
        return [(attrs['value'], 'checked' in attrs) for tag, attrs in Tree(html).tags
                if tag == 'input' and attrs.get('name') == 'AUDIO']

    def test_three_answers_the_current_one_picked_with_the_explanation(self):
        for current in ('no', 'pick', 'open'):
            with self.subTest(current=current):
                html = screens.choices(['eng'], current, 'https://aio.example.com/subsync')
                self.assertEqual(self.radios(html), [(v, v == current) for v in ('no', 'pick', 'open')])
        html = screens.choices(['eng'], 'pick', 'https://aio.example.com/subsync')
        tree = Tree(html)
        self.assertIn('Sync files that have no subtitles of their own, from their audio?', tree.texts)
        self.assertIn(' '.join(explain.QUESTIONS['AUDIO'][1].split()), tree.texts)
        group = [attrs for tag, attrs in tree.tags if tag == 'fieldset'][0]
        self.assertEqual(group.get('aria-describedby'), 'audio-help')
        self.assertNotIn('type="checkbox" id="audio"', html)


class Structure(unittest.TestCase):
    def test_one_h1_lang_and_no_inline_style(self):
        for name, (html, _) in samples.every_state().items():
            with self.subTest(screen=name):
                tree = Tree(html)
                self.assertEqual(sum(1 for tag, _ in tree.tags if tag == 'h1'), 1)
                self.assertIn('<html lang="en">', html)
                self.assertFalse(any('style' in attrs for _, attrs in tree.tags))
                self.assertNotIn('<style', html)
                self.assertNotIn('/privacy', html)                       # the installer has no privacy page

    def test_every_field_has_its_label_and_explanation(self):
        for name, (html, _) in samples.every_state().items():
            tree = Tree(html)
            ids = {attrs.get('id') for _, attrs in tree.tags}
            # a label wrapping its radio or box has no for=: None isn't a field's label
            labelled = {attrs.get('for') for tag, attrs in tree.tags if tag == 'label'} - {None}
            for tag, attrs in tree.tags:
                if tag in ('input', 'select') and attrs.get('type') not in ('hidden', 'radio', 'checkbox'):
                    with self.subTest(screen=name, field=attrs.get('id') or attrs.get('name')):
                        self.assertTrue(attrs.get('id'), f'{name}: a field without an id: {attrs}')
                        self.assertIn(attrs.get('id'), labelled)
                        for ref in (attrs.get('aria-describedby') or '').split():
                            self.assertIn(ref, ids)

    def test_every_question_shows_its_explanation(self):
        # test_flow's typed "?" (it showed the explanation again): on the page, each field has it under it
        html, _ = samples.every_state()['config']
        self.assertIn(htmllib.escape(explain.QUESTIONS['AIOS_CONFIG_ID'][1]), html)
        self.assertIn('aria-describedby="aios_config_password-help"', html)

    def test_the_page_after_setups_rows_are_the_settings_pages_rows(self):
        # one frame (subsync's ui.row) for the update panel, the rollback, your answers and removing, as on the
        # settings page: each section is labelled by its own heading
        html = screens.after_setup('yourself', [('ok', 'running', None)], [('logs', 'docker logs x')], '1.4.0', None,
                                   config='1a2b3c4d…', rollback=('1.3.0', 'databases-20261114'))
        sections = re.findall(r'<section class="grid12 row" aria-labelledby="([^"]+)"><div class="about">'
                              r'<h2 id="([^"]+)">', html)
        self.assertEqual([hid for hid, _ in sections], ['up-h', 'rb-h', 'ans-h', 'rm-h'])
        self.assertTrue(all(hid == heading for hid, heading in sections))
        self.assertIs(pages._row, ui.row)
        self.assertEqual(ui.row('a-h', 'A <b>', '<p class="help">x</p>', '<div class="controls">y</div>'),
                         '<section class="grid12 row" aria-labelledby="a-h"><div class="about"><h2 id="a-h">A &lt;b&gt;'
                         '</h2><p class="help">x</p></div><div class="controls">y</div></section>')

    def test_a_wrong_password_keeps_the_id_and_never_shows_a_password(self):
        # test_flow's "an empty password goes back to the ID", as the page does it
        html, _ = samples.every_state()['config-wrong-password']
        self.assertIn('value="1a2b3c4d-1234-4abc-8def-0123456789ab"', html)
        password = re.search(r'<input[^>]*name="AIOS_CONFIG_PASSWORD"[^>]*>', html).group(0)
        self.assertNotIn('value=', password)
        self.assertIn('aria-invalid="true"', password)


class Rules(unittest.TestCase):
    def test_no_way_forward_while_installing_and_continue_reads_the_config_again_after_its_stop(self):
        states = samples.every_state()
        for name in ('install-running', 'check-running', 'updating'):
            html, _ = states[name]
            with self.subTest(screen=name):
                self.assertIn('<meta http-equiv="refresh" content="2">', html)
                self.assertNotIn('aria-label="Back"', html)
                self.assertNotIn('>Back<', html)
        for name in ('install-running', 'machine-stop'):
            html, _ = states[name]
            with self.subTest(screen=name):
                self.assertRegex(html, r'<button class="btn primary" type="button" disabled>Continue')
        # the Real-Debrid stop's way out: Continue stays a submit button, which reads the config again (web.py)
        html, _ = states['config-rd-only']
        self.assertRegex(html, r'<button class="btn primary" type="submit" name="go" value="next">Continue')
        self.assertNotIn('type="button" disabled', html)
        self.assertIn('>Back<', html)
        for name, (html, _) in states.items():
            if name not in ('install-running', 'check-running', 'updating'):
                with self.subTest(screen=name):
                    self.assertNotIn('http-equiv="refresh"', html)

    def test_a_saved_key_has_the_box_that_removes_it_under_its_field(self):
        states = samples.every_state()
        html, _ = states['choices-saved-keys']
        names = re.findall(r'name="([A-Z_]+)"', html)
        for key in ('SUBDL_KEY', 'TMDB_KEY'):
            with self.subTest(key=key):
                self.assertEqual(names[names.index(key) + 1], f'REMOVE_{key}')     # right under its field
        self.assertNotIn('name="REMOVE_WYZIE_KEY"', html)                  # none saved: no box
        self.assertEqual(html.count('<b>Remove the saved key</b>'), 2)
        html, _ = states['host-choices-saved-keys']
        self.assertEqual(html.count('<b>Remove the saved key</b>'), 3)
        self.assertNotIn('Remove the saved key', states['choices'][0])
        self.assertNotIn('Remove the saved key', states['host-choices'][0])

    def test_installing_shows_one_progress_list(self):
        # as installer piece 3's installing screen: done lines and the running one in one list, problems as check rows
        html, _ = samples.every_state()['install-done']
        self.assertEqual(html.count('<ol class="progress">'), 1)
        self.assertNotIn('<ul class="checks">', html)
        html, _ = samples.every_state()['install-failed']
        self.assertEqual((html.count('<ol class="progress">'), html.count('<ul class="checks">')), (1, 1))

    def test_the_secret_never_reaches_a_page(self):
        html, _ = samples.every_state()['host-signups']
        self.assertIn(f'value="{screens.SECRET_DOTS}" readonly', html)
        self.assertNotIn('name="SUBSYNC_SECRET_KEY"', html)

    def test_every_value_is_escaped(self):
        # every screen that shows a typed value, a value Docker or AIOStreams gives, or a problem or stop text
        rows = [('ok', HOSTILE, HOSTILE), ('bad', HOSTILE, None)]
        stop = (HOSTILE, HOSTILE)
        shown = {
            'machine': screens.machine('yourself', rows),
            'aios-address': screens.aios(rows, ask='AIOS_URL', value=HOSTILE, problem=('AIOS_URL', HOSTILE), stop=stop),
            'aios-containers': screens.aios(rows, ask='AIOS_CONTAINER', value=HOSTILE, containers=[(HOSTILE, HOSTILE)]),
            'config': screens.config(rows, HOSTILE, problem=('AIOS_CONFIG_ID', HOSTILE)),
            'host-address': screens.host_address(HOSTILE, HOSTILE),
            'host-signups': screens.host_signups(HOSTILE),
            'choices': screens.choices(['eng'], 'no', HOSTILE, problem=('LANGUAGES', HOSTILE)),
            'limits': screens.limits('yourself', {'SUBSYNC_DAILY_BUDGET_GB': HOSTILE},
                                     problem=('SUBSYNC_DAILY_BUDGET_GB', HOSTILE)),
            'limits-audio': screens.limits('yourself', {limits.AUDIO.setting: HOSTILE},
                                           problem=(limits.AUDIO.setting, HOSTILE), audio=True),
            'host-limits': screens.limits('host', {'SUBSYNC_SYNCS_AT_ONCE': HOSTILE},
                                          problem=('SUBSYNC_SYNCS_AT_ONCE', HOSTILE)),
            'review': screens.review([(HOSTILE, [('+', HOSTILE)], HOSTILE)]),
            'installing': screens.installing('host', rows, running=False, stop=stop, rule={HOSTILE: [HOSTILE]},
                                             rule_intro=HOSTILE),
            'check': screens.check('host', rows, running=False, offer_test_read=True, stop=stop),
            'host-choices': screens.host_choices(problem=('SUBDL_KEY', HOSTILE)),
            'done': screens.done('yourself', HOSTILE, [HOSTILE, HOSTILE], [('code', HOSTILE), ('p', HOSTILE)]),
            'after-setup': screens.after_setup('yourself', rows, [(HOSTILE, HOSTILE)], HOSTILE, None, config=HOSTILE),
            'after-setup-host': screens.after_setup('host', rows, [(HOSTILE, HOSTILE)], HOSTILE,
                                                    update.Ready(HOSTILE, 'ready'), problem=('CONFIRM', HOSTILE),
                                                    rollback=(HOSTILE, HOSTILE)),
            'working': screens.working(HOSTILE, rows, running=False, stop=stop),
            'removed': screens.removed(HOSTILE, rows, [HOSTILE]),
        }
        for name, html in shown.items():
            with self.subTest(screen=name):
                self.assertNotIn('<script>alert', html)
                self.assertNotIn('<b>x</b>', html)
                self.assertNotIn('"&gt;&lt;script', html)        # the value's quote left raw would end its attribute

    def test_the_steps_are_the_approved_ones(self):
        self.assertEqual(screens.PERSONAL, ('This machine', 'Your AIOStreams', 'The config your apps use',
                                            'Your choices', 'Limits', 'Your AIOStreams configs', 'Installing subsync',
                                            'Checking that it works'))
        self.assertEqual(screens.HOSTING, ('This machine', 'Its web address', 'Sign-ups', 'Your choices', 'Limits',
                                           'Installing AIOSubtitles', 'Checking that it works'))


if __name__ == '__main__':
    unittest.main()
