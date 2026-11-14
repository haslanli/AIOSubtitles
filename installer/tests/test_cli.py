"""./setup's command line: the page by default, the answers file without it, --status, --update, --rollback,
--version, and what setup checks first inside its container."""
import contextlib
import io
import os
import unittest
from unittest import mock

from installer import dockerenv, engine, update, web, where, wizard
from installer.tests.test_flow import FlowFixture
from installer.ui import Stop


class CommandLine(FlowFixture):
    def run_main(self, *argv, env=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, env or {}, clear=False):
            code = wizard.main(list(argv))
        return code, out.getvalue()

    def serve(self, *argv, env=None):
        served = []
        with mock.patch.object(web, 'serve', lambda app, say=print, opener=None: served.append((app, opener)) or 0):
            code, out = self.run_main(*argv, env=env)
        return code, out, served

    def test_setup_with_no_flag_opens_the_page(self):
        code, _, served = self.serve()
        self.assertEqual(code, 0)
        app, opener = served[0]
        self.assertIsInstance(app, web.App)
        self.assertEqual((app.bind, app.port, app.public_port), ('127.0.0.1', 0, None))   # setup's own network
        self.assertIsNone(opener)                                # in a container: no browser to open, the address shows

    def test_on_dockers_bridge_the_page_is_at_the_machines_port(self):
        place = self.place._replace(network='bridge', page_port=8899)
        with mock.patch.object(engine, 'PLACE', place):
            code, _, served = self.serve()
        app, _ = served[0]
        self.assertEqual((app.bind, app.port, app.public_port), ('0.0.0.0', 8789, 8899))

    def test_no_port_for_the_page_says_how_to_add_it(self):
        place = self.place._replace(network='bridge', page_port=None)
        with mock.patch.object(engine, 'PLACE', place):
            code, out, served = self.serve()
        self.assertEqual((code, served), (1, []))
        self.assertIn(where.NO_PAGE_PORT, ' '.join(out.split()))

    def test_version(self):
        with mock.patch.object(engine, 'setup_version', lambda: '1.4.0'), \
                mock.patch.object(where, 'discover', side_effect=AssertionError('no Docker needed')):
            code, out = self.run_main('--version', env={'AIOSUBTITLES_IN_IMAGE': '1'})
        self.assertEqual((code, out), (0, '1.4.0\n'))

    def test_without_the_socket_setup_says_what_to_add(self):
        with mock.patch.object(dockerenv, 'SOCKET', '/nonexistent/docker.sock'), \
                mock.patch.object(where, 'discover', side_effect=AssertionError('not reached')):
            code, out = self.run_main('--status', env={'AIOSUBTITLES_IN_IMAGE': '1'})
        self.assertEqual(code, 1)
        self.assertIn(where.NO_SOCKET, ' '.join(out.split()))
        self.assertNotIn('Traceback', out)

    def test_inside_the_image_setup_reads_where_it_runs_first(self):
        found = self.place._replace(host_folder='/srv/x')
        with mock.patch.object(dockerenv, 'SOCKET', __file__), \
                mock.patch.object(where, 'discover', lambda version: found), \
                mock.patch.object(engine, 'PLACE', None):
            self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
            code, _ = self.run_main('--status', env={'AIOSUBTITLES_IN_IMAGE': '1'})
            self.assertEqual(engine.PLACE, found)
        self.assertEqual(code, 0)

    def test_a_folder_missing_from_the_command_is_a_plain_message(self):
        with mock.patch.object(dockerenv, 'SOCKET', __file__), \
                mock.patch.object(where, 'discover', side_effect=RuntimeError(where.NO_FOLDER)):
            code, out = self.run_main('--status', env={'AIOSUBTITLES_IN_IMAGE': '1'})
        self.assertEqual(code, 1)
        self.assertIn(where.NO_FOLDER, ' '.join(out.split()))

    def test_status(self):
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        code, out = self.run_main('--status')
        self.assertEqual(code, 0)
        self.assertIn('AIOSubtitles · subsync status', out)
        self.assertIn('languages: eng, rus', out)

    def test_update_and_rollback_from_the_terminal(self):
        with mock.patch.object(engine.Engine, 'update', side_effect=Stop(update.NEWEST)):
            code, out = self.run_main('--update')
        self.assertEqual(code, 1)
        self.assertIn('This is the newest version.', out)
        calls = []
        with mock.patch.object(engine.Engine, 'rollback', lambda e, restore=False: calls.append(restore) or True):
            code, _ = self.run_main('--rollback')
            self.assertEqual(code, 0)
            self.run_main('--rollback', '--restore-databases')
        self.assertEqual(calls, [False, True])

    def test_a_problem_in_a_terminal_run_is_a_plain_message(self):
        # the engine's Problem once reached the terminal as a traceback
        with mock.patch.object(engine.dockerenv, 'port_in_use', lambda *a: True), \
                mock.patch.object(engine.dockerenv, 'container_state', lambda n: ''):
            code, out = self.run_main('--answers', self.answers_file(dict(self.ANSWERS, TEST_READ='no')), '--yes')
        self.assertEqual(code, 1)
        self.assertIn('Port 8790 on this machine is already in use.', out)

    def answers_file(self, answers):
        path = os.path.join(self.tmp.name, 'answers-cli')
        with open(path, 'w') as f:
            f.write(''.join(f'{k}={v}\n' for k, v in answers.items()))
        return path

    def test_the_readme_says_how_setup_and_updates_work_now(self):
        readme = (engine.REPO.__class__(__file__).resolve().parent.parent.parent / 'README.md').read_text()
        install = readme.split('## Install', 1)[1].split('\n## ', 1)[0]
        self.assertIn('docker run --rm -it --pull always -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/folder" '
                      '-p 127.0.0.1:8789:8789 ghcr.io/haslanli/aiosubtitles:latest setup\n', install)
        place = self.place._replace(page_port=8789, run_as='ghcr.io/haslanli/aiosubtitles:latest')
        script = where.setup_script(place)
        self.assertIn('-v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/folder" -p 127.0.0.1:8789:8789 '
                      'ghcr.io/haslanli/aiosubtitles:latest setup', script)        # the folder's ./setup: the same
        self.assertIn('Setup runs in your browser.', install)
        self.assertIn('ssh -L', install)
        self.assertIn("### Why setup needs Docker's socket", install)
        updates = readme.split('## Update', 1)[1].split('\n## ', 1)[0]
        self.assertIn('./setup --update', updates)
        self.assertIn(update.ABOUT, ' '.join(updates.split()))
        self.assertIn('./setup --rollback --restore-databases', updates)
        self.assertIn("This folder wasn't downloaded with git? Then it can't fetch a new version",
                      ' '.join(updates.split()))                           # the source route
        self.assertNotIn('Python 3', readme.split('## What you need', 1)[1].split('\n## ', 1)[0])

if __name__ == '__main__':
    unittest.main()
