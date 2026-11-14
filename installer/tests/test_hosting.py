"""The hosting route: a public instance set up by ./setup."""
import base64
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import dockerenv, engine, hosting, limits, wizard

ADDRESS = 'https://subs.example.com'
SECRET_WORDS = 'kept it in state/subsync.env'


class Pieces(unittest.TestCase):
    def test_the_secret_is_32_random_bytes_as_base64url(self):
        secret = hosting.new_secret()
        self.assertEqual(len(base64.urlsafe_b64decode(secret + '=' * (-len(secret) % 4))), 32)
        self.assertNotEqual(secret, hosting.new_secret())

    def test_a_suggested_invite_code_is_easy_to_type(self):
        code = hosting.suggested_invite()
        self.assertRegex(code, r'^[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}$')

    def test_the_address_must_be_https_and_a_whole_host(self):
        self.assertEqual(hosting.check_address('https://Subs.Example.com/'), (True, '', 'https://subs.example.com'))
        for bad in ('http://subs.example.com', 'subs.example.com', 'https://subs.example.com/subsync', 'ftp://x',
                    'https://', 'https://subs.example.com?x=1'):
            with self.subTest(bad=bad):
                good, message, _ = hosting.check_address(bad)
                self.assertFalse(good)
                self.assertIn('with https, like https://subtitles.example.com', message)

    def test_the_env_passes_subsyncs_own_checks_never_has_wyzie_and_audio_only_when_chosen(self):
        answers = {'HOST_ADDRESS': ADDRESS, 'SUBSYNC_TRUSTED_PROXY': 'gateway', 'SUBSYNC_PUBLIC_INVITE': 'abcd-efgh-jkmn',
                   'SUBDL_KEY': 'sd', 'SUBSYNC_SUBSOURCE_KEY': 'ss', 'WYZIE_KEY': 'wz', 'AUDIO': 'yes',
                   'SUBSYNC_MAX_USERS': '150', 'SUBSYNC_SYNCS_AT_ONCE': 'auto'}
        env = hosting.public_env(answers, hosting.new_secret(), {'SUBSYNC_MAX_USERS': '150'})
        self.assertEqual(env['SUBSYNC_PUBLIC'], 'on')
        self.assertEqual(env['SUBSYNC_PUBLIC_BASE'], ADDRESS)
        self.assertEqual(env['SUBSYNC_MAX_USERS'], '150')
        self.assertNotIn('SUBSYNC_SYNCS_AT_ONCE', env)                 # the default: no line
        self.assertNotIn('SUBSYNC_WYZIE_KEY', env)                     # public mode refuses it
        self.assertEqual(env['SUBSYNC_AUDIO_ROUTE'], 'off')             # off unless the host chooses it
        self.assertNotIn('SUBSYNC_AUDIO_BUDGET_GB', env)
        try:     # subsync's settings need numpy, which a server's own Python may not have (the installer doesn't)
            from subsync.settings import Settings
        except ImportError as e:
            self.skipTest(f'subsync.settings needs {e.name}')
        with mock.patch('subsync.settings.default_gateway', lambda path=None: '172.17.0.1'):
            s = Settings(env)
            self.assertEqual(s.missing(), [])
            self.assertEqual(s.conflicts(), [])
        self.assertEqual((s.subdl_key, s.subsource_key, s.invite), ('sd', 'ss', 'abcd-efgh-jkmn'))

    def test_the_audio_route_and_its_limit_when_the_host_chooses_them(self):
        # A public instance may sync files without a subtitle track from their sound, after a pick
        answers = {'HOST_ADDRESS': ADDRESS, 'SUBSYNC_AUDIO_ROUTE': 'yes'}
        env = hosting.public_env(answers, hosting.new_secret(), {'SUBSYNC_AUDIO_BUDGET_GB': '2'})
        self.assertEqual((env['SUBSYNC_AUDIO_ROUTE'], env['SUBSYNC_AUDIO_BUDGET_GB']), ('on', '2'))
        self.assertNotIn('SUBSYNC_AUDIO_ON_PICK', env)                 # never written: it stays on
        default = hosting.public_env(answers, hosting.new_secret(), {'SUBSYNC_AUDIO_BUDGET_GB': '1'})
        self.assertNotIn('SUBSYNC_AUDIO_BUDGET_GB', default)            # the default: no line
        off = hosting.public_env(dict(answers, SUBSYNC_AUDIO_ROUTE='no'), hosting.new_secret(),
                                 {'SUBSYNC_AUDIO_BUDGET_GB': '2'})
        self.assertEqual(off['SUBSYNC_AUDIO_ROUTE'], 'off')
        self.assertNotIn('SUBSYNC_AUDIO_BUDGET_GB', off)                # no limit asked or written with it off
        self.assertTrue({'SUBSYNC_AUDIO_ROUTE', 'SUBSYNC_AUDIO_BUDGET_GB'} <= set(hosting.OWNED))
        try:
            from subsync.settings import Settings
        except ImportError as e:
            self.skipTest(f'subsync.settings needs {e.name}')
        with mock.patch('subsync.settings.default_gateway', lambda path=None: '172.17.0.1'):
            s = Settings(env)
        self.assertEqual(s.conflicts(), [])
        self.assertTrue(s.audio_route and s.audio_on_pick)

    def test_the_hosting_screens_ask_the_audio_route_and_its_limit(self):
        from installer import screens
        page = screens.host_choices(audio=True)
        self.assertIn('Let people sync files that carry no subtitles of their own, by listening to the audio, when '
                      'they pick them?', page)
        self.assertRegex(page, r'<input[^>]*name="SUBSYNC_AUDIO_ROUTE"[^>]*checked')
        self.assertNotRegex(screens.host_choices(audio=False), r'<input[^>]*name="SUBSYNC_AUDIO_ROUTE"[^>]*checked')
        values = {lim.setting: lim.default for lim in limits.hosting(audio=True)}
        self.assertIn('Audio per file', screens.limits('host', values, audio=True))
        self.assertNotIn('Audio per file', screens.limits('host', values, audio=False))

    def test_the_compose_file_listens_on_this_machine_only(self):
        image = 'ghcr.io/haslanli/aiosubtitles:1.4.0@sha256:' + 'a' * 64
        text = hosting.compose_yaml(image, 'id-1', 8790)
        self.assertIn('ports: ["127.0.0.1:8790:8790"]', text)
        self.assertNotIn('external: true', text)
        self.assertIn(f'image: {image}\n', text)
        self.assertIn('org.aiosubtitles.install: "id-1"', text)

    def test_the_web_server_rule_sends_the_whole_host_with_the_visitors_address(self):
        rule = hosting.web_server_rule(ADDRESS, '127.0.0.1:8790')
        self.assertEqual(rule['Caddy'][:2], ['subs.example.com {', '    reverse_proxy 127.0.0.1:8790'])
        self.assertIn('    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;', rule['nginx'])
        self.assertIn('location / {', rule['nginx'])
        self.assertIn('rule: Host(`subs.example.com`)', rule['Traefik'])

    def test_the_hosting_limits_are_the_three_groups(self):
        self.assertEqual(hosting.LIMITS, limits.hosting(usenet=True, audio=True))   # every limit the route may write


class RouteFixture(unittest.TestCase):
    """Docker and the address faked (no tests here: test_web.py drives the page on it too)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / 'state'
        self.up = False
        self.pages = {}

        def compose(state, *args, **kw):
            if 'up' in args:
                self.up = True
            if 'down' in args:
                self.up = False
            return 0, '', ''

        def fetch(url, timeout=10):
            if not self.up:
                raise OSError('connection refused')
            if url.endswith('/health'):
                return 200, {}, b'ok'
            return 200, {'Content-Security-Policy': "default-src 'none'; script-src 'self'"}, b'<!doctype html>'

        def exec_in(name, cmd, stdin=None, timeout=120):
            return 0, json.dumps({'public': True, 'people': 0, 'max_people': 200, 'read_today': 0,
                                  'daily_budget': 50e9}), ''
        for p in (mock.patch.object(engine, 'STATE', self.state), mock.patch.object(engine, 'REPO', Path(self.tmp.name)),
                  mock.patch.object(dockerenv, 'docker_problem', lambda: None),
                  mock.patch.object(dockerenv, 'free_gb', lambda p='/': 100.0),
                  mock.patch.object(dockerenv, 'port_in_use', lambda *a: False),
                  mock.patch.object(dockerenv, 'container_state', lambda n: 'running healthy'),
                  mock.patch.object(dockerenv, 'compose', compose),
                  mock.patch.object(dockerenv, 'run', lambda *a, **k: (0, '', '')),
                  mock.patch.object(dockerenv, 'exec_in', exec_in),
                  mock.patch.object(hosting, 'fetch', fetch)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_setup(self, answers, *extra):
        path = Path(self.tmp.name) / 'answers'
        path.write_text('\n'.join(f'{k}={v}' for k, v in answers.items()) + '\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(['--answers', str(path), '--yes', *extra])
        return code, out.getvalue()



class Route(RouteFixture):
    """The whole hosting route through an answers file."""

    def test_a_public_instance_is_set_up_and_checked(self):
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'SUBSYNC_MAX_USERS': '150'})
        self.assertEqual(code, 0, out)
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual(env['SUBSYNC_PUBLIC'], 'on')
        self.assertEqual(env['SUBSYNC_TRUSTED_PROXY'], 'gateway')
        self.assertEqual(env['SUBSYNC_MAX_USERS'], '150')
        self.assertRegex(env['SUBSYNC_PUBLIC_INVITE'], r'^[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}$')   # suggested
        secret = env['SUBSYNC_SECRET_KEY']
        self.assertNotIn(secret, out)                                                           # never shown
        self.assertNotIn(secret, (self.state / 'settings.json').read_text())
        self.assertIn(SECRET_WORDS, out)
        self.assertEqual(stat.S_IMODE(os.stat(self.state / 'subsync.env').st_mode), 0o600)
        self.assertIn('ports: ["127.0.0.1:8790:8790"]', (self.state / 'compose.yaml').read_text())
        self.assertIn(f'People can reach AIOSubtitles at {ADDRESS}', out)
        self.assertIn(f'The sign-up page loads at {ADDRESS}, with its security headers', out)
        self.assertIn('Starting AIOSubtitles', out)
        self.assertEqual(json.loads((self.state / 'settings.json').read_text())['route'], 'host')

        # a re-run keeps the secret and the invite code, and an emptied invite code opens sign-ups
        invite = env['SUBSYNC_PUBLIC_INVITE']
        code, out = self.run_setup({})
        self.assertEqual(code, 0, out)
        again = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual((again['SUBSYNC_SECRET_KEY'], again['SUBSYNC_PUBLIC_INVITE']), (secret, invite))
        self.assertNotIn(SECRET_WORDS, out)
        code, out = self.run_setup({'SUBSYNC_PUBLIC_INVITE': ''})
        self.assertNotIn('SUBSYNC_PUBLIC_INVITE', engine.read_kv(self.state / 'subsync.env'))

    def test_a_public_instance_with_the_audio_route(self):
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'SUBSYNC_AUDIO_ROUTE': 'yes',
                                    'SUBSYNC_AUDIO_BUDGET_GB': '0.5'})
        self.assertEqual(code, 0, out)
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual((env['SUBSYNC_AUDIO_ROUTE'], env['SUBSYNC_AUDIO_BUDGET_GB']), ('on', '0.5'))
        code, out = self.run_setup({})                                   # a re-run keeps both
        again = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual((again['SUBSYNC_AUDIO_ROUTE'], again['SUBSYNC_AUDIO_BUDGET_GB']), ('on', '0.5'))
        code, out = self.run_setup({'SUBSYNC_AUDIO_ROUTE': 'no'})        # turned off: its limit goes too
        off = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual(off['SUBSYNC_AUDIO_ROUTE'], 'off')
        self.assertNotIn('SUBSYNC_AUDIO_BUDGET_GB', off)

    def test_the_status_and_removing_a_public_instance(self):
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        rows, details = engine.Engine({}, page=True).status_parts()
        self.assertEqual(rows[0], ('ok', 'container aiosubtitles-subsync: running healthy', None))
        self.assertEqual(rows[1], ('ok', 'people signed up: 0 of 200', 'read today: 0.0 GB of 50 GB'))
        e = engine.Engine({}, page=True)
        with self.assertRaises(engine.Problem):
            e.remove_host()                                          # the box isn't ticked: nothing happens
        self.assertTrue((self.state / 'subsync.env').exists())
        e = engine.Engine({'CONFIRM': 'yes'}, page=True)
        e.remove_host()
        self.assertFalse((self.state / 'subsync.env').exists())
        self.assertFalse(self.up)

    def test_the_address_not_answering_is_a_warning_with_the_rule(self):
        with mock.patch.object(hosting, 'fetch', side_effect=OSError('refused')):
            code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 3, out)
        self.assertIn(f'{ADDRESS}/health doesn\'t answer from this machine.', out)
        self.assertIn(f'Your web server needs one rule so {ADDRESS} reaches AIOSubtitles on this machine.', ' '.join(out.split()))
        self.assertIn('reverse_proxy 127.0.0.1:8790', out)

    def test_a_wrong_limit_stops_the_run(self):
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'SUBSYNC_SYNCS_AT_ONCE': 'many'})
        self.assertEqual(code, 1, out)
        self.assertIn('A whole number from 1, or auto.', out)


if __name__ == '__main__':
    unittest.main()
