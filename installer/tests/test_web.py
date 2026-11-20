"""The setup page: 127.0.0.1 only, a one-time key, the Host check, the site's
security header, each piece of work once, the idle stop, and both routes and the page after setup driven end to end
through the real engine (a stand-in AIOStreams, Docker faked)."""
import ast
import contextlib
import html
import io
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from installer import engine, limits, screens, tests, update, web
from installer.tests import samples
from installer.tests.test_flow import PASSWORD, PUBLIC, UUID, FlowFixture
from installer.tests.test_hosting import ADDRESS, RouteFixture
from installer.tests.test_screens import terminal_only_shown
from installer.tests.test_usenet import ENGINE, NEWZNAB, SUBS, TORBOX, ZILEAN
from subsync import explain, ui

REPO = Path(__file__).resolve().parent.parent.parent


def site_headers():
    """subsync's public pages' security header: public.py's PAGE_HEADERS, which it takes from subsync/ui.py (its
    source is read: public.py needs cryptography to import)."""
    tree = ast.parse((REPO / 'subsync' / 'subsync' / 'public.py').read_text())
    imported = any(isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == 'ui'
                   and any(a.name == 'PAGE_HEADERS' and a.asname is None for a in node.names) for node in tree.body)
    assigned = any(isinstance(node, ast.Name) and node.id == 'PAGE_HEADERS' and isinstance(node.ctx, ast.Store)
                   for node in ast.walk(tree))
    if not imported or assigned:
        raise AssertionError("public.py's PAGE_HEADERS is not subsync/ui.py's")
    return ui.PAGE_HEADERS


class Browser:
    """Requests straight to the app, as a browser on this machine sends them."""

    def __init__(self, app):
        self.app, self.pages = app, []

    def request(self, method, path, form=None, host=None):
        body = urllib.parse.urlencode(form or {}, doseq=True).encode() if form is not None else b''
        headers = {'Host': host or f'127.0.0.1:{self.app.port}'}
        if form is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        status, out_headers, page = self.app.handle(method, path, headers, body)
        text = page.decode('utf-8', 'replace')
        self.pages.append(text)
        return status, out_headers, html.unescape(text)

    def get(self, step, **kw):
        return self.request('GET', f'/{self.app.key}/{step}', **kw)

    def post(self, step, form, **kw):
        return self.request('POST', f'/{self.app.key}/{step}', form=form, **kw)

    def follow(self, step, form):
        """A form sent, then the page it leads to, as a browser follows a 303."""
        status, headers, text = self.post(step, form)
        if status == 303:
            return self.get(headers['Location'].rsplit('/', 1)[-1])
        return status, headers, text

    def settle(self, step, seconds=30):
        """The page of a step whose work runs in the background, once the work is done."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status, headers, text = self.get(step)
            if 'http-equiv="refresh"' not in text:
                return status, headers, text
            time.sleep(0.05)
        raise AssertionError(f'{step} still running')


def started(test, app):
    """The app's server on a real socket on 127.0.0.1, stopped when the test ends."""
    server = web.start(app)

    def stop():
        app.finish_in(0)
        app.finished.wait(5)
        server.server_close()
    test.addCleanup(stop)
    return server


class BackgroundWork:
    """For the page tests on a fixture: the background work (installing, checking, updating, removing) of every Session
    the test makes is done before the fixture's fakes go. Left running, it would meet the real Docker and state; a job
    still running JOB_WAIT seconds after the test fails it."""

    JOB_WAIT = 30

    def setUp(self):
        super().setUp()
        sessions = []

        class Recorded(web.Session):
            def __init__(self, *args):
                super().__init__(*args)
                sessions.append(self)
        p = mock.patch.object(web, 'Session', Recorded)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.await_jobs, sessions)          # cleanups run last first: this one before the fakes go

    def await_jobs(self, sessions):
        deadline = time.monotonic() + self.JOB_WAIT
        while any(s.running(step) for s in sessions for step in list(s.jobs)):
            if time.monotonic() > deadline:
                self.fail(f'background work still running {self.JOB_WAIT} s after the test')
            time.sleep(0.05)


class ProgressPages(unittest.TestCase):
    """A page whose work runs loaded itself again every 2 seconds, sending a screen reader back to the top
    each time. Now it does only without the script; with it, the script fetches the page's own address (which only the
    setup page's header allows) and puts in the parts that change, reading out the lines that are new."""

    def test_the_parts_the_script_puts_in_are_on_every_progress_page(self):
        states = samples.every_state()
        self.assertIn('["main .fbar", "main .body", "main .actions"]', ui.APP_JS)
        steps = ('fbar', 'body', 'actions')
        for name, parts in (('install-running', steps), ('check-running', steps), ('updating', ('body',))):
            main = states[name][0].split('<main', 1)[1]
            with self.subTest(screen=name):
                self.assertIn('data-refresh="2"', states[name][0])
                for part in parts:
                    self.assertEqual(main.count(f'<div class="{part}">'), 1, part)
        done = states['updated'][0].split('<main', 1)[1]                  # the actions come once the work has ended
        self.assertIn('<div class="body">', done)
        self.assertIn('<div class="actions">', done)

    def test_the_script_fetches_only_the_pages_own_address_and_reads_out_the_new_lines(self):
        js = ui.APP_JS.split(' work as it goes\n', 1)[1]
        self.assertIn('var every = +body.getAttribute("data-refresh") * 1000;', js)
        self.assertIn('fetch(location.href).then(', js)
        self.assertIn('return answer.ok && !answer.redirected ? answer.text() : Promise.reject();', js)
        self.assertIn('if (fresh.body.hasAttribute("data-refresh")) setTimeout(poll, every);', js)   # until it ends
        self.assertIn('}).catch(function () { location.reload(); });', js)       # as without the script
        self.assertIn('} else if (every) {\n    setTimeout(function () { location.reload(); }, every);', js)
        self.assertIn('said.setAttribute("role", "status");', js)               # one polite region, the new lines only
        self.assertIn('return known.indexOf(line) < 0;', js)
        self.assertIn('p.textContent = line;', js)


class Security(unittest.TestCase):
    def setUp(self):
        self.app = web.App(port=8801)
        self.b = Browser(self.app)

    def test_wrong_key_or_host_is_refused(self):
        status, _, text = self.b.request('GET', '/not-the-key/')
        self.assertEqual(status, 404)
        self.assertIn('This setup page has ended. Run ./setup again for a new address.', text)
        for host in ('evil.example', f'evil.example:{self.app.port}', '127.0.0.1:9', f'192.168.1.5:{self.app.port}'):
            with self.subTest(host=host):
                status, _, text = self.b.get('', host=host)
                self.assertEqual(status, 404)
                self.assertNotIn('Set up AIOSubtitles', text)
        for host in (f'127.0.0.1:{self.app.port}', f'localhost:{self.app.port}'):
            self.assertEqual(self.b.get('', host=host)[0], 200)

    def test_every_page_has_the_sites_security_header(self):
        # with one source more: a page whose work runs fetches its own address to show its progress, which
        # the site's pages never do
        wanted = dict(site_headers())
        self.assertNotIn('connect-src', wanted['Content-Security-Policy'])
        wanted['Content-Security-Policy'] += "; connect-src 'self'"
        # and kept equal to the site's pages: a proxy in front passes them through unchanged (R3-10)
        self.assertEqual(wanted['Cache-Control'], 'no-store, no-transform')
        for path in (f'/{self.app.key}/', '/nope', f'/{self.app.key}/nothing-here'):
            _, headers, _ = self.b.request('GET', path)
            for name, value in wanted.items():
                self.assertEqual(headers.get(name), value, (path, name))

    def test_the_stylesheet_script_and_fonts_are_served_from_here(self):
        status, headers, text = self.b.request('GET', '/static/app.css?v=1')
        self.assertEqual((status, headers['Content-Type']), (200, 'text/css; charset=utf-8'))
        self.assertEqual(self.b.request('GET', '/static/app.js')[0], 200)
        self.assertEqual(self.b.request('GET', '/static/fonts/nope.ttf')[0], 404)
        self.assertEqual(self.b.request('GET', '/static/../web.py')[0], 404)

    def test_the_images_typefaces_and_their_licence_are_served(self):
        with tempfile.TemporaryDirectory() as tmp:
            fonts = Path(tmp) / 'fonts'
            fonts.mkdir()
            (fonts / 'Face-wght.ttf').write_bytes(b'\x00\x01face')
            (fonts / 'OFL.txt').write_bytes(b'licence')
            (Path(tmp) / 'secret.txt').write_bytes(b'not a font')
            with mock.patch.object(web.ui, 'STATIC', tmp):
                status, headers, _ = self.b.request('GET', '/static/fonts/Face-wght.ttf')
                self.assertEqual((status, headers['Content-Type']), (200, 'font/ttf'))
                self.assertEqual(self.app.handle('GET', '/static/fonts/Face-wght.ttf',
                                                 {'Host': f'127.0.0.1:{self.app.port}'}, b'')[2], b'\x00\x01face')
                status, headers, text = self.b.request('GET', '/static/fonts/OFL.txt')
                self.assertEqual((status, headers['Content-Type'], text), (200, 'text/plain; charset=utf-8', 'licence'))
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                for path in ('/static/fonts/../secret.txt', '/static/fonts/secret.txt', '/static/fonts/Face-wght.otf'):
                    with self.subTest(path=path):
                        self.assertEqual(self.b.request('GET', path)[0], 404)

    def test_the_hashed_stylesheet_and_script_and_the_typefaces_are_cached(self):
        # sent with no-store once, so the install, check and update pages, which load themselves again every 2 seconds,
        # fetched them all each time, and the typeface was read from disk again for each
        for path in ('/static/app.css', '/static/app.js'):
            with self.subTest(path=path):
                self.assertEqual(self.b.request('GET', path)[1]['Cache-Control'], 'public, max-age=3600')
        host = {'Host': f'127.0.0.1:{self.app.port}'}
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'fonts').mkdir()
            font = Path(tmp) / 'fonts' / 'Face-wght.ttf'
            font.write_bytes(b'face')
            with mock.patch.object(web.ui, 'STATIC', tmp):
                first = self.app.handle('GET', '/static/fonts/Face-wght.ttf', host, b'')
                font.write_bytes(b'read again')
                again = self.app.handle('GET', '/static/fonts/Face-wght.ttf', host, b'')
        self.assertEqual(first[1]['Cache-Control'], 'public, max-age=31536000, immutable')
        self.assertEqual((first[2], again[2]), (b'face', b'face'))                     # read once

    def test_only_a_request_with_the_key_keeps_the_page_up(self):
        # any request at all, even with no key and another Host, once kept the page (and its root process holding
        # Docker's socket) up past its idle time
        app = web.App(port=8807, idle_s=0.5)
        b = Browser(app)
        time.sleep(0.6)
        self.assertTrue(app.should_stop())
        for method, path, host in (('GET', '/', 'evil.example'), ('GET', f'/{app.key}/', 'evil.example:8807'),
                                   ('GET', '/not-the-key/', None), ('POST', '/not-the-key/', None),
                                   ('GET', '/static/app.css', None)):
            with self.subTest(method=method, path=path, host=host):
                b.request(method, path, host=host)
                self.assertTrue(app.should_stop())
        b.get('')
        self.assertFalse(app.should_stop())

    def test_a_body_over_16_kb_is_refused(self):
        status, _, _ = self.b.request('POST', f'/{self.app.key}/', form={'ROUTE': 'x' * 17000})
        self.assertEqual(status, 413)

    def test_terminal_lines_name_the_tunnel(self):
        lines = web.terminal_lines(8801, 'http://127.0.0.1:8801/abc/')
        self.assertEqual(lines[0], 'Setup runs in your browser. Open this address on this machine:')
        self.assertIn('  http://127.0.0.1:8801/abc/', lines)
        self.assertIn('On another computer, run this there first, then open the same address:', lines)
        self.assertIn('  ssh -L 8801:127.0.0.1:8801 you@this-server   (your usual ssh login to this machine)', lines)
        self.assertEqual(lines[-1], 'The page stops when you finish, after 60 minutes without use, or with Ctrl-C.')

    def test_the_server_listens_on_this_machine_only_and_stops_when_idle(self):
        # idle only after the request: a 0.3 s idle time from the start once stopped the server first on a slow machine
        app = web.App(idle_s=60)
        server = started(self, app)
        self.assertEqual(server.server_address[0], '127.0.0.1')
        with urllib.request.urlopen(f'http://127.0.0.1:{app.port}/{app.key}/', timeout=5) as r:
            self.assertEqual(r.status, 200)
        app.idle_s = 0.3
        self.assertTrue(app.finished.wait(5))


class Personal(BackgroundWork, FlowFixture):
    """Install it for yourself, through the page."""

    def setUp(self):
        super().setUp()
        self.app = web.App(port=8802)
        self.b = Browser(self.app)

    def walk_to_review(self, **choices):
        b = self.b
        self.assertEqual(b.get('')[0], 200)
        status, headers, _ = b.post('', {'ROUTE': 'yourself'})
        self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/machine'))
        _, _, text = b.get('machine')
        self.assertIn('Docker and Docker Compose work', text)
        _, _, text = b.follow('machine', {'go': 'next'})
        self.assertIn('Viewers reach it at', text)
        _, _, text = b.follow('aios', {'go': 'next'})
        self.assertIn('Config ID (or paste your install link)', text)
        _, _, text = b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'AIOS_CONFIG_PASSWORD': 'wrong', 'go': 'next'})
        self.assertIn("don't match", text)
        _, _, text = b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        self.assertIn('Config found', text)
        _, _, text = b.follow('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': '', 'go': 'next'})
        self.assertIn('Which subtitle languages should be synced?', text)
        self.assertIn('Sync files that have no subtitles of their own, from their audio?', text)
        _, _, text = b.follow('choices', {'LANGUAGES': ['eng', 'rus', ''], 'PUBLIC_ADDRESS': f'{PUBLIC}/subsync',
                                          'ADD_OPENSUBTITLES': 'yes', 'go': 'next', **choices})
        self.assertIn('Data a day in total', text)
        _, _, text = b.follow('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', 'go': 'next'})
        self.assertIn('Save these changes', text)
        return text

    def test_a_whole_personal_setup_through_the_page(self):
        self.walk_to_review()
        status, headers, _ = self.b.post('review', {'CONFIRM': 'yes'})
        self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/install'))
        _, _, text = self.b.settle('install')
        self.assertIn('subsync is running (it restarts by itself after a reboot)', text)
        self.assertIn('Your web server needs one rule', text)
        _, _, text = self.b.follow('install', {'go': 'next'})
        _, _, text = self.b.settle('check')
        self.assertIn('AIOStreams asks subsync for subtitles', text)
        self.assertIn('Do the test read now?', text)
        _, _, text = self.b.follow('check', {'go': 'next'})
        self.assertIn('Run ./setup again to see its status, update it, change your answers or remove it.', text)
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual(env['SUBSYNC_LANGUAGES'], 'eng,rus')
        self.assertTrue(self.app.finishing)
        self.assertFalse(any(PASSWORD in page for page in self.b.pages))                # never sent back
        _, _, text = self.b.get('')                                                    # the page after setup now
        self.assertIn('AIOSubtitles · subsync status', text)
        self.assertIn(update.NEWEST, text)

    def test_the_audio_answer_through_the_page(self):
        # One of three answers; a yes asks the audio limit next, and "as soon as a file is opened" is written
        text = self.walk_to_review(AUDIO='open')
        _, _, text = self.b.get('limits')
        self.assertIn('Audio per file', text)
        _, _, text = self.b.get('choices')
        self.assertIn('name="AUDIO" id="AUDIO-open" value="open" checked', text)
        self.b.follow('choices', {'LANGUAGES': ['eng', 'rus', ''], 'PUBLIC_ADDRESS': f'{PUBLIC}/subsync',
                                  'ADD_OPENSUBTITLES': 'yes', 'AUDIO': 'open', 'go': 'next'})
        self.b.follow('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', 'go': 'next'})
        status, headers, _ = self.b.post('review', {'CONFIRM': 'yes'})
        self.assertEqual(status, 303)
        self.b.settle('install')
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual((env['SUBSYNC_AUDIO_ROUTE'], env['SUBSYNC_AUDIO_WHEN']), ('on', 'open'))

    def test_a_check_that_stops_still_leads_to_the_end(self):
        # a check that couldn't run once left no way to the done screen
        self.walk_to_review()
        self.b.post('review', {'CONFIRM': 'yes'})
        self.b.settle('install')
        with mock.patch.object(engine.Engine, 'self_test_checks', side_effect=engine.AiosError('AIOStreams timed out.')):
            self.b.follow('install', {'go': 'next'})
            _, _, text = self.b.settle('check')
        self.assertIn('AIOStreams timed out.', text)
        status, headers, _ = self.b.post('check', {'go': 'next'})
        self.assertEqual((status, headers.get('Location')), (303, f'/{self.app.key}/done'))

    def check_that_stops(self, *patches, test_read=False):
        """The done page after a check (or with `test_read`, the test read) that stops on an error setup doesn't know;
        the error's details go to the terminal, as on every such stop."""
        self.walk_to_review()
        self.b.post('review', {'CONFIRM': 'yes'})
        self.b.settle('install')
        terminal = io.StringIO()
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stderr(terminal))
            self.b.follow('install', {'go': 'next'})
            _, _, text = self.b.settle('check')
            if test_read:
                self.b.post('check', {'TEST_READ': 'yes'})
                _, _, text = self.b.settle('check')
        self.assertIn(screens.UNEXPECTED[0], text)
        self.assertIn('Traceback', terminal.getvalue())
        _, _, text = self.b.follow('check', {'go': 'next'})
        return text

    def test_a_check_stopped_by_an_error_is_never_done(self):
        # The check had set "good" before AIOStreams' listing came back as a JSON array (a list has no .get):
        # the check page showed the stop, and the done page then said "Done", with no warnings
        real = engine.Aios.addon_get

        def listing_as_an_array(aios, base, path, *args, **kwargs):
            return [] if path.startswith('/stream/') else real(aios, base, path, *args, **kwargs)
        text = self.check_that_stops(mock.patch.object(engine.Aios, 'addon_get', listing_as_an_array))
        self.assertIn('<h1>Installed, with warnings</h1>', text)
        self.assertNotIn('<h1>Done</h1>', text)

    def test_a_test_read_stopped_by_an_error_is_never_done(self):
        # A crashed test read left read_good unset, read as a pass
        self.selftest = dict(self.selftest, container=4)           # an answer test_read can't word (no .upper())
        text = self.check_that_stops(test_read=True)
        self.assertIn('<h1>Installed, with warnings</h1>', text)
        self.assertNotIn('<h1>Done</h1>', text)

    def test_a_test_read_from_before_an_answer_changed_goes_with_its_check(self):
        # Going back and saving again ran the check again but kept the earlier test read's rows and result:
        # the new check's page still said the read was refused, and the done page said "Installed, with warnings"
        refused = 'The test read was refused by the debrid service'
        self.walk_to_review()
        self.b.post('review', {'CONFIRM': 'yes'})
        self.b.settle('install')
        self.b.follow('install', {'go': 'next'})
        self.b.settle('check')
        good, self.selftest = self.selftest, {'ok': False, 'error': 'refused'}
        self.b.post('check', {'TEST_READ': 'yes'})
        _, _, text = self.b.settle('check')
        self.assertIn(refused, text)
        self.selftest = good
        self.b.get('choices')                                                       # the browser's Back
        self.b.follow('choices', {'LANGUAGES': ['eng', 'spa', ''], 'PUBLIC_ADDRESS': f'{PUBLIC}/subsync',
                                  'ADD_OPENSUBTITLES': 'yes', 'go': 'next'})
        self.b.follow('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', 'go': 'next'})
        self.b.post('review', {'CONFIRM': 'yes'})
        self.b.settle('install')
        self.b.follow('install', {'go': 'next'})
        _, _, text = self.b.settle('check')
        self.assertIn('AIOStreams asks subsync for subtitles', text)
        self.assertNotIn(refused, text)
        self.assertIn('Do the test read now?', text)                                # offered again
        _, _, text = self.b.follow('check', {'go': 'next'})
        self.assertNotIn('Installed, with warnings', text)
        self.assertNotIn(refused, text)

    def test_a_failed_update_can_be_tried_again(self):
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        app = web.App(port=8806)
        b = Browser(app)
        tries = []

        def first_fails(engine_self):
            tries.append(1)
            if len(tries) == 1:
                raise engine.Stop(update.NEWEST)
        with mock.patch.object(engine.Engine, 'update', first_fails):
            b.post('update', {})
            b.settle('updating')
            b.post('update', {})
            b.settle('updating')
        self.assertEqual(len(tries), 2)

    def test_a_repeated_post_does_the_work_once(self):
        self.walk_to_review()
        self.b.post('review', {'CONFIRM': 'yes'})
        self.b.post('review', {'CONFIRM': 'yes'})                                      # a double click, or Back
        self.b.settle('install')
        self.b.post('review', {'CONFIRM': 'yes'})
        created = [p for m, p, _ in self.aios.log if m == 'POST' and p == '/api/v1/user']
        self.assertEqual(len(created), 1)
        self.assertEqual(self.started_count, 1)
        _, _, text = self.b.get('install')                                             # a reload shows the progress
        self.assertIn('subsync is running', text)

    def test_a_double_click_on_save_saves_once(self):
        # two Saves at once once both ran: AIOStreams got two copies of the config (with its services), and one of them
        # was left there, unknown to setup
        self.walk_to_review()
        create = engine.Aios.create_config

        def slow(aios, *args):
            time.sleep(0.3)
            return create(aios, *args)
        answers = []
        with mock.patch.object(engine.Aios, 'create_config', slow):
            posts = [threading.Thread(target=lambda: answers.append(self.b.post('review', {'CONFIRM': 'yes'})[:2]))
                     for _ in range(2)]
            for post in posts:
                post.start()
            for post in posts:
                post.join(30)
        self.assertEqual([(status, headers['Location']) for status, headers in answers],
                         [(303, f'/{self.app.key}/install')] * 2)
        self.b.settle('install')
        created = [p for m, p, _ in self.aios.log if m == 'POST' and p == '/api/v1/user']
        self.assertEqual(len(created), 1)

    def test_another_configs_id_with_a_wrong_password_leads_nowhere(self):
        # its ID was taken while its password was refused, and the step stayed done: Continue then went on, and the save
        # wrote this config's content into the other one, whose backup held this one's
        other = '2b3c4d5e-1234-4abc-8def-0123456789ab'
        self.aios.users[other] = {'password': PASSWORD, 'config': {
            'services': [], 'presets': [{'type': 'zilean', 'instanceId': 'B-ONLY', 'enabled': True,
                                         'options': {'name': 'Zilean'}}]}}
        self.walk_to_review()
        _, _, text = self.b.post('config', {'AIOS_CONFIG_ID': other, 'AIOS_CONFIG_PASSWORD': 'wrong', 'go': 'next'})
        self.assertIn("don't match", text)
        self.assertEqual(self.app.session.e.uuid, UUID)                 # the config read before stays whole
        status, _, text = self.b.post('config', {'AIOS_CONFIG_ID': other, 'AIOS_CONFIG_PASSWORD': '', 'go': 'next'})
        self.assertEqual(status, 200)                                   # no way on: its password is asked again
        self.assertIn('"Its password" needs an answer.', text)
        for step in ('choices', 'review', 'install'):
            with self.subTest(step=step):
                status, headers, _ = self.b.get(step)
                self.assertEqual((status, headers.get('Location')), (303, f'/{self.app.key}/config'))
        self.assertFalse(any(m == 'PUT' and who == other for m, who, _ in self.aios.log))
        self.assertEqual(self.aios.users[other]['config']['presets'][0]['instanceId'], 'B-ONLY')

    def test_another_configs_id_without_its_password_is_asked_for_not_dropped(self):
        # Once the config was read, Continue with another config's ID and no password went on with the config
        # read before, saying nothing: the new ID was never kept or checked
        other = '2b3c4d5e-1234-4abc-8def-0123456789ab'
        self.aios.users[other] = {'password': PASSWORD, 'config': {'services': [TORBOX], 'presets': [ZILEAN, SUBS]}}
        self.walk_to_config()
        _, _, text = self.b.post('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        self.assertIn('Config found', text)
        status, _, text = self.b.post('config', {'AIOS_CONFIG_ID': other, 'AIOS_CONFIG_PASSWORD': '', 'go': 'next'})
        self.assertEqual(status, 200)
        self.assertIn('"Its password" needs an answer.', text)
        self.assertRegex(self.b.pages[-1], rf'<input[^>]*value="{other}"[^>]*name="AIOS_CONFIG_ID"'
                                           rf'|<input[^>]*name="AIOS_CONFIG_ID"[^>]*value="{other}"')   # as sent
        self.assertEqual(self.app.session.e.uuid, UUID)
        # its install link sent with no password: its ID in the box, never the link's encrypted part
        self.b.post('config', {'AIOS_CONFIG_ID': f'{PUBLIC}/stremio/{other}/OTHER-ENC/manifest.json', 'go': 'next'})
        self.assertIn(f'value="{other}"', self.b.pages[-1])
        self.assertNotIn('OTHER-ENC', self.b.pages[-1])
        # the same config's ID, or its link: on without the password, the problem gone
        status, headers, _ = self.b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'AIOS_CONFIG_PASSWORD': '',
                                                    'go': 'next'})
        self.assertEqual((status, headers.get('Location')), (303, f'/{self.app.key}/choices'))
        self.assertNotIn('needs an answer', self.b.get('config')[2])
        _, _, text = self.b.post('config', {'AIOS_CONFIG_ID': other, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        self.assertIn('Config found', text)
        self.assertEqual(self.app.session.e.uuid, other)

    def test_the_missing_password_message_names_the_fields_label(self):
        # The message was typed out by hand beside the label it quotes
        self.walk_to_config()
        with mock.patch.dict(screens.LABELS, {'AIOS_CONFIG_PASSWORD': 'Its config password'}):
            _, _, text = self.b.post('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': '', 'go': 'next'})
        self.assertIn('"Its config password" needs an answer.', text)

    def test_a_later_step_before_its_turn_goes_back_to_the_first_one_left(self):
        self.b.post('', {'ROUTE': 'yourself'})
        status, headers, _ = self.b.get('review')
        self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/machine'))

    def walk_to_config(self):
        self.b.post('', {'ROUTE': 'yourself'})
        self.b.get('machine')
        self.b.follow('machine', {'go': 'next'})
        self.b.follow('aios', {'go': 'next'})

    def test_your_choices_state_this_installs_own_numbers(self):
        # The audio question's files opened a day and Usenet's searches a day, from state/subsync.env
        self.aios.users[UUID]['config'] = {'services': [TORBOX, ENGINE], 'presets': [ZILEAN, NEWZNAB, SUBS],
                                           'failover': {'enabled': True}}
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'subsync.env').write_text('SUBSYNC_USER_OPENED_PER_DAY=3\nSUBSYNC_USENET_LISTINGS_PER_DAY=7\n')
        self.walk_to_config()
        self.b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        _, _, text = self.b.follow('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': '', 'go': 'next'})
        self.assertIn('Sync files you play from Usenet too?', text)
        self.assertIn('next are read too (at most 3 a day).', text)
        self.assertIn('The cost: at most 7 extra searches a day through your indexers', text)
        self.assertNotIn('(at most 5 a day)', text)
        self.assertNotIn('at most 10 extra searches', text)

    def test_the_usenet_limit_box_emptied_after_a_refused_answer_takes_the_current_value(self):
        # The emptied box was skipped, so the refused answer stayed and was refused again
        self.aios.users[UUID]['config'] = {'services': [TORBOX, ENGINE], 'presets': [ZILEAN, NEWZNAB, SUBS],
                                           'failover': {'enabled': True}}
        self.walk_to_config()
        self.b.post('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        self.b.follow('config', {'AIOS_CONFIG_ID': UUID, 'go': 'next'})
        _, _, text = self.b.follow('choices', {'LANGUAGES': ['eng'], 'PUBLIC_ADDRESS': f'{PUBLIC}/subsync',
                                               'USENET': 'yes', 'go': 'next'})
        self.assertIn(limits.USENET.label, text)
        status, _, text = self.b.post('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', limits.USENET.setting: 'abc',
                                                 'go': 'next'})
        self.assertEqual(status, 200)
        self.assertIn('A whole number from 1', text)
        status, headers, _ = self.b.post('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', limits.USENET.setting: '',
                                                    'go': 'next'})
        self.assertEqual((status, headers.get('Location')), (303, f'/{self.app.key}/review'))
        self.assertEqual(self.app.session.e.limit_values[limits.USENET.setting], limits.current(limits.USENET, {}))

    def test_after_the_real_debrid_stop_continue_reads_the_config_again(self):
        # the stop's way out: once the config has another debrid service, Continue there goes on, with no retyping
        config = self.aios.users[UUID]['config']
        services = config['services']
        config['services'] = [{'id': 'realdebrid', 'enabled': True, 'credentials': {}}]
        self.walk_to_config()
        _, _, text = self.b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'AIOS_CONFIG_PASSWORD': PASSWORD,
                                            'go': 'next'})
        self.assertIn("After adding it on AIOStreams' configure page, press Continue again: setup reads the config "
                      'again.', text)
        self.assertIn('<button class="btn primary" type="submit" name="go" value="next">Continue', self.b.pages[-1])
        _, _, text = self.b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'go': 'next'})       # not added yet: the same
        self.assertIn('press Continue again', text)
        config['services'] = services                                   # TorBox added on AIOStreams
        _, _, text = self.b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'go': 'next'})
        self.assertIn('Debrid services: TorBox', text)
        self.assertNotIn('press Continue again', text)
        status, headers, _ = self.b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'go': 'next'})
        self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/choices'))
        self.assertFalse(any(PASSWORD in page for page in self.b.pages))

    def test_after_a_stop_at_your_choices_continue_reads_the_config_again(self):
        # a config usable only through Usenet, Usenet answered no: once TorBox is added, Continue goes on
        config = self.aios.users[UUID]['config'] = {'services': [ENGINE], 'presets': [NEWZNAB, SUBS],
                                                    'failover': {'enabled': True}}
        self.walk_to_config()
        self.b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        _, _, text = self.b.follow('config', {'AIOS_CONFIG_ID': self.LINK, 'go': 'next'})
        self.assertIn('Sync files you play from Usenet too?', text)
        form = {'LANGUAGES': ['eng'], 'PUBLIC_ADDRESS': f'{PUBLIC}/subsync', 'ADD_OPENSUBTITLES': 'yes', 'go': 'next'}
        _, _, text = self.b.post('choices', form)
        self.assertIn(engine.NO_USENET, text)
        self.assertIn("Answer yes to the Usenet question, or add a debrid service (TorBox, …) on AIOStreams' "
                      'configure page, then press Continue again.', text)
        config['services'].insert(0, TORBOX)                            # TorBox added on AIOStreams
        config['presets'].insert(0, ZILEAN)
        status, headers, _ = self.b.post('choices', form)
        self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/limits'))
        self.assertTrue(self.app.session.e.has_sync)

    def test_a_saved_key_can_be_removed_on_the_page(self):
        self.run_setup(dict(self.ANSWERS, TEST_READ='no', SUBDL_KEY='subdl-key-1', TMDB_KEY='tmdb-key-1'))
        self.b.post('answers', {})
        self.b.get('machine')
        self.b.follow('machine', {'go': 'next'})
        self.b.follow('aios', {'go': 'next'})
        self.b.post('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        _, _, text = self.b.follow('config', {'AIOS_CONFIG_ID': UUID, 'go': 'next'})
        self.assertIn('Remove the saved key', text)
        self.assertIn('name="REMOVE_SUBDL_KEY"', text)
        self.assertIn('name="REMOVE_TMDB_KEY"', text)
        self.assertNotIn('name="REMOVE_WYZIE_KEY"', text)                # none saved: no box
        _, _, text = self.b.follow('choices', {'LANGUAGES': ['eng'], 'PUBLIC_ADDRESS': f'{PUBLIC}/subsync',
                                               'REMOVE_SUBDL_KEY': 'yes', 'go': 'next'})
        self.assertIn('Data a day in total', text)
        self.b.follow('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', 'go': 'next'})
        self.b.post('review', {'CONFIRM': 'yes'})
        self.b.settle('install')
        keys, env = engine.read_kv(self.state / 'secrets.env'), engine.read_kv(self.state / 'subsync.env')
        self.assertNotIn('SUBDL_KEY', keys)
        self.assertNotIn('SUBSYNC_SUBDL_KEY', env)
        self.assertEqual((keys['TMDB_KEY'], env['SUBSYNC_TMDB_KEY']), ('tmdb-key-1', 'tmdb-key-1'))
        self.assertFalse(any('subdl-key-1' in page or 'tmdb-key-1' in page for page in self.b.pages))

    def test_a_key_ticked_for_removal_in_a_refused_step_stays_once_the_box_is_unticked(self):
        # The step took the key out at once, before a later answer was refused: the page asked again without
        # the box, and the next Continue, the box unticked, went on without the key, which the install left out
        self.run_setup(dict(self.ANSWERS, TEST_READ='no', SUBDL_KEY='subdl-key-1'))
        self.b.post('answers', {})
        self.b.get('machine')
        self.b.follow('machine', {'go': 'next'})
        self.b.follow('aios', {'go': 'next'})
        self.b.post('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        self.b.follow('config', {'AIOS_CONFIG_ID': UUID, 'go': 'next'})
        form = {'LANGUAGES': ['eng'], 'PUBLIC_ADDRESS': f'{PUBLIC}/sub sync', 'REMOVE_SUBDL_KEY': 'yes', 'go': 'next'}
        status, _, text = self.b.post('choices', form)
        self.assertEqual(status, 200)
        self.assertIn('<p class="field-error" id="public_address-error">Leave out the spaces.</p>', text)
        self.assertIn('name="REMOVE_SUBDL_KEY"', text)                  # still saved: its box is offered again
        del form['REMOVE_SUBDL_KEY']
        _, _, text = self.b.follow('choices', dict(form, PUBLIC_ADDRESS=f'{PUBLIC}/subsync'))
        self.assertIn('Data a day in total', text)
        self.b.follow('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', 'go': 'next'})
        self.b.post('review', {'CONFIRM': 'yes'})
        self.b.settle('install')
        keys, env = engine.read_kv(self.state / 'secrets.env'), engine.read_kv(self.state / 'subsync.env')
        self.assertEqual((keys['SUBDL_KEY'], env['SUBSYNC_SUBDL_KEY']), ('subdl-key-1', 'subdl-key-1'))

    def test_the_config_step_calls_the_link_an_install_link(self):
        # the box said "Stremio link" while its explanation, the alias errors and the public pages say "install link"
        self.walk_to_config()
        _, _, text = self.b.post('config', {'AIOS_CONFIG_ID': '00000000-0000-4000-8000-000000000000',
                                            'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        self.assertIn('Config ID (or paste your install link)', text)
        self.assertIn('There is no config with that ID on this AIOStreams. Check it, or paste your install link.', text)
        self.assertNotIn('Stremio link', text)

    def test_the_install_page_shows_the_rules_note_under_the_rule(self):
        # the note was the install step's first output and joined the review step's last row, so no page showed it:
        # a web server in Docker got only the rule's 127.0.0.1:8790
        note = ("Web server running in Docker on AIOStreams' network: use aiosubtitles-subsync:8790 instead of "
                '127.0.0.1:8790.')
        self.walk_to_review()
        self.b.post('review', {'CONFIRM': 'yes'})
        _, _, text = self.b.settle('install')
        self.assertEqual(text.count(note), 1)
        self.assertGreater(text.index(note), text.rindex('</pre>'))            # under the rule, as in the terminal
        self.assertNotIn(note, str(self.app.session.rows['review']))

    def test_a_stop_while_saving_shows_on_the_review_page(self):
        # the page came back unchanged: no message, the cache box unticked, the sign-in fields gone, and every retry
        # met AIOStreams' hourly limit blind
        limited = engine.AiosError('Creating the sync config: AIOStreams is limiting requests from this machine (it '
                                   'allows 10 new configs an hour); try again later.')
        unreachable = engine.AiosError("Can't reach AIOStreams at http://aiostreams:3000 (timed out).",
                                       'Check the address, and that AIOStreams is running (docker ps).')
        self.walk_to_review()
        form = {'CONFIRM': 'yes', 'CACHE': 'yes', 'AIOS_USERNAME': 'admin', 'AIOS_PASSWORD': 'adminpw'}
        for error in (limited, unreachable):
            with self.subTest(error=error.message), mock.patch.object(engine.Aios, 'create_config', side_effect=error):
                status, _, text = self.b.post('review', form)
                self.assertEqual(status, 200)
                self.assertIn(error.message, text)
                self.assertIn(error.fix or error.message, text)              # its way out
                self.assertRegex(text, r'<input[^>]*name="CACHE"[^>]*checked')
                self.assertIn('AIOStreams sign-in (admin): username', text)
                self.assertRegex(text, r'<input[^>]*name="AIOS_USERNAME"[^>]*value="admin"')
                if error is limited:                                          # signed in once, the first time
                    self.assertIn('Signed in', text)                          # what the step wrote before it stopped
        self.assertFalse(any('adminpw' in page for page in self.b.pages))

    def test_a_refused_answer_is_summed_up_first_linking_to_its_field(self):
        # The error was only under its box, further down; nothing was focused or announced
        b = self.b
        b.post('', {'ROUTE': 'yourself'})
        b.get('machine')
        b.follow('machine', {'go': 'next'})
        b.follow('aios', {'go': 'next'})
        _, _, text = b.post('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': 'wrong', 'go': 'next'})
        raw = b.pages[-1]
        body = raw.split('<div class="body">', 1)[1]
        self.assertTrue(body.startswith('<div class="note problem" role="alert" id="problem" tabindex="-1">'), body[:80])
        message = re.search(r'<p id="problem-message"><a href="#aios_config_password">([^<]+)</a></p>', body).group(1)
        self.assertIn(f'<p class="field-error" id="aios_config_password-error">{message}</p>', raw)
        self.assertIn("That didn&#x27;t go through", body)
        _, _, text = b.post('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        self.assertNotIn('id="problem"', b.pages[-1])

    def test_a_refused_audio_limit_shows_under_its_field(self):
        # the engine refuses it under its answers-file key, AUDIO_BUDGET, which the page's field isn't named by
        self.walk_to_review(AUDIO='pick')
        _, _, text = self.b.post('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', 'SUBSYNC_AUDIO_BUDGET_GB': '3',
                                            'go': 'next'})
        # in the page's words for its row of buttons (the terminal's are "Type 0.5, 1, 2 or 4 (GB per file).")
        self.assertIn('<p class="field-error" id="SUBSYNC_AUDIO_BUDGET_GB-error">Choose 0.5, 1, 2 or 4 GB.</p>', text)

    def test_the_sign_in_is_labelled_by_why_aiostreams_asks(self):
        # it always said "(admin)", though only the cache setting needs an admin account
        self.aios.gated = True
        self.walk_to_review()
        _, _, text = self.b.post('review', {'CONFIRM': 'yes'})
        self.assertIn('AIOStreams sign-in to save configs: username', text)
        self.assertNotIn('AIOStreams sign-in (admin): username', text)
        self.assertIn('Creating the sync config: This AIOStreams asks you to sign in before configs can be saved.', text)
        _, _, text = self.b.post('review', {'CONFIRM': 'yes', 'CACHE': 'yes'})
        self.assertIn('AIOStreams sign-in (admin): username', text)
        self.assertNotIn('AIOStreams sign-in to save configs: username', text)
        status, headers, _ = self.b.post('review', {'CONFIRM': 'yes', 'AIOS_USERNAME': 'admin',
                                                    'AIOS_PASSWORD': 'adminpw'})
        self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/install'))
        self.b.settle('install')

    def test_the_done_page_shows_the_checks_warnings_under_its_heading(self):
        # it said "Installed, with warnings (see above)" with nothing above: the warnings were on the page before
        self.aios.reaches_subsync = False
        self.walk_to_review()
        self.b.post('review', {'CONFIRM': 'yes'})
        self.b.settle('install')
        self.b.follow('install', {'go': 'next'})
        self.b.settle('check')
        _, _, text = self.b.follow('check', {'go': 'next'})
        self.assertIn('<h1>Installed, with warnings</h1>', text)
        self.assertNotIn('see above', text)
        self.assertLess(text.index('Installed, with warnings'), text.index('AIOStreams did not reach subsync.'))
        self.assertIn('It calls http://aiosubtitles-subsync:8790/…; check that address works from AIOStreams.', text)
        self.assertNotIn('AIOStreams asks subsync for subtitles', text)        # the warnings only

    def up_fails_once(self, error='Error response from daemon: port is already allocated'):
        """Docker's compose, its first `up` failing with `error`."""
        failures, compose = [error], engine.dockerenv.compose

        def up(state, *args, **kw):
            if 'up' in args and failures:
                return 1, '', failures.pop()
            return compose(state, *args, **kw)
        return mock.patch.object(engine.dockerenv, 'compose', up)

    def test_a_stopped_install_can_be_tried_again(self):
        # Back and "Save these changes" showed the same stop again, with a greyed-out Continue: nothing on the page ran
        # the install again
        self.walk_to_review()
        with self.up_fails_once():
            self.b.post('review', {'CONFIRM': 'yes'})
            _, _, text = self.b.settle('install')
            self.assertIn('Starting subsync failed.', text)
            self.assertIn('Error response from daemon: port is already allocated', text)
            self.assertIn('<button class="btn primary" type="submit" name="go" value="again">Try again</button>',
                          self.b.pages[-1])
            self.assertNotIn('disabled', self.b.pages[-1])
            status, headers, _ = self.b.post('install', {'go': 'next'})          # no way on but Try again
            self.assertEqual(status, 200)
            status, headers, _ = self.b.post('install', {'go': 'again'})
            self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/install'))
            _, _, text = self.b.settle('install')
        self.assertIn('subsync is running (it restarts by itself after a reboot)', text)
        self.assertNotIn('Starting subsync failed.', text)
        self.assertEqual(self.started_count, 1)                                 # the one try that worked
        self.assertEqual(len([p for m, p, _ in self.aios.log if m == 'POST' and p == '/api/v1/user']), 1)  # saved once
        status, headers, _ = self.b.post('install', {'go': 'next'})
        self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/check'))

    def test_an_answer_changed_after_the_install_is_saved_and_installed(self):
        # the next "Save these changes" led back to the review page every time, and the new answers were never applied
        checks, real = [], engine.Engine.self_test_checks
        with mock.patch.object(engine.Engine, 'self_test_checks', lambda e: checks.append(e) or real(e)):
            self.walk_to_review()
            self.b.post('review', {'CONFIRM': 'yes'})
            self.b.settle('install')
            self.b.follow('install', {'go': 'next'})
            self.b.settle('check')
            _, _, text = self.b.get('choices')                                      # the browser's Back
            self.assertIn('Which subtitle languages should be synced?', text)
            self.b.follow('choices', {'LANGUAGES': ['eng', 'spa', ''], 'PUBLIC_ADDRESS': f'{PUBLIC}/subsync',
                                      'ADD_OPENSUBTITLES': 'yes', 'go': 'next'})
            self.b.follow('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', 'go': 'next'})
            status, headers, _ = self.b.post('review', {'CONFIRM': 'yes'})
            self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/install'))
            _, _, text = self.b.settle('install')
            self.assertIn('subsync is running (it restarts by itself after a reboot)', text)
            self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_LANGUAGES'], 'eng,spa')
            self.assertEqual(self.started_count, 2)
            self.assertEqual(len([p for m, p, _ in self.aios.log if m == 'POST' and p == '/api/v1/user']), 1)
            self.b.follow('install', {'go': 'next'})
            _, _, text = self.b.settle('check')                                     # the new install checked
        self.assertEqual(len(checks), 2)
        self.assertIn('AIOStreams asks subsync for subtitles', text)

    def test_a_rule_an_earlier_install_showed_is_gone_once_it_isnt_needed(self):
        # The page keeps one engine, and only a run that needed the rule set it: an install after the rule was
        # in place (or for an address with a port) still told the person to add it
        self.walk_to_review()
        self.b.post('review', {'CONFIRM': 'yes'})
        _, _, text = self.b.settle('install')
        self.assertIn('Your web server needs one rule', text)
        self.b.follow('install', {'go': 'next'})
        self.b.settle('check')
        self.b.get('choices')                                                       # the browser's Back
        self.b.follow('choices', {'LANGUAGES': ['eng', 'spa', ''], 'PUBLIC_ADDRESS': f'{PUBLIC}/subsync',
                                  'ADD_OPENSUBTITLES': 'yes', 'go': 'next'})
        self.b.follow('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', 'go': 'next'})
        self.b.post('review', {'CONFIRM': 'yes'})
        _, _, text = self.b.settle('install')
        self.assertIn(f'Your web server already reaches subsync at {PUBLIC}/subsync', text)
        self.assertNotIn('Your web server needs one rule', text)
        self.assertNotIn('reverse_proxy', text)
        self.assertIsNone(self.app.session.e.rule_note)

    def test_a_rule_an_earlier_install_showed_is_gone_for_an_address_with_a_port(self):
        # An address with its own port needs no rule, and the install never asks
        # for one; the earlier install's rule stayed on the page
        self.walk_to_review()
        self.b.post('review', {'CONFIRM': 'yes'})
        _, _, text = self.b.settle('install')
        self.assertIn('Your web server needs one rule', text)
        self.b.follow('install', {'go': 'next'})
        self.b.settle('check')
        self.b.get('choices')                                                       # the browser's Back
        self.b.follow('choices', {'LANGUAGES': ['eng', 'rus', ''], 'PUBLIC_ADDRESS': 'http://192.168.1.5:8795',
                                  'ADD_OPENSUBTITLES': 'yes', 'go': 'next'})
        self.b.follow('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', 'go': 'next'})
        self.b.post('review', {'CONFIRM': 'yes'})
        _, _, text = self.b.settle('install')
        self.assertIn('subsync is running (it restarts by itself after a reboot)', text)
        self.assertNotIn('Your web server needs one rule', text)
        self.assertNotIn('reverse_proxy', text)
        self.assertEqual(self.app.session.e.s['public_kind'], 'port')

    def test_while_the_install_runs_the_steps_before_it_lead_to_its_page(self):
        # an earlier step's answers once changed under the running install
        release = threading.Event()
        self.addCleanup(release.set)
        install = engine.Engine.install
        self.walk_to_review()
        with mock.patch.object(engine.Engine, 'install', lambda e: (release.wait(10), install(e))):
            self.b.post('review', {'CONFIRM': 'yes'})
            for step in ('machine', 'choices', 'limits', 'review'):
                with self.subTest(step=step):
                    status, headers, _ = self.b.get(step)
                    self.assertEqual((status, headers.get('Location')), (303, f'/{self.app.key}/install'))
            status, headers, _ = self.b.post('choices', {'LANGUAGES': ['spa'], 'PUBLIC_ADDRESS': f'{PUBLIC}/subsync',
                                                         'go': 'next'})
            self.assertEqual((status, headers.get('Location')), (303, f'/{self.app.key}/install'))
            self.assertEqual(self.app.session.e.answers['LANGUAGES'], 'eng, rus')
            release.set()
            self.b.settle('install')
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_LANGUAGES'], 'eng,rus')

    def test_an_unexpected_error_in_the_background_is_a_stop_on_the_page(self):
        # the install page showed an empty step whose Continue did nothing; the reason was only in the terminal
        self.walk_to_review()
        terminal = io.StringIO()
        refused = PermissionError(13, 'Permission denied', 'subsync.env')
        with mock.patch.object(engine, 'write_env', side_effect=refused), contextlib.redirect_stderr(terminal):
            self.b.post('review', {'CONFIRM': 'yes'})
            _, _, text = self.b.settle('install')
        self.assertIn('Something unexpected went wrong, so setup stopped here.', text)
        self.assertIn('Run ./setup again. If it happens again, send what the terminal shows with a bug report.', text)
        self.assertIn('value="again">Try again</button>', self.b.pages[-1])
        self.assertNotIn('Permission denied', text)                              # the details: the terminal only
        self.assertIn('PermissionError: [Errno 13] Permission denied', terminal.getvalue())

    def test_an_unexpected_error_in_a_step_is_a_stop_on_its_page(self):
        terminal = io.StringIO()
        self.walk_to_review()
        with mock.patch.object(engine.Engine, 'limits', side_effect=KeyError('settings')), \
                contextlib.redirect_stderr(terminal):
            status, _, text = self.b.post('limits', {'SUBSYNC_DAILY_BUDGET_GB': '50', 'go': 'next'})
        self.assertEqual(status, 200)
        self.assertIn('<h1>Limits</h1>', text)
        self.assertIn('Something unexpected went wrong, so setup stopped here.', text)
        self.assertIn("KeyError: 'settings'", terminal.getvalue())
        self.assertNotIn('settings', text.split('<h1', 1)[1].split('Data a day in total')[0])

    def test_an_unexpected_error_outside_a_step_is_a_page_not_a_dropped_connection(self):
        terminal = io.StringIO()
        with mock.patch.object(engine, 'load_settings', side_effect=PermissionError(13, 'Permission denied')), \
                contextlib.redirect_stderr(terminal):
            status, _, text = self.b.get('')
        self.assertEqual(status, 200)
        self.assertIn('<h1>Set up AIOSubtitles</h1>', text)
        self.assertIn('Something unexpected went wrong, so setup stopped here.', text)
        self.assertIn('Run ./setup again. If it happens again, send what the terminal shows with a bug report.', text)
        self.assertNotIn('Permission denied', text)
        self.assertIn('PermissionError', terminal.getvalue())

    def test_an_aiostreams_error_outside_a_step_shows_its_words(self):
        # it ended the request with no answer
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        error = engine.AiosError("Can't reach AIOStreams at http://aiostreams:3000 (timed out).",
                                 'Check the address, and that AIOStreams is running (docker ps).')
        with mock.patch.object(engine.Engine, 'status_parts', side_effect=error):
            status, _, text = self.b.get('')
        self.assertEqual(status, 200)
        self.assertIn(error.message, text)
        self.assertIn(error.fix, text)

    def test_the_install_page_shows_what_saving_the_configs_warned(self):
        # the cache setting fixed by AIOStreams' environment, with the value to set there, and a sign-in that isn't an
        # admin's, were recorded and shown on no page: the person thought the cache was off
        sign_in = {'CONFIRM': 'yes', 'CACHE': 'yes', 'AIOS_USERNAME': 'admin', 'AIOS_PASSWORD': 'adminpw'}
        self.aios.setting_source = 'environment'
        self.walk_to_review()
        self.b.post('review', sign_in)
        _, _, text = self.b.settle('install')
        locked = "The setting is fixed by AIOStreams' environment (SUBTITLE_CACHE_TTL); change it there:"
        self.assertIn(locked, text)
        self.assertIn('SUBTITLE_CACHE_TTL=*:300,aiosubtitles-subsync:-1', text)
        self.assertLess(text.index(locked), text.index('subsync is running'))
        self.assertNotIn('Signed in', text)                                     # its warnings only
        self.assertFalse(any('adminpw' in page for page in self.b.pages))

    def test_the_install_page_says_a_sign_in_isnt_an_admins(self):
        self.aios.admin = False
        self.walk_to_review()
        self.b.post('review', {'CONFIRM': 'yes', 'CACHE': 'yes', 'AIOS_USERNAME': 'admin', 'AIOS_PASSWORD': 'adminpw'})
        _, _, text = self.b.settle('install')
        self.assertIn('That account is not an admin; the setting stays as it is.', text)

    def test_your_choices_shows_why_usenet_stays_off(self):
        # Usenet answered yes last time, and no service of the config reads it any more: the reason was dropped
        self.aios.users[UUID]['config'] = {'services': [TORBOX, ENGINE], 'presets': [ZILEAN, NEWZNAB, SUBS],
                                           'failover': {'enabled': True}}
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no', USENET='yes'))
        self.assertEqual(code, 0, out)
        config = self.aios.users[UUID]['config']                                # as setup saved it
        config['services'] = [TORBOX, {'id': 'nzbdav', 'enabled': True, 'credentials': {'url': 'http://nzbdav:3000'}}]
        config['presets'] = [ZILEAN, dict(NEWZNAB, options={'name': 'Geek', 'services': ['nzbdav']}), SUBS]
        self.b.post('answers', {})
        self.b.get('machine')
        self.b.follow('machine', {'go': 'next'})
        self.b.follow('aios', {'go': 'next'})
        self.b.post('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        _, _, text = self.b.follow('config', {'AIOS_CONFIG_ID': UUID, 'go': 'next'})
        self.assertNotIn('Sync files you play from Usenet too?', text)            # no service reads it: not asked
        form = {'LANGUAGES': ['eng'], 'PUBLIC_ADDRESS': 'ftp://x', 'go': 'next'}
        _, _, text = self.b.post('choices', form)
        self.assertIn('Type a full address that starts with http:// or https://', text)
        self.assertLess(text.index(explain.SETUP_USENET_WHY['proxy']),     # setup's own reason
                        text.index('Which subtitle languages should be synced?'))
        self.assertNotIn(explain.USENET_WHY['proxy'], text)
        self.assertEqual(text.count(explain.TMDB_NOTICE), 1)                    # under its field only, not a line too
        self.b.follow('choices', dict(form, PUBLIC_ADDRESS=f'{PUBLIC}/subsync'))
        _, _, text = self.b.get('choices')                                       # Back from Limits
        self.assertIn(explain.SETUP_USENET_WHY['proxy'], text)

    def test_an_address_with_a_space_is_asked_again_at_its_field(self):
        # it was taken, and the address's first check then ended in http.client's InvalidURL: the page's "unexpected
        # error" stop
        b = self.b
        b.post('', {'ROUTE': 'yourself'})
        b.get('machine')
        b.follow('machine', {'go': 'next'})
        b.follow('aios', {'go': 'next'})
        b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        b.follow('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': '', 'go': 'next'})
        form = {'LANGUAGES': ['eng'], 'PUBLIC_ADDRESS': 'http://192.168.1.5:8790/sub sync', 'go': 'next'}
        status, _, text = b.post('choices', form)
        self.assertEqual(status, 200)
        self.assertIn('<p class="field-error" id="public_address-error">Leave out the spaces.</p>', text)
        self.assertNotIn(screens.UNEXPECTED[0], text)
        status, headers, _ = b.post('choices', dict(form, PUBLIC_ADDRESS=f'{PUBLIC}/subsync'))
        self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/limits'))

    def test_the_container_choice_shows_each_name_once_with_its_image(self):
        two = [{'name': name, 'image': image, 'port': 3000, 'base_url': PUBLIC, 'host_network': False,
                'networks': ['aio_default'], 'ips': [], 'published': []}
               for name, image in (('aiostreams', 'ghcr.io/viren070/aiostreams:latest'),
                                   ('aiostreams-test', 'ghcr.io/viren070/aiostreams:2.37'))]
        with mock.patch.object(engine.dockerenv, 'aios_containers', lambda: two):
            self.walk_to_config()
            _, _, text = self.b.get('aios')
        self.assertIn('More than one AIOStreams runs here. Which one do your apps use?', text)
        self.assertIn('<b>aiostreams</b></span><span id="AIOS_CONTAINER-aiostreams-help">ghcr.io/viren070/aiostreams:'
                      'latest</span>', self.b.pages[-1])
        self.assertNotIn('aiostreams  (', text)

    def test_a_decimal_comma_in_the_daily_total_is_refused_on_the_page(self):
        # "1,5" was saved as 15 GB
        self.walk_to_review()
        _, _, text = self.b.post('limits', {'SUBSYNC_DAILY_BUDGET_GB': '1,5', 'go': 'next'})
        self.assertIn('A number above 0, with a dot for decimals and no commas, like 1.5.', text)
        self.assertNotIn('review', self.app.session.done)

    def test_the_page_after_setup(self):
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        app = web.App(port=8803)
        b = Browser(app)
        newer = self.place._replace(image='ghcr.io/haslanli/aiosubtitles:1.2.0@sha256:' + 'd' * 64)
        with mock.patch.object(engine, 'PLACE', newer), mock.patch.object(engine, 'setup_version', lambda: '1.2.0'):
            _, _, text = b.get('')
        self.assertIn('AIOSubtitles · subsync status', text)
        self.assertIn('container aiosubtitles-subsync: running healthy', text)
        self.assertIn('Version 1.2.0 is ready.', text)
        self.assertIn(update.ABOUT, text)
        self.assertNotIn('action="rollback"', text)                                     # no earlier version yet
        self.assertIn(f'Password of your config {UUID[:8]}…', text)
        _, _, text = b.post('remove', {'AIOS_CONFIG_PASSWORD': 'wrong', 'REMOVE': 'yes'})
        self.assertIn("don't match", text)
        self.assertTrue((self.state / 'settings.json').exists())
        _, _, text = b.post('remove', {'AIOS_CONFIG_PASSWORD': PASSWORD, 'REMOVE': 'yes', 'REMOVE_OPENSUBTITLES': 'yes'})
        self.assertIn('These stay, in case you set it up again', text)
        self.assertFalse((self.state / 'settings.json').exists())
        self.assertFalse(any(PASSWORD in page for page in b.pages))

    def test_change_your_answers_opens_the_same_steps_filled_in(self):
        # the installed route's own steps, without the route question: it is never switched in place
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        app = web.App(port=8804)
        b = Browser(app)
        _, _, text = b.get('')
        self.assertIn('Already set up here: your answers are kept unless you change them. To switch between '
                      'installing it for yourself and hosting a public instance, remove it first.', text)
        status, headers, _ = b.post('answers', {})
        self.assertEqual((status, headers['Location']), (303, f'/{app.key}/machine'))
        self.assertEqual(app.session.route, 'yourself')
        _, _, text = b.get('')                                          # the start page: the page after setup
        self.assertNotIn('How do you want to use it?', text)
        self.assertIn('AIOSubtitles · subsync status', text)
        status, headers, _ = b.post('', {'ROUTE': 'host'})
        self.assertEqual((status, headers['Location']), (303, f'/{app.key}/'))
        self.assertEqual(app.session.route, 'yourself')
        b.get('machine')
        b.follow('machine', {'go': 'next'})
        _, _, text = b.follow('aios', {'go': 'next'})
        self.assertIn(f'value="{UUID}"', text)                                          # the ID kept, never the password


class Pieces(unittest.TestCase):
    def test_on_dockers_bridge_the_page_listens_inside_and_answers_to_the_machines_port(self):
        app = web.App(port=8789, bind='0.0.0.0', public_port=8899)
        lines = []
        with mock.patch.object(web, 'start', lambda a: setattr(a, 'finished', mock.Mock(wait=lambda: None))
                               or mock.Mock()):
            web.serve(app, say=lines.append)
        self.assertIn(f'  http://127.0.0.1:8899/{app.key}/', lines)
        self.assertTrue(any('ssh -L 8899:127.0.0.1:8899' in line for line in lines))
        for host, status in (('127.0.0.1:8899', 200), ('localhost:8899', 200), ('127.0.0.1:8789', 404),
                             ('evil.example:8899', 404)):
            got = app.handle('GET', f'/{app.key}/', {'Host': host}, b'')[0]
            self.assertEqual(got, status, host)

    def test_the_done_line_only_when_setup_finished(self):
        app = web.App()
        self.assertIsNone(web.stop_line(app))
        app.finish_in(0)
        self.assertEqual(web.stop_line(app), 'Setup is done; the page has stopped.')

    def test_a_step_keeps_a_missing_answer_apart_from_a_wrong_one(self):
        s = web.Session('yourself', {})

        def missing():
            raise engine.Problem('AIOS_URL', missing=True)

        def wrong():
            raise engine.Problem('AIOS_URL', 'Not an address.')
        self.assertFalse(s.run('aios', missing))
        self.assertEqual((s.asks, s.problems), ({'aios': 'AIOS_URL'}, {}))
        self.assertFalse(s.run('aios', wrong))
        self.assertEqual((s.asks, s.problems), ({}, {'aios': ('AIOS_URL', 'Not an address.')}))

    def test_a_step_run_again_undoes_the_steps_after_it_and_itself_when_it_stops(self):
        s = web.Session('yourself', {})
        s.done.update(s.ids[:5])                                        # machine to limits

        def refused():
            raise engine.Problem('AIOS_CONFIG_PASSWORD', "That config ID and password don't match.")
        self.assertFalse(s.run('config', refused))
        self.assertEqual(s.done, {'machine', 'aios'})
        self.assertTrue(s.run('machine', lambda: None))
        self.assertEqual(s.done, {'machine'})

    def test_a_note_that_comes_first_in_a_step_stays_in_that_step(self):
        # it joined the last row of the step before, already shown, and so was shown nowhere
        s = web.Session('yourself', {})
        s.run('review', lambda: s.e.out.ok("Created subsync's own config (91aaaaaa…)"))
        s.run('install', lambda: s.e.out.note('A note first.'))
        self.assertEqual(s.rows['install'], [('note', 'A note first.', None)])
        self.assertEqual(s.e.out.rows[0], ['ok', "Created subsync's own config (91aaaaaa…)", None])
        s.run('check', lambda: (s.e.out.warn('A warning.'), s.e.out.note('Its note.')))
        self.assertEqual(s.rows['check'], [('warn', 'A warning.', 'Its note.')])     # under its own row, as before

    def test_a_stopped_step_leaves_no_turning_ring(self):
        s = web.Session('yourself', {})

        def work():
            s.e.out.working('Building subsync (the first time takes a few minutes) ...')
            raise engine.Stop('Building subsync failed.')
        s.run('install', work)
        self.assertNotIn('now', [kind for kind, _, _ in s.rows['install']])

    def test_an_unexpected_error_is_a_stop_whose_details_stay_in_the_terminal(self):
        # it ended the background thread with no rows and no stop: an empty step whose Continue did nothing
        s = web.Session('yourself', {})
        terminal = io.StringIO()

        def work():
            s.e.out.working('Starting subsync ...')
            raise OSError(28, 'No space left on device', 'state/compose.yaml')
        with contextlib.redirect_stderr(terminal):
            self.assertFalse(s.run('install', work))
        self.assertEqual(s.stops['install'], ('Something unexpected went wrong, so setup stopped here.',
                                              'Run ./setup again. If it happens again, send what the terminal shows '
                                              'with a bug report.'))
        self.assertEqual(s.rows['install'], [('next', 'Starting subsync ...', None)])
        self.assertNotIn('install', s.done)
        self.assertIn('OSError: [Errno 28] No space left on device', terminal.getvalue())

    def test_a_step_run_again_lets_the_finished_work_after_it_run_again(self):
        s = web.Session('yourself', {})

        def ended(job):
            deadline = time.monotonic() + 10
            while s.running(job) and time.monotonic() < deadline:
                time.sleep(0.01)
        for job in ('install', 'check-read'):
            s.start_job(job, lambda: None)
            ended(job)
        release = threading.Event()
        self.addCleanup(release.set)
        s.start_job('check', lambda: release.wait(10))                  # still running
        s.done.update(s.ids[:-1])
        s.saved = s.test_read = True
        self.assertEqual(s.working(), 'check')
        self.assertTrue(s.run('choices', lambda: None))
        self.assertEqual(set(s.jobs), {'check'})                        # the finished work goes, the running stays
        self.assertEqual((s.saved, s.test_read), (False, False))
        self.assertEqual(s.done & set(s.ids), {'machine', 'aios', 'config', 'choices'})
        release.set()
        ended('check')
        self.assertIsNone(s.working())


class Hosting(BackgroundWork, RouteFixture):
    def test_change_your_answers_opens_the_public_instances_own_steps(self):
        # never "Install it for yourself" in place: that is removing it first
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        app = web.App(port=8809)
        b = Browser(app)
        status, headers, _ = b.post('answers', {})
        self.assertEqual((status, headers['Location']), (303, f'/{app.key}/machine'))
        self.assertEqual(app.session.route, 'host')
        b.post('', {'ROUTE': 'yourself'})
        self.assertEqual(app.session.route, 'host')
        b.get('machine')
        _, _, text = b.follow('machine', {'go': 'next'})
        self.assertIn(f'value="{ADDRESS}"', text)                       # its own steps, filled in

    def test_a_busy_port_says_so_on_the_address_step(self):
        # a stop on a busy port once showed nothing, so the page looked unchanged
        app = web.App(port=8808)
        b = Browser(app)
        b.post('', {'ROUTE': 'host'})
        b.get('machine')
        b.follow('machine', {'go': 'next'})
        with mock.patch.object(engine.dockerenv, 'port_in_use', lambda *a: True), \
                mock.patch.object(engine.dockerenv, 'container_state', lambda n: ''):
            _, _, text = b.post('address', {'HOST_ADDRESS': ADDRESS, 'SUBSYNC_TRUSTED_PROXY': 'gateway', 'go': 'next'})
        self.assertIn('Port 8790 on this machine is already in use.', text)

    def test_a_web_address_with_a_space_is_asked_again_at_its_field(self):
        b = Browser(web.App(port=8811))
        b.post('', {'ROUTE': 'host'})
        b.get('machine')
        b.follow('machine', {'go': 'next'})
        _, _, text = b.post('address', {'HOST_ADDRESS': 'https://subs.exa mple.com', 'SUBSYNC_TRUSTED_PROXY': 'gateway',
                                        'go': 'next'})
        self.assertIn('<p class="field-error" id="host_address-error">Leave out the spaces.</p>', text)

    def test_a_hand_set_language_limit_no_button_offers_says_what_to_choose(self):
        # Its problem named the setting and sat inside the row of buttons, widening a phone's page
        self.state.mkdir(parents=True)
        (self.state / 'subsync.env').write_text('SUBSYNC_MAX_LANGUAGES=6\n')
        b = Browser(web.App(port=8812))
        b.post('', {'ROUTE': 'host'})
        b.get('machine')
        b.follow('machine', {'go': 'next'})
        b.follow('address', {'HOST_ADDRESS': ADDRESS, 'SUBSYNC_TRUSTED_PROXY': 'gateway', 'go': 'next'})
        b.follow('signups', {'SUBSYNC_PUBLIC_INVITE': 'my-own-code', 'go': 'next'})
        b.follow('hostchoices', {'go': 'next'})
        status, _, _ = b.post('hostlimits', {'go': 'next'})                # no button is picked: 6 isn't one
        page = b.pages[-1]
        self.assertEqual(status, 200)
        self.assertIn('<p class="field-error" id="SUBSYNC_MAX_LANGUAGES-error">Choose 1, 2, 3 or 4.</p><div '
                      'class="seg-row"><div class="seg" id="SUBSYNC_MAX_LANGUAGES" role="radiogroup" '
                      'aria-labelledby="SUBSYNC_MAX_LANGUAGES-label" aria-describedby="SUBSYNC_MAX_LANGUAGES-error '
                      'SUBSYNC_MAX_LANGUAGES-help" aria-invalid="true">', page)
        self.assertIn('<a href="#SUBSYNC_MAX_LANGUAGES">Choose 1, 2, 3 or 4.</a>', page)
        self.assertNotIn('SUBSYNC_MAX_LANGUAGES must be', page)

    def test_a_public_instance_through_the_page(self):
        app = web.App(port=8805)
        b = Browser(app)
        b.post('', {'ROUTE': 'host'})
        b.get('machine')
        _, _, text = b.follow('machine', {'go': 'next'})
        self.assertIn('Web address', text)
        for refused in ('http://subs.example.com', 'https://subs.example.com/subtitles'):
            _, _, text = b.post('address', {'HOST_ADDRESS': refused, 'SUBSYNC_TRUSTED_PROXY': 'gateway', 'go': 'next'})
            self.assertIn('<p class="field-error" id="host_address-error">Type an https address with no path after '
                          'the name, like https://subtitles.example.com: the pages and links start at its root.</p>',
                          text)
        _, _, text = b.follow('address', {'HOST_ADDRESS': ADDRESS, 'SUBSYNC_TRUSTED_PROXY': 'gateway', 'go': 'next'})
        invite = re.search(r'name="SUBSYNC_PUBLIC_INVITE"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="SUBSYNC_PUBLIC_INVITE"',
                           text)
        self.assertTrue(invite)
        _, _, text = b.follow('signups', {'SUBSYNC_PUBLIC_INVITE': 'my-own-code', 'go': 'next'})
        self.assertIn('SubSource API key (optional)', text)
        _, _, text = b.follow('hostchoices', {'go': 'next'})
        self.assertIn('The whole service', text)
        status, headers, _ = b.post('hostlimits', {'SUBSYNC_MAX_USERS': '150', 'go': 'next'})
        self.assertEqual(headers['Location'], f'/{app.key}/hostinstall')
        _, _, text = b.settle('hostinstall')
        self.assertIn('AIOSubtitles is running (it restarts by itself after a reboot)', text)
        _, _, text = b.follow('hostinstall', {'go': 'next'})
        _, _, text = b.settle('hostcheck')
        self.assertIn(f'People can reach AIOSubtitles at {ADDRESS}', text)
        self.assertIn('Check again', text)
        _, _, text = b.follow('hostcheck', {'go': 'next'})
        self.assertIn('Share the address and the invite code', text)
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual((env['SUBSYNC_PUBLIC_INVITE'], env['SUBSYNC_MAX_USERS']), ('my-own-code', '150'))
        self.assertFalse(any(env['SUBSYNC_SECRET_KEY'] in page for page in b.pages))     # never on a page

    def test_the_choices_page_starts_from_the_switches_subsync_env_has(self):
        # Read as subsync reads them: Yes and 1 are on, a word it doesn't take is its default (off)
        self.state.mkdir(parents=True)
        (self.state / 'subsync.env').write_text('SUBSYNC_USENET=Yes\nSUBSYNC_AUDIO_ROUTE=enabled\n'
                                                'SUBSYNC_TMDB_PERSONAL=1\n')
        b = Browser(web.App(port=8817))
        b.post('', {'ROUTE': 'host'})
        b.get('machine')
        b.follow('machine', {'go': 'next'})
        b.follow('address', {'HOST_ADDRESS': ADDRESS, 'SUBSYNC_TRUSTED_PROXY': 'gateway', 'go': 'next'})
        _, _, text = b.follow('signups', {'SUBSYNC_PUBLIC_INVITE': 'my-own-code', 'go': 'next'})
        ticked = {key: re.search(rf'id="{key.lower()}" name="{key}" value="yes"( checked)?', text).group(1) is not None
                  for key in ('SUBSYNC_USENET', 'SUBSYNC_AUDIO_ROUTE', 'SUBSYNC_TMDB_PERSONAL')}
        self.assertEqual(ticked, {'SUBSYNC_USENET': True, 'SUBSYNC_AUDIO_ROUTE': False, 'SUBSYNC_TMDB_PERSONAL': True})

    def walk_to_limits(self, b):
        b.post('', {'ROUTE': 'host'})
        b.get('machine')
        b.follow('machine', {'go': 'next'})
        b.follow('address', {'HOST_ADDRESS': ADDRESS, 'SUBSYNC_TRUSTED_PROXY': 'gateway', 'go': 'next'})
        b.follow('signups', {'SUBSYNC_PUBLIC_INVITE': 'my-own-code', 'go': 'next'})
        _, _, text = b.follow('hostchoices', {'go': 'next'})
        self.assertIn('The whole service', text)

    def test_a_stopped_install_can_be_tried_again(self):
        # its Continue was greyed out and nothing on the page ran the install again
        b = Browser(web.App(port=8813))
        self.walk_to_limits(b)
        failures, compose = ['Error response from daemon: port is already allocated'], engine.dockerenv.compose

        def up(state, *args, **kw):
            return (1, '', failures.pop()) if 'up' in args and failures else compose(state, *args, **kw)
        with mock.patch.object(engine.dockerenv, 'compose', up):
            b.post('hostlimits', {'SUBSYNC_MAX_USERS': '150', 'go': 'next'})
            _, _, text = b.settle('hostinstall')
            self.assertIn('Starting AIOSubtitles failed.', text)
            self.assertIn('<a class="btn secondary" href="hostlimits">Back</a><button class="btn primary" '
                          'type="submit" name="go" value="again">Try again</button>', b.pages[-1])
            status, headers, _ = b.post('hostinstall', {'go': 'again'})
            self.assertEqual((status, headers['Location']), (303, f'/{b.app.key}/hostinstall'))
            _, _, text = b.settle('hostinstall')
        self.assertIn('AIOSubtitles is running (it restarts by itself after a reboot)', text)
        status, headers, _ = b.post('hostinstall', {'go': 'next'})
        self.assertEqual((status, headers['Location']), (303, f'/{b.app.key}/hostcheck'))

    def test_a_limit_changed_after_the_install_is_installed(self):
        # Continue on the install page showed the same page again, and the new number was never written
        b = Browser(web.App(port=8814))
        self.walk_to_limits(b)
        b.post('hostlimits', {'SUBSYNC_MAX_USERS': '150', 'go': 'next'})
        b.settle('hostinstall')
        b.follow('hostinstall', {'go': 'next'})
        b.settle('hostcheck')
        status, headers, _ = b.post('hostlimits', {'SUBSYNC_MAX_USERS': '120', 'go': 'next'})    # Back, a new number
        self.assertEqual((status, headers['Location']), (303, f'/{b.app.key}/hostinstall'))
        _, _, text = b.settle('hostinstall')
        self.assertIn('AIOSubtitles is running (it restarts by itself after a reboot)', text)
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_MAX_USERS'], '120')
        b.follow('hostinstall', {'go': 'next'})
        _, _, text = b.settle('hostcheck')                                          # checked again
        self.assertIn(f'People can reach AIOSubtitles at {ADDRESS}', text)

    def test_check_again_runs_the_check_once_and_leads_to_its_page(self):
        # Its POST was answered with the page itself, so reloading it sent "Check again" once more
        b = Browser(web.App(port=8818))
        self.walk_to_limits(b)
        checks, real = [], engine.Engine.host_check
        with mock.patch.object(engine.Engine, 'host_check', lambda e: checks.append(e) or real(e)):
            b.post('hostlimits', {'SUBSYNC_MAX_USERS': '150', 'go': 'next'})
            b.settle('hostinstall')
            b.follow('hostinstall', {'go': 'next'})
            b.settle('hostcheck')
            status, headers, _ = b.post('hostcheck', {'go': 'again'})
            self.assertEqual((status, headers.get('Location')), (303, f'/{b.app.key}/hostcheck'))
            _, _, text = b.settle('hostcheck')
        self.assertIn(f'People can reach AIOSubtitles at {ADDRESS}', text)
        self.assertEqual(len(checks), 2)
        status, headers, _ = b.post('hostcheck', {'go': 'next'})
        self.assertEqual((status, headers.get('Location')), (303, f'/{b.app.key}/done'))

    def test_a_limit_box_emptied_after_a_refused_answer_takes_the_current_value(self):
        # An emptied box was skipped, so the refused answer stayed: the same error and the same box again
        b = Browser(web.App(port=8819))
        self.walk_to_limits(b)
        status, _, text = b.post('hostlimits', {'SUBSYNC_MAX_USERS': 'abc', 'go': 'next'})
        self.assertEqual(status, 200)
        self.assertIn('A whole number from 1', text)
        status, headers, _ = b.post('hostlimits', {'SUBSYNC_MAX_USERS': '', 'go': 'next'})
        self.assertEqual((status, headers.get('Location')), (303, f'/{b.app.key}/hostinstall'))
        b.settle('hostinstall')
        users = next(lim for lim in limits.hosting() if lim.setting == 'SUBSYNC_MAX_USERS')
        self.assertEqual(b.app.session.e.limit_values['SUBSYNC_MAX_USERS'], limits.current(users, {}))

    def test_a_limit_subsync_cant_hold_is_refused_on_the_page(self):
        # a 400-digit number was written and crashed subsync at start; over 4300 digits ended the request with no answer
        b = Browser(web.App(port=8815))
        self.walk_to_limits(b)
        for value in ('9' * 400, '9' * 5000):
            with self.subTest(digits=len(value)):
                status, _, text = b.post('hostlimits', {'SUBSYNC_USER_DAILY_MB': value, 'go': 'next'})
                self.assertEqual(status, 200)
                self.assertIn('A whole number from 1, like 10,000.', text)
        self.assertFalse((self.state / 'subsync.env').exists())                     # nothing installed

    def test_removing_a_public_instance_on_the_page(self):
        # its page said "subsync" and "Removing subsync", showed the warning twice, asked for a password it has no
        # field for when the box wasn't ticked, and pointed at --restore for config copies a public instance never makes
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        b = Browser(web.App(port=8812))
        _, _, text = b.get('')
        self.assertIn('<h1>AIOSubtitles status</h1>', text)
        self.assertIn('<h2 id="rm-h">Removing AIOSubtitles</h2>', text)
        self.assertNotIn('subsync status', text)
        self.assertEqual(text.count(screens.REMOVE_HOST_TEXT), 1)
        form = text.split('action="remove">')[1].split('</form>')[0]
        self.assertRegex(form, r'<input type="checkbox" id="confirm" name="REMOVE" value="yes"')
        self.assertNotIn('CONFIRM', form)
        _, _, text = b.post('remove', {'CONFIRM': 'yes'})               # the answer to "Save these changes?" only
        self.assertIn('Tick "Delete everyone\'s records and stored copies" to remove AIOSubtitles.', text)
        self.assertTrue((self.state / 'subsync.env').exists())
        _, _, text = b.post('remove', {})
        self.assertIn('Tick "Delete everyone\'s records and stored copies" to remove AIOSubtitles.', text)
        self.assertNotIn('Its password', text)
        box = re.search(r'<input type="checkbox" id="confirm"[^>]*>', b.pages[-1]).group(0)
        self.assertIn('aria-describedby="confirm-error confirm-help" aria-invalid="true"', box)
        self.assertIn('<p class="field-error" id="confirm-error">', b.pages[-1])
        self.assertTrue((self.state / 'subsync.env').exists())                          # nothing removed
        _, _, text = b.post('remove', {'REMOVE': 'yes'})
        self.assertIn('<h1>Removing AIOSubtitles</h1>', text)
        self.assertIn(f'this folder, {self.state.parent}</li>', text)                   # no database copies here
        self.assertNotIn('--restore', text)
        self.assertFalse((self.state / 'subsync.env').exists())

    def test_no_removal_page_shows_the_terminals_remove_or_restore_text(self):
        # REMOVE's text names ./setup --uninstall; the box sends REMOVE under its own label, never that text
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        b = Browser(web.App(port=8812))
        b.get('')                                                                       # the page with the box
        b.post('remove', {})                                                            # not ticked
        b.post('remove', {'CONFIRM': 'yes'})                                            # not ticked either
        self.assertTrue((self.state / 'subsync.env').exists())
        _, _, text = b.post('remove', {'REMOVE': 'yes'})                                # removed
        self.assertIn('<h1>Removing AIOSubtitles</h1>', text)
        self.assertFalse((self.state / 'subsync.env').exists())
        self.assertEqual(len(b.pages), 4)
        self.assertEqual([found for page in b.pages for found in terminal_only_shown(page)], [])


class OnBridge(BackgroundWork, FlowFixture):
    """The page on Docker's bridge network (the image route), where setup's own container has an address of its own."""

    def setUp(self):
        super().setUp()
        place = self.place._replace(network='bridge', gateway='172.17.0.1', page_port=8789)
        status = engine.Aios.status

        def local_status(aios):            # AIOStreams' own address is this machine's: http://localhost:3000
            found = status(aios)
            found['settings']['baseUrl'] = 'http://localhost:3000'
            return found
        for p in (mock.patch.object(engine, 'PLACE', place),
                  mock.patch.object(engine.dockerenv, 'lan_address', lambda: '172.17.0.3'),    # setup's container
                  mock.patch.object(engine.dockerenv, 'probe', lambda image, checks: {'lan': '192.168.1.7',
                                                                                       'taken': []}),
                  mock.patch.object(engine.dockerenv, 'join', lambda network, container: True),
                  mock.patch.object(engine.Engine, 'aios_urls', lambda e, c: [self.aios.url]),
                  mock.patch.object(engine.Aios, 'status', local_status)):
            p.start()
            self.addCleanup(p.stop)
        self.b = Browser(web.App(port=8816))

    def test_your_choices_fills_in_this_machines_network_address(self):
        # it filled in setup's container's own address (http://172.17.0.3:8790), which phones can't reach, and the page
        # always sends the field, so that address was saved
        b = self.b
        b.post('', {'ROUTE': 'yourself'})
        b.get('machine')
        b.follow('machine', {'go': 'next'})
        b.follow('aios', {'go': 'next'})
        b.post('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        _, _, text = b.follow('config', {'AIOS_CONFIG_ID': UUID, 'go': 'next'})
        self.assertRegex(text, r'<input[^>]*name="PUBLIC_ADDRESS"[^>]*value="http://192\.168\.1\.7:8790"')
        self.assertNotIn('172.17.0.3', text)
        status, headers, _ = b.post('choices', {'LANGUAGES': ['eng'], 'PUBLIC_ADDRESS': 'http://192.168.1.7:8790',
                                                'go': 'next'})
        self.assertEqual((status, headers['Location']), (303, f'/{b.app.key}/limits'))
        self.assertEqual(b.app.session.e.s['public_base'], 'http://192.168.1.7:8790')


class Removing(BackgroundWork, FlowFixture):
    """Remove subsync on the page after setup."""

    def setUp(self):
        super().setUp()
        self.b = Browser(web.App(port=8811))

    def remove(self, **form):
        return self.b.post('remove', dict({'AIOS_CONFIG_PASSWORD': PASSWORD, 'REMOVE': 'yes'}, **form))[2]

    def test_the_text_names_the_configs_this_install_has(self):
        # it said "(1 entry)" and "subsync's own config" also when the Usenet config goes too
        self.run_setup(dict(self.ANSWERS, TEST_READ='no', USENET='yes'))
        _, _, text = self.b.get('')
        self.assertIn("This takes subsync out of your config, deletes subsync's own config and its Usenet config, and "
                      'removes the subsync container with its stored copies. Your other subtitle sources stay.', text)
        self.assertNotIn('(1 entry)', text)
        self.assertIn("Deleted subsync's Usenet config", self.remove())

    def test_removing_asks_the_sign_in_an_aiostreams_wants_to_save_configs(self):
        # the form had no sign-in fields, so setup could never remove subsync from such an AIOStreams
        self.aios.gated = True
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no', AIOS_USERNAME='admin', AIOS_PASSWORD='adminpw'))
        self.assertEqual(code, 0, out)
        text = self.remove()
        asks = 'Saving the config: This AIOStreams asks you to sign in before configs can be saved.'
        self.assertIn(asks, text)
        self.assertLess(text.index(asks), text.index('AIOStreams sign-in to save configs: username'))
        self.assertRegex(self.b.pages[-1], r'<input[^>]*name="AIOS_PASSWORD"[^>]*type="password"')
        self.assertNotIn('needs an answer', text)                                       # asked, not marked wrong
        self.assertTrue((self.state / 'settings.json').exists())
        text = self.remove(AIOS_USERNAME='admin', AIOS_PASSWORD='adminpw', REMOVE_OPENSUBTITLES='yes')
        self.assertIn('These stay, in case you set it up again', text)
        self.assertFalse((self.state / 'settings.json').exists())
        self.assertEqual([p['type'] for p in self.aios.users[UUID]['config']['presets']], ['zilean', 'newznab'])
        self.assertFalse(any('adminpw' in page or PASSWORD in page for page in self.b.pages))

    def test_the_form_sends_remove_and_confirm_alone_removes_nothing(self):
        # The page's Remove subsync sent CONFIRM=yes, the answer to "Save these changes?" too; it sends REMOVE
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        page = self.b.get('')[2]
        form = page.split('action="remove">')[1].split('</form>')[0]
        self.assertIn('<input type="hidden" name="REMOVE" value="yes">', form)
        self.assertNotIn('CONFIRM', form)
        text = self.b.post('remove', {'AIOS_CONFIG_PASSWORD': PASSWORD, 'CONFIRM': 'yes'})[2]
        self.assertIn('Nothing was changed.', text)
        self.assertNotIn('These stay, in case you set it up again', text)
        self.assertTrue((self.state / 'settings.json').exists())
        self.assertNotEqual([p['type'] for p in self.aios.users[UUID]['config']['presets']], ['zilean', 'newznab'])
        text = self.remove(REMOVE_OPENSUBTITLES='yes')
        self.assertIn('These stay, in case you set it up again', text)
        self.assertFalse((self.state / 'settings.json').exists())
        self.assertEqual([p['type'] for p in self.aios.users[UUID]['config']['presets']], ['zilean', 'newznab'])

    def test_no_removal_page_shows_the_terminals_remove_or_restore_text(self):
        # REMOVE's text names ./setup --uninstall; the form sends REMOVE hidden, never showing that text
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.b.get('')                                                                  # the page with the form
        text = self.b.post('remove', {'AIOS_CONFIG_PASSWORD': PASSWORD, 'CONFIRM': 'yes'})[2]
        self.assertIn('Nothing was changed.', text)
        self.b.post('remove', {'REMOVE': 'yes'})                                        # no password: asked for it
        self.assertTrue((self.state / 'settings.json').exists())
        text = self.remove(REMOVE_OPENSUBTITLES='yes')                                  # removed
        self.assertIn('These stay, in case you set it up again', text)
        self.assertFalse((self.state / 'settings.json').exists())
        self.assertEqual(len(self.b.pages), 4)
        self.assertEqual([found for page in self.b.pages for found in terminal_only_shown(page)], [])

    def test_opensubtitles_stays_when_its_box_is_unticked(self):
        # An unticked box sends nothing, which the page reads as no; the engine's own default is yes, and no
        # test looked at the presets after such a removal
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        added = engine.load_settings()['added_opensubtitles']
        self.assertTrue(added)
        self.assertIn('name="REMOVE_OPENSUBTITLES"', self.b.get('')[2])                 # the box the person unticks
        text = self.remove()
        self.assertIn('These stay, in case you set it up again', text)
        self.assertFalse((self.state / 'settings.json').exists())
        presets = self.aios.users[UUID]['config']['presets']
        self.assertEqual([p['type'] for p in presets if p.get('instanceId') == added], ['opensubtitles'])
        self.assertEqual([p['type'] for p in presets], ['zilean', 'newznab', 'opensubtitles'])

    def test_a_failed_container_removal_keeps_setups_files_and_says_what_docker_said(self):
        # it said "Removed the subsync container" and deleted setup's files while the container kept running
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))

        def compose(state, *args, **kw):
            return (1, '', 'Error response from daemon: timeout') if 'down' in args else (0, '', '')
        with mock.patch.object(engine.dockerenv, 'compose', compose):
            text = self.remove()
        self.assertIn('<h1>Removing subsync</h1>', text)
        self.assertIn("Removing the subsync container failed; setup's files were kept, so removing it again finishes "
                      'the job.', text)
        self.assertIn('Error response from daemon: timeout', text)
        self.assertIn('Your config no longer lists subsync', text)                      # what it did before it stopped
        self.assertNotIn('Removed the subsync container', text)
        for name in ('settings.json', 'secrets.env', 'subsync.env', 'compose.yaml'):
            self.assertTrue((self.state / name).exists(), name)
        self.assertIsNone(self.b.app.finishing)                                         # the page stays up
        text = self.remove()
        self.assertIn('Removed the subsync container and its stored copies', text)
        self.assertFalse((self.state / 'settings.json').exists())


class Lengths(unittest.TestCase):
    """A body's length, on a real socket on 127.0.0.1: refused before the page reads anything."""

    def setUp(self):
        self.app = web.App()
        started(self, self.app)

    def status(self, length, body=b'ROUTE=host'):
        """The answer's status code, the connection left open after the body (as a slow client leaves it)."""
        head = f'POST / HTTP/1.1\r\nHost: 127.0.0.1:{self.app.port}\r\n'
        if length is not None:
            head += f'Content-Length: {length}\r\n'
        with socket.create_connection(('127.0.0.1', self.app.port), timeout=3) as c:
            c.sendall(head.encode() + b'\r\n' + body)
            return c.recv(1024).split(b'\r\n', 1)[0].split()[1:2]

    def test_a_length_that_is_missing_or_isnt_a_number_is_refused(self):
        # a non-number once ended the request with a traceback on setup's terminal and no answer
        for length in ('abc', '', None):
            with self.subTest(length=length):
                self.assertEqual(self.status(length), [b'411'])

    def test_a_negative_length_is_refused_at_once(self):
        # read once as "everything until the client closes": past the 16 KB cap, and the thread held meanwhile
        self.assertEqual(self.status(-1), [b'413'])
        self.assertEqual(self.status(web.MAX_BODY + 1), [b'413'])

    def test_a_connection_that_sends_nothing_gives_its_thread_back(self):
        self.assertTrue(web.Handler.timeout and web.Handler.timeout <= 60)


class Rollback(BackgroundWork, FlowFixture):
    """The page after setup offers going back while an earlier version is recorded."""

    def setUp(self):
        super().setUp()
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        s = engine.load_settings()
        s['previous'] = {'image': 'ghcr.io/haslanli/aiosubtitles:0.9.0@sha256:' + '9' * 64, 'version': '0.9.0',
                         'local': False, 'databases': 'databases-20261113-000000'}
        engine.save_settings(s)

    def test_the_rollback_form_and_its_restore_choice(self):
        (self.state / 'backups' / 'databases-20261113-000000').mkdir(parents=True)
        b = Browser(web.App(port=8808))
        _, _, text = b.get('')
        self.assertIn('action="rollback"', text)
        self.assertIn(update.ROLLBACK.format(version='0.9.0'), text)
        self.assertIn('name="RESTORE_DATABASES"', text)
        calls = []
        with mock.patch.object(engine.Engine, 'rollback', lambda e, restore=False: calls.append(restore) or True):
            status, headers, _ = b.post('rollback', {'RESTORE_DATABASES': 'yes'})
            b.settle('updating')
        self.assertEqual((status, headers['Location']), (303, f'/{b.app.key}/updating'))
        self.assertEqual(calls, [True])

    def test_two_rollbacks_sent_at_once_run_once(self):
        # both once got past the check that one runs, and both ran on the later one's engine
        b = Browser(web.App(port=8810))
        ran = []

        class Slow(web.Session):
            def __init__(self, *args):
                time.sleep(0.2)
                super().__init__(*args)

        def rollback(e, restore=False):
            ran.append(e)
            time.sleep(0.3)
            return True
        with mock.patch.object(web, 'Session', Slow), mock.patch.object(engine.Engine, 'rollback', rollback):
            posts = [threading.Thread(target=b.post, args=('rollback', {})) for _ in range(2)]
            for post in posts:
                post.start()
            for post in posts:
                post.join(30)
            b.settle('updating')
        self.assertEqual(ran, [b.app.after.e])

    def test_no_copy_no_restore_choice(self):
        b = Browser(web.App(port=8809))
        _, _, text = b.get('')
        self.assertIn('action="rollback"', text)
        self.assertNotIn('name="RESTORE_DATABASES"', text)


class EarlyInstall(BackgroundWork, FlowFixture):
    """An install setup 1.3.0 or before made: the page after setup offers the update, as for any older version, and
    its Update sends it through the steps once, where it is taken over."""

    def test_its_update_says_go_through_the_steps_once(self):
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 0, out)
        s = engine.load_settings()
        early = dict({k: v for k, v in s.items() if k not in ('install_id', 'image', 'local')}, version='0.9.0')
        engine.save_settings(early)
        compose = (self.state / 'compose.yaml').read_text()
        started = self.started_count
        b = Browser(web.App(port=8812))
        _, _, text = b.get('')
        self.assertIn(update.READY.format(version=engine.setup_version()), text)
        b.post('update', {})
        _, _, text = b.settle('updating')
        self.assertIn('This install was made by setup 1.3.0 or before: press Change your answers and go through the '
                      'steps once; your answers and stored copies are kept.', ' '.join(text.split()))
        self.assertEqual(engine.load_settings(), early)
        self.assertEqual((self.state / 'compose.yaml').read_text(), compose)
        self.assertEqual(self.started_count, started)


class Fixtures(unittest.TestCase):
    """What every page test stands on."""

    def test_no_page_test_reads_a_real_state_folder(self):
        # Security's and Pieces' pages once read state/ of a folder ./setup had set up (or an exported
        # AIOSUBTITLES_STATE), with its secrets, and ran docker against its running service
        self.assertEqual(engine.STATE, Path(tests.STATE))
        self.assertTrue(engine.STATE.is_dir())
        self.assertFalse(engine.STATE.is_relative_to(REPO))

    def test_a_page_test_ends_after_its_background_work(self):
        # work still running when a page test ended once went on after the fakes were gone
        seen = []

        class Ends(Personal):
            def test_it(self):
                web.Session('yourself', {}).start_job('install', lambda: (time.sleep(0.3), seen.append(engine.STATE)))
        case, result = Ends('test_it'), unittest.TestResult()
        case.run(result)
        self.assertEqual((result.errors, result.failures), ([], []))
        self.assertEqual(seen, [case.state])                  # done while the test's own state folder was in place

    def test_work_that_doesnt_end_fails_its_test(self):
        release = threading.Event()
        self.addCleanup(release.set)

        class Hangs(Personal):
            JOB_WAIT = 0.2

            def test_it(self):
                web.Session('yourself', {}).start_job('install', lambda: release.wait(10))
        result = unittest.TestResult()
        Hangs('test_it').run(result)
        self.assertEqual(len(result.failures), 1, result.errors)
        self.assertIn('background work still running 0.2 s after the test', result.failures[0][1])


if __name__ == '__main__':
    unittest.main()
