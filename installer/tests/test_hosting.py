"""The hosting route: a public instance set up by ./setup."""
import ast
import base64
import contextlib
import gc
import http.server
import io
import json
import os
import stat
import tempfile
import threading
import unittest
import warnings
from pathlib import Path
from unittest import mock

from installer import dockerenv, engine, hosting, limits, plan, wizard
from installer.tests.test_flow import AnswersRun

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
        # WYZIE_KEY and AUDIO are a personal install's answers: neither may reach a public instance's lines; the limits
        # come in their own argument, as host_install passes them
        answers = {'HOST_ADDRESS': ADDRESS, 'SUBSYNC_TRUSTED_PROXY': 'gateway', 'SUBSYNC_PUBLIC_INVITE': 'abcd-efgh-jkmn',
                   'SUBDL_KEY': 'sd', 'SUBSYNC_SUBSOURCE_KEY': 'ss', 'WYZIE_KEY': 'wz', 'AUDIO': 'yes'}
        env = hosting.public_env(answers, hosting.new_secret(), {'SUBSYNC_MAX_USERS': '150',
                                                                 'SUBSYNC_SYNCS_AT_ONCE': 'auto'})
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

    def test_tmdb_for_everyone_or_each_persons_own(self):
        # the original-language lookup's key on a public instance: the hoster's own for everyone, or each person's
        base = {'HOST_ADDRESS': ADDRESS}
        env = hosting.public_env(dict(base, TMDB_KEY='tk'), hosting.new_secret(), {}, tmdb_personal=True)
        self.assertEqual(env['SUBSYNC_TMDB_KEY'], 'tk')
        self.assertEqual(env['SUBSYNC_TMDB_PERSONAL'], 'off')          # a key for everyone: never both
        env = hosting.public_env(base, hosting.new_secret(), {}, tmdb_personal=True)
        self.assertNotIn('SUBSYNC_TMDB_KEY', env)
        self.assertEqual(env['SUBSYNC_TMDB_PERSONAL'], 'on')
        env = hosting.public_env(base, hosting.new_secret(), {})
        self.assertEqual(env['SUBSYNC_TMDB_PERSONAL'], 'off')
        self.assertTrue({'SUBSYNC_TMDB_KEY', 'SUBSYNC_TMDB_PERSONAL'} <= set(hosting.OWNED))
        try:
            from subsync.settings import Settings
        except ImportError as e:
            self.skipTest(f'subsync.settings needs {e.name}')
        with mock.patch('subsync.settings.default_gateway', lambda path=None: '172.17.0.1'):
            for answers in (dict(base, TMDB_KEY='tk'), base):
                self.assertEqual(Settings(hosting.public_env(answers, hosting.new_secret(), {}, tmdb_personal=True))
                                 .conflicts(), [])

    def test_the_audio_route_and_its_limit_when_the_host_chooses_them(self):
        # A public instance may sync files without a subtitle track from their sound, after a pick
        answers = {'HOST_ADDRESS': ADDRESS}
        env = hosting.public_env(answers, hosting.new_secret(), {'SUBSYNC_AUDIO_BUDGET_GB': '2'}, audio=True)
        self.assertEqual((env['SUBSYNC_AUDIO_ROUTE'], env['SUBSYNC_AUDIO_BUDGET_GB']), ('on', '2'))
        self.assertNotIn('SUBSYNC_AUDIO_ON_PICK', env)                 # never written: it stays on
        default = hosting.public_env(answers, hosting.new_secret(), {'SUBSYNC_AUDIO_BUDGET_GB': '1'}, audio=True)
        self.assertNotIn('SUBSYNC_AUDIO_BUDGET_GB', default)            # the default: no line
        off = hosting.public_env(answers, hosting.new_secret(), {'SUBSYNC_AUDIO_BUDGET_GB': '2'}, audio=False)
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
        self.assertEqual(s.audio_choice, 'pick')         # the default; in public mode each person chooses

    def test_the_hosting_screens_ask_the_audio_route_and_its_limits(self):
        # Allow it (each person then chooses) or remove it completely, off by default; with it on, its limits
        import html
        from installer import screens
        from subsync import explain
        page = screens.host_choices(audio=True)
        self.assertEqual(hosting.AUDIO_QUESTION, 'Offer syncing files that have no subtitles of their own, from their '
                                                 'audio?')
        self.assertIn(hosting.AUDIO_QUESTION, page)
        self.assertIn(html.escape(explain.QUESTIONS['SUBSYNC_AUDIO_ROUTE'][1]), page)
        self.assertTrue(explain.QUESTIONS['SUBSYNC_AUDIO_ROUTE'][1].startswith('Off by default. Off removes it '
                                                                               'completely'))
        self.assertEqual(explain.QUESTIONS['SUBSYNC_AUDIO_ROUTE'][0], 'no')
        self.assertRegex(page, r'<input[^>]*name="SUBSYNC_AUDIO_ROUTE"[^>]*checked')
        self.assertNotRegex(screens.host_choices(audio=False), r'<input[^>]*name="SUBSYNC_AUDIO_ROUTE"[^>]*checked')
        values = {lim.setting: lim.default for lim in limits.hosting(audio=True)}
        on, off = screens.limits('host', values, audio=True), screens.limits('host', values, audio=False)
        for label in ('Audio per file', 'Audio syncs per person a day', 'Of those, as soon as a file is opened',
                      'Audio syncs at a time'):
            with self.subTest(label=label):
                self.assertIn(label, on)
                self.assertNotIn(label, off)

    def test_the_audio_routes_limits_are_written_only_with_it_on_and_when_not_the_default(self):
        # The two daily counts and the audio syncs at a time
        answers = {'HOST_ADDRESS': ADDRESS}
        changed = {'SUBSYNC_USER_AUDIO_PER_DAY': '10', 'SUBSYNC_USER_OPENED_PER_DAY': '0', 'SUBSYNC_AUDIO_AT_ONCE': '1'}
        env = hosting.public_env(answers, hosting.new_secret(), changed, audio=True)
        self.assertEqual({k: env[k] for k in changed}, changed)
        default = hosting.public_env(answers, hosting.new_secret(), {'SUBSYNC_USER_AUDIO_PER_DAY': '20',
                                                                     'SUBSYNC_USER_OPENED_PER_DAY': '5',
                                                                     'SUBSYNC_AUDIO_AT_ONCE': 'auto'}, audio=True)
        self.assertFalse(set(changed) & set(default))
        off = hosting.public_env(answers, hosting.new_secret(), changed, audio=False)
        self.assertFalse(set(changed) & set(off))
        self.assertLessEqual(set(changed), set(hosting.OWNED))       # a line set by hand gives way to the answer
        try:
            from subsync.settings import Settings
        except ImportError as e:
            self.skipTest(f'subsync.settings needs {e.name}')
        with mock.patch('subsync.settings.default_gateway', lambda path=None: '172.17.0.1'):
            s = Settings(env)
        self.assertEqual(s.conflicts(), [])
        self.assertEqual((s.user_audio_per_day, s.user_opened_per_day, s.audio_at_once), (10, 0, 1))

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

    def test_every_limit_the_route_may_write_is_its_own(self):
        # OWNED: a limit's line set by hand gives way to the answer, whichever of Usenet and the audio route is on
        self.assertEqual(hosting.LIMITS, limits.hosting(usenet=True, audio=True))   # the widest set
        for usenet in (False, True):
            for audio in (False, True):
                with self.subTest(usenet=usenet, audio=audio):
                    self.assertLessEqual(set(limits.names(limits.hosting(usenet, audio))), set(hosting.OWNED))


class Switches(unittest.TestCase):
    """The host's three choices reach public_env as True or False, never as words it reads again; a switch already in
    state/subsync.env is read as subsync reads it."""

    def test_the_host_choices_are_written_as_chosen(self):
        answers = {'HOST_ADDRESS': ADDRESS}
        for chosen in (False, True):
            with self.subTest(chosen=chosen):
                env = hosting.public_env(answers, 's' * 43, {}, usenet=chosen, audio=chosen, tmdb_personal=chosen)
                switches = ('SUBSYNC_USENET', 'SUBSYNC_AUDIO_ROUTE', 'SUBSYNC_TMDB_PERSONAL')
                self.assertEqual({k: env[k] for k in switches}, dict.fromkeys(switches, 'on' if chosen else 'off'))
        env = hosting.public_env(answers, 's' * 43, {})
        self.assertEqual((env['SUBSYNC_USENET'], env['SUBSYNC_AUDIO_ROUTE'], env['SUBSYNC_TMDB_PERSONAL']),
                         ('off', 'off', 'off'))                                      # nothing chosen: all off

    def test_a_switch_is_read_with_subsyncs_own_words(self):
        tree = ast.parse((Path(__file__).resolve().parents[2] / 'subsync' / 'subsync' / 'settings.py').read_text())
        on = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_on')
        self.assertEqual(sorted(plan.ON), sorted(ast.literal_eval(on.body[0].value.comparators[0])))
        for value, expected in (('1', True), (' ON ', True), ('Yes', True), ('true', True), ('y', False),
                                ('off', False), ('0', False), ('', False), (None, False)):
            with self.subTest(value=value):
                self.assertIs(plan.is_on(value), expected)
        self.assertEqual(plan.audio_when_of({'SUBSYNC_AUDIO_ON_PICK': '1'}), 'pick')
        self.assertEqual(plan.audio_when_of({'SUBSYNC_AUDIO_ON_PICK': 'off'}), 'open')


class Fetch(unittest.TestCase):
    """One GET from this machine (127.0.0.1 here), as hosting.check makes them."""

    def setUp(self):
        class Answer(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                status = 200 if self.path == '/health' else 502
                self.send_response(status)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok' if status == 200 else b'no')

            def log_message(self, *args):
                pass
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Answer)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'

    def test_an_error_answer_is_closed_at_once(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            for _ in range(3):
                status, _, body = hosting.fetch(self.base + '/')
            gc.collect()
        self.assertEqual((status, body), (502, b''))
        self.assertEqual([str(w.message) for w in caught if issubclass(w.category, ResourceWarning)], [])
        self.assertEqual(hosting.fetch(self.base + '/health')[::2], (200, b'ok'))


class RouteFixture(AnswersRun, unittest.TestCase):
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
            if cmd == engine.HEALTH:                        # subsync's /health, once it runs
                return (0, 'ok\n', '') if self.up else (1, '', 'Connection refused')
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
        self.assertEqual(code, 0, out)
        again = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual((again['SUBSYNC_AUDIO_ROUTE'], again['SUBSYNC_AUDIO_BUDGET_GB']), ('on', '0.5'))
        code, out = self.run_setup({'SUBSYNC_AUDIO_ROUTE': 'no'})        # turned off: its limit goes too
        off = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual(off['SUBSYNC_AUDIO_ROUTE'], 'off')
        self.assertNotIn('SUBSYNC_AUDIO_BUDGET_GB', off)

    def test_the_audio_routes_daily_counts_and_places_through_the_answers_file(self):
        # Asked with the audio route on, with their defaults; audio syncs at a time within the files at a time
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'SUBSYNC_AUDIO_ROUTE': 'yes',
                                    'SUBSYNC_USER_OPENED_PER_DAY': '0', 'SUBSYNC_SYNCS_AT_ONCE': '4',
                                    'SUBSYNC_AUDIO_AT_ONCE': '2'})
        self.assertEqual(code, 0, out)
        flat = ' '.join(out.split())
        self.assertIn('Offer syncing files that have no subtitles of their own, from their audio?', flat)
        for label in ('Audio syncs per person a day', 'Of those, as soon as a file is opened', 'Audio syncs at a time'):
            self.assertIn(label, flat)
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual((env['SUBSYNC_USER_OPENED_PER_DAY'], env['SUBSYNC_AUDIO_AT_ONCE']), ('0', '2'))
        self.assertNotIn('SUBSYNC_USER_AUDIO_PER_DAY', env)                  # the default: no line
        code, out = self.run_setup({'SUBSYNC_AUDIO_AT_ONCE': '5'})          # more than the 4 files at a time
        self.assertNotEqual(code, 0)
        self.assertIn('A whole number from 1 to 4 (the files synced at a time), or auto.', ' '.join(out.split()))
        code, out = self.run_setup({'SUBSYNC_AUDIO_ROUTE': 'no'})            # off: none of them asked or written
        self.assertEqual(code, 0, out)
        self.assertNotIn('Audio syncs at a time', out)
        off = engine.read_kv(self.state / 'subsync.env')
        self.assertFalse({'SUBSYNC_USER_AUDIO_PER_DAY', 'SUBSYNC_USER_OPENED_PER_DAY', 'SUBSYNC_AUDIO_AT_ONCE'} & set(off))

    def test_a_wyzie_key_written_by_hand_goes_on_a_rerun(self):
        # public mode refuses a Wyzie Subs key (subsync wouldn't start): the line is setup's on this route too
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        with open(self.state / 'subsync.env', 'a') as env:
            env.write('SUBSYNC_WYZIE_KEY=wz\n')
        code, out = self.run_setup({})
        self.assertEqual(code, 0, out)
        self.assertNotIn('SUBSYNC_WYZIE_KEY', engine.read_kv(self.state / 'subsync.env'))

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

    def test_removing_in_a_terminal_says_what_stays_naming_the_database_copies(self):
        # it printed no list; the page's said "with copies of your config in state/backups/ (./setup --restore FILE puts
        # one back)", though a public instance keeps only the databases' copies there, with everyone's records
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        (self.state / 'backups' / 'databases-20261114-061500').mkdir(parents=True)
        code, out = self.run_setup({'CONFIRM': 'yes'}, '--uninstall')
        self.assertEqual(code, 0, out)
        self.assertIn('These stay, in case you set it up again; remove them when you no longer need them:\n'
                      '    • the subsync image (about 1 GB): docker image rm ', out)
        self.assertIn(f'    • this folder, {self.tmp.name}, with the database copies made before updates in '
                      "state/backups/, which hold everyone's records\n", out)
        self.assertNotIn('--restore', out)
        self.assertFalse((self.state / 'subsync.env').exists())

    def test_removing_answered_no_in_a_terminal_changes_nothing(self):
        # it ended "CONFIRM needs an answer." and "Add CONFIRM=... to the answers file."
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        code, out = self.run_setup({'CONFIRM': 'no'}, '--uninstall')
        self.assertEqual(code, 1, out)
        self.assertTrue(out.rstrip().endswith('Nothing was changed.'), out[-200:])
        self.assertNotIn('CONFIRM needs an answer', out)
        self.assertTrue((self.state / 'subsync.env').exists())
        with self.assertRaises(engine.Problem) as caught:                   # the page: the box not ticked
            engine.Engine({}, page=True).remove_host()
        self.assertEqual((caught.exception.field, caught.exception.message),
                         ('CONFIRM', 'Tick "Delete everyone\'s records and stored copies" to remove AIOSubtitles.'))

    def test_a_failed_container_removal_keeps_the_public_instances_files(self):
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        with mock.patch.object(dockerenv, 'compose', lambda state, *args, **kw: (1, 'still running', '')):
            with self.assertRaises(engine.Stop) as caught:
                engine.Engine({'CONFIRM': 'yes'}, page=True).remove_host()
        self.assertEqual((caught.exception.message, caught.exception.fix), (engine.REMOVE_FAILED, 'still running'))
        for name in ('settings.json', 'secrets.env', 'subsync.env', 'compose.yaml'):
            self.assertTrue((self.state / name).exists(), name)

    def test_the_status_heading_names_aiosubtitles(self):
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wizard.main(['--status']), 0)
        self.assertIn('AIOSubtitles status', out.getvalue())
        self.assertNotIn('subsync status', out.getvalue())

    def test_the_address_not_answering_is_a_warning_with_the_rule(self):
        with mock.patch.object(hosting, 'fetch', side_effect=OSError('refused')):
            code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 3, out)
        self.assertIn(f'{ADDRESS}/health doesn\'t answer from this machine.', out)
        self.assertIn(f'Your web server needs one rule so {ADDRESS} reaches AIOSubtitles on this machine.', ' '.join(out.split()))
        self.assertIn('reverse_proxy 127.0.0.1:8790', out)

    def test_a_removed_tmdb_key_asks_each_persons_question_again(self):
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'TMDB_KEY': 'tmdb-key-1'})
        self.assertEqual(code, 0, out)
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual((env['SUBSYNC_TMDB_KEY'], env['SUBSYNC_TMDB_PERSONAL']), ('tmdb-key-1', 'off'))
        code, out = self.run_setup({'SUBSYNC_TMDB_PERSONAL': 'yes'})       # no line: kept, the question not asked
        self.assertEqual(code, 0, out)
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_TMDB_PERSONAL'], 'off')
        code, out = self.run_setup({'TMDB_KEY': '', 'SUBSYNC_TMDB_PERSONAL': 'yes'})
        self.assertEqual(code, 0, out)
        self.assertIn('Removed the saved TMDB API key', out)
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertNotIn('SUBSYNC_TMDB_KEY', env)
        self.assertEqual(env['SUBSYNC_TMDB_PERSONAL'], 'on')
        self.assertNotIn('TMDB_KEY', engine.read_kv(self.state / 'secrets.env'))

    def test_a_public_instance_isnt_turned_into_an_install_for_yourself(self):
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        before = (self.state / 'subsync.env').read_text()
        code, out = self.run_setup({'ROUTE': 'yourself', 'AIOS_URL': 'http://127.0.0.1:9'})
        self.assertEqual(code, 1, out)
        self.assertIn("A public instance runs here; setup can't turn it into an install for yourself.", out)
        self.assertIn("Run ./setup --uninstall first (it deletes everyone's records), then ./setup again.", out)
        self.assertEqual((self.state / 'subsync.env').read_text(), before)
        self.assertEqual(json.loads((self.state / 'settings.json').read_text())['route'], 'host')

    def test_a_wrong_limit_stops_the_run(self):
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'SUBSYNC_SYNCS_AT_ONCE': 'many'})
        self.assertEqual(code, 1, out)
        self.assertIn('A whole number from 1, or auto.', out)


if __name__ == '__main__':
    unittest.main()
