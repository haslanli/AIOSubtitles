"""./setup's command line: the page by default, the answers file without it, --status, --update, --rollback,
--version, and what setup checks first inside its container."""
import contextlib
import io
import os
import unittest
from pathlib import Path
from unittest import mock

from installer import dockerenv, engine, update, web, where, wizard
from installer.tests.test_flow import FlowFixture
from installer.ui import Stop

REPO = Path(__file__).resolve().parent.parent.parent


class CommandLine(FlowFixture):
    def run_main(self, *argv, env=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, env or {}, clear=False):
            code = wizard.main(list(argv))
        return code, out.getvalue()

    def serve(self, *argv, env=None):
        served = []
        with mock.patch.object(web, 'serve', lambda app, say=print, **more: served.append((app, more)) or 0):
            code, out = self.run_main(*argv, env=env)
        return code, out, served

    def test_setup_with_no_flag_opens_the_page(self):
        code, _, served = self.serve()
        self.assertEqual(code, 0)
        app, more = served[0]
        self.assertIsInstance(app, web.App)
        self.assertEqual((app.bind, app.port, app.public_port), ('127.0.0.1', 0, None))   # setup's own network
        self.assertEqual(more, {})              # in a container: no browser to open (no opener), the address shows

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

    def test_a_page_port_other_computers_reach_is_refused(self):
        # -p 8789:8789 started the page; one try from another computer sent its key over the network in clear
        place = self.place._replace(network='bridge', page_port=8789, page_exposed=True)
        with mock.patch.object(engine, 'PLACE', place):
            code, out, served = self.serve()
        self.assertEqual((code, served), (1, []))
        self.assertIn('✗ The setup page must be reachable on this machine only: use -p 127.0.0.1:8789:8789 in the '
                      'docker run command (README, Install), not -p 8789:8789.', ' '.join(out.split()))

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

    def test_help_names_both_routes(self):
        # it said only "Add subsync (synced subtitles) to your AIOStreams."
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            wizard.main(['--help'])
        self.assertIn('Set up AIOSubtitles: add subsync (synced subtitles) to your AIOStreams, or host a public '
                      'instance.', ' '.join(out.getvalue().split()))

    def test_the_readme_says_what_uninstall_deletes(self):
        # it left out the Usenet config, the stored copies and a public instance's records
        readme = ' '.join((REPO / 'README.md').read_text().split())
        self.assertIn("```bash ./setup --uninstall ``` Takes subsync out of your config, deletes subsync's own configs "
                      'on your AIOStreams (its Usenet config too) and removes the container with its stored copies. It '
                      "then lists what stays: the subsync image, your web server's rule for subsync, and this folder "
                      'with the copies of your config. On a public instance it removes AIOSubtitles with everyone\'s '
                      "records and stored copies, after you confirm; the sync configs it made on people's AIOStreams "
                      'stay.', readme)

    def test_the_readme_says_how_setup_and_updates_work_now(self):
        readme = (REPO / 'README.md').read_text()
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

    def test_the_readme_says_an_answers_file_goes_in_state(self):
        # setup sees the folder's state/ on both routes; the source route mounts nothing else
        install = ' '.join((REPO / 'README.md').read_text().split('## Install', 1)[1].split('\n## ', 1)[0].split())
        self.assertIn('To set it up without the page, give the answers in a file in `state/`: '
                      '`./setup --answers state/FILE --yes` (see `docs/answers.example`).', install)
        self.assertNotIn('`./setup --answers FILE --yes`', install)

if __name__ == '__main__':
    unittest.main()
