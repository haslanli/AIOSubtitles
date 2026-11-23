"""The hosting route: a public instance set up by ./setup."""
import ast
import base64
import contextlib
import gc
import html
import http.server
import io
import json
import os
import socket
import stat
import string
import tempfile
import threading
import unittest
import urllib.error
import warnings
from pathlib import Path
from unittest import mock

from installer import dockerenv, docs, engine, hosting, limits, plan, screens, wizard
from installer.tests.test_engine import typing
from installer.tests.test_flow import POLL, AnswersRun
from subsync import explain, realdebrid
from subsync.settings import Settings

ADDRESS = 'https://subs.example.com'
SECRET_WORDS = 'kept it in state/subsync.env'
# a suggested invite code: three groups of four of the letters and digits easy to read out and type (no 0/o, 1/l/i)
INVITE_CODE = r'^[a-hjkmnp-z2-9]{4}-[a-hjkmnp-z2-9]{4}-[a-hjkmnp-z2-9]{4}$'


class Pieces(unittest.TestCase):
    def test_the_secret_is_32_random_bytes_as_base64url(self):
        secret = hosting.new_secret()
        self.assertEqual(len(base64.urlsafe_b64decode(secret + '=' * (-len(secret) % 4))), 32)
        self.assertNotEqual(secret, hosting.new_secret())

    def test_a_suggested_invite_code_is_easy_to_type(self):
        # The pattern took i, l and o too, the letters the code leaves out, and one random code could miss them
        self.assertEqual(set(hosting._INVITE_LETTERS), set(string.ascii_lowercase + string.digits) - set('01ilo'))
        self.assertRegex(hosting.suggested_invite(), INVITE_CODE)

    def test_an_invite_code_takes_letters_digits_spaces_and_three_signs(self):
        # Docker Compose substitutes $name in state/subsync.env's unquoted lines and cuts a value at " #", so
        # "join$us" reached subsync as "join" and "movie night #2" as "movie night": nobody could sign up with them
        self.assertEqual(hosting.INVITE_REFUSED, 'Letters, digits, spaces, - _ and . only, like k7mp-x4rt-9nwe.')
        for good in ('k7mp-x4rt-9nwe', 'movie night 2', 'Film_Club.2026', 'Café-Ünï', hosting.suggested_invite(), ''):
            with self.subTest(good=good):
                self.assertEqual(hosting.check_invite(good), (True, '', good))
        self.assertEqual(hosting.check_invite('  spaced out  '), (True, '', 'spaced out'))
        for bad in ('join$us', '${HOME}', 'movie night #2', 'a#b', '"quoted"', "it's", 'tab\there', 'a/b', 'a;b'):
            with self.subTest(bad=bad):
                self.assertEqual(hosting.check_invite(bad), (False, hosting.INVITE_REFUSED, bad))

    def test_a_trusted_proxy_is_gateway_or_ip_addresses(self):
        # subsync compares a request's own address with each entry as written, so a range, a name or a typo
        # never matched, and every visitor counted as the web server's one address for the sign-up limit
        for good in ('gateway', 'Gateway', '172.18.0.1', 'gateway, 127.0.0.1', '::1', ' 10.0.0.2 ,fd00::1 '):
            with self.subTest(good=good):
                self.assertEqual(hosting.check_trusted_proxy(good), (True, '', good.strip()))
        for bad, entry in (('172.18.0.0/16', '172.18.0.0/16'), ('caddy', 'caddy'), ('gatway', 'gatway'),
                           ('gateway, caddy', 'caddy'), ('127.0.0.1, 10.0.0.0/8, web', '10.0.0.0/8')):
            with self.subTest(bad=bad):
                self.assertEqual(hosting.check_trusted_proxy(bad), (
                    False, f"Type gateway or IP addresses separated by commas, like 172.18.0.1: {entry} isn't one.", bad))

    def test_the_address_must_be_https_and_a_whole_host(self):
        self.assertEqual(hosting.check_address('https://Subs.Example.com/'), (True, '', 'https://subs.example.com'))
        # An address with a user name or password would be written into every person's addon link
        for bad in ('http://subs.example.com', 'subs.example.com', 'https://subs.example.com/subsync', 'ftp://x',
                    'https://', 'https://subs.example.com?x=1', 'https://user:pw@subs.example.com',
                    'https://user@subs.example.com', 'https://:pw@subs.example.com'):
            with self.subTest(bad=bad):
                good, message, _ = hosting.check_address(bad)
                self.assertFalse(good)
                # it repeated the question's own explanation, with no word of what was wrong (a path, say)
                self.assertEqual(message, 'Type an https address with no path after the name, like '
                                          'https://subtitles.example.com: the pages and links start at its root.')

    def test_a_name_that_cant_be_looked_up_is_refused(self):
        # IDNA refuses an empty label or one over 63 characters; the address was taken, and every check of it
        # then stopped setup with UnicodeError (after the install, and at each later update and rollback)
        for bad in ('https://subtitles..example.com', 'https://.example.com', f'https://{"a" * 64}.example.com'):
            with self.subTest(bad=bad):
                self.assertEqual(hosting.check_address(bad), (False, hosting.ADDRESS_REFUSED, bad))
        for good in (f'https://{"a" * 63}.example.com', 'https://bücher.example', 'https://subs.example.com:8443'):
            with self.subTest(good=good):
                self.assertEqual(hosting.check_address(good), (True, '', good))

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
        with mock.patch('subsync.settings.default_gateway', lambda path=None: '172.17.0.1'):
            s = Settings(env)
        self.assertEqual(s.conflicts(), [])
        self.assertEqual(s.audio_choice, 'pick')         # the default; in public mode each person chooses

    def test_the_hosting_screens_ask_the_audio_route_and_its_limits(self):
        # Allow it (each person then chooses) or remove it completely, off by default; with it on, its limits
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
        # a public instance setup 1.3.0 or before made keeps its volume, with everyone's records, as it is
        kept = hosting.compose_yaml(image, 'id-1', 8790, external_volume='aiosubtitles_subsync-data')
        self.assertTrue(kept.endswith('\nvolumes:\n  subsync-data: {external: true, name: "aiosubtitles_subsync-data"}\n'),
                        kept)
        self.assertEqual(kept.replace('{external: true, name: "aiosubtitles_subsync-data"}',
                                      '{labels: {org.aiosubtitles.install: "id-1"}}'), text)

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

    def read(self, text):
        """state/subsync.env written as `text`, read as setup reads it: its switches and when the audio route starts."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, 'STATE', Path(tmp)):
            (Path(tmp) / 'subsync.env').write_text(text)
            kept = engine.env_settings()
            return (kept.usenet, kept.audio_route, kept.tmdb_personal), engine.env_audio_when()

    def test_a_switch_is_read_with_subsyncs_own_words(self):
        # Setup reads them with subsync's own settings (it had copies of their words and defaults)
        tree = ast.parse((Path(__file__).resolve().parents[2] / 'subsync' / 'subsync' / 'settings.py').read_text())
        on = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_on')
        words = ast.literal_eval(on.body[0].value.comparators[0])
        self.assertTrue(words)
        cases = [*[(word, True) for word in words], ('1', True), (' ON ', True), ('Yes', True), ('true', True),
                 ('y', False), ('off', False), ('0', False), ('', False), (None, False), ('enabled', False)]
        for value, expected in cases:
            with self.subTest(value=value):
                lines = '' if value is None else ''.join(f'{key}={value}\n' for key in (
                    'SUBSYNC_USENET', 'SUBSYNC_AUDIO_ROUTE', 'SUBSYNC_TMDB_PERSONAL'))
                self.assertEqual(self.read(lines)[0], (expected,) * 3)
        self.assertEqual(self.read('SUBSYNC_AUDIO_ON_PICK=1\n')[1], 'pick')
        self.assertEqual(self.read('SUBSYNC_AUDIO_ON_PICK=off\n')[1], 'open')

    def test_the_host_choices_start_from_the_switches_state_subsync_env_has(self):
        # each one read as subsync reads it: Yes and 1 are on, a word it doesn't take is its default (off)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, 'STATE', Path(tmp)), \
                contextlib.redirect_stdout(io.StringIO()):
            (Path(tmp) / 'subsync.env').write_text('SUBSYNC_USENET=Yes\nSUBSYNC_AUDIO_ROUTE=enabled\n'
                                                   'SUBSYNC_TMDB_PERSONAL=1\n')
            for page in (True, False):                             # the page, and ./setup --yes
                with self.subTest(page=page):
                    e = engine.Engine({}, page=page)
                    e.host_choices()
                    self.assertEqual((e.host_usenet, e.host_audio, e.host_tmdb_personal), (True, False, True))

    def test_when_the_audio_route_starts_is_read_as_subsync_reads_it(self):
        # as state/subsync.env starts it, the setting SUBSYNC_AUDIO_WHEN replaced included (moved from test_plan)
        for env, when in (({}, 'pick'), ({'SUBSYNC_AUDIO_WHEN': 'open'}, 'open'), ({'SUBSYNC_AUDIO_ON_PICK': 'off'}, 'open'),
                          ({'SUBSYNC_AUDIO_ON_PICK': 'on'}, 'pick'), ({'SUBSYNC_AUDIO_ON_PICK': ''}, 'open'),
                          ({'SUBSYNC_AUDIO_WHEN': 'pick', 'SUBSYNC_AUDIO_ON_PICK': 'off'}, 'pick')):
            with self.subTest(env=env):
                self.assertEqual(self.read(''.join(f'{k}={v}\n' for k, v in env.items()))[1], when)
        self.assertEqual(self.read('SUBSYNC_AUDIO_WHEN="open"\n')[1], 'open')      # a quoted value, as Compose reads it
        # Written wrong, it reads as its default, as subsync reads it and every setting written wrong (setup's
        # copy fell back to SUBSYNC_AUDIO_ON_PICK: open here, while subsync refuses the value)
        self.assertEqual(self.read('SUBSYNC_AUDIO_WHEN=later\nSUBSYNC_AUDIO_ON_PICK=off\n')[1], 'pick')


class Rows:
    """hosting.check's `out`: the rows it shows, as (kind, text)."""

    def __init__(self):
        self.rows = []

    def __getattr__(self, kind):
        return lambda text: self.rows.append((kind, text))


class NotHttp(http.server.BaseHTTPRequestHandler):
    """Something else than a web server answering: an SSH server's first line."""

    def handle(self):
        self.request.recv(1024)
        self.request.sendall(b'SSH-2.0-OpenSSH_9.6\r\n')


class Fetch(unittest.TestCase):
    """One GET from this machine (127.0.0.1 here), as hosting.check makes them."""

    def serve(self, handler):
        """A stand-in server on 127.0.0.1 for this test, closed after it; its address. It looks for shutdown() every
        POLL seconds, so closing it takes no longer than that (serve_forever's default is half a second)."""
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': POLL}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_address[1]}'

    def setUp(self):
        self.agents = []
        agents = self.agents

        class Answer(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                agents.append(self.headers.get('User-Agent'))
                status = 200 if self.path == '/health' else 502
                self.send_response(status)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok' if status == 200 else b'no')

            def log_message(self, *args):
                pass
        self.base = self.serve(Answer)

    def test_an_error_answer_is_closed_at_once(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            for _ in range(3):
                status, _, body = hosting.fetch(self.base + '/')
            gc.collect()
        self.assertEqual((status, body), (502, b''))
        self.assertEqual([str(w.message) for w in caught if issubclass(w.category, ResourceWarning)], [])
        self.assertEqual(hosting.fetch(self.base + '/health')[::2], (200, b'ok'))

    def test_the_health_check_is_one_ok_from_this_machine(self):
        # One check of an address on both routes (the personal route's asked through AIOStreams' client)
        self.assertTrue(hosting.health_ok(self.base))
        self.assertFalse(hosting.health_ok(self.base + '/elsewhere'))                 # 502
        closed = http.server.ThreadingHTTPServer(('127.0.0.1', 0), http.server.BaseHTTPRequestHandler)
        port = closed.server_address[1]
        closed.server_close()
        self.assertFalse(hosting.health_ok(f'http://127.0.0.1:{port}'))              # nothing answers
        self.assertFalse(hosting.health_ok(self.serve(NotHttp)))                       # something else answers

    def test_an_answer_that_isnt_http_or_a_name_that_cant_be_looked_up_is_a_reason_not_a_stop(self):
        # The sign-up page's check caught OSError only, so a non-HTTP answer (BadStatusLine) or a name IDNA
        # refuses (UnicodeError) stopped setup with a traceback, at the install and at every update's check
        base = self.serve(NotHttp)
        with self.assertRaises(OSError) as caught:
            hosting.fetch(base + '/')
        self.assertEqual(str(caught.exception), 'SSH-2.0-OpenSSH_9.6')         # one line, the answer's own words
        tried, real_connect = [], socket.create_connection

        def connect(address, *args, **kwargs):
            """As the resolver starts (IDNA first, before any lookup), then this machine only."""
            address[0].encode('idna')
            if address[0] != '127.0.0.1':
                tried.append(address)
                raise OSError(f'test: no connection to {address[0]}')
            return real_connect(address, *args, **kwargs)
        for address, reason in ((base, 'SSH-2.0-OpenSSH_9.6'), ('https://subtitles..example.com', 'idna')):
            with self.subTest(address=address), mock.patch.object(socket, 'create_connection', connect):
                out = Rows()
                self.assertFalse(hosting.check(address, out))
                self.assertEqual(out.rows[0], ('warn', hosting.NO_ANSWER.format(address=address)))
                kind, text = out.rows[-1]
                self.assertEqual(kind, 'bad')
                self.assertTrue(text.startswith(f"The sign-up page doesn't load at {address}: "), text)
                self.assertIn(reason, text)                     # its own words (IDNA's differ by Python version)
                self.assertNotIn('\n', text)
        self.assertEqual(tried, [])

    def test_no_request_reaches_real_debrid_nor_a_redirect_to_it(self):
        # Setup's one request outside subsync's gate (realdebrid.urlopen) opened the address as typed, and
        # followed a redirect to wherever it pointed
        tried, opened = [], []
        real_connect = socket.create_connection

        def connect(address, *args, **kwargs):     # nothing but this machine, whatever the code under test does
            if address[0] not in ('127.0.0.1', 'localhost'):
                tried.append(address)
                raise OSError(f'test: no connection to {address[0]}')
            return real_connect(address, *args, **kwargs)

        class Away(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', 'https://real-debrid.com/health')
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass
        away = self.serve(Away)
        with mock.patch.object(socket, 'create_connection', connect), \
                mock.patch('urllib.request.urlopen', lambda *a, **k: opened.append(a) or self.fail('urllib opened it')):
            for url in ('https://real-debrid.com/health', 'https://API.Real-Debrid.com./',
                        f'{away}/health'):
                with self.subTest(url=url), self.assertLogs('subsync.realdebrid', 'WARNING') as logged, \
                        self.assertRaises(realdebrid.Blocked):
                    hosting.fetch(url)
                self.assertEqual(logged.output, ['WARNING:subsync.realdebrid:blocked a request to Real-Debrid '
                                                 f'({"api.real-debrid.com" if "API" in url else "real-debrid.com"})'])
        self.assertEqual((tried, opened), ([], []))

    def check_with(self, health, page):
        """hosting.check of ADDRESS, with /health and the sign-up page answering as given: (status, headers, body), or
        the error the request raises. (what it returns, the rows it shows)"""
        def fetch(url, timeout=10):
            answer = {f'{ADDRESS}/health': health, f'{ADDRESS}/': page}[url]
            if isinstance(answer, Exception):
                raise answer
            return answer
        out = Rows()
        with mock.patch.object(hosting, 'fetch', fetch):
            return hosting.check(ADDRESS, out), out.rows

    def test_the_address_and_the_sign_up_page_with_its_security_headers_are_checked(self):
        # The public instance's last step, and the check before and after an update ("Done" or 3); only an
        # address where nothing answers was tested, so a page without its security headers or another page at /health
        # could have passed unnoticed
        ok = (200, {}, b'ok\n')
        page = (200, {'Content-Security-Policy': "default-src 'none'; script-src 'self'"}, b'<!doctype html>')
        reach = ('ok', f'People can reach AIOSubtitles at {ADDRESS}')
        loads = ('ok', f'The sign-up page loads at {ADDRESS}, with its security headers')
        no_answer = [('warn', hosting.NO_ANSWER.format(address=ADDRESS)),
                     *(('note', line) for line in hosting.NO_ANSWER_NOTES)]
        doesnt = f"The sign-up page doesn't load at {ADDRESS}: "
        without = (f'The sign-up page loads at {ADDRESS}, but without its security headers: make your web server pass '
                   'them on as they are.')
        for case, health, answer, expected in (
                ('both good', ok, page, (True, [reach, loads])),
                ('its header named in lower case', ok, (200, {'content-security-policy': "default-src 'none'"}, b''),
                 (True, [reach, loads])),
                # These two said "The sign-up page doesn't load at ...: HTTP 200."
                ('no security header', ok, (200, {'Content-Type': 'text/html'}, b'<!doctype html>'),
                 (False, [reach, ('bad', without)])),
                ("the web server's own header instead", ok,
                 (200, {'Content-Security-Policy': "default-src 'self'"}, b''),
                 (False, [reach, ('bad', without)])),
                ('an error page', ok, (502, {'Content-Security-Policy': "default-src 'none'"}, b''),
                 (False, [reach, ('bad', doesnt + 'HTTP 502.')])),
                ('the page refused', ok, urllib.error.URLError('refused'),
                 (False, [reach, ('bad', doesnt + 'refused.')])),
                ('no route to it', ok, OSError('no route to host'),
                 (False, [reach, ('bad', doesnt + 'no route to host.')])),
                ("a web server's own page at /health", (200, {}, b'<h1>Welcome to nginx!</h1>'), page,
                 (False, [*no_answer, loads])),
                ('/health answering an error', (503, {}, b'ok'), page, (False, [*no_answer, loads]))):
            with self.subTest(case):
                self.assertEqual(self.check_with(health, answer), expected)

    def test_the_pages_own_security_header_counts_beside_another_of_the_web_servers(self):
        # Fetch kept one header of each name, the last, so a web server adding a policy of its own after the
        # page's (nginx's add_header) failed the check though the page's own was there
        class TwoPolicies(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                if self.path == '/':
                    self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'")
                    self.send_header('Content-Security-Policy', "frame-ancestors 'self'")
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')

            def log_message(self, *args):
                pass
        base = self.serve(TwoPolicies)
        self.assertEqual(hosting.fetch(base + '/')[1].get_all('Content-Security-Policy'),
                         ["default-src 'none'; script-src 'self'", "frame-ancestors 'self'"])
        out = Rows()
        self.assertTrue(hosting.check(base, out))
        self.assertEqual(out.rows, [('ok', f'People can reach AIOSubtitles at {base}'),
                                    ('ok', f'The sign-up page loads at {base}, with its security headers')])

    def test_it_says_which_release_of_setup_asks(self):
        # It said "AIOSubtitles-setup", no version; every User-Agent now carries the release's (VERSION)
        version = (Path(__file__).resolve().parents[2] / 'VERSION').read_text().strip()
        hosting.fetch(self.base + '/health')
        self.assertEqual(self.agents, [f'AIOSubtitles/{version} (setup; +https://github.com/haslanli/AIOSubtitles)'])


class RouteFixture(AnswersRun, unittest.TestCase):
    """Docker and the address faked (no tests here: test_web.py drives the page on it too). Everything it sets up is
    undone by cleanups, last first, so a setUp that fails halfway leaves nothing behind for later tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
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
            if cmd == dockerenv.HEALTH:                     # subsync's /health, once it runs
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
        self.stand_in_tmdb()


class Fixture(unittest.TestCase):
    def test_a_set_up_that_fails_halfway_still_undoes_everything(self):
        # The folder's cleanup was registered last, after ten patches and stand_in_tmdb, so a setUp stopped
        # by either left the folder to the garbage collector (and its ResourceWarning)
        made = []

        class Broken(RouteFixture):
            def stand_in_tmdb(self):
                made.append(Path(self.tmp.name))
                raise RuntimeError('a later step of setUp failed')

            def test_nothing(self):
                pass

        def patched():
            return engine.STATE, engine.REPO, dockerenv.compose, dockerenv.exec_in, hosting.fetch
        before = patched()
        result = unittest.TestResult()
        Broken('test_nothing').run(result)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(patched(), before)
        self.assertFalse(made[0].exists())


class Route(RouteFixture):
    """The whole hosting route through an answers file."""

    def test_a_public_instance_is_set_up_and_checked(self):
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'SUBSYNC_MAX_USERS': '150'})
        self.assertEqual(code, 0, out)
        env = engine.read_kv(self.state / 'subsync.env')
        self.assertEqual(env['SUBSYNC_PUBLIC'], 'on')
        self.assertEqual(env['SUBSYNC_TRUSTED_PROXY'], 'gateway')
        self.assertEqual(env['SUBSYNC_MAX_USERS'], '150')
        self.assertRegex(env['SUBSYNC_PUBLIC_INVITE'], INVITE_CODE)                         # suggested
        secret = env['SUBSYNC_SECRET_KEY']
        self.assertNotIn(secret, out)                                                           # never shown
        self.assertNotIn(secret, (self.state / 'settings.json').read_text())
        self.assertIn(SECRET_WORDS, out)
        self.assertEqual(stat.S_IMODE(os.stat(self.state / 'subsync.env').st_mode), 0o600)
        self.assertIn('ports: ["127.0.0.1:8790:8790"]', (self.state / 'compose.yaml').read_text())
        self.assertIn(f'People can reach AIOSubtitles at {ADDRESS}', out)
        self.assertIn(f'The sign-up page loads at {ADDRESS}, with its security headers', out)
        # The address answers once the container runs, so the web server's rule is in place: not shown again
        self.assertNotIn('Your web server needs one rule', out)
        self.assertNotIn('reverse_proxy', out)
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
        self.assertEqual(code, 0, out)                          # the whole run, not only its env lines
        self.assertNotIn('SUBSYNC_PUBLIC_INVITE', engine.read_kv(self.state / 'subsync.env'))

    def test_the_invite_code_is_asked_in_a_terminal_and_shown_at_the_end(self):
        # A terminal never asked it, and the end said to share "the invite code" without saying it
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        invite = engine.read_kv(self.state / 'subsync.env')['SUBSYNC_PUBLIC_INVITE']
        self.assertRegex(invite, INVITE_CODE)                                     # the suggested one
        said = ' '.join(out.replace('│', ' ').split())
        explanation = ' '.join(explain.SETTINGS['SUBSYNC_PUBLIC_INVITE'][1].split())
        self.assertIn(f'Invite code {explanation} Enter = {invite} > (default)', said)
        self.assertIn(f'Share the address and the invite code, {invite}, with the people you want to use it: each of '
                      'them signs up there with their own AIOStreams link.', said)
        e = engine.Engine({})
        e.address, e.invite = ADDRESS, ''                                         # anyone may sign up: no code to give
        self.assertEqual(e.host_summary_parts(True)[1][1], 'Share the address with the people you want to use it: each '
                                                           'of them signs up there with their own AIOStreams link.')

    def ask_invite(self, answers, *typed):
        """The sign-ups step in a terminal without --yes: (the invite code taken, what was printed)."""
        e = engine.Engine(answers, yes=False)
        out = io.StringIO()
        with typing(*typed), contextlib.redirect_stdout(out):
            e.host_signups()
        return e.invite, out.getvalue()

    def test_in_a_terminal_enter_takes_the_suggested_code_and_remove_lets_anyone_sign_up(self):
        invite, out = self.ask_invite({}, '')
        self.assertRegex(invite, INVITE_CODE)
        self.assertIn(f'Enter = {invite}', out)
        self.assertEqual(self.ask_invite({}, 'remove')[0], '')                   # on a first setup too
        self.assertEqual(self.ask_invite({}, 'Movie Night')[0], 'Movie Night')

    def test_an_invite_code_compose_would_change_is_refused_and_asked_again(self):
        # From an answers file with --yes setup stops before writing anything; in a terminal it is asked again
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'SUBSYNC_PUBLIC_INVITE': 'join$us'})
        self.assertEqual(code, 1, out)
        self.assertIn(f'SUBSYNC_PUBLIC_INVITE: {hosting.INVITE_REFUSED}', ' '.join(out.split()))
        self.assertFalse((self.state / 'subsync.env').exists())
        invite, out = self.ask_invite({'SUBSYNC_PUBLIC_INVITE': 'join$us'}, 'join us')
        self.assertEqual(invite, 'join us')
        self.assertIn(hosting.INVITE_REFUSED, out)
        # a code kept in state/subsync.env with such a character (written by hand) is asked again too
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'SUBSYNC_PUBLIC_INVITE': 'kept-code'})
        self.assertEqual(code, 0, out)
        path = self.state / 'subsync.env'
        path.write_text(path.read_text().replace('INVITE=kept-code', 'INVITE=movie$night'))
        code, out = self.run_setup({})
        self.assertEqual(code, 1, out)
        self.assertIn(f'SUBSYNC_PUBLIC_INVITE: {hosting.INVITE_REFUSED}', ' '.join(out.split()))
        invite, out = self.ask_invite({}, '', 'movie night')                      # Enter keeps it: refused, asked again
        self.assertEqual(invite, 'movie night')
        self.assertIn('Enter keeps: movie$night; type remove to delete it', out)

    def test_a_trusted_proxy_subsync_would_never_match_is_refused(self):
        # From an answers file setup stops before writing anything; one written by hand is refused on a re-run
        refused = "SUBSYNC_TRUSTED_PROXY: Type gateway or IP addresses separated by commas, like 172.18.0.1: {} isn't one."
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'SUBSYNC_TRUSTED_PROXY': '172.18.0.0/16'})
        self.assertEqual(code, 1, out)
        self.assertIn(refused.format('172.18.0.0/16'), ' '.join(out.split()))
        self.assertFalse((self.state / 'subsync.env').exists())
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'SUBSYNC_TRUSTED_PROXY': '172.18.0.1'})
        self.assertEqual(code, 0, out)
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_TRUSTED_PROXY'], '172.18.0.1')
        path = self.state / 'subsync.env'
        path.write_text(path.read_text().replace('PROXY=172.18.0.1', 'PROXY=caddy'))
        code, out = self.run_setup({})
        self.assertEqual(code, 1, out)
        self.assertIn(refused.format('caddy'), ' '.join(out.split()))

    def test_going_through_the_steps_again_keeps_the_running_version_and_its_rollback(self):
        # The steps moved a public instance to setup's own version with no copy of the databases, no check
        # beside the old one and no way back, and forgot the last update's record (./setup --rollback had nothing)
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        running = 'ghcr.io/haslanli/aiosubtitles:0.9.0@sha256:' + '1' * 64
        previous = {'image': 'ghcr.io/haslanli/aiosubtitles:0.8.0@sha256:' + '0' * 64, 'version': '0.8.0',
                    'local': False, 'databases': 'databases-20261101-000000'}
        engine.save_settings(dict(engine.load_settings(), image=running, local=False, version='0.9.0',
                                  previous=previous))
        code, out = self.run_setup({'SUBSYNC_MAX_USERS': '120'})
        self.assertEqual(code, 0, out)
        s = engine.load_settings()
        self.assertEqual((s['image'], s['local'], s['version'], s['previous']), (running, False, '0.9.0', previous))
        self.assertIn(f'    image: {running}\n', (self.state / 'compose.yaml').read_text())
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_MAX_USERS'], '120')     # the answer is

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
        self.assertEqual(code, 0, out)
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
        e = engine.Engine({'REMOVE': 'yes'}, page=True)
        e.remove_host()
        self.assertFalse((self.state / 'subsync.env').exists())
        self.assertFalse(self.up)

    def test_removing_in_a_terminal_says_what_stays_naming_the_database_copies(self):
        # it printed no list; the page's said "with copies of your config in state/backups/ (./setup --restore FILE puts
        # one back)", though a public instance keeps only the databases' copies there, with everyone's records
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        (self.state / 'backups' / 'databases-20261114-061500').mkdir(parents=True)
        code, out = self.run_setup({'REMOVE': 'yes'}, '--uninstall')
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
        code, out = self.run_setup({'REMOVE': 'no'}, '--uninstall')
        self.assertEqual(code, 1, out)
        self.assertTrue(out.rstrip().endswith('Nothing was changed.'), out[-200:])
        self.assertNotIn('needs an answer', out)                         # neither CONFIRM's nor REMOVE's
        self.assertTrue((self.state / 'subsync.env').exists())
        with self.assertRaises(engine.Problem) as caught:                   # the page: the box not ticked
            engine.Engine({}, page=True).remove_host()
        self.assertEqual((caught.exception.field, caught.exception.message),
                         ('REMOVE', 'Tick "Delete everyone\'s records and stored copies" to remove AIOSubtitles.'))

    def test_an_answers_file_with_confirm_and_no_remove_removes_nothing(self):
        # CONFIRM answered "Delete everyone's records and stored copies" too, and docs/answers.example ships
        # CONFIRM=yes, so the answers file a public instance was set up with, given to ./setup --uninstall, deleted
        # everyone's records and asked nothing; REMOVE alone decides now, and the example's REMOVE=no keeps them
        install = {'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'CONFIRM': 'yes'}
        code, out = self.run_setup(install)
        self.assertEqual(code, 0, out)
        example = Path(self.tmp.name) / 'example-answers'
        example.write_text(docs.answers_example())
        files = sorted(p.name for p in self.state.iterdir())
        for name, answers in (('the install', self.answers_file(install)), ('the example', str(example))):
            argv = ['--answers', answers, '--yes']
            with self.subTest(answers=name):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = wizard.main([*argv, '--uninstall'])
                out = out.getvalue()
                self.assertEqual(code, 1, out)
                self.assertIn("Delete everyone's records and stored copies [y/n]", out)
                self.assertTrue(out.rstrip().endswith('Nothing was changed.'), out[-200:])
                self.assertEqual(sorted(p.name for p in self.state.iterdir()), files)
                self.assertTrue(self.up)
        with self.assertRaises(engine.Problem) as caught:                   # the page: CONFIRM is not its box
            engine.Engine({'CONFIRM': 'yes'}, page=True).remove_host()
        self.assertEqual((caught.exception.field, caught.exception.message), ('REMOVE', screens.TICK_TO_REMOVE))
        self.assertTrue(self.up)
        code, out = self.run_setup({'CONFIRM': 'no', 'REMOVE': 'yes'}, '--uninstall')
        self.assertEqual(code, 0, out)
        self.assertFalse((self.state / 'subsync.env').exists())
        self.assertFalse(self.up)

    def test_a_failed_container_removal_keeps_the_public_instances_files(self):
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 0, out)
        with mock.patch.object(dockerenv, 'compose', lambda state, *args, **kw: (1, 'still running', '')):
            with self.assertRaises(engine.Stop) as caught:
                engine.Engine({'REMOVE': 'yes'}, page=True).remove_host()
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

    def test_the_rule_says_how_a_web_server_in_docker_reaches_it(self):
        # The rule points at this machine's 127.0.0.1, which a web server in a container (its own 127.0.0.1)
        # never reaches; your own install's route had a note for that case, a public instance's none
        note = ("Web server running in Docker: run it on this machine's network (network_mode: host), so it reaches "
                '127.0.0.1:8790.')
        with mock.patch.object(hosting, 'fetch', side_effect=OSError('refused')):
            code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS})
        self.assertEqual(code, 3, out)
        said = ' '.join(out.split())
        self.assertIn(note, said)
        self.assertLess(said.index('reverse_proxy 127.0.0.1:8790'), said.index(note))      # under the rule
        e = engine.Engine({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS}, page=True)
        with mock.patch.object(hosting, 'fetch', side_effect=OSError('refused')):
            e.check_machine()
            e.host_address()
            e.host_signups()
            e.host_choices()
            e.host_limits()
            e.host_install()
        self.assertEqual(e.rule_note, note)                                     # the page shows it under the rule

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

    def test_the_tmdb_key_for_everyone_is_checked_with_tmdb_first(self):
        # As on your own install, a new key is checked once; refused, nothing is set up
        self.tmdb_answer = False
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'TMDB_KEY': 'tmdb-key-1'})
        self.assertEqual(code, 1, out)
        self.assertIn(f"TMDB_KEY: {explain.PUBLIC_TEXTS['tmdb_refused']}", ' '.join(out.split()))
        self.assertFalse((self.state / 'subsync.env').exists())
        self.tmdb_answer = True
        code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': ADDRESS, 'TMDB_KEY': 'tmdb-key-1'})
        self.assertEqual(code, 0, out)
        self.assertIn('✓  TMDB accepts this key', out)
        self.assertEqual([key for _, key in self.tmdb_checked], ['tmdb-key-1', 'tmdb-key-1'])
        self.assertEqual(engine.read_kv(self.state / 'subsync.env')['SUBSYNC_TMDB_KEY'], 'tmdb-key-1')

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
