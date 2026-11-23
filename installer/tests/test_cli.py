"""./setup's command line: the page by default, the answers file without it, --status, --update, --rollback,
--version, and what setup checks first inside its container."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import dockerenv, engine, ui, update, web, where, wizard
from installer.tests.test_flow import FlowFixture
from installer.ui import Stop

REPO = Path(__file__).resolve().parent.parent.parent


class NotSetUpHere(unittest.TestCase):
    """--status, --uninstall and --update in a folder where nothing is set up: their plain stop, and nothing asked of
    Docker or AIOStreams, nothing written."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / 'state'
        never = AssertionError('nothing is set up here: nothing to ask')
        for p in (mock.patch.object(engine, 'STATE', self.state), mock.patch.object(engine, 'PLACE', None),
                  mock.patch.object(dockerenv, 'run', side_effect=never),
                  mock.patch.object(engine.Aios, 'call', side_effect=never),
                  mock.patch.object(ui, 'COLOR', False), mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop('AIOSUBTITLES_IN_IMAGE', None)

    def test_each_command_stops_with_its_words(self):
        # No test reached these stops; without them --update went on to Docker's errors
        for argv, words in ((['--status'], ['subsync is not set up here yet.', 'Run ./setup']),
                            (['--uninstall', '--yes'], ['subsync is not set up here.']),
                            (['--update'], ['subsync is not set up here yet.', 'Run ./setup'])):
            with self.subTest(argv=argv):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = wizard.main(argv)
                self.assertEqual(code, 1)
                self.assertEqual(out.getvalue().split('\n')[1:-2], [f'  ✗  {words[0]}'] + [f'      {line}' for line in
                                                                                             words[1:]])
                self.assertFalse(self.state.exists())


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
        self.assertEqual((app.bind, app.port, app.public_port, app.public_host), ('0.0.0.0', 8789, 8899, '127.0.0.1'))
        # published on::1 only: the page's address is [::1]'s
        with mock.patch.object(engine, 'PLACE', place._replace(page_host='::1')):
            code, _, served = self.serve()
        self.assertEqual((code, served[0][0].public_host), (0, '[::1]'))

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
        # it left out the Usenet config, the stored copies and a public instance's records; and, on a public instance,
        # the database copies made before updates, which stay and hold everyone's records (engine.remove_host)
        readme = ' '.join((REPO / 'README.md').read_text().split())
        self.assertIn("```bash ./setup --uninstall ``` Takes subsync out of your config, deletes subsync's own configs "
                      'on your AIOStreams (its Usenet config too) and removes the container with its stored copies. It '
                      "then lists what stays: the subsync image, your web server's rule for subsync, and this folder "
                      'with the copies of your config. On a public instance it removes AIOSubtitles with everyone\'s '
                      "records and stored copies, after you confirm; the sync configs it made on people's AIOStreams "
                      'stay, and so do the copies of the databases made before updates, in `state/backups/`, which hold '
                      "everyone's records: delete them yourself when you no longer need them. ```bash", readme)
        self.assertIn("the database copies made before updates in state/backups/, which hold everyone's records",
                      (REPO / 'installer' / 'engine.py').read_text())

    def test_the_readme_says_how_setup_and_updates_work_now(self):
        readme = (REPO / 'README.md').read_text()
        install = readme.split('## Install', 1)[1].split('\n## ', 1)[0]
        self.assertIn('docker run --rm -it --pull always -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/folder" '
                      '-p 127.0.0.1:8789:8789 ghcr.io/haslanli/aiosubtitles:latest setup\n', install)
        place = self.place._replace(page_port=8789, run_as='ghcr.io/haslanli/aiosubtitles:latest')
        script = where.setup_script(place)
        self.assertIn('-v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/folder" -p 127.0.0.1:8789:8789 '
                      'ghcr.io/haslanli/aiosubtitles:latest setup', script)        # the folder's ./setup: the same
        # it said the ssh -L line is printed only on a server; setup always prints it
        self.assertIn('Setup runs in your browser: it prints the address to open on this machine, and an `ssh -L` line '
                      'for opening it from another computer (run that line on the other computer first, then open the '
                      'same address there).', ' '.join(install.split()))
        self.assertTrue(any(line.startswith('  ssh -L 8789:127.0.0.1:8789 ')
                            for line in web.terminal_lines(8789, 'http://127.0.0.1:8789/')))
        self.assertIn("### Why setup needs Docker's socket", install)
        updates = readme.split('## Update', 1)[1].split('\n## ', 1)[0]
        self.assertIn('./setup --update', updates)
        self.assertIn(update.ABOUT, ' '.join(updates.split()))
        self.assertIn('./setup --rollback --restore-databases', updates)
        self.assertIn("This folder wasn't downloaded with git? Then it can't fetch a new version",
                      ' '.join(updates.split()))                           # the source route
        self.assertNotIn('Python 3', readme.split('## What you need', 1)[1].split('\n## ', 1)[0])

    def test_the_readme_says_an_answers_file_goes_in_state(self):
        # setup sees the folder's state/ on both routes; the source route mounts nothing else. The image route's
        # ./setup is written only once setup has installed AIOSubtitles, so a first install gives the answers to the
        # docker run command (it said "After this first run", and sent a first install to ./setup)
        install = ' '.join((REPO / 'README.md').read_text().split('## Install', 1)[1].split('\n## ', 1)[0].split())
        self.assertIn('To set it up without the page, give the answers in a file in `state/`: '
                      '`./setup --answers state/FILE --yes` (see `docs/answers.example`). Before the first install '
                      'there is no `./setup` yet: make `state/`, put the file in it, and add `--answers state/FILE '
                      '--yes` after `setup` at the end of the command above.', install)
        self.assertIn('Once setup has installed AIOSubtitles, the folder has a `./setup` that runs the same command: '
                      'use it from then on.', install)
        self.assertNotIn('After this first run', install)
        self.assertNotIn('`./setup --answers FILE --yes`', install)

    def test_the_readme_says_to_change_only_the_first_port_number(self):
        # "change both numbers" (-p 127.0.0.1:9000:9000) left the page's port unpublished, and setup stopped
        readme = (REPO / 'README.md').read_text()
        install = ' '.join(readme.split('## Install', 1)[1].split('\n## ', 1)[0].split())
        self.assertIn(f'- `-p 127.0.0.1:8789:{where.PAGE_PORT}`: the setup page, on this machine only. If 8789 is '
                      f'taken here, change the first number only, like `-p 127.0.0.1:9000:{where.PAGE_PORT}`.', install)
        self.assertNotIn('change both numbers', install)
        source = ' '.join(readme.split('### Build it from source', 1)[1].split('\n## ', 1)[0].split())
        self.assertIn('It needs Docker and git, nothing else. Off Linux the page uses port 8789 on this machine; if it '
                      'is taken, run `AIOSUBTITLES_SETUP_PORT=9000 ./setup`.', source)
        self.assertIn(f'net="-p 127.0.0.1:${{AIOSUBTITLES_SETUP_PORT:-8789}}:{where.PAGE_PORT}"',
                      (REPO / 'setup').read_text())                     # the variable the source route's ./setup reads


if __name__ == '__main__':
    unittest.main()
