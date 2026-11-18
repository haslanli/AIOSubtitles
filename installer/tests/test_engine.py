"""The engine's own rules and its less common paths: the state files it writes and reads, what reaches the terminal,
the step headings, the address checks, AIOStreams' error codes, the recovery paths of the configs it keeps, the
shapes of AIOStreams other than one container on a Docker network (host network, elsewhere on the network, two of
them), the saved AIOStreams reached again by a later ./setup, the last step's checks when they don't hold, the wait for
the service, a public instance's port and the one wording of each question, against the stand-in AIOStreams of
test_flow with Docker stubbed out. Nothing connects beyond 127.0.0.1."""
import contextlib
import io
import os
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import dockerenv, engine, hosting, plan, screens, ui, wizard
from installer.tests.test_flow import IMAGE, PASSWORD, PUBLIC, UUID, FlowFixture
from installer.tests.test_usenet import ENGINE, NEWZNAB, SUBS, TORBOX, ZILEAN
from subsync import aiosconfig, explain

LAN_AIOS = 'http://192.168.1.5:3000'     # an AIOStreams elsewhere on the network, by its LAN address
HEADING = re.compile(r'Step (\d+) of (\d+) · (.+)')


def refused(status, body, doing):
    """The AiosError AIOStreams' client makes of a refused call (an instance's own: a body it can't read names the
    address)."""
    return aiosconfig.AiosApi(PUBLIC)._error(status, body, doing)


def flat(text):
    return ' '.join(text.split())


class TmpState(unittest.TestCase):
    """state/ in a temporary folder, and no colour in what is printed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        for patch in (mock.patch.object(engine, 'STATE', self.state), mock.patch.object(ui, 'COLOR', False)):
            patch.start()
            self.addCleanup(patch.stop)


class StateFiles(TmpState):
    def test_secrets_are_read_as_every_env_and_answers_file_is_read(self):
        # one parser: read_kv is plan.parse_env's non-empty values (it read quoted values, empty ones and odd keys
        # otherwise, so ./setup --restore read an answers file otherwise than --answers and --uninstall)
        text = ('# a comment\nPLAIN=ab  # a comment\nQUOTED="x #y"\nEMPTY=\nSPACED = v\nlower-key=z\n'
                'KEY=first\nKEY=second\n')
        path = Path(self.tmp.name) / 'file.env'
        path.write_text(text)
        self.assertEqual(engine.read_kv(path), {k: v for k, v in plan.parse_env(text).items() if v})
        self.assertEqual(engine.read_kv(path), {'PLAIN': 'ab', 'QUOTED': '"x #y"', 'KEY': 'second'})
        self.assertEqual(engine.read_kv(Path(self.tmp.name) / 'none.env'), {})

    def test_a_value_with_a_line_break_is_never_written(self):
        # a line break in a value (from AIOStreams' answers) would add settings of its own to subsync's environment:
        # every break str.splitlines() knows, since setup reads the file back with it, and NUL
        for brk in ('\n', '\r', '\r\n', '\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029', '\x00'):
            with self.subTest(brk=repr(brk)):
                value = f'x{brk}PYTHONWARNINGS=all:0:antigravity.x:0:0'
                with self.assertRaises(ui.Stop) as caught:
                    engine.write_env(self.state / 'subsync.env', {'SUBSYNC_LANGUAGES': 'eng', 'SUBSYNC_X': value})
                self.assertEqual(caught.exception.message, 'Setup stopped before writing state/subsync.env: the value '
                                                           'for SUBSYNC_X has a line break in it.')
                with self.assertRaises(ui.Stop) as caught:
                    engine.write_kv(self.state / 'secrets.env', {'ADDON_KEY': 'k', 'SYNC_UUID': value}, 'header')
                self.assertEqual(caught.exception.message, 'Setup stopped before writing state/secrets.env: the value '
                                                           'for SYNC_UUID has a line break in it.')
                self.assertFalse(self.state.exists())
        engine.write_env(self.state / 'subsync.env', {'SUBSYNC_LANGUAGES': 'eng', 'SUBSYNC_EXTRA_SOURCES': ''})
        self.assertEqual(plan.parse_env((self.state / 'subsync.env').read_text()),
                         {'SUBSYNC_LANGUAGES': 'eng', 'SUBSYNC_EXTRA_SOURCES': ''})
        engine.write_kv(self.state / 'secrets.env', {'ADDON_KEY': 'k' * 32, 'GONE': None, 'EMPTY': ''}, 'header')
        self.assertEqual(engine.read_kv(self.state / 'secrets.env'), {'ADDON_KEY': 'k' * 32})


class Terminal(TmpState):
    def said(self, *texts):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for text in texts:
                ui.say(text)
        return out.getvalue()

    def test_control_characters_never_reach_the_terminal(self):
        # text from AIOStreams is printed in setup's lines: no cursor moves, cleared lines, window titles or clipboard
        hostile = 'a\x1b]0;title\x07b\x1b[31mc\x1b[0m\rd\x00e\x7f\x9bf\x1b]52;c;eA==\x1b\\g\th\ni'
        self.assertEqual(self.said(hostile), 'a]0;titlebcdef]52;c;eA==\\gh\ni\n')
        self.assertEqual(self.said(ui.paint('32', 'ok')), 'ok\n')
        with mock.patch.object(ui, 'COLOR', True):          # paint's colour codes stay, anything else goes
            self.assertEqual(self.said(ui.paint('1;32', 'ok') + ' \x1b[2K\x1b[1Ax\x1b]0;t\x07'),
                             '\x1b[1;32mok\x1b[0m [2K[1Ax]0;t\n')

    def test_a_box_keeps_its_border_round_what_say_takes_out(self):
        for color in (False, True):
            with self.subTest(color=color), mock.patch.object(ui, 'COLOR', color):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    ui.box('Done', ['plain', 'AIOStreams 2.37\x1b]0;t\x07', ui.paint('1', 'bold')], width=40)
                rows = [ui.strip_ansi(line) for line in out.getvalue().splitlines() if line.startswith('  │')]
                self.assertEqual(len(rows), 4)
                self.assertEqual({len(row) for row in rows}, {42})            # the border: 40 wide, indented by 2
                self.assertTrue(any(row.startswith('  │ AIOStreams 2.37]0;t  ') for row in rows), rows)

    def test_step_headings_are_the_pages_steps(self):
        for route, titles, ids in (('yourself', screens.PERSONAL, screens.PERSONAL_IDS),
                                   ('host', screens.HOSTING, screens.HOSTING_IDS)):
            e = engine.Engine({})
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                for step_id in ids:
                    e.show_step(step_id, route)
            self.assertEqual(HEADING.findall(out.getvalue()),
                             [(str(i), str(len(ids)), title) for i, title in enumerate(titles, 1)])


class Addresses(unittest.TestCase):
    def test_an_address_with_a_port_that_cant_be_one_is_refused(self):
        # urlsplit lets these through and raises ValueError only when the port is read (later, outside the check)
        for bad in ('http://192.168.1.10:87900', 'http://host:99999', 'http://host:abc', 'http://host.example:0',
                    'https://a.example.com:-1/subsync'):
            with self.subTest(bad=bad):
                self.assertEqual(plan.check_public(bad), (False, 'Type a full address that starts with http:// or '
                                                                 'https://', bad))
        for good in ('http://192.168.1.10:8790', 'https://a.example.com:65535/subsync', 'http://host'):
            with self.subTest(good=good):
                self.assertEqual(plan.check_public(good), (True, '', good))

    def test_a_public_instances_address_with_a_port_that_cant_be_one_is_refused(self):
        for bad in ('https://subs.example.com:abc', 'https://subs.example.com:99999', 'https://subs.example.com:0'):
            with self.subTest(bad=bad):
                self.assertEqual(hosting.check_address(bad), (False, hosting.ADDRESS_WORDS, bad))
        self.assertEqual(hosting.check_address('https://Subs.Example.com:8443/'), (True, '', 'https://subs.example.com:8443'))


class ErrorCodes(TmpState):
    def test_a_sign_in_request_is_told_by_its_code_and_aiostreams_own_words(self):
        for status, body, asks in (
                (403, {'error': {'code': 'FORBIDDEN', 'message': 'nope'}}, True),
                (401, {'error': {'code': 'UNAUTHORIZED'}}, True),
                (403, {'error': {'message': 'Not allowed without an account'}}, True),
                (400, {'error': {'code': 'USER_INVALID_CONFIG', 'message': 'not allowed: Real-Debrid'}}, False),
                (401, {'error': {'code': 'USER_INVALID_DETAILS', 'message': 'not allowed'}}, False),
                (500, {'error': {'code': 'X', 'message': 'please sign in first'}}, False)):
            with self.subTest(body=body):
                e = refused(status, body, 'Saving the config')
                self.assertIs(aiosconfig.needs_sign_in(e), asks)
                self.assertEqual('asks you to sign in' in e.message, asks)       # what its message says, exactly
        for code in aiosconfig.WRONG_PASSWORD_CODES:
            self.assertIn("don't match", aiosconfig._plain(code, 'words'))

    def test_aiostreams_own_words_never_start_a_sign_in(self):
        e = engine.Engine({}, page=True)
        e.aios = engine.Aios('http://127.0.0.1:9')
        refusal = refused(500, {'error': {'code': 'X', 'message': 'please sign in first'}}, 'Saving the config')
        with self.assertRaises(engine.AiosError) as caught:
            e.with_sign_in(mock.Mock(side_effect=refusal))
        self.assertIs(caught.exception, refusal)
        forbidden = refused(403, {'error': {'code': 'FORBIDDEN'}}, 'Saving the config')
        with self.assertRaises(engine.Problem) as caught:                        # this one asks for the sign-in
            e.with_sign_in(mock.Mock(side_effect=forbidden))
        self.assertEqual((caught.exception.field, caught.exception.missing), ('AIOS_USERNAME', True))


class Offline(FlowFixture):
    """The flow fixture with every connection beyond this machine refused (an address on the LAN included), and no
    colour in what is printed."""

    def setUp(self):
        super().setUp()
        real = socket.create_connection

        def local_only(address, *args, **kwargs):
            if address[0] not in ('127.0.0.1', 'localhost', '::1'):
                raise AssertionError(f'a connection to {address[0]} was opened')
            return real(address, *args, **kwargs)
        for patch in (mock.patch('socket.create_connection', local_only), mock.patch.object(ui, 'COLOR', False)):
            patch.start()
            self.patches.append(patch)

    def keys(self):
        return engine.read_kv(self.state / 'secrets.env')

    def env(self):
        return engine.read_kv(self.state / 'subsync.env')

    def compose(self):
        return (self.state / 'compose.yaml').read_text()

    def ours(self):
        """subsync's entries in the viewer's config: the address AIOStreams calls it at."""
        return [p['options']['manifestUrl'] for p in self.aios.users[UUID]['config']['presets'] if p['type'] == 'custom']

    def page_engine(self, answers):
        """The page's engine through "The config your apps use", with these answers."""
        e = engine.Engine(dict(answers, AIOS_CONFIG_ID=UUID, AIOS_CONFIG_PASSWORD=PASSWORD), page=True)
        e.check_machine()
        e.find_aios()
        e.read_viewer()
        return e

    def install(self, **answers):
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no', **answers))
        self.assertEqual(code, 0, out)
        return self.keys()

    def again(self, *extra, **answers):
        return self.run_setup(dict({'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no'}, **answers), *extra)


class StateFlows(Offline):
    def test_a_line_break_in_aiostreams_answer_never_reaches_subsyncs_settings(self):
        real = engine.Aios.create_config

        def hostile(aios, config, password):
            uuid, _ = real(aios, config, password)
            return uuid, 'x\nPYTHONWARNINGS=all:0:antigravity.x:0:0\nZ=z'
        with mock.patch.object(engine.Aios, 'create_config', hostile):
            code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 1, out)
        self.assertIn('Setup stopped before writing state/subsync.env: the value for SUBSYNC_SOURCE_MANIFEST has a line '
                      'break in it.', flat(out))
        self.assertFalse((self.state / 'subsync.env').exists())
        self.assertFalse(self.aios.subsync_up)                                  # nothing started
        self.assertEqual(self.ours(), [])                                        # the viewer's config unchanged

    def test_a_line_break_in_a_config_id_never_reaches_the_keys(self):
        real = engine.Aios.create_config

        def hostile(aios, config, password):
            uuid, encrypted = real(aios, config, password)
            return f'{uuid}\nADDON_KEY=attacker', encrypted
        with mock.patch.object(engine.Aios, 'create_config', hostile):
            code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 1, out)
        self.assertIn('Setup stopped before writing state/secrets.env: the value for SYNC_UUID has a line break in it.',
                      flat(out))
        secrets = self.state / 'secrets.env'
        self.assertNotIn('attacker', secrets.read_text() if secrets.exists() else '')

    def test_restore_reads_its_answers_file_as_every_answers_file_is_read(self):
        self.install()
        copy = next((self.state / 'backups').iterdir())
        text = 'AIOS_CONFIG_PASSWORD="pass #word"\nCONFIRM=yes\n'
        self.aios.users[UUID]['password'] = plan.parse_env(text)['AIOS_CONFIG_PASSWORD']    # as --answers reads it
        answers = Path(self.tmp.name) / 'restore-answers'
        answers.write_text(text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(['--restore', str(copy), '--answers', str(answers), '--yes'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('Restored.', out.getvalue())
        self.assertEqual(self.ours(), [])                                         # the copy from before subsync

    def test_text_from_aiostreams_reaches_the_terminal_without_control_characters(self):
        real = engine.Aios.status

        def hostile(aios):
            return dict(real(aios), version='2.37.0\x1b]0;owned\x07\x1b[2K\x1b[1A\r')
        with mock.patch.object(engine.Aios, 'status', hostile):
            code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 0, out)
        self.assertIn('Found AIOStreams 2.37.0]0;owned[2K[1A in the container "aiostreams"', out)
        for char in ('\x1b', '\x07', '\r'):
            self.assertNotIn(char, out)

    def test_the_terminals_step_headings_are_the_pages_steps(self):
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 0, out)
        self.assertEqual(HEADING.findall(out), [(str(i), '8', title) for i, title in enumerate(screens.PERSONAL, 1)])

    def test_an_address_with_a_port_that_cant_be_one_is_asked_again(self):
        e = self.page_engine({'LANGUAGES': 'English', 'PUBLIC_ADDRESS': 'http://192.168.1.10:87900'})
        with self.assertRaises(engine.Problem) as caught:
            e.choices()
        self.assertEqual((caught.exception.field, caught.exception.missing), ('PUBLIC_ADDRESS', False))
        self.assertEqual(caught.exception.message, 'Type a full address that starts with http:// or https://')
        e = engine.Engine({'HOST_ADDRESS': 'https://subs.example.com:abc'}, page=True)
        with self.assertRaises(engine.Problem) as caught:
            e.host_address()
        self.assertEqual((caught.exception.field, caught.exception.message), ('HOST_ADDRESS', hosting.ADDRESS_WORDS))


class ConfigRecovery(Offline):
    """What setup does when a config it keeps is gone, refused, or not saved as sent, and with AIOStreams' cache
    setting when it can't be changed (the stand-in deletes and shows the settings only with the right sign-in)."""

    def test_the_stand_in_deletes_only_with_the_configs_password(self):
        sync_id = self.install()['SYNC_UUID']
        with self.assertRaises(engine.AiosError) as caught:
            engine.Aios(self.aios.url).delete_config(sync_id, 'wrong')
        self.assertIn(str(caught.exception.code), engine.WRONG_PASSWORD_CODES)
        self.assertIn(sync_id, self.aios.users)

    def test_the_stand_ins_settings_need_an_admins_sign_in(self):
        api = engine.Aios(self.aios.url)
        with self.assertRaises(engine.AiosError):
            api.subtitle_cache_setting()
        with self.assertRaises(engine.AiosError):
            api.set_setting('resources.cache.subtitle.ttl', {'*': 1})
        self.assertEqual(self.aios.setting, {'*': 300})

    def test_a_sync_config_gone_from_aiostreams_is_made_again(self):
        before = self.install()
        old = before['SYNC_UUID']
        del self.aios.users[old]
        made = len([1 for m, p, _ in self.aios.log if (m, p) == ('POST', '/api/v1/user')])
        code, out = self.again()
        self.assertEqual(code, 0, out)
        self.assertIn("subsync's old config is gone from AIOStreams; making a new one.", out)
        self.assertEqual(len([1 for m, p, _ in self.aios.log if (m, p) == ('POST', '/api/v1/user')]), made + 1)
        new = self.keys()['SYNC_UUID']
        self.assertIn(new, self.aios.users)
        self.assertEqual(self.aios.users[new]['password'], self.keys()['SYNC_PASSWORD'])     # made new, with its own
        self.assertNotEqual(self.keys()['SYNC_PASSWORD'], before['SYNC_PASSWORD'])
        self.assertIn(f'/stremio/{new}/', self.env()['SUBSYNC_SOURCE_MANIFEST'])

    def test_a_refused_sync_config_is_never_taken_for_a_gone_one(self):
        # AIOStreams' own words in a refusal (here with "match" in them) are no sign that the config is gone: only its
        # wrong-password codes are; a refused config stops setup, and no second sync config is made
        sync_id = self.install()['SYNC_UUID']
        real = engine.Aios.save_config

        def refusing(aios, uuid, password, config):
            if uuid == sync_id:
                raise refused(400, {'error': {'code': 'USER_INVALID_CONFIG',
                                              'message': 'the services do not match the presets'}}, 'Saving the config')
            return real(aios, uuid, password, config)
        users = set(self.aios.users)
        with mock.patch.object(engine.Aios, 'save_config', refusing):
            code, out = self.again()
        self.assertEqual(code, 1, out)
        self.assertIn('AIOStreams refused the config: the services do not match the presets', out)
        self.assertNotIn('is gone from AIOStreams', out)
        self.assertEqual(set(self.aios.users), users)
        self.assertEqual(self.keys()['SYNC_UUID'], sync_id)

    def test_a_config_no_longer_used_is_forgotten_only_when_it_is_gone(self):
        self.aios.users[UUID]['config'] = {'services': [TORBOX, ENGINE], 'presets': [ZILEAN, NEWZNAB, SUBS]}
        usenet_id = self.install(USENET='yes')['USENET_UUID']
        e = engine.Engine({}, page=True)
        e.aios = engine.Aios(self.aios.url)
        e.aios.signed_in = True                                   # a refusal asking for a sign-in is not asked again
        for code, forgotten in (('USER_INVALID_CONFIG', False), ('FORBIDDEN', False), ('USER_NOT_FOUND', True)):
            with self.subTest(code=code):
                e.keys = self.keys()
                refusal = refused(400, {'error': {'code': code, 'message': "doesn't match"}}, 'Removing the sync config')
                with mock.patch.object(e.aios, 'delete_config', mock.Mock(side_effect=refusal)):
                    if forgotten:
                        e.drop_config('USENET', "Deleted subsync's Usenet config")
                    else:
                        with self.assertRaises(engine.AiosError):
                            e.drop_config('USENET', "Deleted subsync's Usenet config")
                self.assertEqual('USENET_UUID' not in self.keys(), forgotten)
        self.assertIn(usenet_id, self.aios.users)

    def test_a_usenet_config_already_gone_is_forgotten_when_usenet_is_turned_off(self):
        self.aios.users[UUID]['config'] = {'services': [TORBOX, ENGINE], 'presets': [ZILEAN, NEWZNAB, SUBS]}
        usenet_id = self.install(USENET='yes')['USENET_UUID']
        del self.aios.users[usenet_id]
        code, out = self.again(USENET='no')
        self.assertEqual(code, 0, out)
        self.assertNotIn('USENET_UUID', self.keys())
        self.assertNotIn("Deleted subsync's Usenet config", out)
        self.assertIn(('DELETE', False, None), self.aios.log)                 # asked, and refused: it was gone

    def test_a_config_saved_without_subsync_stops_with_the_saved_copy(self):
        real = engine.Aios.save_config

        def dropping(aios, uuid, password, config):
            if uuid == UUID:
                config = dict(config, presets=[p for p in config['presets'] if p['type'] != 'custom'])
            return real(aios, uuid, password, config)
        with mock.patch.object(engine.Aios, 'save_config', dropping):
            code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 1, out)
        copy = next((self.state / 'backups').iterdir())
        self.assertIn('AIOStreams saved the config, but subsync is not in it when read back. Your saved copy is '
                      f'state/backups/{copy.name}; ./setup --restore puts it back.', flat(out))

    def cache_setting(self, **stand_in):
        """A first install that asks to turn the cache off, signed in as admin, with the stand-in set so."""
        for name, value in stand_in.items():
            setattr(self.aios, name, value)
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no', CACHE='yes', AIOS_USERNAME='admin',
                                        AIOS_PASSWORD='adminpw'))
        self.assertEqual(code, 0, out)
        self.assertNotIn('AIOStreams: no subtitle cache for subsync', out)
        self.assertEqual(self.aios.setting, {'*': 300})                       # unchanged
        return flat(out)

    def test_the_cache_setting_with_an_account_that_is_not_an_admin(self):
        self.assertIn('That account is not an admin; the setting stays as it is.', self.cache_setting(admin=False))
        self.assertFalse([p for m, p, _ in self.aios.log if p == '/api/v1/dashboard/settings'])   # never asked

    def test_the_cache_setting_on_an_aiostreams_without_it(self):
        self.assertIn('This AIOStreams version has no subtitle cache setting; nothing to change.',
                      self.cache_setting(has_cache_setting=False))

    def test_the_cache_setting_fixed_by_aiostreams_environment(self):
        self.assertIn("The setting is fixed by AIOStreams' environment (SUBTITLE_CACHE_TTL); change it there: "
                      'SUBTITLE_CACHE_TTL=*:300,aiosubtitles-subsync:-1', self.cache_setting(setting_source='environment'))

    def test_the_cache_setting_not_kept(self):
        self.assertIn('AIOStreams did not keep the cache setting; synced copies may take 5 minutes.',
                      self.cache_setting(keeps_setting=False))

    def test_uninstall_warns_when_a_config_of_subsyncs_is_gone(self):
        sync_id = self.install()['SYNC_UUID']
        del self.aios.users[sync_id]
        code, out = self.again('--uninstall', CONFIRM='yes')
        self.assertEqual(code, 0, out)
        self.assertIn("Removing the sync config: That config ID and password don't match a config on this AIOStreams.",
                      out)
        self.assertNotIn("Deleted subsync's own config", out)
        self.assertIn('Done.', out)
        self.assertFalse((self.state / 'secrets.env').exists())


class AiostreamsShapes(Offline):
    """AIOStreams on the host network, elsewhere on the network (a LAN address), and two of them on this machine."""

    def container(self, **kw):
        return dict({'name': 'aiostreams', 'image': 'ghcr.io/viren070/aiostreams:latest', 'port': 3000, 'base_url': PUBLIC,
                     'host_network': False, 'networks': ['aio_default'], 'ips': [], 'published': []}, **kw)

    def test_aiostreams_on_the_host_network(self):
        with mock.patch.object(dockerenv, 'aios_containers', lambda: [self.container(host_network=True, networks=[])]):
            self.install()
        compose, env, key = self.compose(), self.env(), self.keys()['ADDON_KEY']
        self.assertIn('    network_mode: host\n', compose)
        self.assertNotIn('ports:', compose)
        self.assertNotIn('networks:', compose)
        self.assertEqual(env['SUBSYNC_PORT'], '8790')
        self.assertEqual(env['SUBSYNC_URL_REWRITE'], f'{PUBLIC}=http://127.0.0.1:3000')
        self.assertTrue(env['SUBSYNC_SOURCE_MANIFEST'].startswith('http://127.0.0.1:3000/stremio/'))
        self.assertEqual(env['SUBSYNC_PUBLIC_BASE'], f'{PUBLIC}/subsync')
        self.assertEqual(self.ours(), [f'http://127.0.0.1:8790/{key}/manifest.json'])
        s = engine.load_settings()
        self.assertEqual((s['mode'], s['public_kind'], s['bind'], s['port']), ('host', 'path', None, 8790))

    def test_aiostreams_elsewhere_on_the_network(self):
        # the common LAN case: no AIOStreams container here, AIOStreams at http://192.168.x.x:3000; subsync on this
        # machine's own port, on every interface, called by AIOStreams at that address
        real = engine.Aios.status

        def status(aios):
            data = real(aios)
            return dict(data, settings=dict(data['settings'], baseUrl=LAN_AIOS))
        with mock.patch.object(dockerenv, 'aios_containers', lambda: []), \
                mock.patch.object(engine.Aios, 'status', status), \
                mock.patch.object(engine.Engine, 'public_health', lambda e: self.aios.subsync_up):
            code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no', AIOS_URL=self.aios.url))
        self.assertEqual(code, 0, out)
        compose, env, key = self.compose(), self.env(), self.keys()['ADDON_KEY']
        self.assertIn('    ports: ["0.0.0.0:8790:8790"]\n', compose)
        self.assertNotIn('network_mode', compose)
        self.assertNotIn('aios0', compose)
        self.assertNotIn('SUBSYNC_URL_REWRITE', env)
        self.assertNotIn('SUBSYNC_PORT', env)
        self.assertNotIn('SUBSYNC_BASE_PATH', env)
        self.assertEqual(env['SUBSYNC_PUBLIC_BASE'], 'http://192.168.1.5:8790')
        self.assertTrue(env['SUBSYNC_SOURCE_MANIFEST'].startswith(f'{LAN_AIOS}/stremio/'))
        self.assertEqual(self.ours(), [f'http://192.168.1.5:8790/{key}/manifest.json'])
        self.assertNotIn('Your web server needs one rule', out)
        s = engine.load_settings()
        self.assertEqual((s['mode'], s['public_kind'], s['bind'], s['port']), ('remote', 'port', '0.0.0.0', 8790))

    def test_an_address_without_a_port_takes_its_schemes(self):
        for address, port in (('http://192.168.1.5', 80), ('https://192.168.1.5', 443),
                              ('https://subs.example.com:8443', 8443)):
            with self.subTest(address=address):
                e = self.page_engine({'LANGUAGES': 'English', 'PUBLIC_ADDRESS': address})
                e.choices()
                self.assertEqual((e.s['public_kind'], e.s['port'], e.s['bind']), ('port', port, '0.0.0.0'))
                self.assertIn(f'ports: ["0.0.0.0:{port}:8790"]', engine.compose_for(dict(e.s, image=IMAGE, install_id='i')))

    def test_a_port_in_use_on_every_interface_is_asked_again(self):
        asked = []
        with mock.patch.object(dockerenv, 'port_in_use', lambda port, host: asked.append((port, host)) or True), \
                mock.patch.object(dockerenv, 'container_state', lambda name: ''):
            e = self.page_engine({'LANGUAGES': 'English', 'PUBLIC_ADDRESS': 'http://192.168.1.5'})
            with self.assertRaises(engine.Problem) as caught:
                e.choices()
        self.assertEqual((caught.exception.field, caught.exception.message),
                         ('PUBLIC_ADDRESS', 'Port 80 on this machine is already in use.'))
        self.assertEqual(asked, [(80, '0.0.0.0')])

    def test_two_aiostreams_on_this_machine(self):
        two = [self.container(name='aio-one', networks=['one_default']),
               self.container(name='aio-two', networks=['two_default'])]
        with mock.patch.object(dockerenv, 'aios_containers', lambda: two):
            e = engine.Engine({}, page=True)
            e.check_machine()
            with self.assertRaises(engine.Problem) as caught:             # the page asks which one
                e.find_aios()
            self.assertEqual((caught.exception.field, caught.exception.missing), ('AIOS_CONTAINER', True))
            code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no', AIOS_CONTAINER='aio-two'))
            self.assertEqual(code, 0, out)
            self.assertIn('More than one AIOStreams runs here. Which one do your apps use?', out)
            compose, env = self.compose(), self.env()
            self.assertIn('  aios0: {external: true, name: "two_default"}', compose)
            self.assertNotIn('one_default', compose)
            self.assertEqual(env['SUBSYNC_URL_REWRITE'], f'{PUBLIC}=http://aio-two:3000')
            self.assertTrue(env['SUBSYNC_SOURCE_MANIFEST'].startswith('http://aio-two:3000/stremio/'))
            self.assertEqual(engine.load_settings()['container'], 'aio-two')
            code, out = self.again()
            self.assertEqual(code, 0, out)
            self.assertEqual(engine.load_settings()['container'], 'aio-two')      # a re-run keeps the choice


class OnBridge(Offline):
    """Setup on Docker's bridge network (the image route), set up as test_flow.Bridge sets it up."""

    def setUp(self):
        super().setUp()
        self.place = self.place._replace(network='bridge', gateway='172.17.0.1', page_port=8789)
        self.joined, self.probed = [], []

        def probe(image, checks):
            self.probed.append((image, list(checks)))
            return {'lan': '192.168.1.7', 'taken': [f'{h}:{p}' for h, p in checks if p == 8790]}
        for p in (mock.patch.object(engine, 'PLACE', self.place),
                  mock.patch.object(dockerenv, 'join', lambda net, c: self.joined.append((net, c)) or True),
                  mock.patch.object(dockerenv, 'probe', probe)):
            p.start()
            self.patches.append(p)
        self.status = engine.Aios(self.aios.url).status()

    def test_a_rerun_with_the_last_address_filled_in_finds_aiostreams_again(self):
        # "Change your answers" fills in the last address, http://aiostreams:3000, which only a container on
        # AIOStreams' network resolves: setup's container joins it when it finds AIOStreams, so finding comes first
        def status(aios):
            if aios.base == 'http://aiostreams:3000' and self.joined:
                return self.status
            raise engine.AiosError(f"Can't reach AIOStreams at {aios.base} (no such host).")
        engine.save_settings({'installed': '2026-11-01', 'aios_url': 'http://aiostreams:3000'})
        filled = engine.Engine({}, page=True).prefill()
        self.assertEqual(filled['AIOS_URL'], 'http://aiostreams:3000')
        with mock.patch.object(engine.Aios, 'status', status):
            e = engine.Engine({k: v for k, v in filled.items() if k != 'installed'}, page=True)   # as the page starts
            e.check_machine()
            e.find_aios()
        self.assertEqual(self.joined, [('aio_default', 'c0ffee')])
        self.assertEqual((e.s['aios_url'], e.s['mode']), ('http://aiostreams:3000', 'network'))
        self.assertIn(('ok', 'Found AIOStreams 2.37.0 in the container "aiostreams"'), [tuple(r[:2]) for r in e.out.rows])

    def test_an_address_given_is_used_when_the_container_cant_be_reached(self):
        gateway = f'http://172.17.0.1:{self.aios.server.server_address[1]}'    # this machine, from setup's container

        def status(aios):
            if aios.base == gateway:
                return self.status
            raise engine.AiosError(f"Can't reach AIOStreams at {aios.base}.")
        with mock.patch.object(engine.Aios, 'status', status), \
                mock.patch.object(engine.Engine, 'aios_urls', lambda e, c: ['http://aiostreams:3000']):
            e = engine.Engine({'AIOS_URL': self.aios.url}, page=True)
            e.check_machine()
            e.find_aios()
        rows = [tuple(r[:2]) for r in e.out.rows]
        self.assertIn(('warn', 'The container "aiostreams" looks like AIOStreams, but I can\'t reach it from here.'), rows)
        self.assertIn(('ok', 'AIOStreams 2.37.0 answers there'), rows)
        self.assertEqual(e.s['aios_url'], gateway)

    def lan_lookups(self):
        return [checks for _, checks in self.probed if not checks]

    def address_engine(self, answers, page=True):
        e = engine.Engine(answers, page=page)
        e.s.update(aios_public='http://127.0.0.1:3000', mode='network')    # AIOStreams' own address is local
        return e

    def test_the_lan_address_is_looked_up_only_for_a_default_that_needs_it(self):
        # on the bridge network each lookup starts a helper container
        e = self.address_engine({})                       # no answer, no last one: the default is the LAN address
        e.public_address()
        self.assertEqual(e.s['public_base'], 'http://192.168.1.7:8790')
        self.assertEqual(len(self.lan_lookups()), 1)

        self.probed.clear()
        e = self.address_engine({'PUBLIC_ADDRESS': 'http://192.168.1.7:8791'})       # the page sent its answer
        e.public_address()
        self.assertEqual(self.probed, [(IMAGE, [('0.0.0.0', 8791)])])               # its port only

        engine.save_settings({'public_base': 'http://192.168.1.7:8791', 'port': 8791})
        self.probed.clear()
        for answers in ({}, {'PUBLIC_ADDRESS': 'http://192.168.1.7:8791'}):          # a last answer to keep
            with self.subTest(answers=answers):
                e = self.address_engine(answers)
                e.public_address()
                self.assertEqual(e.s['public_base'], 'http://192.168.1.7:8791')
        self.assertEqual(self.probed, [])

        engine.save_settings({})                          # the terminal shows the default by its question
        e = self.address_engine({'PUBLIC_ADDRESS': 'http://192.168.1.7:8791'}, page=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            e.public_address()
        self.assertIn('Enter = http://192.168.1.7:8790', out.getvalue())
        self.assertEqual(len(self.lan_lookups()), 1)


class SavedAiostreams(Offline):
    """Removing subsync and restoring a copy of the config reach the AIOStreams the install found. On the bridge network
    each ./setup runs in a new container, and Docker resolves AIOStreams' container name only for a container on its
    network."""

    def setUp(self):
        super().setUp()
        self.place = self.place._replace(network='bridge', gateway='172.17.0.1', page_port=8789)
        self.joined, self.joins = [], True
        connect = socket.create_connection                  # Offline's: nothing beyond this machine
        fake = ('127.0.0.1', self.aios.server.server_address[1])

        def docker_dns(address, *args, **kwargs):
            if address[0] == 'aiostreams':
                if not self.joined:
                    raise socket.gaierror(socket.EAI_NONAME, 'Name or service not known')
                address = fake
            return connect(address, *args, **kwargs)

        def join(network, container):
            if self.joins:
                self.joined.append((network, container))
            return self.joins
        for p in (mock.patch.object(engine, 'PLACE', self.place), mock.patch.object(dockerenv, 'join', join),
                  mock.patch.object(dockerenv, 'probe', lambda image, checks: {'lan': '192.168.1.7', 'taken': []}),
                  mock.patch('socket.create_connection', docker_dns)):
            p.start()
            self.patches.append(p)
        self.install()
        self.assertEqual(engine.load_settings()['aios_url'], 'http://aiostreams:3000')      # found by its name
        self.joined.clear()                                 # the next ./setup: a new container

    def test_removing_on_the_page_joins_aiostreams_network_again(self):
        e = engine.Engine({'AIOS_CONFIG_PASSWORD': PASSWORD, 'REMOVE_OPENSUBTITLES': 'yes', 'CONFIRM': 'yes'}, page=True)
        e.remove()
        self.assertEqual(self.joined, [('aio_default', 'c0ffee')])
        self.assertEqual(self.ours(), [])
        self.assertIn(('ok', 'Done.'), [tuple(r[:2]) for r in e.out.rows])

    def test_uninstall_in_a_terminal_joins_aiostreams_network_again(self):
        sync_id = self.keys()['SYNC_UUID']
        code, out = self.again('--uninstall', CONFIRM='yes')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ours(), [])
        self.assertNotIn(sync_id, self.aios.users)

    def test_restore_joins_aiostreams_network_again(self):
        copy = next((self.state / 'backups').iterdir())
        answers = Path(self.tmp.name) / 'restore-answers'
        answers.write_text(f'AIOS_CONFIG_PASSWORD={PASSWORD}\nCONFIRM=yes\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(['--restore', str(copy), '--answers', str(answers), '--yes'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(self.joined, [('aio_default', 'c0ffee')])
        self.assertEqual(self.ours(), [])                                         # the copy from before subsync

    def test_aiostreams_own_address_when_the_saved_one_cant_be_reached(self):
        self.joins = False                                   # its network is gone (renamed by its compose project)
        s = engine.load_settings()
        engine.save_settings(dict(s, aios_public=self.aios.url))
        e = engine.Engine({'AIOS_CONFIG_PASSWORD': PASSWORD, 'REMOVE_OPENSUBTITLES': 'yes', 'CONFIRM': 'yes'}, page=True)
        e.remove()
        self.assertEqual(self.ours(), [])


class SelfCheck(Offline):
    """The personal route's last step when a check doesn't hold: its row, and code 3 for the whole run."""

    def test_the_stand_in_passes_subtitle_requests_on_only_from_a_config_that_lists_subsync(self):
        keys = self.install()
        aios, before = engine.Aios(self.aios.url), self.aios.subsync_calls
        aios.addon_get(f'{self.aios.url}/stremio/{UUID}/ENC-1a2b', '/subtitles/movie/tt0816692.json')
        self.assertEqual(self.aios.subsync_calls, before + 1)
        aios.addon_get(f"{self.aios.url}/stremio/{keys['SYNC_UUID']}/ENC", '/subtitles/movie/tt0816692.json')
        self.assertEqual(self.aios.subsync_calls, before + 1)                   # subsync's own config leaves it out

    def test_aiostreams_not_reaching_subsync_is_a_problem(self):
        self.aios.reaches_subsync = False
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 3, out)
        self.assertIn('✗  AIOStreams did not reach subsync.', out)
        self.assertIn('It calls http://aiosubtitles-subsync:8790/…; check that address works from AIOStreams.', flat(out))
        self.assertNotIn('AIOStreams asks subsync for subtitles', out)
        self.assertIn('Installed, with warnings (see above)', out)

    def test_the_address_apps_use_not_answering_is_a_warning(self):
        real = engine.Aios.call

        def call(aios, method, path, *a, **k):
            if aios.base.startswith(PUBLIC) and path == '/health':        # the web server has no rule for it yet
                return 502, b''
            return real(aios, method, path, *a, **k)
        with mock.patch.object(engine.Aios, 'call', call):
            code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 3, out)
        self.assertIn(f"!  {PUBLIC}/subsync/health doesn't answer from this machine.", out)
        self.assertIn(flat(' '.join(hosting.NO_ANSWER_NOTES)), flat(out))     # the hosting route's notes, word for word
        self.assertIn('AIOStreams asks subsync for subtitles', out)              # the other checks still run

    def test_no_ready_file_for_the_two_films_is_a_warning(self):
        self.aios.sizes = []
        code, out = self.run_setup(self.ANSWERS)
        self.assertEqual(code, 3, out)
        self.assertIn("!  subsync's config found no ready debrid file for two well-known films.", out)
        self.assertIn('If your apps do show ready files for them, run ./setup --status and send its output with a bug '
                      'report.', flat(out))
        self.assertEqual([c for c in self.exec_calls if 'subsync.selftest' in c[0]], [])     # nothing to read

    def test_the_usenet_config_not_answering_is_a_warning(self):
        real = engine.Aios.addon_get

        def addon_get(aios, base, path, **kw):
            if path == '/manifest.json':
                raise engine.AiosError('AIOStreams answered HTTP 500 for manifest.json.')
            return real(aios, base, path, **kw)
        with mock.patch.object(engine.Aios, 'addon_get', addon_get):
            code, out = self.run_setup(dict(self.ANSWERS, USENET='yes', TEST_READ='no'))
        self.assertEqual(code, 3, out)
        self.assertIn(flat("!  subsync's Usenet config doesn't answer. Run ./setup again, or send the output of "
                           './setup --status with a bug report.'), flat(out))

    def test_each_test_read_result(self):
        cases = [
            ({'ok': False, 'error': 'refused', 'bytes': 0, 'requests': 1}, 3,
             ['!  The test read was refused by the debrid service (HTTP 403 or 429), most likely a limit on your account.',
              'subsync itself is installed and waits a while after a refusal. Try ./setup again later.']),
            ({'ok': True, 'note': 'subsync reads this file another way', 'bytes': 100000, 'requests': 1}, 0,
             ['✓  Read works (0.1 MB in 1 requests); subsync reads this file another way']),
            ({'ok': False, 'error': 'read', 'detail': 'the link answered HTTP 404', 'bytes': 0, 'requests': 1}, 3,
             ["!  The test read didn't work: the link answered HTTP 404.",
              'subsync is installed. Run ./setup again later, or send the output of ./setup --status with a bug '
              'report.']),
            ({'ok': False}, 3, ["!  The test read didn't work: no answer from subsync."]),
        ]
        for selftest, expected, lines in cases:
            with self.subTest(selftest=selftest):
                self.selftest = selftest
                code, out = self.run_setup(self.ANSWERS)
                self.assertEqual(code, expected, out)
                for line in lines:
                    self.assertIn(flat(line), flat(out))

    def test_an_update_or_a_rollback_whose_check_fails_ends_with_code_3(self):
        for flag, method in (('--update', 'update'), ('--rollback', 'rollback')):
            for result, expected in ((True, 0), (False, 3)):
                with self.subTest(flag=flag, result=result), \
                        mock.patch.object(engine.Engine, method, lambda e, *a, result=result, **k: result), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(wizard.main([flag]), expected)


class WaitHealthy(TmpState):
    """The wait for the service after a start: its /health asked inside its container, not Docker's health check, whose
    first probe runs only after 30 seconds on Docker Engine 23 and 24."""

    def wait(self, answers, seconds=90):
        clock, asked, slept = [0.0], [], []

        def exec_in(name, cmd, stdin=None, timeout=120):
            asked.append((name, cmd))
            return answers(len(asked))

        def sleep(s):
            slept.append(s)
            clock[0] += s
            if len(slept) > 100:          # a wait that would never end (90 seconds is 45 questions)
                raise AssertionError('the wait never ends')
        with mock.patch.object(dockerenv, 'exec_in', exec_in), \
                mock.patch.object(dockerenv, 'container_state', lambda name: 'running starting'), \
                mock.patch.object(engine.time, 'sleep', sleep), \
                mock.patch.object(engine.time, 'monotonic', lambda: clock[0]):
            return engine.Engine({}, page=True).wait_healthy(seconds), asked, slept

    def test_the_service_answering_ends_the_wait_at_once(self):
        healthy, asked, slept = self.wait(lambda n: (0, 'ok\n', '') if n == 3 else (1, '', 'Connection refused'))
        self.assertTrue(healthy)                          # while Docker's own check still says "starting"
        self.assertEqual(asked, [(engine.CONTAINER, engine.HEALTH)] * 3)
        self.assertEqual(slept, [2, 2])

    def test_a_service_that_never_answers_is_given_its_seconds(self):
        healthy, asked, slept = self.wait(lambda n: (1, '', 'Connection refused'))
        self.assertFalse(healthy)
        self.assertEqual(sum(slept), 90)
        self.assertEqual(len(asked), 45)

    def test_the_question_goes_to_the_port_subsync_listens_on(self):
        opened = []

        class Answer:
            def read(self):
                return b'ok'

        def urlopen(url, timeout):
            opened.append(url)
            return Answer()
        for setting, port in ((None, '8790'), ('8791', '8791')):          # SUBSYNC_PORT is set on the host network
            with self.subTest(setting=setting), mock.patch.dict(os.environ), \
                    mock.patch('urllib.request.urlopen', urlopen), contextlib.redirect_stdout(io.StringIO()) as out:
                os.environ.pop('SUBSYNC_PORT', None)
                if setting:
                    os.environ['SUBSYNC_PORT'] = setting
                self.assertEqual(engine.HEALTH[:2], ['python', '-c'])
                exec(compile(engine.HEALTH[2], 'health', 'exec'), {'__name__': '__main__'})
            self.assertEqual(opened[-1], f'http://127.0.0.1:{port}/health')
            self.assertEqual(out.getvalue(), 'ok\n')


class HostPort(TmpState):
    """A public instance's port: its own on a re-run, never a personal install's; checked unless this install's
    container already holds it."""

    def host_address(self, prev, running=True, taken=()):
        engine.save_settings(prev)
        asked = []

        def in_use(port, host='0.0.0.0'):
            asked.append((port, host))
            return port in taken
        with mock.patch.object(dockerenv, 'container_state', lambda name: 'running healthy' if running else ''), \
                mock.patch.object(dockerenv, 'port_in_use', in_use):
            e = engine.Engine({'HOST_ADDRESS': 'https://subs.example.com'}, page=True)
            e.host_address()
        return e.s['port'], asked

    def test_a_personal_installs_port_is_never_taken_over(self):
        # a personal address typed without a port listens on 80: the hoster's web server needs that one
        personal = {'route': 'yourself', 'port': 80, 'public_base': 'http://192.168.1.10', 'public_kind': 'port'}
        self.assertEqual(self.host_address(personal), (hosting.PORT, [(hosting.PORT, '127.0.0.1')]))
        with self.assertRaises(engine.Stop) as caught:
            self.host_address(personal, taken={hosting.PORT})
        self.assertEqual(caught.exception.message, f'Port {hosting.PORT} on this machine is already in use.')

    def test_the_port_this_installs_container_holds_is_not_checked(self):
        for prev in ({'route': 'yourself', 'port': hosting.PORT}, {'route': 'host', 'port': 8791}):
            with self.subTest(prev=prev):
                self.assertEqual(self.host_address(prev), (prev['port'], []))

    def test_a_public_instances_port_without_its_container_is_checked(self):
        with self.assertRaises(engine.Stop):
            self.host_address({'route': 'host', 'port': 8791}, running=False, taken={8791})
        self.assertEqual(self.host_address({'route': 'host', 'port': 8791}, running=False), (8791, [(8791, '127.0.0.1')]))


class Labels(Offline):
    """Each question has one wording: the label the page shows is the question the engine asks and names."""

    def test_a_label_reworded_is_reworded_on_both(self):
        with mock.patch.dict(screens.LABELS, {'AIOS_CONFIG_ID': 'The config', 'HOST_ADDRESS': 'The address'}):
            e = engine.Engine({}, page=True)
            e.check_machine()
            e.find_aios()
            with self.assertRaises(engine.Problem) as caught:
                e.read_viewer()
            self.assertEqual(caught.exception.message, '"The config" needs an answer.')
            self.assertIn('The config', screens.config([]))
            with self.assertRaises(engine.Problem) as caught:
                engine.Engine({}, page=True).host_address()
            self.assertEqual(caught.exception.message, '"The address" needs an answer.')
            self.assertIn('The address', screens.host_address())

    def test_the_routes_and_their_question_are_the_pages(self):
        self.assertEqual(engine.Engine.ROUTES, [('yourself', 'Install it for yourself'), ('host', 'Host a public instance')])
        self.assertEqual(engine.Engine.ROUTES, [(value, title) for value, title, _ in screens.WAYS])
        e = engine.Engine({}, page=True)
        e.ask = mock.Mock(**{'choose.return_value': 'host'})
        self.assertEqual(e.choose_route(), 'host')
        e.ask.choose.assert_called_once_with('ROUTE', screens.QUESTION, engine.Engine.ROUTES, current='yourself')

    def test_every_label_is_an_approved_word_with_its_explanation(self):
        for key, label in screens.LABELS.items():
            with self.subTest(key=key):
                self.assertIn(label, screens.INSTALLER_WORDS)
                self.assertTrue(explain.QUESTIONS.get(key) or explain.SETTINGS.get(key))


if __name__ == '__main__':
    unittest.main()
