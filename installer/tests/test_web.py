"""The setup page: 127.0.0.1 only, a one-time key, the Host check, the site's
security header, each piece of work once, the idle stop, and both routes and the page after setup driven end to end
through the real engine (a stand-in AIOStreams, Docker faked)."""
import ast
import html
import re
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from installer import engine, update, web
from installer.tests.test_flow import PASSWORD, PUBLIC, UUID, FlowFixture
from installer.tests.test_hosting import ADDRESS, RouteFixture

REPO = Path(__file__).resolve().parent.parent.parent


def site_headers():
    """subsync's public pages' security header, read from its source (public.py needs cryptography to import)."""
    tree = ast.parse((REPO / 'subsync' / 'subsync' / 'public.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', None) == 'PAGE_HEADERS':
            return ast.literal_eval(node.value)
    raise AssertionError('PAGE_HEADERS not found')


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
        wanted = site_headers()
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
        app = web.App(idle_s=0.3)
        server = web.start(app)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], '127.0.0.1')
        with urllib.request.urlopen(f'http://127.0.0.1:{app.port}/{app.key}/', timeout=5) as r:
            self.assertEqual(r.status, 200)
        self.assertTrue(app.finished.wait(5))


class Personal(FlowFixture):
    """Install it for yourself, through the page."""

    def setUp(self):
        super().setUp()
        self.app = web.App(port=8802)
        self.b = Browser(self.app)

    def walk_to_review(self):
        b = self.b
        self.assertEqual(b.get('')[0], 200)
        status, headers, _ = b.post('', {'ROUTE': 'yourself'})
        self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/machine'))
        _, _, text = b.get('machine')
        self.assertIn('Docker and Docker Compose work', text)
        _, _, text = b.follow('machine', {'go': 'next'})
        self.assertIn('Viewers reach it at', text)
        _, _, text = b.follow('aios', {'go': 'next'})
        self.assertIn('Config ID (or paste your Stremio link)', text)
        _, _, text = b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'AIOS_CONFIG_PASSWORD': 'wrong', 'go': 'next'})
        self.assertIn("don't match", text)
        _, _, text = b.post('config', {'AIOS_CONFIG_ID': self.LINK, 'AIOS_CONFIG_PASSWORD': PASSWORD, 'go': 'next'})
        self.assertIn('Config found', text)
        _, _, text = b.follow('config', {'AIOS_CONFIG_ID': UUID, 'AIOS_CONFIG_PASSWORD': '', 'go': 'next'})
        self.assertIn('Which subtitle languages should be synced?', text)
        _, _, text = b.follow('choices', {'LANGUAGES': ['eng', 'rus', ''], 'PUBLIC_ADDRESS': f'{PUBLIC}/subsync',
                                          'ADD_OPENSUBTITLES': 'yes', 'go': 'next'})
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

    def test_a_later_step_before_its_turn_goes_back_to_the_first_one_left(self):
        self.b.post('', {'ROUTE': 'yourself'})
        status, headers, _ = self.b.get('review')
        self.assertEqual((status, headers['Location']), (303, f'/{self.app.key}/machine'))

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
        _, _, text = b.post('remove', {'AIOS_CONFIG_PASSWORD': 'wrong', 'CONFIRM': 'yes'})
        self.assertIn("don't match", text)
        self.assertTrue((self.state / 'settings.json').exists())
        _, _, text = b.post('remove', {'AIOS_CONFIG_PASSWORD': PASSWORD, 'CONFIRM': 'yes', 'REMOVE_OPENSUBTITLES': 'yes'})
        self.assertIn('These stay, in case you set it up again', text)
        self.assertFalse((self.state / 'settings.json').exists())
        self.assertFalse(any(PASSWORD in page for page in b.pages))

    def test_change_your_answers_opens_the_steps_filled_in(self):
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        app = web.App(port=8804)
        b = Browser(app)
        b.get('')
        status, headers, _ = b.post('answers', {})
        self.assertEqual(status, 303)
        _, _, text = b.get('')
        self.assertIn('How do you want to use it?', text)
        b.post('', {'ROUTE': 'yourself'})
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

    def test_a_stopped_step_leaves_no_turning_ring(self):
        s = web.Session('yourself', {})

        def work():
            s.e.out.working('Building subsync (the first time takes a few minutes) ...')
            raise engine.Stop('Building subsync failed.')
        s.run('install', work)
        self.assertNotIn('now', [kind for kind, _, _ in s.rows['install']])


class Hosting(RouteFixture):
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

    def test_a_public_instance_through_the_page(self):
        app = web.App(port=8805)
        b = Browser(app)
        b.post('', {'ROUTE': 'host'})
        b.get('machine')
        _, _, text = b.follow('machine', {'go': 'next'})
        self.assertIn('Web address', text)
        _, _, text = b.post('address', {'HOST_ADDRESS': 'http://subs.example.com', 'SUBSYNC_TRUSTED_PROXY': 'gateway',
                                        'go': 'next'})
        self.assertIn('with https, like https://subtitles.example.com', text)
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


if __name__ == '__main__':
    unittest.main()


class Rollback(FlowFixture):
    """The page after setup offers going back while an earlier version is recorded (Q5)."""

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

    def test_no_copy_no_restore_choice(self):
        b = Browser(web.App(port=8809))
        _, _, text = b.get('')
        self.assertIn('action="rollback"', text)
        self.assertNotIn('name="RESTORE_DATABASES"', text)
