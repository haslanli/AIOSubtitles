"""The setup page's screens: approved words only, the parts' structure, every value
escaped, no inline style, no way forward while installing or on a stop (but one about the config, whose Continue reads
it again), and the secret never on a page."""
import re
import unittest
from html.parser import HTMLParser
from unittest import mock

from installer import dockerenv, engine, limits, plan, screens, update
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
        # a Show button's spoken name: its words before its box's label, an approved text or a value
        named = {words + text for words in (ui.SHOW, ui.HIDE) for text in corpus}
        for name, (html, values) in samples.every_state().items():
            values = {' '.join(v.split()) for v in values}
            reveals = named | {words + value for words in (ui.SHOW, ui.HIDE) for value in values}
            with self.subTest(screen=name):
                for text in Tree(html).texts:
                    self.assertTrue(text in corpus or text in values or text in slots or text in reveals,
                                    f'{name}: "{text}"')

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

    def test_the_first_screen_needs_docker_and_4_gb(self):
        # it asked for Docker Compose, which is in the image, and 4 GB where the README said 3
        self.assertEqual(screens.BEFORE[:2], ('Docker on this machine.', 'About 4 GB of free disk space.'))
        texts = Tree(screens.start()).texts
        self.assertIn('Docker on this machine.', texts)
        self.assertIn('About 4 GB of free disk space.', texts)
        self.assertEqual([text for text in texts if 'Compose' in text], [])

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

    def test_each_choice_is_named_by_its_title_alone(self):
        # A tick box or a way was named by its whole row, title and explanation, then described by the
        # explanation again, so a screen reader read it twice; the whole row is still its label
        for name, (html, _) in samples.every_state().items():
            for row in re.findall(r'<label class="(?:choice|way)"[^>]*><input [^>]*>', html):
                with self.subTest(screen=name, row=row):
                    box = re.search(r' id="([^"]+)"', row).group(1)
                    self.assertIn(f' aria-labelledby="{box}-title"', row)
                    self.assertRegex(html, rf'<(b|span class="head") id="{re.escape(box)}-title">')
        html, _ = samples.every_state()['aios-several']
        self.assertIn('<span class="head" id="AIOS_CONTAINER-aiostreams-title"><b>aiostreams</b></span><span '
                      'id="AIOS_CONTAINER-aiostreams-help">', html)

    def test_the_show_buttons_name_their_box(self):
        # The keys step had three buttons named "Show", alike in a list of its controls or to voice control
        html, _ = samples.every_state()['choices']
        buttons = re.findall(r'<button class="reveal"[^>]*>', html)
        named = [(re.search(r'aria-label="([^"]*)"', b).group(1), re.search(r'data-hide-label="([^"]*)"', b).group(1))
                 for b in buttons]
        self.assertEqual(named, [('Show: SubDL API key (optional)', 'Hide: SubDL API key (optional)'),
                                 ('Show: Wyzie Subs API key (optional)', 'Hide: Wyzie Subs API key (optional)'),
                                 ('Show: TMDB API key (optional)', 'Hide: TMDB API key (optional)')])

    def test_the_reviews_config_headings_are_one_level_under_the_steps(self):
        # Each config's heading was an h3 right under the step's h1, skipping level 2 in a heading list
        for name in ('review', 'review-usenet'):
            with self.subTest(screen=name):
                html, _ = samples.every_state()[name]
                headings = [tag for tag, _ in Tree(html).tags if re.fullmatch(r'h\d', tag)]
                self.assertEqual(headings[0], 'h1')
                self.assertEqual(set(headings[1:]), {'h2'})
                self.assertIn('<div class="changes"><section><h2>', html)

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
                # it loads itself again without the script only; the script shows the progress
                self.assertIn('<noscript><meta http-equiv="refresh" content="2"></noscript>', html)
                self.assertEqual(html.count('http-equiv="refresh"'), 1)
                self.assertIn('<body class="p-letter" data-refresh="2">', html)
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
                    self.assertNotIn('data-refresh', html)

    def test_on_phones_only_the_back_link_is_hidden_and_check_again_stays(self):
        # Below 1024 px wide or 600 px tall the rule that hides the Back link (the top bar has the back arrow)
        # hid every secondary button of a step, "Check again" of a public instance's last check with it
        phone = ui.APP_CSS.split('@media (max-width:1023px),(max-height:599px){', 1)[1]
        self.assertEqual(re.findall(r'\.steparea \.actions ([^{]*)\{display:none\}', phone), ['a.secondary'])
        actions = screens.check('host', [], running=False).split('<div class="actions">', 1)[1]
        self.assertTrue(actions.startswith('<button class="btn secondary" type="submit" name="go" value="again">'
                                           'Check again</button>'))
        back = screens.limits('host', {}).split('<div class="actions">', 1)[1]
        self.assertTrue(back.startswith('<a class="btn secondary" href="hostchoices">Back</a>'))

    def test_a_saved_key_has_the_box_that_removes_it_under_its_field(self):
        states = samples.every_state()
        html, _ = states['choices-saved-keys']
        names = re.findall(r'name="([A-Z_]+)"', html)
        for key in ('SUBDL_KEY', 'TMDB_KEY'):
            with self.subTest(key=key):
                self.assertEqual(names[names.index(key) + 1], f'REMOVE_{key}')     # right under its field
        self.assertNotIn('name="REMOVE_WYZIE_KEY"', html)                  # none saved: no box
        saved = r'<b id="remove-[a-z_]+-title">Remove the saved key</b>'     # the box's title, its name
        self.assertEqual(len(re.findall(saved, html)), 2)
        html, _ = states['host-choices-saved-keys']
        self.assertEqual(len(re.findall(saved, html)), 3)
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


class StopsAndLines(unittest.TestCase):
    """A stopped install's Try again, the stop for an error setup doesn't know on every step that can meet one, the
    lines of saving the configs over the install's progress, Your choices' own lines, and the container choice's
    rows."""

    def test_a_stopped_install_has_back_and_try_again(self):
        # it had Back and a greyed-out Continue, and nothing on the page ran the install again
        for name in ('install-failed', 'install-unexpected'):
            html, _ = samples.every_state()[name]
            with self.subTest(screen=name):
                self.assertIn('<div class="actions"><a class="btn secondary" href="review">Back</a><button class="btn '
                              'primary" type="submit" name="go" value="again">Try again</button></div>', html)
                self.assertNotIn('disabled', html)
        html = screens.installing('host', [], running=False, stop=('Starting AIOSubtitles failed.', None))
        self.assertIn('<a class="btn secondary" href="hostlimits">Back</a><button class="btn primary" type="submit" '
                      'name="go" value="again">Try again</button>', html)
        self.assertNotIn('Try again', samples.every_state()['install-done'][0])
        self.assertNotIn('Try again', samples.every_state()['install-running'][0])
        self.assertIn('Try again', screens.INSTALLER_WORDS)

    def test_an_unexpected_error_is_a_stop_with_its_way_out(self):
        self.assertEqual(screens.UNEXPECTED, ('Something unexpected went wrong, so setup stopped here.',
                                              'Run ./setup again. If it happens again, send what the terminal shows '
                                              'with a bug report.'))
        for words in screens.UNEXPECTED:
            self.assertIn(words, screens.INSTALLER_WORDS)
        for name in ('install-unexpected', 'limits-stopped', 'host-signups-stopped', 'host-choices-stopped',
                     'host-limits-stopped', 'stopped-unexpectedly'):
            texts = Tree(samples.every_state()[name][0]).texts
            with self.subTest(screen=name):
                self.assertIn(screens.UNEXPECTED[0], texts)
                self.assertEqual(texts[texts.index(screens.UNEXPECTED[0]) + 1], screens.UNEXPECTED[1])

    def test_the_install_shows_what_saving_the_configs_warned_over_its_progress(self):
        texts = Tree(samples.every_state()['install-review-warned'][0]).texts
        locked = texts.index("The setting is fixed by AIOStreams' environment (SUBTITLE_CACHE_TTL); change it there:")
        self.assertEqual(texts[locked + 1], 'SUBTITLE_CACHE_TTL=*:300,aiosubtitles-subsync:-1')
        self.assertLess(locked, texts.index('That account is not an admin; the setting stays as it is.'))
        self.assertLess(texts.index('That account is not an admin; the setting stays as it is.'),
                        texts.index("Created subsync's own config (5e6f7a8b…)"))

    def test_your_choices_shows_its_own_lines_first(self):
        # why Usenet stays off, answered yes before, was dropped: the step's lines were shown on no page
        texts = Tree(samples.every_state()['choices-usenet-why'][0]).texts
        self.assertLess(texts.index(explain.SETUP_USENET_WHY['proxy']),
                        texts.index('Which subtitle languages should be synced?'))
        self.assertNotIn(explain.SETUP_USENET_WHY['proxy'], Tree(samples.every_state()['choices'][0]).texts)

    def test_the_container_choice_shows_each_name_once_with_its_image_under_it(self):
        # each row read "aiostreams" and under it "aiostreams  (ghcr.io/viren070/aiostreams:latest)"
        html, _ = samples.every_state()['aios-several']
        self.assertIn('<b>aiostreams</b></span><span id="AIOS_CONTAINER-aiostreams-help">'
                      'ghcr.io/viren070/aiostreams:latest</span>', html)
        self.assertEqual(Tree(html).texts.count('aiostreams'), 1)
        self.assertNotIn('aiostreams  (', html)

    def test_the_values_of_these_parts_are_escaped(self):
        rows = [('warn', HOSTILE, HOSTILE), ('note', HOSTILE, None)]
        for html in (screens.installing('yourself', [], running=False, warnings=rows, stop=(HOSTILE, HOSTILE)),
                     screens.choices(['eng'], 'no', HOSTILE, rows=rows),
                     screens.aios([], ask='AIOS_CONTAINER', containers=[('a', HOSTILE), ('b', HOSTILE)]),
                     screens.limits('host', {}, stop=(HOSTILE, HOSTILE)),
                     screens.host_signups('x', stop=(HOSTILE, None)),
                     screens.host_choices(stop=(HOSTILE, HOSTILE))):
            self.assertNotIn('<script>alert', html)
            self.assertNotIn('"&gt;&lt;script', html)


class Titles(unittest.TestCase):
    """Each page's own browser title: every page was "Set up AIOSubtitles", so tabs, history and a screen reader's page
    announcement couldn't tell the steps apart."""

    @staticmethod
    def title(html):
        return htmllib.unescape(re.search(r'<title>(.*?)</title>', html).group(1))

    def test_a_step_is_titled_by_its_name(self):
        for route, names, ids in (('yourself', screens.PERSONAL, screens.PERSONAL_IDS),
                                  ('host', screens.HOSTING, screens.HOSTING_IDS)):
            for name, step_id in zip(names, ids):
                with self.subTest(route=route, step=step_id):
                    self.assertEqual(self.title(screens.step(route, step_id, '', '')), f'{name} · Set up AIOSubtitles')
        states = samples.every_state()
        for name, title in (('machine', 'This machine · Set up AIOSubtitles'),
                            ('install-failed', 'Installing subsync · Set up AIOSubtitles'),
                            ('host-limits', 'Limits · Set up AIOSubtitles')):
            with self.subTest(screen=name):
                self.assertEqual(self.title(states[name][0]), title)

    def test_a_progress_or_result_page_is_titled_by_its_heading(self):
        states = samples.every_state()
        for name, title in (('updating', 'Updates · Set up AIOSubtitles'),
                            ('update-failed', 'Updates · Set up AIOSubtitles'),
                            ('removing-failed', 'Removing subsync · Set up AIOSubtitles'),
                            ('removed', 'Removing subsync · Set up AIOSubtitles'),
                            ('removed-host', 'Removing AIOSubtitles · Set up AIOSubtitles'),
                            ('done', 'Done · Set up AIOSubtitles'),
                            ('done-warned', 'Installed, with warnings · Set up AIOSubtitles')):
            with self.subTest(screen=name):
                self.assertEqual(self.title(states[name][0]), title)

    def test_the_page_after_setup_is_titled_by_its_heading(self):
        states = samples.every_state()
        self.assertEqual(self.title(states['after'][0]), 'AIOSubtitles · subsync status')
        self.assertEqual(self.title(states['after-host'][0]), 'AIOSubtitles status')

    def test_the_start_and_ended_pages_keep_setups_name(self):
        states = samples.every_state()
        for name in ('start', 'start-host', 'ended'):
            with self.subTest(screen=name):
                self.assertEqual(self.title(states[name][0]), 'Set up AIOSubtitles')
        # a stop outside a step (web.App.handle): headed by setup's name, which the title doesn't repeat
        self.assertEqual(self.title(screens.working(screens.TITLE, [], False, stop=('x', None))), 'Set up AIOSubtitles')


class Removal(unittest.TestCase):
    """The page after setup's removing: its words by route and by the configs an install has, the box's problem tied to
    it, and the sign-in an AIOStreams may ask for."""

    def test_the_text_names_the_configs_this_install_has(self):
        # one sentence for the page and the terminal; it said "(1 entry), deletes subsync's own config" whatever the
        # install had
        ends = 'and removes the subsync container with its stored copies. Your other subtitle sources stay.'
        for keys, configs in (({'SYNC_UUID': 'x'}, "subsync's own config,"),
                              ({'SYNC_UUID': 'x', 'USENET_UUID': 'y'}, "subsync's own config and its Usenet config,"),
                              ({'USENET_UUID': 'y'}, "subsync's Usenet config,")):
            with self.subTest(keys=keys):
                self.assertEqual(screens.remove_text(keys), f'This takes subsync out of your config, deletes {configs} '
                                                            f'{ends}')
                self.assertIn(screens.remove_text(keys), screens.INSTALLER_WORDS)
                self.assertEqual(screens.remove_text(keys, ' (2 entries)'),
                                 f'This takes subsync out of your config (2 entries), deletes {configs} {ends}')
        html, _ = samples.every_state()['after-usenet']
        self.assertIn(htmllib.escape(screens.remove_text({'SYNC_UUID': 'x', 'USENET_UUID': 'y'})), html)

    def test_a_public_instance_has_its_own_words_and_the_warning_once(self):
        states = samples.every_state()
        tree = Tree(states['after-host'][0])
        self.assertIn('AIOSubtitles status', tree.texts)
        self.assertIn('Removing AIOSubtitles', tree.texts)
        self.assertNotIn('AIOSubtitles · subsync status', tree.texts)
        self.assertNotIn('Removing subsync', tree.texts)
        self.assertEqual(tree.texts.count(screens.REMOVE_HOST_TEXT), 1)
        tree = Tree(states['after'][0])                                   # a personal install's: as they were
        self.assertIn('AIOSubtitles · subsync status', tree.texts)
        self.assertIn('Removing subsync', tree.texts)
        self.assertIn('Removing AIOSubtitles', Tree(states['removed-host'][0]).texts)

    def test_the_unticked_box_says_so_and_is_tied_to_its_problem(self):
        html, _ = samples.every_state()['after-host-not-ticked']
        box = re.search(r'<input type="checkbox" id="confirm"[^>]*>', html).group(0)
        self.assertIn('aria-describedby="confirm-error confirm-help" aria-invalid="true"', box)
        self.assertIn('<p class="field-error" id="confirm-error">Tick &quot;Delete everyone&#x27;s records and stored '
                      'copies&quot; to remove AIOSubtitles.</p>', html)
        self.assertNotIn('aria-invalid', samples.every_state()['after-host'][0])

    def test_the_sign_in_shows_under_why_it_is_asked(self):
        html, _ = samples.every_state()['after-sign-in']
        asks = 'Saving the config: This AIOStreams asks you to sign in before configs can be saved.'
        names = re.findall(r'name="([A-Z_]+)"', html)
        self.assertEqual(names[names.index('AIOS_CONFIG_PASSWORD'):][:3],
                         ['AIOS_CONFIG_PASSWORD', 'AIOS_USERNAME', 'AIOS_PASSWORD'])
        self.assertLess(html.index(htmllib.escape(asks)), html.index('AIOStreams sign-in to save configs: username'))
        self.assertIn('value="admin"', html)
        self.assertNotIn('field-error', html)                             # asked, not marked wrong
        self.assertNotIn('name="AIOS_USERNAME"', samples.every_state()['after'][0])

    def test_the_new_values_are_escaped(self):
        rows = [('warn', HOSTILE, HOSTILE)]
        shown = [screens.after_setup('yourself', rows, [], '1.0.0', None, config=HOSTILE, removing=HOSTILE,
                                     sign_in=True, username=HOSTILE, written=rows),
                 screens.review([], rows=rows, stop=(HOSTILE, HOSTILE), sign_in=True, username=HOSTILE),
                 screens.installing('yourself', rows, running=False, rule={HOSTILE: [HOSTILE]}, rule_intro=HOSTILE,
                                    rule_note=HOSTILE),
                 screens.done('yourself', HOSTILE, [HOSTILE], [], warnings=rows)]
        for html in shown:
            self.assertNotIn('<script>alert', html)
            self.assertNotIn('"&gt;&lt;script', html)


class StepsAndEnd(unittest.TestCase):
    """The sign-in's label by why AIOStreams asks, a stop while saving, the web server rule's note and the end's
    warnings."""

    def test_the_sign_in_label_says_why_it_is_asked(self):
        # it always said "(admin)", which only the cache setting needs
        states = samples.every_state()
        self.assertIn('AIOStreams sign-in to save configs: username', Tree(states['review-sign-in-to-save'][0]).texts)
        self.assertNotIn('AIOStreams sign-in (admin): username', Tree(states['review-sign-in-to-save'][0]).texts)
        self.assertIn('AIOStreams sign-in (admin): username', Tree(states['review-sign-in'][0]).texts)

    def test_a_stop_while_saving_shows_first_with_what_was_written(self):
        html, _ = samples.every_state()['review-stopped']
        texts = Tree(html).texts
        stop = texts.index('Creating the sync config: AIOStreams is limiting requests from this machine (it allows 10 '
                           'new configs an hour); try again later.')
        self.assertLess(texts.index('Signed in'), stop)
        self.assertLess(stop, texts.index('Your config 1a2b3c4d…'))
        self.assertRegex(html, r'<input[^>]*name="CACHE"[^>]*checked')
        self.assertIn('name="AIOS_USERNAME"', html)

    def test_the_rules_note_is_under_the_rule(self):
        html, _ = samples.every_state()['install-rule-note']
        note = htmllib.escape("Web server running in Docker on AIOStreams' network: use aiosubtitles-subsync:8790 "
                              'instead of 127.0.0.1:8790.')
        self.assertGreater(html.index(note), html.rindex('</pre>'))
        self.assertNotIn('Web server running in Docker', samples.every_state()['install-done'][0])

    def test_the_end_shows_the_checks_warnings_under_its_heading(self):
        texts = Tree(samples.every_state()['done-warned'][0]).texts
        heading = texts.index('Installed, with warnings')
        self.assertEqual(texts[heading + 2:heading + 4],          # after the problem mark's spoken word
                         ['AIOStreams did not reach subsync.',
                          'It calls http://aiosubtitles-subsync:8790/…; check that address works from AIOStreams.'])
        self.assertNotIn('<ul class="checks">', samples.every_state()['done'][0])


class LimitProblems(unittest.TestCase):
    """A limit's problem shows under its label, tied to its box or row of buttons as every other question's
    is; a row of buttons' problem stays outside the row and says what to choose."""

    @staticmethod
    def tag(html, pattern):
        return re.search(pattern, html).group(0)

    def test_a_number_limits_box_is_described_by_its_problem_and_marked(self):
        # the problem was pasted in after the label: the box was neither described by it nor marked
        html, _ = samples.every_state()['host-limits-problem']
        box = self.tag(html, r'<input id="SUBSYNC_SYNCS_AT_ONCE"[^>]*>')
        self.assertIn('aria-describedby="SUBSYNC_SYNCS_AT_ONCE-error SUBSYNC_SYNCS_AT_ONCE-help" aria-invalid="true"',
                      box)
        self.assertIn('<label for="SUBSYNC_SYNCS_AT_ONCE">Files synced at a time</label><p class="field-error" '
                      'id="SUBSYNC_SYNCS_AT_ONCE-error">A whole number from 1, or auto.</p><div class="num">', html)
        box = self.tag(html, r'<input id="SUBSYNC_USER_FILES_PER_DAY"[^>]*>')         # the others as they were
        self.assertIn('aria-describedby="SUBSYNC_USER_FILES_PER_DAY-help">', box)
        self.assertNotIn('aria-invalid', box)
        html, _ = samples.every_state()['limits-problem']                             # a personal install's too
        self.assertIn('aria-describedby="SUBSYNC_DAILY_BUDGET_GB-error SUBSYNC_DAILY_BUDGET_GB-help" '
                      'aria-invalid="true"', self.tag(html, r'<input id="SUBSYNC_DAILY_BUDGET_GB"[^>]*>'))

    def test_a_row_of_buttons_problem_is_under_its_label_outside_the_row_and_says_what_to_choose(self):
        # it was put inside the row, after the last button, unread with the group and naming the setting
        for name, setting, words in (('host-limits-languages-problem', 'SUBSYNC_MAX_LANGUAGES', 'Choose 1, 2, 3 or 4.'),
                                     ('limits-audio-problem', 'SUBSYNC_AUDIO_BUDGET_GB', 'Choose 0.5, 1, 2 or 4 GB.')):
            html, _ = samples.every_state()[name]
            with self.subTest(screen=name):
                self.assertIn(f'<span class="label" id="{setting}-label">', html)
                self.assertIn(f'</span><p class="field-error" id="{setting}-error">{words}</p><div class="seg-row">'
                              f'<div class="seg" id="{setting}" role="radiogroup" aria-labelledby="{setting}-label" '
                              f'aria-describedby="{setting}-error {setting}-help" aria-invalid="true">', html)
                row = html.split(f'id="{setting}" role="radiogroup"', 1)[1].split('</div>', 1)[0]
                self.assertNotIn('field-error', row)
                self.assertIn(words, screens.INSTALLER_WORDS)
        # the terminal, where the answer is typed, keeps its words
        self.assertEqual(limits.NOT_A_LANGUAGE_LIMIT, 'SUBSYNC_MAX_LANGUAGES must be a whole number from 1 to 4.')
        self.assertEqual(limits.NOT_AN_AUDIO_LIMIT, 'Type 0.5, 1, 2 or 4 (GB per file).')
        self.assertEqual([limits.choose(lim) for lim in limits.hosting(audio=True) if lim.kind == 'segmented'],
                         ['Choose 1, 2, 3 or 4.', 'Choose 0.5, 1, 2 or 4 GB.'])

    def test_each_limits_box_asks_phones_for_the_keypad_of_its_kind_and_has_no_pattern(self):
        # Every box asked for digits only, so "Data a day in total" (2.5 GB) couldn't be typed on an iPhone,
        # and carried a digits-only pattern that called its own "10,000" invalid
        keypads = {'number': 'numeric', 'count': 'numeric', 'decimal': 'decimal', 'auto': 'text'}
        for route, asked in (('host', limits.hosting(usenet=True, audio=True)),
                             ('yourself', limits.personal(audio=True, usenet=True))):
            html = screens.limits(route, {}, audio=True, usenet=True)
            boxes = [lim for lim in asked if lim.kind != 'segmented']
            self.assertLessEqual({lim.kind for lim in boxes}, set(keypads))
            for lim in boxes:
                with self.subTest(route=route, setting=lim.setting):
                    box = self.tag(html, rf'<input id="{lim.setting}"[^>]*>')
                    self.assertIn(f'inputmode="{keypads[lim.kind]}"', box)
                    self.assertNotIn('pattern=', box)
        daily = self.tag(screens.limits('yourself', {}), r'<input id="SUBSYNC_DAILY_BUDGET_GB"[^>]*>')
        self.assertIn('inputmode="decimal"', daily)
        per_person = self.tag(screens.limits('host', {}), r'<input id="SUBSYNC_USER_DAILY_MB"[^>]*>')
        self.assertIn('value="10,000"', per_person)

    def test_without_a_problem_the_rows_of_buttons_are_as_they_were(self):
        html, _ = samples.every_state()['host-limits-audio']
        for setting in ('SUBSYNC_MAX_LANGUAGES', 'SUBSYNC_AUDIO_BUDGET_GB'):
            self.assertIn(f'<div class="seg" id="{setting}" role="radiogroup" aria-labelledby="{setting}-label" '
                          f'aria-describedby="{setting}-help">', html)
        self.assertNotIn('field-error', html)


class RefusedAnswerSummary(unittest.TestCase):
    """A step that comes back with a refused answer starts with the public pages' summary, focused by the
    script, its message linking to the field; the headings have no unused focus mark."""

    def summary(self, html):
        return re.search(r'<div class="body">(<div class="note problem"[^>]*>.*?</div></div>)', html)

    def test_the_summary_comes_first_with_the_fields_own_words_linking_to_it(self):
        states = samples.every_state()
        for name, field, words in (
                ('config-wrong-password', 'aios_config_password', samples.aios_error(
                    samples.PUBLIC, lambda aios: aios.get_config(samples.CONFIG_ID, 'x'),
                    samples.Offline(401, {'error': {'code': 'USER_INVALID_PASSWORD'}})).message),
                ('choices-problem', 'languages', plan.parse_languages('Klingon')[1]),
                ('limits-problem', 'SUBSYNC_DAILY_BUDGET_GB', limits.check(limits.DAILY, '0')[1]),
                ('host-limits-languages-problem', 'SUBSYNC_MAX_LANGUAGES', 'Choose 1, 2, 3 or 4.'),
                ('host-address-problem', 'host_address', screens.hosting.ADDRESS_REFUSED)):
            html, _ = states[name]
            with self.subTest(screen=name):
                found = self.summary(html)
                self.assertTrue(found, f'{name}: no summary first in the body')
                self.assertEqual(found.group(1)[:found.group(1).index('<svg')],
                                 '<div class="note problem" role="alert" id="problem" tabindex="-1">')
                self.assertIn(f"<b>That didn&#x27;t go through</b><p id=\"problem-message\"><a href=\"#{field}\">"
                              f'{htmllib.escape(words)}</a></p>', found.group(1))
                self.assertIn(f' id="{field}"', html)                               # the link's field is there
                self.assertIn(f'>{htmllib.escape(words)}</p>', html.split('id="problem-message"', 1)[1])  # and under it
        self.assertEqual(screens.REFUSED, "That didn't go through")
        self.assertIn(screens.REFUSED, pages.UI_WORDS)                             # the settings page's title

    def test_no_summary_without_a_refused_answer(self):
        for name in ('config', 'choices', 'limits', 'host-limits', 'host-address', 'after-sign-in'):
            with self.subTest(screen=name):
                self.assertNotIn('id="problem"', samples.every_state()[name][0])

    def test_no_heading_has_a_focus_mark_and_the_script_focuses_the_summary(self):
        for name, (html, _) in samples.every_state().items():
            with self.subTest(screen=name):
                self.assertNotRegex(html, r'<h1 [^>]*tabindex')
        self.assertIn('var problem = document.getElementById("problem");\n  if (problem && !(layer && '
                      'layer.contains(problem))) problem.focus();', ui.APP_JS)


class ProgressNotes(unittest.TestCase):
    """The notes under a finished line show under it in the progress list, as the check rows show theirs."""

    def test_an_updates_notes_show_under_its_line(self):
        html, _ = samples.every_state()['updated']
        note = (update.KEPT.format(version='1.1.0') + ' ' + update.OLD_IMAGES.format(image='3320f59e4ec8'))
        progress = html.split('<ol class="progress">', 1)[1].split('</ol>', 1)[0]
        self.assertIn(f'<span class="tx">{htmllib.escape(update.SWITCHED.format(version="1.2.0"))}'
                      f'<span class="more">{htmllib.escape(note)}</span></span>', progress)
        self.assertRegex(ui.APP_CSS, r'(?m)^\.checks \.more,\.progress \.more\{display:block;margin-top:6px;')

    def test_a_note_written_as_a_job_starts_is_its_own_line(self):
        # Rows.note once joined the row before it, the last step's, which the job's page doesn't show
        out = engine.Rows()
        out.ok('Saved the configs')
        out.begin()
        out.note('Your web server needs one rule.')
        out.working('Starting subsync')
        self.assertIn('<p class="help">Your web server needs one rule.</p>',
                      screens.working('Updates', [tuple(r) for r in out.rows[1:]], running=True))


if __name__ == '__main__':
    unittest.main()
