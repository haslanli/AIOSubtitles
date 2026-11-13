"""The setup page's screens: approved words only, the parts' structure, every value
escaped, no inline style, no way forward while installing or on a stop, and the secret never on a page."""
import re
import unittest
from html.parser import HTMLParser

from installer import limits, screens
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
    texts |= {option for lim in limits.personal(audio=True) + limits.hosting() for option in lim.options}
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
                      'This setup page has ended. Run ./setup again for a new address.'):
            self.assertIn(words, screens.INSTALLER_WORDS)


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
            labelled = {attrs.get('for') for tag, attrs in tree.tags if tag == 'label'}
            for tag, attrs in tree.tags:
                if tag in ('input', 'select') and attrs.get('type') not in ('hidden', 'radio', 'checkbox'):
                    with self.subTest(screen=name, field=attrs.get('id')):
                        self.assertIn(attrs.get('id'), labelled)
                        for ref in (attrs.get('aria-describedby') or '').split():
                            self.assertIn(ref, ids)

    def test_every_question_shows_its_explanation(self):
        # test_flow's typed "?" (it showed the explanation again): on the page, each field has it under it
        html, _ = samples.every_state()['config']
        self.assertIn(htmllib.escape(explain.QUESTIONS['AIOS_CONFIG_ID'][1]), html)
        self.assertIn('aria-describedby="aios_config_password-help"', html)

    def test_a_wrong_password_keeps_the_id_and_never_shows_a_password(self):
        # test_flow's "an empty password goes back to the ID", as the page does it
        html, _ = samples.every_state()['config-wrong-password']
        self.assertIn('value="1a2b3c4d-1234-4abc-8def-0123456789ab"', html)
        password = re.search(r'<input[^>]*name="AIOS_CONFIG_PASSWORD"[^>]*>', html).group(0)
        self.assertNotIn('value=', password)
        self.assertIn('aria-invalid="true"', password)


class Rules(unittest.TestCase):
    def test_no_way_forward_while_installing_or_on_the_real_debrid_stop(self):
        states = samples.every_state()
        for name in ('install-running', 'check-running', 'updating'):
            html, _ = states[name]
            with self.subTest(screen=name):
                self.assertIn('<meta http-equiv="refresh" content="2">', html)
                self.assertNotIn('aria-label="Back"', html)
                self.assertNotIn('>Back<', html)
        for name in ('install-running', 'config-rd-only', 'machine-stop'):
            html, _ = states[name]
            with self.subTest(screen=name):
                self.assertRegex(html, r'<button class="btn primary" type="button" disabled>Continue')
        html, _ = states['config-rd-only']
        self.assertIn('>Back<', html)
        for name, (html, _) in states.items():
            if name not in ('install-running', 'check-running', 'updating'):
                with self.subTest(screen=name):
                    self.assertNotIn('http-equiv="refresh"', html)

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
        rows = [('ok', HOSTILE, HOSTILE), ('bad', HOSTILE, None)]
        for html in (screens.machine('yourself', rows), screens.config(rows, HOSTILE, problem=('AIOS_CONFIG_ID', HOSTILE)),
                     screens.host_address(HOSTILE, HOSTILE), screens.host_signups(HOSTILE),
                     screens.choices(['eng'], False, HOSTILE, problem=('LANGUAGES', HOSTILE)),
                     screens.review([(HOSTILE, [('+', HOSTILE)], HOSTILE)]),
                     screens.done('yourself', HOSTILE, [HOSTILE, HOSTILE], [('code', HOSTILE), ('p', HOSTILE)]),
                     screens.after_setup('yourself', rows, [(HOSTILE, HOSTILE)], HOSTILE, None, config=HOSTILE),
                     screens.removed(HOSTILE, rows, [HOSTILE])):
            self.assertNotIn('<script>alert', html)
            self.assertNotIn('<b>x</b>', html)

    def test_the_steps_are_the_approved_ones(self):
        self.assertEqual(screens.PERSONAL, ('This machine', 'Your AIOStreams', 'The config your apps use',
                                            'Your choices', 'Limits', 'Your AIOStreams configs', 'Installing subsync',
                                            'Checking that it works'))
        self.assertEqual(screens.HOSTING, ('This machine', 'Its web address', 'Sign-ups', 'Your choices', 'Limits',
                                           'Installing AIOSubtitles', 'Checking that it works'))


if __name__ == '__main__':
    unittest.main()
