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

from installer import dockerenv, engine, wizard
from subsync import explain, realdebrid

UUID = '1a2b3c4d-1234-4abc-8def-0123456789ab'
PASSWORD = 'viewer-secret-password'
PUBLIC = 'https://aio.example.com'


SERVICE_NAMES = {'aiostreams': 'AIOStreams', 'nzbdav': 'NzbDAV', 'altmount': 'AltMount', 'torbox': 'TorBox',
                 'stremio_nntp': 'Stremio NNTP', 'stremthru_newz': 'StremThru Newz', 'easynews': 'Easynews',
                 'realdebrid': 'Real-Debrid'}


def refusal(config):
    """Why AIOStreams refuses a config when it is created or saved (None: it doesn't), as far as its check of each
    addon's own services goes (Preset.getUsableServices, v2.35.8; the rule of subsync/tests/helpers.py): every service
    an enabled addon names must be on the config's list, enabled, with credentials; an empty choice leaves it none."""
    services = {s.get('id'): s for s in (config or {}).get('services') or [] if isinstance(s, dict)}
    for preset in (config or {}).get('presets') or []:
        chosen = (preset.get('options') or {}).get('services')
        if not preset.get('enabled') or not isinstance(chosen, list):
            continue
        name = (preset.get('options') or {}).get('name') or preset.get('type')
        for service in chosen:
            found = services.get(service)
            if not found or not found.get('enabled') or found.get('credentials') is None:
                return (f'You have specified {SERVICE_NAMES.get(service, service)} in your configuration for {name}, '
                        'but it is not enabled or has missing credentials')
        if not chosen:
            return f'{name} requires at least one usable service, but none were found.'
    return None


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
                refused = refusal(body['config'])
                if refused:
                    return self._json(400, {'success': False, 'error': {'code': 'USER_INVALID_CONFIG', 'message': refused}})
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
                refused = refusal(body['config'])
                if refused:
                    return self._json(400, {'success': False, 'error': {'code': 'USER_INVALID_CONFIG', 'message': refused}})
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


class FlowFixture(unittest.TestCase):
    """A stand-in AIOStreams with Docker stubbed out (no tests here: test_web.py drives the page on it too)."""

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
            mock.patch.object(engine, 'STATE', self.state),
            mock.patch.object(engine, 'REPO', Path(self.tmp.name)),
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
            mock.patch.object(engine.Aios, 'call', self._call_public_health(engine.Aios.call, self.aios)),
        ]
        for p in self.patches:
            p.start()

    def _compose(self, state, *args, **kw):
        if 'build' in args:
            self.built = True
            self.built_count = getattr(self, 'built_count', 0) + 1
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


class Flow(FlowFixture):


    def test_install_rerun_uninstall(self):
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 0, out)
        self.assertNotIn(PASSWORD, out)
        viewer = self.aios.users[UUID]['config']
        ours = [p for p in viewer['presets'] if p['type'] == 'custom']
        self.assertEqual(len(ours), 1)
        key = (engine.read_kv(self.state / 'secrets.env'))['ADDON_KEY']
        self.assertEqual(ours[0]['options']['manifestUrl'], f'http://aiosubtitles-subsync:8790/{key}/manifest.json')
        self.assertTrue(any(p['type'] == 'opensubtitles' for p in viewer['presets']))   # had no subtitle source
        sync_id = engine.read_kv(self.state / 'secrets.env')['SYNC_UUID']
        sync = self.aios.users[sync_id]['config']
        self.assertEqual([p['type'] for p in sync['presets']], ['zilean', 'opensubtitles'])
        self.assertEqual([s['id'] for s in sync['services']], ['torbox'])
        self.assertIs(sync['failover']['enabled'], False)
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual(env['SUBSYNC_SOURCE_MANIFEST'], f'http://aiostreams:3000/stremio/{sync_id}/ENC-{sync_id[:4]}/manifest.json')
        self.assertEqual(env['SUBSYNC_URL_REWRITE'], f'{PUBLIC}=http://aiostreams:3000')
        self.assertEqual(env['SUBSYNC_PUBLIC_BASE'], f'{PUBLIC}/subsync')
        self.assertEqual(env['SUBSYNC_LANGUAGES'], 'eng,rus')
        self.assertEqual(env['SUBSYNC_SOURCE_PASSWORD'], engine.read_kv(self.state / 'secrets.env')['SYNC_PASSWORD'])
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
        self.assertEqual(engine.read_kv(self.state / 'secrets.env')['SYNC_UUID'], sync_id)
        self.assertEqual(sum(1 for m, *_ in self.aios.log if m == 'PUT') - puts_before, 1)   # only the sync config
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_LANGUAGES'], 'eng,rus')
        self.assertEqual(len(list((self.state / 'backups').iterdir())), 1)

        # uninstall: subsync out of the viewer config, its own config deleted, state files gone
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'CONFIRM': 'yes'}, '--uninstall')
        self.assertEqual(code, 0, out)
        self.assertEqual([p['type'] for p in self.aios.users[UUID]['config']['presets']], ['zilean', 'newznab'])   # as before
        self.assertNotIn(sync_id, self.aios.users)
        self.assertFalse((self.state / 'secrets.env').exists())

    def test_the_audio_limit_is_asked_only_with_the_audio_route(self):
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 0, out)
        self.assertNotIn('How much of each file', out)
        self.assertNotIn('SUBSYNC_AUDIO_BUDGET_GB', engine.read_kv(self.state / 'subsync.env'))

        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no', 'AUDIO': 'yes'})
        self.assertEqual(code, 0, out)
        self.assertIn('How much of each file', out)
        self.assertIn(explain.QUESTIONS['AUDIO_BUDGET'][1][:60], ' '.join(out.split()))
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual(env['SUBSYNC_AUDIO_BUDGET_GB'], '1')          # the recommended default
        self.assertEqual(env['SUBSYNC_AUDIO_ROUTE'], 'on')

        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no', 'AUDIO_BUDGET': '2'})
        self.assertEqual(code, 0, out)
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_AUDIO_BUDGET_GB'], '2')

        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no'})   # a re-run keeps it
        self.assertEqual(code, 0, out)
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_AUDIO_BUDGET_GB'], '2')

        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no', 'AUDIO': 'no'})
        self.assertEqual(code, 0, out)
        self.assertNotIn('SUBSYNC_AUDIO_BUDGET_GB', engine.read_kv(self.state / 'subsync.env'))
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no', 'AUDIO': 'yes'})
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_AUDIO_BUDGET_GB'], '2')   # remembered

    def test_an_audio_limit_set_by_hand_is_kept_while_the_audio_route_is_off(self):
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 0, out)
        env_path = self.state / 'subsync.env'
        env_path.write_text(env_path.read_text() + 'SUBSYNC_AUDIO_BUDGET_GB=3\n')
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no'})      # audio stays off
        self.assertEqual(code, 0, out)
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no', 'AUDIO': 'yes'})
        self.assertEqual(code, 0, out)
        self.assertEqual(engine.read_kv(env_path)['SUBSYNC_AUDIO_BUDGET_GB'], '3')

    def test_a_wrong_audio_limit_is_asked_again(self):
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no', AUDIO='yes', AUDIO_BUDGET='3'))
        self.assertNotEqual(code, 0)
        self.assertIn('0.5, 1, 2 or 4', out)

    def test_an_unknown_config_id_is_caught_before_the_password(self):
        # test 13: a well-formed but unknown ID used to ask for the password forever
        code, out = self.run_setup(dict(self.ANSWERS, AIOS_CONFIG_ID='00000000-0000-4000-8000-000000000000'))
        self.assertEqual(code, 1, out)
        self.assertIn('There is no config with that ID on this AIOStreams', out)
        self.assertFalse([1 for m, p, _ in self.aios.log if m == 'GET' and p == '/api/v1/user'])   # never asked

    def test_without_the_head_check_the_id_is_taken_as_given(self):
        self.aios.head_check = False                                   # an AIOStreams without it: no false "unknown"
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 0, out)
        self.assertNotIn('There is no config with that ID', out)

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
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual((env['SUBSYNC_SUBDL_KEY'], env['SUBSYNC_WYZIE_KEY']), ('subdl-secret-key', 'wyzie-secret-key'))
        self.assertNotIn('subdl-secret-key', out)
        self.assertNotIn('wyzie-secret-key', out)
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no'})   # a re-run keeps them
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_SUBDL_KEY'], 'subdl-secret-key')

    def test_without_keys_nothing_is_written(self):
        self.run_setup(self.ANSWERS)
        env = engine.read_kv(self.state / 'subsync.env')
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
        sync = self.aios.users[engine.read_kv(self.state / 'secrets.env')['SYNC_UUID']]['config']
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
        self.assertFalse(engine.readable(row))
        row['streamData']['service']['id'] = 'torbox'
        self.assertTrue(engine.readable(row))

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


    # ------------------------------------------------------------------ the engine as the setup page drives it

    def page_engine(self, answers):
        return engine.Engine(answers, page=True)

    def test_the_stand_in_refuses_an_addon_naming_a_service_the_config_lacks(self):
        config = {'services': [{'id': 'torbox', 'enabled': True, 'credentials': {}}],
                  'presets': [{'type': 'prowlarr', 'enabled': True, 'options': {'name': 'Prowlarr',
                                                                                'services': ['torbox', 'nzbdav']}}]}
        self.assertIn('You have specified NzbDAV in your configuration for Prowlarr', refusal(config))
        config['presets'][0]['options']['services'] = ['torbox']
        self.assertIsNone(refusal(config))

    def test_a_prowlarr_addon_naming_usenet_doesnt_stop_setup(self):
        # the sync config leaves Usenet out, so each kept addon's own Services choice loses it too
        self.aios.users[UUID]['config']['presets'].append(
            {'type': 'prowlarr', 'instanceId': 'prw', 'enabled': True,
             'options': {'name': 'Prowlarr', 'services': ['torbox', 'nzbdav']}})
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 0, out)
        sync = self.aios.users[engine.read_kv(self.state / 'secrets.env')['SYNC_UUID']]['config']
        self.assertEqual([p['options']['services'] for p in sync['presets'] if p['type'] == 'prowlarr'], [['torbox']])

    def test_a_1_0_0_install_opens_with_its_answers(self):
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 0, out)
        env_path = self.state / 'subsync.env'
        env_path.write_text(env_path.read_text() + 'SUBSYNC_DAILY_BUDGET_GB=20\nSUBSYNC_MAX_TRIES=9\n')
        filled = self.page_engine({}).prefill()
        self.assertTrue(filled['installed'])
        self.assertEqual(filled['AIOS_CONFIG_ID'], UUID)
        self.assertEqual(filled['LANGUAGES'], 'English, Russian')
        self.assertEqual(filled['AUDIO'], 'no')
        self.assertEqual(filled['PUBLIC_ADDRESS'], f'{PUBLIC}/subsync')
        self.assertEqual(filled['SUBSYNC_DAILY_BUDGET_GB'], '20')
        self.assertNotIn('AIOS_CONFIG_PASSWORD', filled)                 # never kept, never shown again

    def test_the_daily_total_is_written_only_when_it_differs_from_the_default(self):
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no', SUBSYNC_DAILY_BUDGET_GB='20'))
        self.assertEqual(code, 0, out)
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_DAILY_BUDGET_GB'], '20')
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no'})       # kept on a re-run
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_DAILY_BUDGET_GB'], '20')
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no',
                                    'SUBSYNC_DAILY_BUDGET_GB': '50'})
        self.assertEqual(code, 0, out)
        self.assertNotIn('SUBSYNC_DAILY_BUDGET_GB', engine.read_kv(self.state / 'subsync.env'))
        code, out = self.run_setup({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no',
                                    'SUBSYNC_DAILY_BUDGET_GB': '0'})
        self.assertEqual(code, 1, out)
        self.assertIn('A number above 0, like 50.', out)

    def test_each_step_returns_a_problem_at_its_field(self):
        cases = [
            ({'AIOS_CONFIG_ID': 'nope'}, 'read_viewer', 'AIOS_CONFIG_ID', 'That has no config ID in it'),
            ({'AIOS_CONFIG_ID': self.LINK, 'AIOS_CONFIG_PASSWORD': 'wrong'}, 'read_viewer', 'AIOS_CONFIG_PASSWORD',
             "don't match"),
            ({'LANGUAGES': 'Klingon'}, 'choices', 'LANGUAGES', ''),
            ({'LANGUAGES': 'English', 'PUBLIC_ADDRESS': 'ftp://x'}, 'choices', 'PUBLIC_ADDRESS', ''),
            ({'LANGUAGES': 'English', 'SUBSYNC_DAILY_BUDGET_GB': '0'}, 'limits', 'SUBSYNC_DAILY_BUDGET_GB',
             'A number above 0, like 50.'),
        ]
        for answers, step, field, words in cases:
            with self.subTest(field=field):
                if step != 'read_viewer':
                    answers = dict(answers, AIOS_CONFIG_ID=self.LINK, AIOS_CONFIG_PASSWORD=PASSWORD)
                e = self.page_engine(dict(answers))
                e.check_machine()
                e.find_aios()
                if step != 'read_viewer':
                    e.read_viewer()
                if step == 'limits':
                    e.choices()
                with self.assertRaises(engine.Problem) as caught:
                    getattr(e, step)()
                self.assertEqual(caught.exception.field, field)
                self.assertIn(words, caught.exception.message)
                self.assertFalse(caught.exception.missing)

    def test_a_question_not_answered_yet_is_missing_not_wrong(self):
        e = self.page_engine({})
        e.check_machine()
        e.find_aios()
        with self.assertRaises(engine.Problem) as caught:
            e.read_viewer()
        self.assertEqual(caught.exception.field, 'AIOS_CONFIG_ID')
        self.assertTrue(caught.exception.missing)

    def test_an_address_question_when_aiostreams_isnt_found(self):
        with mock.patch.object(dockerenv, 'aios_containers', lambda: []):
            e = self.page_engine({})
            e.check_machine()
            with self.assertRaises(engine.Problem) as caught:
                e.find_aios()
            self.assertEqual((caught.exception.field, caught.exception.missing), ('AIOS_URL', True))
            e = self.page_engine({'AIOS_URL': 'http://127.0.0.1:1'})
            with self.assertRaises(engine.Problem) as caught:
                e.find_aios()
            self.assertEqual((caught.exception.field, caught.exception.missing), ('AIOS_URL', False))
            e = self.page_engine({'AIOS_URL': self.aios.url})
            e.find_aios()
            self.assertIn(('ok', 'AIOStreams 2.37.0 answers there'), [tuple(r[:2]) for r in e.out.rows])

    def test_the_real_debrid_stop_on_the_page_says_go_back(self):
        self.aios.users[UUID]['config']['services'] = [{'id': 'realdebrid', 'enabled': True, 'credentials': {}}]
        e = self.page_engine({'AIOS_CONFIG_ID': self.LINK, 'AIOS_CONFIG_PASSWORD': PASSWORD})
        e.check_machine()
        e.find_aios()
        with self.assertRaises(engine.Stop) as caught:
            e.read_viewer()
        self.assertIn("AIOSubtitles can't work with Real-Debrid only", caught.exception.message)
        self.assertEqual(caught.exception.page_fix,
                         "After adding it on AIOStreams' configure page, go back one step and continue again.")

    def test_the_status_as_the_page_after_setup_shows_it(self):
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        rows, details = self.page_engine({}).status_parts()
        self.assertEqual(rows[0], ('ok', 'container aiosubtitles-subsync: running healthy', None))
        self.assertEqual(dict(details)['AIOStreams'], PUBLIC)
        self.assertEqual(dict(details)['languages'], 'eng, rus')
        self.assertEqual(dict(details)['audio'], 'off')
        self.assertEqual(dict(details)['config'], UUID[:8] + '…')
        self.assertEqual(dict(details)['logs'], 'docker logs aiosubtitles-subsync')

    def test_removing_on_the_page(self):
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        e = self.page_engine({'AIOS_CONFIG_PASSWORD': PASSWORD, 'REMOVE_OPENSUBTITLES': 'yes', 'CONFIRM': 'yes'})
        e.remove()
        self.assertEqual([p['type'] for p in self.aios.users[UUID]['config']['presets']], ['zilean', 'newznab'])
        self.assertIn(('ok', 'Done.'), [tuple(r[:2]) for r in e.out.rows])
        self.assertTrue(any('docker image rm aiosubtitles/subsync:' in line for line in e.stays))
        self.assertFalse((self.state / 'settings.json').exists())

    def test_removing_needs_the_config_password(self):
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        e = self.page_engine({'CONFIRM': 'yes'})
        with self.assertRaises(engine.Problem) as caught:
            e.remove()
        self.assertEqual(caught.exception.field, 'AIOS_CONFIG_PASSWORD')
        self.assertTrue((self.state / 'settings.json').exists())


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
            with self.assertRaises(engine.AiosError) as caught:
                engine.Aios('https://api.real-debrid.com').call('GET', '/rest/1.0/user')
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
            with self.assertRaises(engine.AiosError):
                engine.Aios(f'http://127.0.0.1:{server.server_address[1]}').call('GET', '/api/v1/status')


if __name__ == '__main__':
    unittest.main()
