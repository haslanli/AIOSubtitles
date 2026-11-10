"""A whole ./setup run against a stand-in AIOStreams (HTTP) with Docker stubbed out: install, re-run, uninstall."""
import base64
import contextlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from installer import dockerenv, wizard

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
                        row = {'url': f'{PUBLIC}/api/v1/debrid/playback/x/file.mkv', 'behaviorHints': {'videoSize': 1000}}
                        if data:
                            row['streamData'] = {'type': 'debrid', 'size': 1000, 'service': {'id': 'torbox', 'cached': True}}
                        return self._json(200, {'streams': [row]})
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
        container = {'name': 'aiostreams', 'image': 'ghcr.io/viren070/aiostreams:latest', 'port': 3000, 'base_url': PUBLIC,
                     'host_network': False, 'networks': ['aio_default'], 'ips': [], 'published': []}

        def exec_in(name, cmd, stdin=None, timeout=120):
            self.exec_calls.append((cmd, stdin))
            if 'subsync.selftest' in cmd:
                return 0, json.dumps({'ok': True, 'matroska': True, 'size': 1000, 'text_tracks': [{'language': 'eng', 'lines': 900, 'forced': False}],
                                      'image_tracks': 0, 'bytes': 700000, 'requests': 4}) + '\n', ''
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
            mock.patch.object(dockerenv, 'run', lambda *a, **k: (0, '', '')),
            mock.patch.object(dockerenv, 'exec_in', exec_in),
            mock.patch.object(wizard.Aios, 'call', self._call_public_health(wizard.Aios.call)),
        ]
        for p in self.patches:
            p.start()

    def _compose(self, state, *args, **kw):
        if 'up' in args:
            self.aios.subsync_up = True
        if 'down' in args:
            self.aios.subsync_up = False
        return 0, '', ''

    @staticmethod
    def _call_public_health(real):
        def call(self, method, path, *a, **k):
            if self.base.startswith(PUBLIC) and path == '/health':
                return 200, b'ok'
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
        self.assertIn('ports: ["127.0.0.1:8790:8790"]', (self.state / 'compose.yaml').read_text())
        for name in ('secrets.env', 'subsync.env', 'settings.json'):
            self.assertEqual(stat.S_IMODE(os.stat(self.state / name).st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        backups = list((self.state / 'backups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())['config']['presets'][0]['type'], 'zilean')
        selftest = [c for c in self.exec_calls if 'subsync.selftest' in c[0]]
        self.assertEqual(selftest[0][1], f'{PUBLIC}/api/v1/debrid/playback/x/file.mkv\n')    # the link goes in on stdin
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

    def test_short_rate_limit_is_waited_out(self):
        self.aios.throttle = 2
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.aios.throttle, 0)

    def test_wrong_password_is_asked_again_then_stops(self):
        code, out = self.run_setup(dict(self.ANSWERS, AIOS_CONFIG_PASSWORD='nope'))
        self.assertEqual(code, 1)
        self.assertIn("don't match", out)
        self.assertNotIn('nope', out.replace('(from the answers file)', ''))

    def test_gated_instance_signs_in_and_cache_setting(self):
        self.aios.gated = True
        code, out = self.run_setup(dict(self.ANSWERS, AIOS_USERNAME='admin', AIOS_PASSWORD='adminpw', CACHE='yes'))
        self.assertEqual(code, 0, out)
        self.assertIn('sign in', out)
        self.assertEqual(self.aios.setting, {'*': 300, 'aiosubtitles-subsync': -1})
        self.assertNotIn('adminpw', out)


if __name__ == '__main__':
    unittest.main()
