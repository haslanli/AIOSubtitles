"""The source folder's./setup (§6): builds the image here, then runs setup inside it on state/; --update first
fetches the folder's newer commit with git, and a failed build puts the folder back. Real git in throwaway folders
(the "origin" is a local folder, so nothing leaves this machine); docker and uname are stand-ins."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from installer import update

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = (REPO / 'setup').read_text()


def git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c',
                           'init.defaultBranch=main', *args], cwd=cwd, check=True, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60).stdout.strip()


@unittest.skipUnless(shutil.which('git') and shutil.which('bash'), 'needs git and bash')
class SourceSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.origin = self.tmp / 'origin'
        (self.origin / 'subsync').mkdir(parents=True)
        (self.origin / 'subsync' / 'Dockerfile').write_text('FROM scratch\n')
        (self.origin / 'VERSION').write_text('1.4.0\n')
        (self.origin / '.gitignore').write_text('state/\n')
        (self.origin / 'setup').write_text(SCRIPT)
        (self.origin / 'setup').chmod(0o755)
        git(self.origin, 'init', '-q')
        git(self.origin, 'add', '-A')
        git(self.origin, 'commit', '-qm', 'one')
        git(self.tmp, 'clone', '-q', str(self.origin), 'folder')
        self.folder = self.tmp / 'folder'
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        self.log = self.tmp / 'docker.log'
        self.stub('docker', 'echo "$*" >> "$LOG"; case "$1" in build) [ -z "$BUILD_FAILS" ];; esac')
        self.stub('uname', 'echo "$UNAME"')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f'#!/bin/sh\n{body}\n')
        path.chmod(0o755)

    def run_setup(self, *args, system='Linux', build_fails=False):
        env = {'PATH': f'{self.bin}:/usr/bin:/bin', 'LOG': str(self.log), 'UNAME': system, 'HOME': str(self.tmp),
               'BUILD_FAILS': '1' if build_fails else ''}
        done = subprocess.run(['bash', str(self.folder / 'setup'), *args], capture_output=True, text=True,
                              timeout=60, env=env, stdin=subprocess.DEVNULL, cwd=self.tmp)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        self.log.unlink(missing_ok=True)
        return done.returncode, done.stdout + done.stderr, calls

    def release(self, version):
        (self.origin / 'VERSION').write_text(version + '\n')
        git(self.origin, 'commit', '-qam', version)

    def test_it_builds_then_runs_setup_on_state(self):
        code, out, calls = self.run_setup('--status')
        self.assertEqual(code, 0, out)
        self.assertEqual(calls[0], 'build -f subsync/Dockerfile -t aiosubtitles:local-1.4.0 .')
        run = calls[1]
        self.assertTrue(run.startswith('run --rm -i '), run)
        self.assertIn('--network host', run)                                   # Linux: setup on the machine's network
        self.assertIn('-v /var/run/docker.sock:/var/run/docker.sock', run)
        self.assertIn(f'-v {self.folder}/state:/folder/state', run)           # only state/, never the code
        self.assertIn('-e AIOSUBTITLES_ROUTE=source', run)
        self.assertTrue(run.endswith('aiosubtitles:local-1.4.0 setup --status'), run)
        self.assertTrue((self.folder / 'state').is_dir())

    def test_on_a_mac_the_page_is_on_a_published_port(self):
        _, _, calls = self.run_setup(system='Darwin')
        self.assertIn('-p 127.0.0.1:8789:8789', calls[1])
        self.assertNotIn('--network host', calls[1])

    def test_a_failed_build_runs_nothing(self):
        code, _, calls = self.run_setup(build_fails=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(len(calls), 1)

    def test_update_fetches_the_newer_version_then_builds_and_switches(self):
        self.release('1.5.0')
        code, out, calls = self.run_setup('--update')
        self.assertEqual(code, 0, out)
        self.assertEqual((self.folder / 'VERSION').read_text().strip(), '1.5.0')
        self.assertEqual(calls[0], 'build -f subsync/Dockerfile -t aiosubtitles:local-1.5.0 .')
        self.assertTrue(calls[1].endswith('aiosubtitles:local-1.5.0 setup --update'), calls[1])

    def test_a_failed_build_puts_the_folder_back(self):
        self.release('1.5.0')
        before = git(self.folder, 'rev-parse', 'HEAD')
        code, out, calls = self.run_setup('--update', build_fails=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(git(self.folder, 'rev-parse', 'HEAD'), before)
        self.assertEqual((self.folder / 'VERSION').read_text().strip(), '1.4.0')
        self.assertIn(update.FAILED.format(reason='building version 1.5.0 failed', version='1.4.0'), out)
        self.assertEqual(len(calls), 1)                                        # the old version keeps running

    def test_changes_by_hand_stop_the_update_before_anything(self):
        self.release('1.5.0')
        (self.folder / 'VERSION').write_text('1.4.0-mine\n')
        code, out, calls = self.run_setup('--update')
        self.assertNotEqual(code, 0)
        self.assertIn(update.CHANGED.format(files='VERSION'), out)
        self.assertEqual(calls, [])

    def test_a_folder_without_git_says_so(self):
        shutil.rmtree(self.folder / '.git')
        code, out, calls = self.run_setup('--update')
        self.assertNotEqual(code, 0)
        self.assertIn(update.NOT_GIT, out)
        self.assertEqual(calls, [])

    def test_nothing_newer_still_asks_setup(self):
        code, out, calls = self.run_setup('--update')
        self.assertEqual(code, 0, out)
        self.assertTrue(calls[1].endswith('aiosubtitles:local-1.4.0 setup --update'))   # setup says it's the newest

    def test_no_docker_is_a_plain_message(self):
        os.unlink(self.bin / 'docker')
        env = {'PATH': f'{self.bin}:/bin', 'LOG': str(self.log), 'UNAME': 'Linux'}
        done = subprocess.run(['bash', str(self.folder / 'setup')], capture_output=True, text=True, timeout=30,
                              env=env, stdin=subprocess.DEVNULL)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('Docker is not installed.', done.stdout)

    def test_the_scripts_words_are_setups_words(self):
        for words in (update.NOT_GIT, update.CHANGED.split('{')[0], update.LOOK_FAILED.split('{')[0],
                      update.FAILED.split('{')[0]):
            self.assertIn(words, SCRIPT)


if __name__ == '__main__':
    unittest.main()
