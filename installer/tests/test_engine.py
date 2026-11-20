"""The engine's own rules and its less common paths: the state files it writes and reads, what reaches the terminal,
the step headings, the address checks, AIOStreams' error codes, the recovery paths of the configs it keeps, the
shapes of AIOStreams other than one container on a Docker network (host network, elsewhere on the network, two of
them), the saved AIOStreams reached again by a later ./setup, the last step's checks when they don't hold, the wait for
the service, a public instance's port and the one wording of each question, against the stand-in AIOStreams of
test_flow with Docker stubbed out. Nothing connects beyond 127.0.0.1."""
import contextlib
import errno
import io
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from html import escape as html_escape
from pathlib import Path
from unittest import mock

from installer import dockerenv, docs, engine, hosting, plan, screens, ui, wizard
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
        self.assertEqual(engine.read_kv(path), {'PLAIN': 'ab', 'QUOTED': 'x #y', 'SPACED': 'v', 'KEY': 'second'})
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

    def test_a_compose_file_written_half_way_leaves_the_last_one_as_it_was(self):
        # It was the one state file written in place; a full disk or a setup stopped while writing it left it
        # short, and ./setup --uninstall's compose down then stopped on it
        s = {'image': 'aiosubtitles:1', 'install_id': 'id-1', 'port': 8790, 'bind': '127.0.0.1'}
        self.state.mkdir()
        engine.write_compose(s)
        before = (self.state / 'compose.yaml').read_text()

        def full_disk(path, text, *args, **kwargs):
            path.open('w').close()                         # opened (so emptied) and nothing written: the disk is full
            raise OSError(errno.ENOSPC, 'No space left on device')
        with mock.patch.object(Path, 'write_text', full_disk), self.assertRaises(OSError):
            engine.write_compose(dict(s, port=8791))
        self.assertEqual((self.state / 'compose.yaml').read_text(), before)
        engine.write_compose(dict(s, port=8791))           # the next write takes the place of the one left behind
        self.assertIn('"127.0.0.1:8791:8790"', (self.state / 'compose.yaml').read_text())
        self.assertEqual(stat.S_IMODE((self.state / 'compose.yaml').stat().st_mode), 0o644)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ['compose.yaml'])

    def test_a_number_written_wrong_in_subsync_env_reads_as_its_default_and_the_others_as_written(self):
        # subsync's settings read every number one way and never raise; one line written wrong had every
        # question that states a setting fall back to the defaults
        self.state.mkdir()
        (self.state / 'subsync.env').write_text('SUBSYNC_MAX_TRIES=6.0\nSUBSYNC_USER_FILES_PER_DAY=150\n')
        s = engine.env_settings()
        self.assertEqual((s.max_tries, s.user_files_per_day), (6, 150))
        self.assertEqual(s.written_wrong, ['SUBSYNC_MAX_TRIES must be a whole number from 1, like 6.'])


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
                self.assertEqual(hosting.check_address(bad), (False, hosting.ADDRESS_REFUSED, bad))
        self.assertEqual(hosting.check_address('https://Subs.Example.com:8443/'), (True, '', 'https://subs.example.com:8443'))

    def test_an_address_with_a_space_in_it_is_asked_again(self):
        # urlsplit lets a space through (and drops a tab); the address's first check then ended in http.client's
        # InvalidURL: a traceback in a terminal, the page's "unexpected error" stop
        for bad in ('http://exa mple.com', 'http://x/sub sync', 'https://a.example.com/sub\tsync',
                    'http://192.168.1.5:8790/sub sync'):
            with self.subTest(bad=bad):
                self.assertEqual(plan.check_public(bad), (False, 'Leave out the spaces.', bad))
        self.assertEqual(plan.check_public('  http://x/sub%20sync/ '), (True, '', 'http://x/sub%20sync'))   # % stays
        for bad in ('https://exa mple.com', 'https://subs.exa mple.com/', 'https://subs.example\t.com'):
            with self.subTest(bad=bad):
                self.assertEqual(hosting.check_address(bad), (False, 'Leave out the spaces.', bad))
        self.assertEqual(hosting.check_address(' https://subs.example.com '), (True, '', 'https://subs.example.com'))
        # the neighbours, unchanged
        self.assertEqual(plan.check_public('exa mple.com')[1], 'Type a full address that starts with http:// or https://')
        self.assertEqual(plan.check_public('http://x/a b?c')[1], 'Leave out anything after ? or #.')


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


class OtherDigits(TmpState):
    """str.isdigit also takes superscripts, which int refuses, and int reads every script's decimal
    digits as if they were ASCII ones; an answer written so is refused as any other answer that isn't a number."""

    def choose(self, answer):
        with contextlib.redirect_stdout(io.StringIO()):
            return ui.Asker({'AUDIO': answer}, yes=True).choose('AUDIO', screens.LABELS['AUDIO'], plan.AUDIO_ANSWERS)

    def test_a_choice_in_other_digits_is_no_choice(self):
        # "²" raised ValueError out of the question; "١" and "２" chose the first and second option
        self.assertEqual(self.choose('2'), plan.AUDIO_ANSWERS[1][0])
        for answer in ('²', '١', '２', '1²'):
            with self.subTest(answer=answer):
                with self.assertRaises(ui.Stop) as caught:
                    self.choose(answer)
                self.assertEqual(caught.exception.message,
                                 f'AUDIO: Type a number from 1 to {len(plan.AUDIO_ANSWERS)}.')

    def test_the_audio_answer_in_other_digits_is_no_number(self):
        # "²" and "١" were refused as a number (plan.AUDIO_NUMBER_REFUSED); they are no number, so the question's own
        # refusal of an answer it doesn't know says what to type, as for "maybe"
        def asked(answer):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ui.Stop) as caught:
                engine.Engine({'AUDIO': answer}).ask_audio()
            return caught.exception.message
        self.assertEqual(asked('2'), plan.AUDIO_NUMBER_REFUSED)
        for answer in ('maybe', '²', '١'):
            with self.subTest(answer=answer):
                self.assertEqual(asked(answer), f'AUDIO: Type a number from 1 to {len(plan.AUDIO_ANSWERS)}.')

    def test_the_files_synced_at_a_time_are_read_in_ascii_digits_only(self):
        e = engine.Engine({})
        for value, places in (('4', 4), ('１２', None), ('²', None), ('', None), (None, None)):
            with self.subTest(value=value):
                e.limit_values = {'SUBSYNC_SYNCS_AT_ONCE': value}
                self.assertEqual(e.places(), places)


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
            self.addCleanup(patch.stop)

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
        text = 'AIOS_CONFIG_PASSWORD="pass #word"\nRESTORE=yes\n'
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

    def test_an_aiostreams_whose_status_has_no_settings_is_set_up_as_one_without_them(self):
        # The client takes a status answer with a version alone; setup read its settings as always there (a
        # KeyError: a traceback in a terminal, the unexpected-error stop on the page), the public sign-up as optional
        real = engine.Aios.status

        def bare(aios):
            return {'version': real(aios)['version']}
        with mock.patch.object(engine.Aios, 'status', bare):
            code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        self.assertEqual(code, 0, out)
        self.assertEqual(engine.load_settings()['aios_public'], PUBLIC)          # the container's own address
        self.assertEqual(len(self.ours()), 1)

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
        self.assertEqual((caught.exception.field, caught.exception.message), ('HOST_ADDRESS', hosting.ADDRESS_REFUSED))


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

    def test_uninstall_skips_a_config_of_subsyncs_already_gone(self):
        # it warned once ("Removing the sync config: That config ID and password don't match …"): a removal stopped
        # half way had deleted it, and the next removal, which finishes the job, skips what is already done
        sync_id = self.install()['SYNC_UUID']
        del self.aios.users[sync_id]
        code, out = self.again('--uninstall', REMOVE='yes')
        self.assertEqual(code, 0, out)
        self.assertNotIn("don't match", out)
        self.assertNotIn('!  ', out)                                    # no warning at all
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

    def test_subsync_listens_on_this_machine_only_behind_the_web_server_with_host_networking(self):
        # With host networking it listened on every interface, a plain-http way around the web server, while
        # the same answers on Docker's network publish it on 127.0.0.1 only
        host = [self.container(host_network=True, networks=[])]
        with mock.patch.object(dockerenv, 'aios_containers', lambda: host):
            self.install()
        self.assertEqual(self.env()['SUBSYNC_BIND'], '127.0.0.1')
        self.assertIn(f"# {explain.SETTINGS['SUBSYNC_BIND'][1][:60]}", (self.state / 'subsync.env').read_text())
        self.assertEqual(self.ours(), [f"http://127.0.0.1:8790/{self.keys()['ADDON_KEY']}/manifest.json"])  # still
        with mock.patch.object(dockerenv, 'aios_containers', lambda: host), \
                mock.patch.object(engine.Engine, 'public_health', lambda e: self.aios.subsync_up):
            code, out = self.again(PUBLIC_ADDRESS='http://192.168.1.5:8790')        # its own port: every interface
        self.assertEqual(code, 0, out)
        self.assertNotIn('SUBSYNC_BIND', self.env())

    def test_on_dockers_network_the_published_port_decides_where_it_listens(self):
        self.install()
        self.assertNotIn('SUBSYNC_BIND', self.env())                    # 0.0.0.0 in its container, published on
        self.assertIn('    ports: ["127.0.0.1:8790:8790"]\n', self.compose())   # 127.0.0.1 only
        self.assertEqual(explain.SETTINGS['SUBSYNC_BIND'], (
            '0.0.0.0', "The address subsync listens on. ./setup sets 127.0.0.1 when AIOStreams uses host networking and "
            "your web server passes subsync's address on, so nothing reaches subsync except through your web server. "
            'Leave it as it is otherwise: in a container, Docker needs 0.0.0.0 to pass its port on.'))
        self.assertIn('SUBSYNC_BIND', explain.WRITTEN_BY_SETUP)          # so a re-run that doesn't write it removes it

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
            self.addCleanup(p.stop)
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

    def test_a_local_address_whose_port_cant_be_one_is_refused_as_on_every_route(self):
        # Its port was read to put the gateway in its place: letters or a number out of range raised
        # ValueError (the page's unexpected-error stop, a traceback in a terminal), and port 0 was tried as port 80
        for address in ('localhost:abc', 'http://127.0.0.1:99999', 'localhost:0'):
            with self.subTest(address=address):
                good, message, value = engine.Engine({})._check_aios(address)
                with mock.patch.object(engine, 'PLACE', None):
                    elsewhere = engine.Engine({})._check_aios(address)
                self.assertEqual((good, message, value), elsewhere)          # the client's own refusal
                self.assertFalse(good)
                self.assertTrue(message.startswith("Can't reach AIOStreams at http://"), message)
                self.assertNotIn(self.place.gateway, message + value)

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
            self.addCleanup(p.stop)
        self.install()
        self.assertEqual(engine.load_settings()['aios_url'], 'http://aiostreams:3000')      # found by its name
        self.joined.clear()                                 # the next ./setup: a new container

    def test_removing_on_the_page_joins_aiostreams_network_again(self):
        e = engine.Engine({'AIOS_CONFIG_PASSWORD': PASSWORD, 'REMOVE_OPENSUBTITLES': 'yes', 'REMOVE': 'yes'}, page=True)
        e.remove()
        self.assertEqual(self.joined, [('aio_default', 'c0ffee')])
        self.assertEqual(self.ours(), [])
        self.assertIn(('ok', 'Done.'), [tuple(r[:2]) for r in e.out.rows])

    def test_uninstall_in_a_terminal_joins_aiostreams_network_again(self):
        sync_id = self.keys()['SYNC_UUID']
        code, out = self.again('--uninstall', REMOVE='yes')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.ours(), [])
        self.assertNotIn(sync_id, self.aios.users)

    def test_restore_joins_aiostreams_network_again(self):
        copy = next((self.state / 'backups').iterdir())
        answers = Path(self.tmp.name) / 'restore-answers'
        answers.write_text(f'AIOS_CONFIG_PASSWORD={PASSWORD}\nRESTORE=yes\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(['--restore', str(copy), '--answers', str(answers), '--yes'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(self.joined, [('aio_default', 'c0ffee')])
        self.assertEqual(self.ours(), [])                                         # the copy from before subsync

    def test_restore_after_an_uninstall_finds_aiostreams_as_installing_does(self):
        # --uninstall deletes settings.json, and its closing list points to --restore: that asked
        # http://127.0.0.1:3000, setup's own container here
        copy = (self.state / 'backups').glob('viewer-*').__next__()       # the install's: before subsync was added
        copy = copy.rename(copy.with_name(f'viewer-{UUID[:8]}-20261117-120000.json'))    # one the removal can't reuse
        code, out = self.again('--uninstall', REMOVE='yes')
        self.assertEqual(code, 0, out)
        self.assertFalse((self.state / 'settings.json').exists())
        self.aios.users[UUID]['config']['presets'].append({'type': 'custom', 'options': {'manifestUrl': 'x'}})
        self.joined.clear()                                 # the next ./setup: a new container
        answers = self.answers_file({'AIOS_CONFIG_PASSWORD': PASSWORD, 'RESTORE': 'yes'})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(['--restore', str(copy), '--answers', answers, '--yes'])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(self.joined, [('aio_default', 'c0ffee')])
        self.assertIn('Found AIOStreams 2.37.0 in the container "aiostreams"', out.getvalue())
        self.assertNotIn('Viewers reach it at', out.getvalue())          # the AIOStreams step's install rows: none
        self.assertNotIn('Step 2', out.getvalue())
        self.assertIn('Restored.', out.getvalue())
        self.assertEqual(self.aios.users[UUID]['config'], json.loads(copy.read_text())['config'])
        self.assertEqual(self.ours(), [])                                   # the change since is gone

    def test_aiostreams_own_address_when_the_saved_one_cant_be_reached(self):
        self.joins = False                                   # its network is gone (renamed by its compose project)
        s = engine.load_settings()
        engine.save_settings(dict(s, aios_public=self.aios.url))
        e = engine.Engine({'AIOS_CONFIG_PASSWORD': PASSWORD, 'REMOVE_OPENSUBTITLES': 'yes', 'REMOVE': 'yes'}, page=True)
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


@contextlib.contextmanager
def typing(*answers):
    """What a person types at the terminal's questions, in order (a password too); then the input ends, so a question
    asked once more than expected stops the run ("No more input.") instead of waiting."""
    typed = mock.Mock(side_effect=[*answers, EOFError])
    with mock.patch('builtins.input', typed), mock.patch.object(ui.getpass, 'getpass', typed):
        yield typed


class TerminalAsks(Offline):
    """In a terminal, ./setup asks what an answers file leaves out, unless --yes is given: ./setup --uninstall asks for
    the config's password, as the README and the password's explanation say."""

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(list(argv))
        return code, out.getvalue()

    def test_uninstall_in_a_terminal_asks_for_the_config_password(self):
        self.install()
        code, out = self.run_main('--uninstall', '--yes')                # --yes: nothing asked, as before
        self.assertEqual(code, 1)
        self.assertIn(f'"Password of your config {UUID[:8]}…" needs an answer.', out)
        self.assertTrue((self.state / 'settings.json').exists())
        with typing(PASSWORD, 'y', 'y') as typed:                         # the password, OpenSubtitles out, confirm
            code, out = self.run_main('--uninstall')
        self.assertEqual(code, 0, out)
        self.assertEqual(typed.call_count, 3)
        self.assertNotIn(PASSWORD, out)
        self.assertIn('Done.', out)
        self.assertFalse((self.state / 'settings.json').exists())
        self.assertEqual([p['type'] for p in self.aios.users[UUID]['config']['presets']], ['zilean', 'newznab'])

    def test_an_answers_file_without_yes_asks_the_lines_left_out(self):
        # docs/answers.example: "Leave a line out to be asked"; with --yes nothing is asked
        answers = dict(self.ANSWERS, ROUTE='yourself', AIOS_CONFIG_ID=self.LINK, AUDIO='no', USENET='no', SUBDL_KEY='',
                       WYZIE_KEY='', TMDB_KEY='', ADD_OPENSUBTITLES='yes', PUBLIC_ADDRESS=f'{PUBLIC}/subsync',
                       SUBSYNC_DAILY_BUDGET_GB='50', CACHE='no', CONFIRM='yes', TEST_READ='no')
        del answers['AIOS_CONFIG_PASSWORD']
        path = self.answers_file(answers)
        code, out = self.run_main('--answers', path, '--yes')
        self.assertEqual(code, 1)
        self.assertIn('"Its password" needs an answer.', out)
        with typing(PASSWORD, '') as typed:                              # the password; Enter after the web server rule
            code, out = self.run_main('--answers', path)
        self.assertEqual(code, 0, out)
        self.assertEqual(typed.call_count, 2)
        self.assertNotIn(PASSWORD, out)
        self.assertTrue(any(p['type'] == 'custom' for p in self.aios.users[UUID]['config']['presets']))


class OptionalKeys(Offline):
    """A saved optional key can be removed: remove typed in a terminal, its empty line in an answers file (no line keeps
    it), or the page's "Remove the saved key" box."""

    def asked(self, answers, *typed, yes=False):
        out = io.StringIO()
        with typing(*typed), contextlib.redirect_stdout(out):
            value = ui.Asker(answers, yes=yes).text('SUBDL_KEY', screens.LABELS['SUBDL_KEY'], secret=True,
                                                    optional=True, current='k' * 32)
        return value, out.getvalue()

    def test_the_terminal_says_how_and_takes_remove(self):
        value, out = self.asked({}, 'remove')
        self.assertEqual(value, '')
        self.assertIn('Enter keeps: set (32 characters); type remove to delete it', out)
        self.assertEqual(self.asked({}, '')[0], 'k' * 32)                  # Enter keeps it
        self.assertEqual(self.asked({'SUBDL_KEY': ''})[0], '')             # an answers file's empty line
        self.assertEqual(self.asked({'SUBDL_KEY': ''}, yes=True)[0], '')
        self.assertEqual(self.asked({}, yes=True)[0], 'k' * 32)            # no line: kept
        out = io.StringIO()
        with typing(), contextlib.redirect_stdout(out):                  # a question that isn't optional: no remove
            ui.Asker({}, yes=True).text('AIOS_URL', 'Address', current='http://127.0.0.1:3000')
        self.assertNotIn('type remove', out.getvalue())

    def test_an_answers_files_empty_line_removes_the_saved_key(self):
        self.install(SUBDL_KEY='subdl-key-1', TMDB_KEY='tmdb-key-1')
        code, out = self.again()                                         # no line: both kept
        self.assertEqual(code, 0, out)
        self.assertEqual((self.keys()['SUBDL_KEY'], self.env()['SUBSYNC_SUBDL_KEY']), ('subdl-key-1', 'subdl-key-1'))
        code, out = self.again(SUBDL_KEY='')
        self.assertEqual(code, 0, out)
        self.assertIn('Removed the saved SubDL API key', out)
        self.assertNotIn('SUBDL_KEY', self.keys())
        self.assertNotIn('SUBSYNC_SUBDL_KEY', self.env())
        self.assertEqual(self.env()['SUBSYNC_TMDB_KEY'], 'tmdb-key-1')       # the other key stays

    def test_the_pages_box_removes_the_saved_key(self):
        self.install(TMDB_KEY='tmdb-key-1')
        e = self.page_engine({'TMDB_KEY': ''})
        e.choices()
        self.assertNotIn('TMDB_KEY', e.keys)
        self.assertIn(('ok', 'Removed the saved TMDB API key'), [tuple(r[:2]) for r in e.out.rows])
        e = self.page_engine({})                                         # no answer: kept
        e.choices()
        self.assertEqual(e.keys['TMDB_KEY'], 'tmdb-key-1')


class TmdbKeyCheck(Offline):
    """A new TMDB key is checked with TMDB once, with one request (as the site's settings page checks a
    person's own): a refused one is asked again with the site's words and isn't kept; one TMDB doesn't answer about is
    saved with a warning; the saved key isn't checked again. TMDB's answer is the fixture's stand-in."""

    REFUSED = ("TMDB didn't accept this key. Copy it again from themoviedb.org (your account's settings, API) and paste "
               'it here.')

    def test_a_new_key_is_checked_once_then_saved(self):
        self.install(TMDB_KEY='tmdb-key-1')
        self.assertEqual(self.tmdb_checked, [('https://api.themoviedb.org/3', 'tmdb-key-1')])
        self.assertEqual(self.env()['SUBSYNC_TMDB_KEY'], 'tmdb-key-1')
        code, out = self.again()                                         # the saved key: not checked again
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.tmdb_checked), 1)
        self.assertNotIn('TMDB accepts this key', out)
        code, out = self.again(TMDB_KEY='tmdb-key-2')                    # a new one is
        self.assertEqual(code, 0, out)
        self.assertEqual(self.tmdb_checked[-1][1], 'tmdb-key-2')
        self.assertIn('✓  TMDB accepts this key', out)
        self.assertNotIn('tmdb-key-2', out)

    def test_a_refused_key_stops_an_answers_run_with_the_sites_words(self):
        self.tmdb_answer = False
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no', TMDB_KEY='tmdb-key-1'))
        self.assertEqual(code, 1, out)
        self.assertIn(f'TMDB_KEY: {self.REFUSED}', ' '.join(out.split()))
        self.assertEqual(self.REFUSED, explain.PUBLIC_TEXTS['tmdb_refused'])
        self.assertFalse((self.state / 'subsync.env').exists())          # nothing saved
        self.assertEqual(len(self.tmdb_checked), 1)

    def test_a_key_tmdb_doesnt_answer_about_is_saved_with_a_warning(self):
        self.tmdb_answer = None
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no', TMDB_KEY='tmdb-key-1'))
        self.assertEqual(code, 0, out)
        self.assertIn("!  TMDB didn't answer, so the key wasn't checked; it is saved as you typed it.", out)
        self.assertNotIn('TMDB accepts this key', out)
        self.assertEqual(self.env()['SUBSYNC_TMDB_KEY'], 'tmdb-key-1')

    def test_on_the_page_a_refused_key_is_under_its_field_and_left_out(self):
        self.tmdb_answer = False
        e = self.page_engine({'TMDB_KEY': 'tmdb-key-1'})
        with self.assertRaises(engine.Problem) as refused:
            e.choices()
        self.assertEqual((refused.exception.field, refused.exception.message), ('TMDB_KEY', self.REFUSED))
        self.assertNotIn('TMDB_KEY', e.answers)                         # its box is empty again
        e.choices()                                                      # left empty, the step goes on without it
        self.assertNotIn('TMDB_KEY', e.keys)
        self.assertEqual(len(self.tmdb_checked), 1)
        self.tmdb_answer = True
        e.answers['TMDB_KEY'] = 'tmdb-key-2'
        e.choices()
        self.assertEqual(e.keys['TMDB_KEY'], 'tmdb-key-2')
        self.assertIn(('ok', 'TMDB accepts this key'), [tuple(r[:2]) for r in e.out.rows])
        e.choices()                                                      # the same step again: checked once
        self.assertEqual([key for _, key in self.tmdb_checked], ['tmdb-key-1', 'tmdb-key-2'])
        html = screens.choices(['eng'], 'no', f'{PUBLIC}/subsync', problem=('TMDB_KEY', self.REFUSED))
        self.assertIn(f'<p class="field-error" id="tmdb_key-error">{html_escape(self.REFUSED)}</p>', html)
        self.assertIn(f'<a href="#tmdb_key">{html_escape(self.REFUSED)}</a>', html)       # the summary's link

    def test_the_key_is_checked_at_the_tmdb_address_subsync_uses(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'subsync.env').write_text('SUBSYNC_TMDB_URL=http://127.0.0.1:9/3\n')
        self.assertEqual(engine.Engine({}, page=True).check_tmdb_key('tmdb-key-1'), (True, 'TMDB accepts this key',
                                                                                     'tmdb-key-1'))
        self.assertEqual(self.tmdb_checked, [('http://127.0.0.1:9/3', 'tmdb-key-1')])


class PlainHttp(Offline):
    """An AIOStreams address typed without http:// or https:// is tried with https first when its host isn't local or
    private; plain http to such a host gets a warning before the config's password is asked."""

    def found(self, address, https=True):
        tried = []

        def status(aios):
            tried.append(aios.base)
            if aios.base.startswith('https://') and not https:
                raise engine.AiosError(f"Can't reach AIOStreams at {aios.base}.")
            return {'version': '2.37.0', 'settings': {'baseUrl': 'https://aio.example.com'}}
        with mock.patch.object(dockerenv, 'aios_containers', lambda: []), \
                mock.patch.object(engine.Aios, 'status', status):
            e = engine.Engine({'AIOS_URL': address}, page=True)
            e.find_aios()
        warned = [r[1] for r in e.out.rows if r[0] == 'warn']
        return e.s['aios_url'], tried, warned

    def test_https_first_for_a_public_host(self):
        self.assertEqual(self.found('aio.example.com'), ('https://aio.example.com', ['https://aio.example.com'], []))

    def test_http_when_https_doesnt_answer_with_a_warning(self):
        url, tried, warned = self.found('aio.example.com:3000', https=False)
        self.assertEqual((url, tried), ('http://aio.example.com:3000',
                                        ['https://aio.example.com:3000', 'http://aio.example.com:3000']))
        self.assertEqual(warned, ["AIOStreams at http://aio.example.com:3000 uses plain http, so the config's "
                                  'password would go over the network unencrypted. Use its https address if it has '
                                  'one.'])

    def test_plain_http_typed_to_a_public_host_is_kept_with_the_warning(self):
        url, tried, warned = self.found('http://aio.example.com')
        self.assertEqual((url, tried), ('http://aio.example.com', ['http://aio.example.com']))
        self.assertEqual(warned, [engine.PLAIN_HTTP.format(address='http://aio.example.com')])

    def test_local_and_private_hosts_keep_http_without_a_warning(self):
        for address in ('192.168.1.5:3000', 'localhost:3000', 'aiostreams:3000', 'nas.local:3000'):
            with self.subTest(address=address):
                self.assertEqual(self.found(address), (f'http://{address}', [f'http://{address}'], []))

    def test_a_pasted_page_or_install_link_is_cut_back_to_aiostreams_own_address(self):
        # The address bar of AIOStreams' configure page, an install link or its Jellyfin address is asked and
        # saved as AIOStreams' own address, which every API call is made under
        for address, url in (('https://aio.example.com/configure', 'https://aio.example.com'),
                             (f'aio.example.com/stremio/{UUID}/ENC/manifest.json', 'https://aio.example.com'),
                             ('http://192.168.1.5:3000/jellyfin/', 'http://192.168.1.5:3000')):
            with self.subTest(address=address):
                self.assertEqual(self.found(address), (url, [url], []))


class CacheRecord(Offline):
    def test_turning_the_subtitle_cache_off_is_saved(self):
        # the closing text of later runs and removing's note read it
        self.install(AIOS_USERNAME='admin', AIOS_PASSWORD='adminpw', CACHE='yes')
        self.assertEqual(engine.load_settings()['cache_before'],
                         {'key': 'resources.cache.subtitle.ttl', 'value': {'*': 300}})
        code, out = self.again()
        self.assertEqual(code, 0, out)
        flat_out = flat(out.replace('│', ' '))
        self.assertIn('the synced copy shows the next time you open that file.', flat_out)
        self.assertNotIn('after up to 5 minutes', flat_out)
        code, out = self.again('--uninstall', REMOVE='yes', REMOVE_OPENSUBTITLES='yes')
        self.assertEqual(code, 0, out)
        self.assertIn("AIOStreams' subtitle cache setting for subsync stays; it does nothing without subsync.", out)


class ClosingWords(Offline):
    def test_the_closing_text_says_how_an_original_is_listed(self):
        self.install()
        e = engine.Engine({})
        e.languages, e.add_os = plan.parse_languages('English')[0], False
        _, lines, _ = e.summary_parts(True)
        self.assertIn('When no copy is clearly closer than the original, no synced copy is added: the originals stay, '
                      'and the one that fits the file best is listed once as "<Language> (original)".', lines[1])
        self.assertNotIn('nothing is added', lines[1])

    def test_the_status_line_says_the_outcomes_in_words(self):
        self.install()

        def status(results):
            st = {'subtitle_requests': 12, 'queue': 0, 'bytes_today': 4.2e6, 'store': {'results': results}}
            with mock.patch.object(engine, 'subsync_status', lambda: st):
                return engine.Engine({}).status_parts()[0][1][2]
        self.assertEqual(status({'in_sync': 2, 'keep_original': 5, 'synced': 3}),
                         'synced copies offered: 3; subtitles checked: 10 (3 synced, 2 already in sync, 5 original '
                         'kept); read today: 4.2 MB')
        self.assertEqual(status({}), 'synced copies offered: 0; subtitles checked: 0 (none yet); read today: 4.2 MB')
        self.assertEqual(status({'other': 1, 'keep_original': 1}),       # an outcome without words: its own name, last
                         'synced copies offered: 0; subtitles checked: 2 (1 original kept, 1 other); read today: '
                         '4.2 MB')


class Removal(Offline):
    """./setup --uninstall and --restore on an AIOStreams that saves configs only for a signed-in account, Docker failing
    to take the service down, and what removing says it deletes."""

    SIGN_IN = {'AIOS_USERNAME': 'admin', 'AIOS_PASSWORD': 'adminpw'}

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(list(argv))
        return code, out.getvalue()

    def test_uninstall_signs_in_when_aiostreams_asks(self):
        # it stopped with "This AIOStreams asks you to sign in before configs can be saved." and never asked
        self.aios.gated = True
        self.install(**self.SIGN_IN)
        code, out = self.again('--uninstall', REMOVE='yes', **self.SIGN_IN)
        self.assertEqual(code, 0, out)
        self.assertIn('!  Saving the config: This AIOStreams asks you to sign in before configs can be saved.', out)
        self.assertIn('AIOStreams sign-in to save configs: username', out)
        self.assertIn('Signed in', out)
        self.assertEqual(self.ours(), [])
        self.assertNotIn('adminpw', out)

    def test_restore_signs_in_when_aiostreams_asks(self):
        self.aios.gated = True
        self.install(**self.SIGN_IN)
        copy = next((self.state / 'backups').iterdir())
        answers = self.answers_file(dict(AIOS_CONFIG_PASSWORD=PASSWORD, RESTORE='yes', **self.SIGN_IN))
        code, out = self.run_main('--restore', str(copy), '--answers', answers, '--yes')
        self.assertEqual(code, 0, out)
        self.assertIn('Restored.', out)
        self.assertEqual(self.ours(), [])                                     # the copy from before subsync

    def test_a_failed_container_removal_keeps_setups_files_and_the_next_one_finishes(self):
        # it said "Removed the subsync container and its stored copies" and deleted setup's files while the container
        # kept running; the next ./setup then stopped at a container "this setup didn't make"
        self.install()

        def compose(state, *args, **kw):
            return (1, '', 'Error response from daemon: timeout') if 'down' in args else (0, '', '')
        with mock.patch.object(dockerenv, 'compose', compose):
            code, out = self.again('--uninstall', REMOVE='yes')
        self.assertEqual(code, 1, out)
        self.assertIn("✗  Removing the subsync container failed; setup's files were kept, so removing it again finishes "
                      'the job.\n      Error response from daemon: timeout\n', out)
        self.assertNotIn('Removed the subsync container', out)
        for name in ('settings.json', 'secrets.env', 'subsync.env', 'compose.yaml'):
            self.assertTrue((self.state / name).exists(), name)
        code, out = self.again('--uninstall', REMOVE='yes')                 # its sync config is gone already
        self.assertEqual(code, 0, out)
        self.assertNotIn('!  ', out)
        self.assertIn('Removed the subsync container and its stored copies', out)
        self.assertFalse((self.state / 'settings.json').exists())

    def test_uninstall_names_the_configs_it_deletes(self):
        # it said "deletes subsync's own config" also when the Usenet config goes too, from a second copy of the page's
        self.install(USENET='yes')
        code, out = self.again('--uninstall', REMOVE='yes')
        self.assertEqual(code, 0, out)
        self.assertIn("This takes subsync out of your config (1 entry), deletes subsync's own config and its Usenet "
                      'config, and removes the subsync container with its stored copies. Your other subtitle sources '
                      'stay.', flat(out))
        self.assertIn("Deleted subsync's Usenet config", out)


class Restore(Offline):
    """./setup --restore FILE, the documented way back after a bad save: the copy put back with the current config saved
    first; a wrong password, a no, and a file that can't be read or isn't a saved copy change nothing and end with
    code 1 and a plain message (they ended in a Python traceback, and no test ran --restore)."""

    GIVE = ('Give a saved copy from state/backups/, like '
            './setup --restore state/backups/viewer-1a2b3c4d-20261117-120000.json.')

    def setUp(self):
        super().setUp()
        self.install()
        self.copy = next(p for p in (self.state / 'backups').iterdir() if p.name.startswith('viewer-'))

    def restore(self, path, **answers):
        answers = self.answers_file(dict({'AIOS_CONFIG_PASSWORD': PASSWORD, 'RESTORE': 'yes'}, **answers))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(['--restore', str(path), '--answers', answers, '--yes'])
        return code, out.getvalue()

    def unchanged(self):
        """The config as it is now, and the copies: what a restore that stops must leave as they are."""
        return json.dumps(self.aios.users[UUID]['config'], sort_keys=True), sorted(os.listdir(self.state / 'backups'))

    def test_the_copy_is_put_back_and_the_current_config_saved_first(self):
        current, saved = self.aios.users[UUID]['config'], json.loads(self.copy.read_text())
        code, out = self.restore(self.copy)
        self.assertEqual(code, 0, out)
        self.assertIn('Restored.', out)
        self.assertEqual(self.aios.users[UUID]['config'], saved['config'])
        before = [p for p in (self.state / 'backups').iterdir() if p.name.startswith('before-restore-')]
        self.assertEqual([p.name[:24] for p in before], [f'before-restore-{UUID[:8]}-'])
        self.assertEqual(json.loads(before[0].read_text()), {'uuid': UUID, 'config': current})

    def test_a_wrong_password_changes_nothing(self):
        before = self.unchanged()
        code, out = self.restore(self.copy, AIOS_CONFIG_PASSWORD='wrong')
        self.assertEqual(code, 1, out)
        self.assertIn("don't match", out)
        self.assertNotIn('Restored.', out)
        self.assertEqual(self.unchanged(), before)

    def test_answering_no_changes_nothing(self):
        before = self.unchanged()
        code, out = self.restore(self.copy, RESTORE='no')
        self.assertEqual(code, 1, out)
        self.assertIn('Nothing was changed.', out)
        self.assertEqual(self.unchanged(), before)

    def test_a_file_that_cant_be_read_or_isnt_a_saved_copy_is_a_plain_message(self):
        folder = Path(self.tmp.name)
        files = {'not-json.json': 'not json', 'no-id.json': '{"config": {}}', 'list.json': '[1, 2]',
                 'id-a-number.json': '{"uuid": 5, "config": {}}', 'config-a-list.json': '{"uuid": "x", "config": []}'}
        for name, text in files.items():
            (folder / name).write_text(text)
        (folder / 'binary.json').write_bytes(b'\xff\xfe\x00\x01')
        databases = self.state / 'backups' / 'databases-20261117-120000'
        databases.mkdir()
        before = self.unchanged()
        cant_open, not_a_copy = "Setup can't open {}.", "{} isn't a saved copy of a config."
        cases = [(folder / 'missing.json', cant_open), ('no-such-folder/viewer-1.json', cant_open),
                 (databases, not_a_copy), (folder / 'binary.json', not_a_copy)]
        cases += [(folder / name, not_a_copy) for name in files]
        for path, message in cases:
            with self.subTest(path=Path(path).name):
                code, out = self.restore(path)
                self.assertEqual(code, 1, out)
                self.assertIn(f'✗ {message.format(path)} {self.GIVE}', flat(out))
                self.assertEqual(self.unchanged(), before)

    def test_after_an_uninstall_aiostreams_is_found_or_asked_as_installing_does(self):
        copy = self.copy.rename(self.copy.with_name(f'viewer-{UUID[:8]}-20261117-120000.json'))   # the removal's own
        code, out = self.again('--uninstall', REMOVE='yes')                                     # copy can't reuse it
        self.assertEqual(code, 0, out)
        self.assertFalse((self.state / 'settings.json').exists())
        saved = json.loads(copy.read_text())['config']
        # no AIOStreams container on this machine: its address is asked, as the AIOStreams step asks it
        with mock.patch.object(dockerenv, 'aios_containers', lambda: []):
            code, out = self.restore(copy, AIOS_URL=self.aios.url)
        self.assertEqual(code, 0, out)
        self.assertIn('What address do you open AIOStreams at?', out)
        self.assertIn('✓ AIOStreams 2.37.0 answers there', flat(out))
        self.assertIn('Restored.', out)
        self.assertEqual(self.aios.users[UUID]['config'], saved)
        # two: which one is asked, as there
        two = [{'name': name, 'image': 'ghcr.io/viren070/aiostreams:latest', 'port': 3000, 'base_url': PUBLIC,
                'host_network': False, 'networks': [], 'ips': [], 'published': []} for name in ('aio-a', 'aio-b')]
        with mock.patch.object(dockerenv, 'aios_containers', lambda: two):
            code, out = self.restore(copy, AIOS_CONTAINER='aio-b')
        self.assertEqual(code, 0, out)
        self.assertIn('More than one AIOStreams runs here. Which one do your apps use?', out)
        self.assertIn('Found AIOStreams 2.37.0 in the container "aio-b"', out)
        self.assertIn('Restored.', out)

    def test_an_answers_file_setup_cant_open_is_a_plain_message(self):
        # it ended in a Python traceback (FileNotFoundError); the source route mounts only state/, so a file elsewhere
        # in the folder is never found there
        folder = Path(self.tmp.name)
        (folder / 'binary.txt').write_bytes(b'\xff\xfe\x00\x01')
        unreadable = folder / 'unreadable.txt'
        unreadable.write_text('AIOS_CONFIG_PASSWORD=x\n')
        unreadable.chmod(0o000)
        self.addCleanup(unreadable.chmod, 0o600)
        give = 'Give an answers file in state/, like ./setup --answers state/answers.txt --yes.'
        before = self.unchanged(), engine.load_settings()
        paths = [folder / 'missing.txt', 'my-answers.txt', folder, folder / 'binary.txt']
        paths += [unreadable] if os.geteuid() != 0 else []          # root reads it anyway
        for path in paths:
            for argv in (['--answers', str(path), '--yes'], ['--answers', str(path)],
                         ['--uninstall', '--answers', str(path), '--yes'],
                         ['--restore', str(self.copy), '--answers', str(path), '--yes']):
                with self.subTest(path=str(path), argv=argv[0]):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        code = wizard.main(argv)
                    self.assertEqual(code, 1, out.getvalue())
                    self.assertIn(f"✗ Setup can't open {path}. {give}", flat(out.getvalue()))
                    self.assertEqual((self.unchanged(), engine.load_settings()), before)


class OwnYes(Offline):
    """Removing and restoring each ask their own yes, REMOVE and RESTORE, never CONFIRM's. CONFIRM answered
    "Save these changes?", "Remove subsync?" and the restore question alike, and docs/answers.example ships CONFIRM=yes,
    so the answers file you installed with, given to ./setup --uninstall, removed subsync and asked nothing."""

    def setUp(self):
        super().setUp()
        self.install()
        self.copy = next(p for p in (self.state / 'backups').iterdir() if p.name.startswith('viewer-'))

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(list(argv))
        return code, out.getvalue()

    def everything(self):
        """What removing or restoring changes: setup's files with the copies, every config on AIOStreams, the service."""
        return (sorted(str(p.relative_to(self.state)) for p in self.state.rglob('*')),
                json.dumps(self.aios.users, sort_keys=True, default=str), self.aios.subsync_up)

    def changes_nothing(self, argv, question):
        before = self.everything()
        code, out = self.run_main(*argv)
        self.assertEqual(code, 1, out)
        self.assertIn(f'{question} [y/n]', out)
        self.assertIn('(default)', out.split(question)[1])                           # not answered by the file
        self.assertTrue(out.rstrip().endswith('Nothing was changed.'), out[-200:])
        self.assertNotIn('Restored.', out)
        self.assertNotIn('Done.', out)
        self.assertEqual(self.everything(), before)

    def test_confirm_yes_without_remove_removes_nothing(self):
        install = self.answers_file(dict(self.ANSWERS, TEST_READ='no', CONFIRM='yes'))   # the file it installed with
        self.changes_nothing(['--uninstall', '--answers', install, '--yes'], 'Remove subsync?')
        self.assertNotEqual(self.ours(), [])
        self.assertTrue(self.aios.subsync_up)
        code, out = self.run_main('--uninstall', '--answers', self.answers_file(
            {'AIOS_CONFIG_PASSWORD': PASSWORD, 'CONFIRM': 'no', 'REMOVE': 'yes'}), '--yes')
        self.assertEqual(code, 0, out)                                                  # REMOVE alone decides
        self.assertIn('Done.', out)
        self.assertEqual(self.ours(), [])
        self.assertFalse(self.aios.subsync_up)
        self.assertFalse((self.state / 'settings.json').exists())

    def test_confirm_yes_without_restore_restores_nothing(self):
        self.aios.users[UUID]['config']['presets'].append({'type': 'custom', 'options': {'manifestUrl': 'x'}})
        answers = self.answers_file({'AIOS_CONFIG_PASSWORD': PASSWORD, 'CONFIRM': 'yes'})
        self.changes_nothing(['--restore', str(self.copy), '--answers', answers, '--yes'],
                             f'Put the copy from {self.copy.name} back into config {UUID[:8]}…?')
        self.assertFalse([p for p in (self.state / 'backups').iterdir() if p.name.startswith('before-restore-')])
        answers = self.answers_file({'AIOS_CONFIG_PASSWORD': PASSWORD, 'CONFIRM': 'no', 'RESTORE': 'yes'})
        code, out = self.run_main('--restore', str(self.copy), '--answers', answers, '--yes')
        self.assertEqual(code, 0, out)                                                  # RESTORE alone decides
        self.assertIn('Restored.', out)
        self.assertEqual(self.aios.users[UUID]['config'], json.loads(self.copy.read_text())['config'])

    def test_the_example_answers_file_removes_and_restores_nothing(self):
        # docs/answers.example as docs.py writes it, with the one line it leaves for you to fill in, the password
        text = docs.answers_example()
        self.assertEqual(text.count('\nAIOS_CONFIG_PASSWORD=\n'), 1)
        for line in ('\nCONFIRM=yes\n', '\nREMOVE=no\n', '\nRESTORE=no\n'):
            self.assertEqual(text.count(line), 1, line)
        answers = Path(self.tmp.name) / 'example-answers'
        answers.write_text(text.replace('\nAIOS_CONFIG_PASSWORD=\n', f'\nAIOS_CONFIG_PASSWORD={PASSWORD}\n'))
        before = self.everything()
        code, out = self.run_main('--uninstall', '--answers', str(answers), '--yes')
        self.assertEqual(code, 1, out)
        self.assertIn('Remove subsync? [y/n]\n', out)
        self.assertIn('  > no   (from the answers file)\n', out.split('Remove subsync?')[1])
        self.assertTrue(out.rstrip().endswith('Nothing was changed.'), out[-200:])
        self.assertEqual(self.everything(), before)
        question = f'Put the copy from {self.copy.name} back into config {UUID[:8]}…?'
        code, out = self.run_main('--restore', str(self.copy), '--answers', str(answers), '--yes')
        self.assertEqual(code, 1, out)
        self.assertIn(f'{question} [y/n]\n', out)
        self.assertIn('  > no   (from the answers file)\n', out.split(question)[1])
        self.assertTrue(out.rstrip().endswith('Nothing was changed.'), out[-200:])
        self.assertNotIn('Restored.', out)
        self.assertEqual(self.everything(), before)


class Advice(Offline):
    """The way out the terminal gives after an answer refused or not given: by the answers file, and none to give for a
    public instance's removal answered no."""

    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(list(argv))
        return code, out.getvalue()

    def test_the_three_ways_out(self):
        self.assertEqual(ui.answers_advice('PUBLIC_ADDRESS', {'PUBLIC_ADDRESS': 'x'}),
                         'Change PUBLIC_ADDRESS in the answers file.')
        self.assertEqual(ui.answers_advice('PUBLIC_ADDRESS', {}), 'Add PUBLIC_ADDRESS=... to the answers file.')
        self.assertEqual(ui.answers_advice('PUBLIC_ADDRESS', None),
                         'Give PUBLIC_ADDRESS=... in an answers file: ./setup --answers FILE --yes.')

    def test_a_refused_answer_the_file_gives_is_to_be_changed_there(self):
        # it said "Add PUBLIC_ADDRESS=... to the answers file." to a file that has that line
        with mock.patch.object(dockerenv, 'port_in_use', lambda *a: True), \
                mock.patch.object(dockerenv, 'container_state', lambda n: ''):
            code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no', PUBLIC_ADDRESS=f'{PUBLIC}/subsync'))
            self.assertEqual(code, 1, out)
            self.assertIn('✗  Port 8790 on this machine is already in use.\n      Change PUBLIC_ADDRESS in the answers '
                          'file.\n', out)
            code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no'))   # the line left out: the default refused
            self.assertIn('      Add PUBLIC_ADDRESS=... to the answers file.\n', out)

    def test_a_missing_answer_says_where_to_give_it(self):
        # it said "Add …=... to the answers file." also when no answers file was given, or the file has that line
        self.install()
        code, out = self.run_main('--uninstall', '--yes')
        self.assertEqual(code, 1, out)
        self.assertIn(f'✗  "Password of your config {UUID[:8]}…" needs an answer.\n      Give AIOS_CONFIG_PASSWORD=... in '
                      'an answers file: ./setup --answers FILE --yes.\n', out)
        for lines, advice in (({'REMOVE': 'yes'}, 'Add AIOS_CONFIG_PASSWORD=... to the answers file.'),
                              ({'AIOS_CONFIG_PASSWORD': '', 'REMOVE': 'yes'},
                               'Change AIOS_CONFIG_PASSWORD in the answers file.')):
            with self.subTest(lines=lines):
                code, out = self.run_main('--uninstall', '--answers', self.answers_file(lines), '--yes')
                self.assertEqual(code, 1, out)
                self.assertIn(f' needs an answer.\n      {advice}\n', out)
        self.assertTrue((self.state / 'settings.json').exists())


class AnswersEcho(Offline):
    """An answers run prints each answer back, never a pasted link: its second part is the config's encrypted
    password."""

    def test_a_pasted_install_link_is_shown_by_its_config_id(self):
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no',
                                        AIOS_CONFIG_ID=f'{PUBLIC}/stremio/{UUID}/SECRETSEGMENT/manifest.json'))
        self.assertEqual(code, 0, out)
        self.assertIn(f'  > {UUID[:8]}…   (from the answers file)\n', out)
        self.assertNotIn('SECRETSEGMENT', out)
        self.assertNotIn(f'/stremio/{UUID}/', out)

    def test_an_alias_link_is_shown_as_one(self):
        self.aios.aliases['Home'] = UUID
        code, out = self.run_setup(dict(self.ANSWERS, TEST_READ='no',
                                        AIOS_CONFIG_ID=f'{PUBLIC}/stremio/u/Home/manifest.json'))
        self.assertEqual(code, 0, out)
        self.assertIn('  > an alias link   (from the answers file)\n', out)
        self.assertNotIn('/stremio/u/Home', out)


class RouteKept(Offline):
    """Setup never turns an installed folder into the other route: an answers file asking for it stops."""

    def test_a_personal_install_isnt_turned_into_a_public_instance(self):
        self.install()
        before = (self.state / 'settings.json').read_text()
        with mock.patch.object(hosting, 'fetch', side_effect=OSError('not reached')):
            code, out = self.run_setup({'ROUTE': 'host', 'HOST_ADDRESS': 'https://subs.example.com'})
        self.assertEqual(code, 1, out)
        self.assertIn("subsync is installed here for yourself; setup can't turn it into a public instance.", out)
        self.assertIn('Run ./setup --uninstall first, then ./setup again.', out)
        self.assertEqual((self.state / 'settings.json').read_text(), before)
        code, out = self.run_setup({'ROUTE': 'yourself', 'AIOS_CONFIG_PASSWORD': PASSWORD, 'TEST_READ': 'no'})
        self.assertEqual(code, 0, out)                                   # its own route goes on
        self.assertNotIn('How do you want to use it?', self.again()[1])  # and isn't asked again


FOREIGN = dockerenv.foreign         # the real ownership check, taken before any test puts a stand-in in its place
KEPT_VOLUME = 'aiosubtitles_subsync-data'
# state/compose.yaml as setup 1.3.0 wrote it for this install (installer/plan.py at aiosubtitles-public f4313df)
COMPOSE_1_3_0 = '''# Written by ./setup (AIOSubtitles). Run ./setup again to change it; edits here are replaced.
name: aiosubtitles
services:
  subsync:
    build: ../subsync
    image: aiosubtitles/subsync:1.3.0
    container_name: aiosubtitles-subsync
    restart: unless-stopped
    env_file: [subsync.env]
    volumes: ["subsync-data:/data"]
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}
    ports: ["127.0.0.1:8790:8790"]
    networks:
      default: {}
      aios0: {aliases: [aiosubtitles-subsync]}
networks:
  aios0: {external: true, name: "aio_default"}
volumes:
  subsync-data: {}
'''


class TakeOver(Offline):
    """An install setup 1.3.0 or before made: the same names, no install id, nothing labelled. Setup's steps take
    it over once, and its volume, which Docker can't label, is kept as it is: declared external, recorded in
    settings.json, accepted by the guard, removed by its name. Docker is a stand-in that lists what it has with each
    one's label; its compose up makes the container and network again with the compose file's label, and refuses a
    volume declared otherwise than it was made (Compose 2.27 and later asks "Recreate (data will be lost)?", which
    nobody would see). Nothing here is checked on a real Docker: see the ledger for that test."""

    def setUp(self):
        super().setUp()
        self.docker, self.volume_calls = {}, []
        self.install()                      # the folder's answers, keys and config copies, as an install leaves them
        s = engine.load_settings()
        self.settings_1_3_0 = dict({k: v for k, v in s.items() if k not in ('install_id', 'image', 'local')},
                                   version='1.3.0')
        engine.save_settings(self.settings_1_3_0)
        (self.state / 'compose.yaml').write_text(COMPOSE_1_3_0)
        self.docker = {('container', 'aiosubtitles-subsync'): '', ('volume', KEPT_VOLUME): '',
                       ('network', 'aiosubtitles_default'): ''}
        for p in (mock.patch.object(dockerenv, 'foreign', FOREIGN), mock.patch.object(dockerenv, 'run', self.run_docker)):
            p.start()
            self.addCleanup(p.stop)

    def run_docker(self, cmd, timeout=60, stdin=None):
        """Docker's listings with each item's install label, and its volume commands."""
        kind = {('docker', 'ps'): 'container', ('docker', 'volume'): 'volume', ('docker', 'network'): 'network'}.get(
            tuple(cmd[:2]))
        if kind and cmd[2] in ('-a', 'ls'):
            return 0, ''.join(f'{name}\t{label}\n' for (k, name), label in self.docker.items() if k == kind), ''
        if cmd[:2] == ['docker', 'volume']:
            self.volume_calls.append(cmd)
            if cmd[2] == 'rm' and self.docker.pop(('volume', cmd[3]), None) is None:
                return 1, '', f'Error response from daemon: get {cmd[3]}: no such volume'
        return 0, '', ''

    def _compose(self, state, *args, **kw):
        text = (state / 'compose.yaml').read_text()
        external = f'\n  subsync-data: {{external: true, name: "{KEPT_VOLUME}"}}\n' in text
        if 'up' in args:
            label = re.search(r'org\.aiosubtitles\.install: "(\w+)"', text).group(1)
            if not external and self.docker.get(('volume', KEPT_VOLUME), label) != label:
                return 1, '', (f'Volume "{KEPT_VOLUME}" exists but doesn\'t match configuration in compose file. '
                               'Recreate (data will be lost)? [y/N]')
            self.docker.update({('container', 'aiosubtitles-subsync'): label, ('network', 'aiosubtitles_default'): label})
            self.docker.setdefault(('volume', KEPT_VOLUME), label)
        if 'down' in args:
            self.docker.pop(('container', 'aiosubtitles-subsync'), None)
            self.docker.pop(('network', 'aiosubtitles_default'), None)
            if '-v' in args and not external:
                self.docker.pop(('volume', KEPT_VOLUME), None)
        return super()._compose(state, *args, **kw)

    def take_over(self):
        code, out = self.again()
        self.assertEqual(code, 0, out)
        return engine.load_settings(), out

    def test_its_steps_take_it_over_and_keep_its_volume(self):
        s, out = self.take_over()
        self.assertIn('✓ Took over the install an earlier setup (1.3.0 or before) made here; its stored copies are '
                      'kept.', flat(out))
        self.assertRegex(s['install_id'], r'^[0-9a-f]{16}$')
        self.assertEqual(s['external_volume'], KEPT_VOLUME)                   # recorded in settings.json
        compose = self.compose()
        label = f'{{org.aiosubtitles.install: "{s["install_id"]}"}}'
        self.assertIn(f'    labels: {label}\n', compose)                              # the container
        self.assertIn(f'  default: {{labels: {label}}}\n', compose)                   # its network
        self.assertIn(f'\nvolumes:\n  subsync-data: {{external: true, name: "{KEPT_VOLUME}"}}\n', compose)
        self.assertEqual(self.docker, {('container', 'aiosubtitles-subsync'): s['install_id'],
                                       ('volume', KEPT_VOLUME): '',          # never made, labelled or made again
                                       ('network', 'aiosubtitles_default'): s['install_id']})
        self.assertEqual(self.volume_calls, [])
        # a second ./setup passes the guard: the one unlabelled volume is this install's own, nothing is taken over
        code, out = self.again()
        self.assertEqual(code, 0, out)
        self.assertNotIn('Took over', out)
        again = engine.load_settings()
        self.assertEqual((again['install_id'], again['external_volume']), (s['install_id'], KEPT_VOLUME))
        self.assertIn(f'  subsync-data: {{external: true, name: "{KEPT_VOLUME}"}}\n', self.compose())

    def test_on_the_page_too(self):
        e = self.page_engine({})                    # through step 1's guard, AIOStreams and the config
        e.choices()
        e.limits()
        e.review_and_save()
        e.install()
        self.assertIn(('ok', engine.TAKEN_OVER), [tuple(r[:2]) for r in e.out.rows])
        self.assertEqual(engine.load_settings()['external_volume'], KEPT_VOLUME)

    def test_what_another_install_made_still_stops_it(self):
        # the take-over accepts only what carries no install label at all
        self.docker[('container', 'aiosubtitles-subsync')] = '0123456789abcdef'
        code, out = self.again()
        self.assertEqual(code, 1, out)
        self.assertIn(engine.GUARD_STOP.format(kind='container', name='aiosubtitles-subsync'), flat(out))
        self.assertEqual(engine.load_settings(), self.settings_1_3_0)
        self.assertEqual(self.compose(), COMPOSE_1_3_0)

    def test_a_volume_this_setup_didnt_make_says_what_it_may_hold(self):
        self.docker[('volume', KEPT_VOLUME)] = '0123456789abcdef'
        code, out = self.again()
        self.assertEqual(code, 1, out)
        self.assertIn("✗ Setup stopped without changing anything: Docker has a volume named aiosubtitles_subsync-data "
                      "that this setup didn't make. It may hold an AIOSubtitles' stored copies (on a public instance, "
                      "everyone's records): if it belongs to another AIOSubtitles, run that one's setup; remove it only "
                      'once you are sure nothing in it is needed.', flat(out))
        self.assertNotIn('Step 2', out)
        self.assertEqual(engine.load_settings(), self.settings_1_3_0)
        # a container or a network keeps the text it had
        self.assertEqual(engine.GUARD_STOP.format(kind='network', name='aiosubtitles_default'),
                         "Setup stopped without changing anything: Docker has a network named aiosubtitles_default that "
                         "this setup didn't make. If it belongs to another AIOSubtitles, run that one's setup; if not, "
                         'rename or remove it yourself.')

    def test_its_update_goes_through_the_steps_once(self):
        # its image was built from the code an update replaced, and settings.json has no image: Update can't switch
        for page, words in ((True, 'This install was made by setup 1.3.0 or before: press Change your answers and go '
                                   'through the steps once; your answers and stored copies are kept.'),
                            (False, 'This install was made by setup 1.3.0 or before: run ./setup and go through its '
                                    'steps once; your answers and stored copies are kept.')):
            with self.subTest(page=page), self.assertRaises(ui.Stop) as caught:
                engine.Engine({}, page=page).update()
            self.assertEqual(caught.exception.message, words)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(['--update'])
        self.assertEqual(code, 1)
        self.assertIn('✗ This install was made by setup 1.3.0 or before: run ./setup and go through its steps once; '
                      'your answers and stored copies are kept.', flat(out.getvalue()))
        self.assertEqual(engine.load_settings(), self.settings_1_3_0)
        self.assertEqual(self.compose(), COMPOSE_1_3_0)
        self.assertFalse([c for c, _ in self.exec_calls if 'copy-databases' in c])
        # once taken over, it updates like any other install
        self.take_over()
        with self.assertRaises(ui.Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, 'This is the newest version.')

    def test_removing_it_takes_its_volume_by_name(self):
        self.take_over()
        code, out = self.again('--uninstall', REMOVE='yes')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.volume_calls, [['docker', 'volume', 'rm', KEPT_VOLUME]])   # Compose never removes it
        self.assertEqual(self.docker, {})
        self.assertFalse((self.state / 'settings.json').exists())

    def test_a_removal_that_stopped_after_the_volume_went_finishes(self):
        self.take_over()
        self.docker.pop(('volume', KEPT_VOLUME))
        code, out = self.again('--uninstall', REMOVE='yes')
        self.assertEqual(code, 0, out)
        self.assertFalse((self.state / 'settings.json').exists())

    def test_removing_it_before_it_is_taken_over(self):
        # it is this folder's own: Compose removes it with its volume, which the 1.3.0 compose file doesn't declare
        # external
        code, out = self.again('--uninstall', REMOVE='yes')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.docker, {})
        self.assertEqual(self.volume_calls, [])

    def test_a_public_instance_keeps_its_volume_without_the_tick_and_removes_it_with_it(self):
        settings = {'route': 'host', 'installed': '2026-11-13', 'install_id': 'id-1', 'external_volume': KEPT_VOLUME,
                    'image': IMAGE, 'version': '1.6.0', 'port': 8790, 'public_base': 'https://subs.example.com'}
        engine.save_settings(settings)
        engine.write_compose(settings)
        self.docker = {('container', 'aiosubtitles-subsync'): 'id-1', ('volume', KEPT_VOLUME): '',
                       ('network', 'aiosubtitles_default'): 'id-1'}
        with self.assertRaises(ui.Stop) as caught:
            engine.Engine({'REMOVE': 'no'}).remove_host()
        self.assertEqual(caught.exception.message, 'Nothing was changed.')
        self.assertIn(('volume', KEPT_VOLUME), self.docker)
        self.assertEqual(self.volume_calls, [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine.Engine({'REMOVE': 'yes'}).remove_host()
        self.assertEqual(self.docker, {})
        self.assertEqual(self.volume_calls, [['docker', 'volume', 'rm', KEPT_VOLUME]])

    def test_the_old_updaters_second_half_says_how_to_finish(self):
        # setup 1.3.0's ./setup --update fetches the new files, then runs their wizard.py with --update-finish COMMIT
        # on this machine's Python; argparse refused the option ("unrecognized arguments")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = wizard.main(['--update-finish', 'f4313df6da78014141458d9e1b16d3b9206c1496'])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), '\n  !  The files are updated. Run ./setup to finish the update: it keeps your '
                                         'answers.\n\n')
        self.assertEqual(engine.load_settings(), self.settings_1_3_0)          # nothing else is done


class OldUpdater(unittest.TestCase):
    def test_its_second_half_runs_on_this_machines_python(self):
        # as 1.3.0's updater starts it: python3 -I installer/wizard.py --update-finish COMMIT, outside the image, with
        # nothing on its import path
        wizard_py = Path(wizard.__file__).resolve()
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONDONTWRITEBYTECODE': '1'}
        done = subprocess.run([sys.executable, '-I', str(wizard_py), '--update-finish', 'f4313df'], capture_output=True,
                              text=True, timeout=60, env=env, stdin=subprocess.DEVNULL, cwd=wizard_py.parent.parent)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(done.stdout, '\n  !  The files are updated. Run ./setup to finish the update: it keeps your '
                                      'answers.\n\n')
        self.assertEqual(done.stderr, '')


if __name__ == '__main__':
    unittest.main()
