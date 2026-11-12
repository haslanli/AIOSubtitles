"""A whole ./setup run against a stand-in AIOStreams (HTTP) with Docker stubbed out: install, re-run, uninstall."""
import base64
import contextlib
import io
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from installer import dockerenv, wizard
from subsync import explain, realdebrid

UUID = '1a2b3c4d-1234-4abc-8def-0123456789ab'
PASSWORD = 'viewer-secret-password'
PUBLIC = 'https://aio.example.com'


class FakeAios:
    """Just enough of AIOStreams v2.37: status, the user API, sign-in, settings, and the addon routes."""

    def __init__(self, gated=False):
        self.users = {UUID: {'password': PASSWORD, 'config': {
            'services': [{'id': 'torbox', 'enabled': True, 'credentials': {'apiKey': 'tb'}},
                         {'id': 'nzbdav', 'enabled': True, 'credentials': {}}],
            'presets': [{'type': 'zilean', 'instanceId': 'zil', 'enabled': True, 'options': {'name': 'Zilean'}},
                        {'type': 'newznab', 'instanceId': 'nzb', 'enabled': True, 'options': {'name': 'Geek'}}],
            'failover': {'enabled': True}, 'preloadStreams': {'enabled': True}}}}
        self.gated, self.signed_in, self.subsync_up, self.throttle = gated, False, False, 0
        self.head_check = True      # stock 2.37: HEAD /api/v1/user?uuid= says whether a config exists
        self.sizes = [1000]         # the ready files the sync config lists
        self.setting = {'*': 300}
        self.log = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                if body.get('_cookie'):
                    self.send_header('Set-Cookie', 'session=1; Path=/')
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self):
                h = self.headers.get('Authorization', '')
                if not h.startswith('Basic '):
                    return None
                uuid, _, pw = base64.b64decode(h[6:]).decode().partition(':')
                u = outer.users.get(uuid)
                return uuid if u and u['password'] == pw else False

            def _body(self):
                n = int(self.headers.get('Content-Length') or 0)
                return json.loads(self.rfile.read(n) or b'{}')

            def do_HEAD(self):
                u = urllib.parse.urlsplit(self.path)
                outer.log.append(('HEAD', u.path, None))
                code = 404
                if u.path == '/api/v1/user' and outer.head_check:
                    code = 200 if urllib.parse.parse_qs(u.query).get('uuid', [''])[0] in outer.users else 400
                self.send_response(code)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                outer.log.append(('GET', path, self.headers.get('User-Agent', '')))
                if path == '/api/v1/status':
                    return self._json(200, {'success': True, 'data': {'version': '2.37.0', 'settings': {
                        'baseUrl': PUBLIC, 'forced': {'proxy': {'enabled': None}}, 'jellyfin': {'enabled': True},
                        'presets': [{'ID': 'custom', 'OPTIONS': [{'id': 'timeout', 'default': 7000}]},
                                    {'ID': 'opensubtitles', 'OPTIONS': [{'id': 'timeout', 'default': 7000}]}]}}})
                if path == '/api/v1/user':
                    uuid = self._auth()
                    if not uuid:
                        return self._json(401, {'success': False, 'error': {'code': 'USER_INVALID_DETAILS', 'message': 'Invalid'}})
                    return self._json(200, {'success': True, 'data': {'userData': outer.users[uuid]['config'],
                                                                      'encryptedPassword': 'ENC-' + uuid[:4]}})
                if path == '/api/v1/dashboard/settings':
                    return self._json(200, {'success': True, 'data': {'settings': [
                        {'key': 'resources.cache.subtitle.ttl', 'value': outer.setting, 'source': 'database', 'env': 'SUBTITLE_CACHE_TTL'}]}})
                parts = path.split('/')
                if len(parts) > 4 and parts[1] == 'stremio':
                    uuid, resource = parts[2], parts[4]
                    if resource == 'subtitles':
                        outer.subtitle_calls = getattr(outer, 'subtitle_calls', 0) + 1
                        return self._json(200, {'subtitles': []})
                    if resource == 'stream':
                        data = self.headers.get('User-Agent', '').startswith('AIO')
                        rows = []
                        for size in outer.sizes:
                            row = {'url': f'{PUBLIC}/api/v1/debrid/playback/x/file-{size}.mkv', 'behaviorHints': {'videoSize': size}}
                            if data:
                                row['streamData'] = {'type': 'debrid', 'size': size, 'service': {'id': 'torbox', 'cached': True}}
                            rows.append(row)
                        return self._json(200, {'streams': rows})
                return self._json(404, {'success': False})

            def do_PUT(self):
                if outer.throttle > 0:   # like AIOStreams' user API limiter: 429 with Retry-After
                    outer.throttle -= 1
                    self.rfile.read(int(self.headers.get('Content-Length') or 0))
                    self.send_response(429)
                    self.send_header('Retry-After', '1')
                    self.send_header('Content-Length', '2')
                    self.end_headers()
                    self.wfile.write(b'{}')
                    return
                uuid = self._auth()
                body = self._body()
                outer.log.append(('PUT', uuid, None))
                if not uuid:
                    return self._json(401, {'success': False, 'error': {'code': 'USER_INVALID_DETAILS'}})
                if outer.gated and not self.headers.get('Cookie'):
                    return self._json(403, {'success': False, 'error': {'code': 'FORBIDDEN', 'message': 'not allowed'}})
                if not outer.manifests_ok(body['config']):
                    return self._json(400, {'success': False, 'error': {'code': 'USER_INVALID_CONFIG',
                                                                        'message': 'Failed to fetch manifest for subsync: fetch failed'}})
                outer.users[uuid]['config'] = body['config']
                return self._json(200, {'success': True, 'data': {'uuid': uuid}})

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                body = self._body()
                outer.log.append(('POST', path, None))
                if path == '/api/v1/auth/login':
                    ok = body == {'username': 'admin', 'password': 'adminpw'}
                    return self._json(200 if ok else 401, {'success': ok, 'data': {'isAdmin': True}, '_cookie': ok})
                if outer.gated and not self.headers.get('Cookie'):
                    return self._json(403, {'success': False, 'error': {'code': 'FORBIDDEN', 'message': 'not allowed'}})
                if not outer.manifests_ok(body['config']):
                    return self._json(400, {'success': False, 'error': {'code': 'USER_INVALID_CONFIG', 'message': 'fetch failed'}})
                uuid = f'9{len(outer.users)}aaaaaa-1234-4abc-8def-0123456789ab'
                outer.users[uuid] = {'password': body['password'], 'config': body['config']}
                return self._json(201, {'success': True, 'data': {'uuid': uuid, 'encryptedPassword': 'ENC-' + uuid[:4]}})

            def do_DELETE(self):
                uuid = self._auth()
                outer.users.pop(uuid, None)
                return self._json(200, {'success': True})

            def do_PATCH(self):
                body = self._body()
                outer.setting = body['resources.cache.subtitle.ttl']
                return self._json(200, {'success': True})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def manifests_ok(self, config):
        """Like AIOStreams: a config listing a custom addon is saved only if that addon's manifest answers."""
        return self.subsync_up or not any(p.get('type') == 'custom' for p in config.get('presets') or [])

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.aios = FakeAios()
        self.exec_calls = []
        self.built = False
        self.selftest = {'ok': True, 'container': 'mkv', 'matroska': True, 'size': 1000,
                         'text_tracks': [{'language': 'eng', 'lines': 900, 'forced': False}], 'image_tracks': 0,
                         'bytes': 700000, 'requests': 4}
        container = {'name': 'aiostreams', 'image': 'ghcr.io/viren070/aiostreams:latest', 'port': 3000, 'base_url': PUBLIC,
                     'host_network': False, 'networks': ['aio_default'], 'ips': [], 'published': []}

        def exec_in(name, cmd, stdin=None, timeout=120):
            self.exec_calls.append((cmd, stdin))
            if 'subsync.selftest' in cmd:
                return (0 if self.selftest.get('ok') else 1), json.dumps(self.selftest) + '\n', ''
            return 0, json.dumps({'subtitle_requests': getattr(self.aios, 'subtitle_calls', 0)}), ''

        self.patches = [
            mock.patch.object(wizard, 'STATE', self.state),
            mock.patch.object(wizard, 'REPO', Path(self.tmp.name)),
            mock.patch.object(dockerenv, 'docker_problem', lambda: None),
            mock.patch.object(dockerenv, 'free_gb', lambda p='/': 100.0),
            mock.patch.object(dockerenv, 'aios_containers', lambda: [container]),
            mock.patch.object(dockerenv, 'host_urls', lambda c: [self.aios.url]),
            mock.patch.object(dockerenv, 'port_in_use', lambda *a: False),
            mock.patch.object(dockerenv, 'lan_address', lambda: '192.168.1.5'),
            mock.patch.object(dockerenv, 'container_state', lambda n: 'running healthy'),
            mock.patch.object(dockerenv, 'compose', self._compose),
            mock.patch.object(dockerenv, 'run', lambda *a, **k: (0, '["sha256:built"]' if self.built else '', '')),
            mock.patch.object(dockerenv, 'exec_in', exec_in),
            mock.patch.object(wizard.Aios, 'call', self._call_public_health(wizard.Aios.call, self.aios)),
        ]
        for p in self.patches:
            p.start()

    def _compose(self, state, *args, **kw):
        if 'build' in args:
            self.built = True
        if 'up' in args:
            self.aios.subsync_up = True
        if 'down' in args:
            self.aios.subsync_up = False
        return 0, '', ''

    @staticmethod
    def _call_public_health(real, aios):
        def call(self, method, path, *a, **k):
            if self.base.startswith(PUBLIC) and path == '/health':   # the user's web server, once subsync runs
                return (200, b'ok') if aios.subsync_up else (502, b'')
            return real(self, method, path, *a, **k)
        return call

    def run_interactive(self, inputs, *extra, enters=12):
        """A run where the user types the answers, then presses Enter at most `enters` times (accepting defaults);
        after that the input ends (EOFError), so a question asked over and over stops the run instead of looping."""
        answers = iter(list(inputs) + [''] * enters)

        def typed(prompt=''):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError from None
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch('builtins.input', typed):
            code = wizard.main(list(extra))
        return code, out.getvalue()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.aios.close()
        self.tmp.cleanup()

    def run_setup(self, answers, *extra):
        path = Path(self.tmp.name) / 'answers'
        path.write_text('\n'.join(f'{k}={v}' for k, v in answers.items()) + '\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(['--answers', str(path), '--yes', *extra])
        return code, out.getvalue()

    ANSWERS = {'AIOS_CONFIG_ID': f'{PUBLIC}/stremio/{UUID}/ENC/manifest.json', 'AIOS_CONFIG_PASSWORD': PASSWORD,
               'LANGUAGES': 'English, Russian', 'TEST_READ': 'yes'}
    LINK = f'{PUBLIC}/stremio/{UUID}/ENC/manifest.json'


    def test_install_rerun_uninstall(self):
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 0, out)
        self.assertNotIn(PASSWORD, out)
        viewer = self.aios.users[UUID]['config']
        ours = [p for p in viewer['presets'] if p['type'] == 'custom']
        self.assertEqual(len(ours), 1)
        key = (wizard.read_kv(self.state / 'secrets.env'))['ADDON_KEY']
        self.assertEqual(ours[0]['options']['manifestUrl'], f'http://aiosubtitles-subsync:8790/{key}/manifest.json')
        self.assertTrue(any(p['type'] == 'opensubtitles' for p in viewer['presets']))   # had no subtitle source
        sync_id = wizard.read_kv(self.state / 'secrets.env')['SYNC_UUID']
        sync = self.aios.users[sync_id]['config']
        self.assertEqual([p['type'] for p in sync['presets']], ['zilean', 'opensubtitles'])
        self.assertEqual([s['id'] for s in sync['services']], ['torbox'])
        self.assertIs(sync['failover']['enabled'], False)
        env = wizard.read_kv(self.state / 'subsync.env')
        self.assertEqual(env['SUBSYNC_SOURCE_MANIFEST'], f'http://aiostreams:3000/stremio/{sync_id}/ENC-{sync_id[:4]}/manifest.json')
        self.assertEqual(env['SUBSYNC_URL_REWRITE'], f'{PUBLIC}=http://aiostreams:3000')
        self.assertEqual(env['SUBSYNC_PUBLIC_BASE'], f'{PUBLIC}/subsync')
        self.assertEqual(env['SUBSYNC_LANGUAGES'], 'eng,rus')
        self.assertEqual(env['SUBSYNC_SOURCE_PASSWORD'], wizard.read_kv(self.state / 'secrets.env')['SYNC_PASSWORD'])
        env_text = ' '.join((self.state / 'subsync.env').read_text().replace('\n# ', ' ').split())
        self.assertIn(explain.SETTINGS['SUBSYNC_LANGUAGES'][1] + ' SUBSYNC_LANGUAGES=eng,rus', env_text)
        self.assertIn('ports: ["127.0.0.1:8790:8790"]', (self.state / 'compose.yaml').read_text())
        for name in ('secrets.env', 'subsync.env', 'settings.json'):
            self.assertEqual(stat.S_IMODE(os.stat(self.state / name).st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        backups = list((self.state / 'backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())['config']['presets'][0]['type'], 'zilean')
        selftest = [c for c in self.exec_calls if 'subsync.selftest' in c[0]]
        self.assertEqual(selftest[0][1], f'{PUBLIC}/api/v1/debrid/playback/x/file-1000.mkv\n')    # the link goes in on stdin
        self.assertIn('--size', selftest[0][0])
        listing_agents = {ua for m, p, ua in self.aios.log if m == 'GET' and '/stream/' in str(p)}
        self.assertTrue(all(ua.startswith('AIO') for ua in listing_agents))
        self.assertIn('reads 1 MB', out.replace('Read 0.7 MB', 'reads 1 MB'))

        # a re-run with all answers kept: the viewer config is not saved again, the sync config is updated in place
        puts_before = sum(1 for m, *_ in self.aios.log if m == 'PUT')
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no'})
        self.assertEqual(code, 0, out)
        self.assertIn('no change (subsync is already in it)', out)
        self.assertEqual(wizard.read_kv(self.state / 'secrets.env')['SYNC_UUID'], sync_id)
        self.assertEqual(sum(1 for m, *_ in self.aios.log if m == 'PUT') - puts_before, 1)   # only the sync config
        self.assertEqual(wizard.read_kv(self.state / 'subsync.env')['SUBSYNC_LANGUAGES'], 'eng,rus')
        self.assertEqual(len(list((self.state / 'backups').iterdir())), 1)

        # uninstall: subsync out of the viewer config, its own config deleted, state files gone
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'CONFIRM': 'yes'}, '--uninstall')
        self.assertEqual(code, 0, out)
        self.assertEqual([p['type'] for p in self.aios.users[UUID]['config']['presets']], ['zilean', 'newznab'])   # as before
        self.assertNotIn(sync_id, self.aios.users)
        self.assertFalse((self.state / 'secrets.env').exists())

    def test_an_unknown_config_id_is_caught_before_the_password(self):
        # test 13: a well-formed but unknown ID used to ask for the password forever
        code, out = self.run_interactive(['00000000-0000-4000-8000-000000000000', self.LINK, PASSWORD])
        self.assertEqual(code, 0, out)
        self.assertIn('There is no config with that ID on this AIOStreams', out)
        self.assertEqual(out.count('Its password'), 1)

    def test_without_the_head_check_the_id_is_taken_as_given(self):
        self.aios.head_check = False                                   # an AIOStreams without it: no false "unknown"
        code, out = self.run_interactive([self.LINK, PASSWORD])
        self.assertEqual(code, 0, out)
        self.assertNotIn('There is no config with that ID', out)

    def test_an_empty_password_goes_back_to_the_config_id(self):
        code, out = self.run_interactive([self.LINK, 'wrong-password', '', self.LINK, PASSWORD])
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count('Config ID (or paste your Stremio link)'), 2)
        self.assertNotIn('wrong-password', out)

    def test_each_question_shows_its_explanation_and_again_on_a_question_mark(self):
        code, out = self.run_interactive(['?', self.LINK, PASSWORD])
        self.assertEqual(code, 0, out)
        flat = ' '.join(out.split())
        self.assertEqual(flat.count('part of its install link (…/stremio/<ID>/…)'), 2)     # shown, then shown again
        self.assertIn('Each extra language adds those downloads', flat)

    def test_the_test_read_defaults_to_no(self):
        answers = dict(self.ANSWERS)
        del answers['TEST_READ']
        code, out = self.run_setup(answers)
        self.assertEqual(code, 0, out)
        self.assertEqual([c for c in self.exec_calls if 'subsync.selftest' in c[0]], [])   #
        self.assertIn('Do the test read now? [y/n]', out)

    def test_the_test_read_takes_the_smallest_ready_file_with_subsync_s_own_cap(self):
        self.aios.sizes = [102_682_361_361, 1_511_641_761]             # a 102.7 GB remux was listed first
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 0, out)
        cmd, stdin = next(c for c in self.exec_calls if 'subsync.selftest' in c[0])
        self.assertEqual(stdin, f'{PUBLIC}/api/v1/debrid/playback/x/file-1511641761.mkv\n')
        self.assertNotIn('--cap-mb', cmd)                              # subsync's own index cap (16 MB)

    def test_the_test_read_skips_files_listed_with_an_impossible_size(self):
        # Oracle, 2026-11-10: Torrentio listed 1679 and 229376 bytes for 3 GB files, and the size check refused them
        self.aios.sizes = [1679, 229_376, 102_682_361_361, 1_511_641_761]
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 0, out)
        cmd, stdin = next(c for c in self.exec_calls if 'subsync.selftest' in c[0])
        self.assertEqual(stdin, f'{PUBLIC}/api/v1/debrid/playback/x/file-1511641761.mkv\n')

    def test_an_index_over_subsync_s_cap_is_a_note_not_a_failure(self):
        self.selftest = {'ok': False, 'error': 'cap', 'bytes': 262160, 'requests': 2,
                         'detail': 'its index is larger than subsync reads (16 MB): subsync keeps the original subtitles for this file'}
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 0, out)
        self.assertNotIn('The test read failed', out)
        self.assertIn('its index is larger than subsync reads', out)

    def test_closing_text(self):
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 0, out)
        self.assertNotIn(f'/stremio/{UUID}/ENC-', out)                 # no install links with the encrypted password
        self.assertIn('JELLYFIN_SUBTITLE_TTL', out)                    # AIOMetadata users are told
        self.assertIn('docker restart', out)
        self.assertIn('a few minutes', out)                            # the build took 3, not 10

    def test_the_web_server_rule_is_shown_only_while_it_is_needed(self):
        code, out = self.run_setup(self.ANSWERS)
        self.assertIn('Your web server needs one rule', out)
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no'})
        self.assertEqual(code, 0, out)
        self.assertNotIn('already built: a few seconds', out)          # an update rebuilds: minutes, not seconds
        self.assertNotIn('Your web server needs one rule', out)
        self.assertIn('already reaches subsync', out)

    def test_uninstall_says_what_stays(self):
        self.run_setup(self.ANSWERS)
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'CONFIRM': 'yes'}, '--uninstall')
        self.assertEqual(code, 0, out)
        self.assertIn('docker image rm aiosubtitles/subsync:', out)
        self.assertIn('state/backups', out)
        self.assertIn('web server', out)

    def test_optional_subtitle_source_keys_reach_subsync_and_are_never_shown(self):
        # SubDL and Wyzie are searched by subsync itself when the user gives a free key
        code, out = self.run_setup(dict(self.ANSWERS, SUBDL_KEY='subdl-secret-key', WYZIE_KEY='wyzie-secret-key'))
        self.assertEqual(code, 0, out)
        env = wizard.read_kv(self.state / 'subsync.env')
        self.assertEqual((env['SUBSYNC_SUBDL_KEY'], env['SUBSYNC_WYZIE_KEY']), ('subdl-secret-key', 'wyzie-secret-key'))
        self.assertNotIn('subdl-secret-key', out)
        self.assertNotIn('wyzie-secret-key', out)
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no'})   # a re-run keeps them
        self.assertEqual(wizard.read_kv(self.state / 'subsync.env')['SUBSYNC_SUBDL_KEY'], 'subdl-secret-key')

    def test_without_keys_nothing_is_written(self):
        self.run_setup(self.ANSWERS)
        env = wizard.read_kv(self.state / 'subsync.env')
        self.assertNotIn('SUBSYNC_SUBDL_KEY', env)
        self.assertNotIn('SUBSYNC_WYZIE_KEY', env)

    def test_short_rate_limit_is_waited_out(self):
        self.aios.throttle = 2
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.aios.throttle, 0)

    def test_a_rerun_keeps_settings_added_by_hand(self):
        self.run_setup(self.ANSWERS)
        with open(self.state / 'subsync.env', 'a') as f:
            f.write('SUBSYNC_MAX_TRIES=9\nSUBSYNC_EXTRA_SOURCES=\nSUBSYNC_LANGUAGES=fre\n')
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no'})
        self.assertEqual(code, 0, out)
        text = (self.state / 'subsync.env').read_text()
        self.assertIn('SUBSYNC_MAX_TRIES=9\n', text)
        self.assertIn('SUBSYNC_EXTRA_SOURCES=\n', text)
        self.assertEqual(text.count('SUBSYNC_LANGUAGES='), 1)
        self.assertIn('SUBSYNC_LANGUAGES=eng,rus\n', text)
        flat = ' '.join(text.replace('\n# ', ' ').split())
        self.assertIn(explain.SETTINGS['SUBSYNC_MAX_TRIES'][1] + ' SUBSYNC_MAX_TRIES=9', flat)
        self.assertIn('Kept the lines of state/subsync.env that ./setup doesn\'t set: SUBSYNC_MAX_TRIES, SUBSYNC_EXTRA_SOURCES', out)

    def test_real_debrid_only_stops_setup_with_the_reason(self):
        self.aios.users[UUID]['config']['services'] = [{'id': 'realdebrid', 'enabled': True, 'credentials': {'apiKey': 'rd'}},
                                                       {'id': 'nzbdav', 'enabled': True, 'credentials': {}}]
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 1, out)
        self.assertIn(realdebrid.REAL_DEBRID_ONLY, out)
        self.assertEqual([m for m, *_ in self.aios.log if m in ('PUT', 'POST')], [])   # nothing saved or created
        self.assertEqual(list(self.aios.users), [UUID])

    def test_real_debrid_beside_another_service_never_reaches_subsync(self):
        config = self.aios.users[UUID]['config']
        config['services'].append({'id': 'realdebrid', 'enabled': True, 'credentials': {'apiKey': 'rd'}})
        config['presets'].append({'type': 'custom', 'instanceId': 'tio', 'enabled': True, 'options': {
            'name': 'Torrentio RD', 'manifestUrl': 'https://torrentio.example/realdebrid=KEY/manifest.json'}})
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 0, out)
        sync = self.aios.users[wizard.read_kv(self.state / 'secrets.env')['SYNC_UUID']]['config']
        self.assertEqual([s['id'] for s in sync['services']], ['torbox'])
        self.assertNotIn('Torrentio RD', json.dumps(sync))
        self.assertIn('Real-Debrid is never used: subsync reads through TorBox only', out)
        self.assertIn('"Torrentio RD" left out: its link names Real-Debrid', out)
        self.assertNotIn('index-only', out)

    def test_without_real_debrid_nothing_is_said_about_it(self):
        self.aios.users[UUID]['config']['presets'].append({'type': 'custom', 'instanceId': 'mf', 'enabled': True, 'options': {
            'name': 'Encrypted', 'manifestUrl': 'https://mf.example/D-q83hf8Hf73hf73Hfh38fh3/manifest.json'}})
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 0, out)
        self.assertNotIn('Real-Debrid is never used', out)
        self.assertIn('"Encrypted" left out', out)

    def test_the_self_test_never_takes_a_real_debrid_row(self):
        row = {'url': 'http://x', 'streamData': {'type': 'debrid', 'service': {'id': 'realdebrid', 'cached': True}}}
        self.assertFalse(wizard.readable(row))
        row['streamData']['service']['id'] = 'torbox'
        self.assertTrue(wizard.readable(row))

    def test_wrong_password_is_asked_again_then_stops(self):
        code, out = self.run_setup(dict(self.ANSWERS, AIOS_CONFIG_PASSWORD='nope'))
        self.assertEqual(code, 1)
        self.assertIn("don't match", out)
        self.assertNotIn('nope', out.replace('(from the answers file)', ''))

    def test_gated_instance_signs_in_and_cache_setting(self):
        self.aios.gated = True
        code, out = self.run_setup(dict(self.ANSWERS, AIOS_USERNAME='admin', AIOS_PASSWORD='adminpw', CACHE='yes'))
        self.assertEqual(code, 0, out)
        self.assertIn('Signed in', out)
        self.assertEqual(self.aios.setting, {'*': 300, 'aiosubtitles-subsync': -1})
        self.assertNotIn('adminpw', out)


class RealDebridGate(unittest.TestCase):
    def setUp(self):
        real = socket.create_connection

        def local_only(address, *args, **kwargs):   # fails before any lookup or packet leaves this machine
            if address[0] not in ('127.0.0.1', 'localhost', '::1'):
                raise AssertionError(f'a connection to {address[0]} was opened')
            return real(address, *args, **kwargs)
        guard = mock.patch('socket.create_connection', local_only)
        guard.start()
        self.addCleanup(guard.stop)

    def test_setup_never_sends_a_request_to_real_debrid(self):
        with self.assertLogs('subsync.realdebrid', 'WARNING'):
            with self.assertRaises(wizard.AiosError) as caught:
                wizard.Aios('https://api.real-debrid.com').call('GET', '/rest/1.0/user')
        self.assertIn('Real-Debrid', caught.exception.message)

    def test_setup_never_follows_a_redirect_to_real_debrid(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(307)
                self.send_header('Location', 'https://1.download.real-debrid.com/d/x')
                self.send_header('Content-Length', '0')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with self.assertLogs('subsync.realdebrid', 'WARNING'):
            with self.assertRaises(wizard.AiosError):
                wizard.Aios(f'http://127.0.0.1:{server.server_address[1]}').call('GET', '/api/v1/status')


if __name__ == '__main__':
    unittest.main()
