"""Easy updates: only when asked, by git; the old version keeps running
until the new one answers, and a failed build puts the folder back. Real git, in throwaway folders: the "origin" is a
local folder, so nothing leaves this machine."""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import dockerenv, engine, update
from installer.ui import Stop


def git(cwd, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'init.defaultBranch=main',
                           *args], cwd=cwd, check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout


class Repos(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.origin = self.tmp / 'origin'
        self.origin.mkdir()
        git(self.origin, 'init', '-q')
        (self.origin / 'VERSION').write_text('1.1.0\n')
        (self.origin / '.gitignore').write_text('state/\n')
        (self.origin / 'setup').write_text('#!/bin/sh\n')
        git(self.origin, 'add', '-A')
        git(self.origin, 'commit', '-qm', 'one')
        git(self.tmp, 'clone', '-q', str(self.origin), 'folder')
        self.folder = self.tmp / 'folder'

    def release(self, version):
        (self.origin / 'VERSION').write_text(version + '\n')
        git(self.origin, 'commit', '-qam', version)


class Look(Repos):
    def test_the_newest_version(self):
        self.assertEqual(update.look(self.folder), update.Ready(None, 'newest', None))

    def test_a_newer_version_is_ready(self):
        self.release('1.2.0')
        self.assertEqual(update.look(self.folder), update.Ready('1.2.0', 'git', None))
        self.assertEqual(update.running(self.folder), '1.1.0')          # looking changes nothing here

    def test_a_folder_not_cloned_with_git(self):
        plain = self.tmp / 'plain'
        plain.mkdir()
        (plain / 'VERSION').write_text('1.1.0\n')
        self.assertEqual(update.look(plain).how, 'not-git')

    def test_no_network_is_a_reason_not_a_crash(self):
        shutil.rmtree(self.origin)
        ready = update.look(self.folder)
        self.assertEqual(ready.how, 'error')
        self.assertTrue(ready.reason)

    def test_the_words(self):
        self.assertEqual(update.NEWEST, 'This is the newest version.')
        self.assertEqual(update.LOOK_FAILED.format(reason='x'), "Couldn't look for a new version: x.")
        self.assertEqual(update.NOT_GIT, "This folder wasn't downloaded with git, so it can't fetch a new version: "
                                         "download the new version, copy this folder's state/ into it, and run ./setup "
                                         "there.")


class Apply(Repos):
    def test_a_folder_changed_by_hand_stops_before_changing_anything(self):
        self.release('1.2.0')
        (self.folder / 'setup').write_text('#!/bin/sh\necho mine\n')
        head = git(self.folder, 'rev-parse', 'HEAD')
        with self.assertRaises(update.UpdateStopped) as caught:
            update.fast_forward(self.folder)
        self.assertEqual(caught.exception.message, 'This folder has changes of its own, so the update stopped before '
                                                   'changing anything: setup.')
        self.assertEqual(git(self.folder, 'rev-parse', 'HEAD'), head)
        self.assertEqual(update.running(self.folder), '1.1.0')

    def test_state_is_never_counted_as_a_change(self):
        self.release('1.2.0')
        (self.folder / 'state').mkdir()
        (self.folder / 'state' / 'subsync.env').write_text('X=1\n')
        update.look(self.folder)                                         # what was shown is what is applied
        update.fast_forward(self.folder)
        self.assertEqual(update.running(self.folder), '1.2.0')
        self.assertTrue((self.folder / 'state' / 'subsync.env').exists())

    def test_fast_forward_then_put_back(self):
        self.release('1.2.0')
        update.look(self.folder)
        old = update.fast_forward(self.folder)
        self.assertEqual(update.running(self.folder), '1.2.0')
        self.assertEqual(update.version_at(self.folder, old), '1.1.0')
        update.put_back(self.folder, old)
        self.assertEqual(update.running(self.folder), '1.1.0')

    def test_keep_the_newest_three(self):
        backups = self.tmp / 'backups'
        for stamp in ('20261101-000000', '20261102-000000', '20261103-000000', '20261104-000000'):
            (backups / f'databases-{stamp}').mkdir(parents=True)
        (backups / 'viewer-1a2b3c4d-20261101-000000.json').write_text('{}')
        update.keep_newest(backups, 'databases-', 3)
        self.assertEqual(sorted(p.name for p in backups.iterdir()),
                         ['databases-20261102-000000', 'databases-20261103-000000', 'databases-20261104-000000',
                          'viewer-1a2b3c4d-20261101-000000.json'])


class EngineUpdate(Repos):
    """The update as the page and ./setup --update run it, with Docker faked."""

    def setUp(self):
        super().setUp()
        self.state = self.folder / 'state'
        self.events = []
        self.build_fails = False

        def compose(state, *args, **kw):
            text = (state / 'compose.yaml').read_text()
            tag = text.split('image: aiosubtitles/subsync:')[1].split()[0]
            self.events.append((args[0], tag))
            if args[0] == 'build' and self.build_fails:
                return 1, '', 'boom: the build failed'
            return 0, '', ''

        def exec_in(name, cmd, stdin=None, timeout=120):
            self.events.append(('exec', ' '.join(cmd)))
            if 'copy-databases' in cmd:
                return 0, json.dumps({'copied': ['users.sqlite3', 'subsync.sqlite3']}), ''
            return 0, '{}', ''

        def run(cmd, timeout=60, stdin=None):
            if cmd[:2] == ['docker', 'cp']:
                self.events.append(('cp', cmd[2]))
                dest = Path(cmd[3])
                dest.mkdir(parents=True)
                (dest / 'users.sqlite3').write_text('x')
            return 0, '', ''
        for p in (mock.patch.object(engine, 'STATE', self.state), mock.patch.object(engine, 'REPO', self.folder),
                  mock.patch.object(dockerenv, 'compose', compose), mock.patch.object(dockerenv, 'run', run),
                  mock.patch.object(dockerenv, 'exec_in', exec_in),
                  mock.patch.object(dockerenv, 'container_state', lambda n: 'running healthy'),
                  mock.patch.object(engine.Engine, 'public_health', lambda self: True),
                  mock.patch('installer.hosting.check', lambda address, out: out.ok('checked') or True)):
            p.start()
            self.addCleanup(p.stop)

    def install(self, route='yourself'):
        self.state.mkdir()
        settings = {'route': route, 'version': '1.1.0', 'mode': 'remote', 'networks': [], 'bind': '127.0.0.1',
                    'port': 8790, 'public_base': 'https://aio.example.com/subsync', 'installed': '2026-11-13'}
        engine.save_settings(settings)
        (self.state / 'compose.yaml').write_text(engine.compose_for(settings, '1.1.0'))
        self.compose_before = (self.state / 'compose.yaml').read_text()

    def update(self):
        """The whole update in this process: the restart runs the second half on 'the new code' (the same code here)."""
        e = engine.Engine({}, page=True)
        e.update(restart=lambda old: engine.Engine({}, page=True, out=e.out).finish_update(old))
        return e

    def test_nothing_is_fetched_until_asked(self):
        self.install()
        with mock.patch.object(update, 'look', side_effect=AssertionError('fetched')):
            engine.Engine({}, page=True).prefill()

    def test_a_good_update_builds_the_new_version_while_the_old_one_runs(self):
        self.install()
        self.release('1.2.0')
        e = self.update()
        rows = [tuple(r[:2]) for r in e.out.rows]
        self.assertIn(('done', 'Fetching version 1.2.0'), rows)
        self.assertIn(('done', 'Starting setup again on the new version'), rows)
        self.assertIn('image: aiosubtitles/subsync:1.2.0', (self.state / 'compose.yaml').read_text())
        self.assertEqual([ev for ev in self.events if ev[0] in ('build', 'up')], [('build', '1.2.0'), ('up', '1.2.0')])
        self.assertEqual(engine.load_settings()['version'], '1.2.0')

    def test_update_never_leaves_a_half_update(self):
        self.install()
        self.release('1.2.0')
        self.build_fails = True
        with self.assertRaises(Stop) as caught:
            self.update()
        self.assertEqual(caught.exception.message, "The update didn't go through: Building subsync failed. Version "
                                                   "1.1.0 keeps running.")
        self.assertEqual(update.running(self.folder), '1.1.0')                   # the folder is put back
        self.assertEqual((self.state / 'compose.yaml').read_text(), self.compose_before)
        self.assertNotIn(('up', '1.2.0'), self.events)                           # the new one never started
        self.assertEqual(engine.load_settings()['version'], '1.1.0')

    def test_an_update_that_cant_go_ahead_changes_nothing(self):
        self.install()
        e = engine.Engine({}, page=True)
        with self.assertRaises(Stop) as caught:
            e.update(restart=lambda old: self.fail('restarted'))
        self.assertEqual(caught.exception.message, 'This is the newest version.')
        self.assertEqual(self.events, [])

    def test_a_public_instance_copies_its_databases_first(self):
        self.install(route='host')
        self.release('1.2.0')
        backups = self.state / 'backups'
        for stamp in ('20261101-000000', '20261102-000000', '20261103-000000'):
            (backups / f'databases-{stamp}').mkdir(parents=True)
        e = self.update()
        kinds = [ev[0] for ev in self.events]
        self.assertLess(kinds.index('cp'), kinds.index('build'))                  # the copy before anything changes
        copies = sorted(p.name for p in backups.iterdir() if p.name.startswith('databases-'))
        self.assertEqual(len(copies), 3)
        self.assertNotIn('databases-20261101-000000', copies)
        newest = backups / copies[-1]
        self.assertEqual(stat.S_IMODE(os.stat(newest).st_mode), 0o700)
        self.assertTrue(any(r[1].startswith('Saved a copy of the databases: state/backups/databases-') for r in e.out.rows))


if __name__ == '__main__':
    unittest.main()
