"""./setup's command line: the page by default, the answers file without it,
--status, --update and the second half of an update after its restart."""
import contextlib
import io
import os
import unittest
from unittest import mock

from installer import engine, fonts, update, web, wizard
from installer.tests.test_flow import FlowFixture
from installer.ui import Stop


class CommandLine(FlowFixture):
    def run_main(self, *argv, env=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.dict(os.environ, env or {}, clear=False):
            code = wizard.main(list(argv))
        return code, out.getvalue()

    def test_setup_with_no_flag_opens_the_page(self):
        served = []
        with mock.patch.object(web, 'serve', lambda app, say=print, opener=None: served.append((app, opener)) or 0), \
                mock.patch.object(fonts, 'ensure', lambda state, fetch=None: None):
            code, _ = self.run_main('--no-browser')
        self.assertEqual(code, 0)
        app, opener = served[0]
        self.assertIsInstance(app, web.App)
        self.assertIsNone(opener)
        self.assertIsNone(app.resume)

    def test_the_second_half_of_an_update_resumes_on_the_same_page(self):
        served = []
        env = {'AIOSUBTITLES_SETUP_PORT': '8899', 'AIOSUBTITLES_SETUP_KEY': 'k' * 32, 'AIOSUBTITLES_SETUP_RESUME': 'abc123'}
        with mock.patch.object(web, 'serve', lambda app, say=print, opener=None: served.append(app) or 0), \
                mock.patch.object(fonts, 'ensure', lambda state, fetch=None: None), \
                mock.patch.object(web.App, 'start_resumed', lambda self: setattr(self, 'resumed', True)):
            self.run_main(env=env)
        app = served[0]
        self.assertEqual((app.port, app.key, app.resume, app.resumed), (8899, 'k' * 32, 'abc123', True))
        self.assertNotIn('AIOSUBTITLES_SETUP_KEY', os.environ)          # not passed on to anything else

    def test_status(self):
        self.run_setup(dict(self.ANSWERS, TEST_READ='no'))
        code, out = self.run_main('--status')
        self.assertEqual(code, 0)
        self.assertIn('AIOSubtitles · subsync status', out)
        self.assertIn('languages: eng, rus', out)

    def test_update_from_the_terminal(self):
        with mock.patch.object(engine.Engine, 'update', side_effect=Stop(update.NEWEST)):
            code, out = self.run_main('--update')
        self.assertEqual(code, 1)
        self.assertIn('This is the newest version.', out)
        with mock.patch.object(engine.Engine, 'finish_update', lambda self, old: old == 'abc123'):
            code, _ = self.run_main('--update-finish', 'abc123')
        self.assertEqual(code, 0)

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
        self.assertIn('Setup runs in your browser.', install)
        self.assertIn('ssh -L', install)
        updates = readme.split('## Update', 1)[1].split('\n## ', 1)[0]
        self.assertIn('./setup --update', updates)
        self.assertIn(update.ABOUT, ' '.join(updates.split()))


if __name__ == '__main__':
    unittest.main()
