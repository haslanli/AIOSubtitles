"""The setup page: 127.0.0.1 only, a one-time key, the Host check, the site's
security header, each piece of work once, the idle stop, and both routes and the page after setup driven end to end
through the real engine (a stand-in AIOStreams, Docker faked)."""
import ast
import html
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

from installer import engine, update, web
from installer.tests.test_flow import PASSWORD, PUBLIC, UUID, FlowFixture
from installer.tests.test_hosting import ADDRESS, RouteFixture
from subsync import ui

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
        self.assertIn('Config ID (or paste your Stremio link)', text)
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


class Lengths(unittest.TestCase):
    """A body's length, on a real socket on 127.0.0.1: refused before the page reads anything."""

    def setUp(self):
        self.app = web.App()
        server = web.start(self.app)

        def stop():
            self.app.finish_in(0)
            self.app.finished.wait(5)
            server.server_close()
        self.addCleanup(stop)

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


class Rollback(FlowFixture):
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


if __name__ == '__main__':
    unittest.main()
