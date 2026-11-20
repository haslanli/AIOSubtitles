"""Updates as a newer image: only when asked; the databases copied first; the new version checked
while the old one keeps running; then the switch, checked, and put back when it fails, with the databases from
before the update when the old version needs them; a rollback on request. Docker is a stand-in that knows which image
the service runs and whether that one answers."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import dockerenv, engine, plan, update, where
from installer.ui import Stop

OLD = 'ghcr.io/haslanli/aiosubtitles:1.3.0@sha256:' + '1' * 64
NEW = 'ghcr.io/haslanli/aiosubtitles:1.4.0@sha256:' + '2' * 64


def place(image=NEW, version_local=False):
    return where.Place('image', 'bridge', 'c0ffee', image, version_local, 'ghcr.io/haslanli/aiosubtitles:latest', 8789,
                       '/home/me/aiosubtitles', (os.getuid(), os.getgid()), '172.17.0.1')


class Compare(unittest.TestCase):
    def test_a_newer_setup_is_an_update(self):
        self.assertEqual(update.compare({'version': '1.3.0', 'image': OLD}, NEW, '1.4.0'), update.Ready('1.4.0', 'ready'))

    def test_the_same_one_is_the_newest(self):
        self.assertEqual(update.compare({'version': '1.4.0', 'image': NEW}, NEW, '1.4.0').how, 'newest')

    def test_an_older_setup_offers_only_a_rollback(self):
        self.assertEqual(update.compare({'version': '1.4.0', 'image': NEW}, OLD, '1.3.0').how, 'newer-here')
        self.assertEqual(update.compare({'version': '1.10.0', 'image': NEW}, OLD, '1.9.0').how, 'newer-here')

    def test_another_build_of_the_same_version_is_an_update(self):
        # a release candidate's 1.4.0 before the release's 1.4.0
        self.assertEqual(update.compare({'version': '1.4.0', 'image': OLD}, NEW, '1.4.0').how, 'ready')


class EngineUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        self.events = []
        self.answers = {OLD: True, NEW: True}       # which image's service answers
        self.answers_now = lambda image: self.answers.get(image)
        self.readable = lambda image: True              # whether that image's service reads its databases
        self.check_answers = True                   # the throwaway start check of the new image
        self.running = None
        self.foreign = []
        self.public_ok = [True, True]                # the route's own check, before and after the switch
        self.compose_fails = {}                     # (command, image): how many of those calls Compose refuses next
        self.waited = []                            # the images the engine waited on to answer

        def compose(state, *args, **kw):
            text = (state / 'compose.yaml').read_text()
            image = text.split('    image: ')[1].split('\n')[0]
            self.events.append((args[0], image))
            if self.compose_fails.get((args[0], image)):
                self.compose_fails[(args[0], image)] -= 1
                return 1, '', 'Error response from daemon: the daemon is busy'
            if args[0] == 'up':
                self.running = image
            if args[0] == 'stop':
                self.running = None
            return 0, '', ''

        def exec_in(name, cmd, stdin=None, timeout=120):
            self.events.append(('exec', ' '.join(cmd)))
            if 'copy-databases' in cmd:
                return 0, json.dumps({'copied': ['users.sqlite3', 'subsync.sqlite3']}), ''
            if cmd[-2:] == ['subsync.hostcmd', 'status']:
                return (0, '{"people": 0}', '') if self.readable(self.running) else (1, '', 'file is not a database')
            return 0, '{}', ''

        def run(cmd, timeout=60, stdin=None):
            if cmd[:2] == ['docker', 'cp']:
                self.events.append(('cp', cmd[2]))
                dest = Path(cmd[3])
                dest.mkdir(parents=True)
                (dest / 'users.sqlite3').write_bytes(b'users before')
                (dest / 'subsync.sqlite3').write_bytes(b'store before')
            return 0, '', ''

        def check_image(image, project, install_id, seconds=90):
            self.events.append(('check', image))
            return (True, '') if self.check_answers else (False, 'no answer within 90 seconds')

        def restore(image, volume, files):
            self.events.append(('restore', image, volume, tuple(sorted(files))))
            self.restored = files
            return True, ''

        def state_of(name):
            if not self.running:
                return 'exited'
            return 'running healthy' if self.answers_now(self.running) else 'running unhealthy'

        def route_check(e, out=None):
            return self.public_ok.pop(0) if self.public_ok else True

        def wait_healthy(e, seconds=90):
            self.waited.append(self.running)
            return dockerenv.container_state(engine.CONTAINER).split()[-1:] == ['healthy']
        for p in (mock.patch.object(engine, 'STATE', self.state), mock.patch.object(engine, 'PLACE', place()),
                  mock.patch.object(dockerenv, 'compose', compose), mock.patch.object(dockerenv, 'run', run),
                  mock.patch.object(dockerenv, 'exec_in', exec_in),
                  mock.patch.object(dockerenv, 'container_state', state_of),
                  mock.patch.object(dockerenv, 'check_image', check_image),
                  mock.patch.object(dockerenv, 'restore_databases', restore),
                  mock.patch.object(dockerenv, 'foreign',
                                    lambda project, container, install_id, unlabelled=(): self.foreign),
                  mock.patch.object(engine.Engine, 'wait_healthy', wait_healthy),
                  mock.patch.object(engine.Engine, 'route_check', route_check),
                  mock.patch.object(engine, 'setup_version', lambda: '1.4.0')):
            p.start()
            self.addCleanup(p.stop)

    def install(self, route='yourself', image=OLD, version='1.3.0', previous=None, **more):
        self.state.mkdir()
        settings = {'route': route, 'version': version, 'image': image, 'install_id': 'id-1', 'mode': 'remote',
                    'networks': [], 'bind': '127.0.0.1', 'port': 8790, 'public_base': 'https://aio.example.com/subsync',
                    'installed': '2026-11-13', **more}
        if previous:
            settings['previous'] = previous
        engine.save_settings(settings)
        (self.state / 'compose.yaml').write_text(engine.compose_for(settings))
        self.running = image
        self.compose_before = (self.state / 'compose.yaml').read_text()

    def rows(self, e):
        return [tuple(r[:2]) for r in e.out.rows]

    def kinds(self):
        return [ev[0] for ev in self.events]

    def test_a_good_update_checks_the_new_version_before_switching(self):
        self.install()
        e = engine.Engine({}, page=True)
        self.assertTrue(e.update())
        kinds = self.kinds()
        self.assertLess(kinds.index('cp'), kinds.index('check'))               # the copy before anything else
        self.assertLess(kinds.index('check'), kinds.index('up'))               # the new one answers before the switch
        self.assertEqual([ev for ev in self.events if ev[0] == 'up'], [('up', NEW)])
        self.assertIn(f'    image: {NEW}\n', (self.state / 'compose.yaml').read_text())
        s = engine.load_settings()
        self.assertEqual((s['version'], s['image']), ('1.4.0', NEW))
        self.assertEqual(s['previous']['image'], OLD)
        self.assertEqual(s['previous']['version'], '1.3.0')
        self.assertTrue(s['previous']['databases'].startswith('databases-'))
        rows = self.rows(e)
        self.assertIn(('ok', update.CHECKED), rows)
        self.assertIn(('ok', update.SWITCHED.format(version='1.4.0')), rows)
        switched = next(r for r in e.out.rows if r[1] == update.SWITCHED.format(version='1.4.0'))
        self.assertEqual(switched[2], update.KEPT.format(version='1.3.0') + ' '
                         + update.OLD_IMAGES.format(image=OLD))                  # under the switch, not the check

    def test_the_words_are_the_approved_drafts(self):
        self.assertEqual(update.ABOUT, 'Updating saves a copy of the databases, checks the new version while the old '
                                       'one keeps running, then switches: AIOSubtitles is offline for a few seconds. If '
                                       "the new version doesn't answer, the old one is put back.")
        self.assertEqual(update.ROLLED_BACK.format(new='1.4.0', old='1.3.0'),
                         "Version 1.4.0 didn't answer: version 1.3.0 is running again")
        self.assertEqual(update.KEPT.format(version='1.3.0'), 'Version 1.3.0 is kept for a rollback: ./setup --rollback.')

    def test_a_new_version_that_doesnt_answer_beside_the_old_changes_nothing(self):
        self.install()
        self.check_answers = False
        with self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, update.FAILED.format(reason='no answer within 90 seconds',
                                                                        version='1.3.0'))
        self.assertNotIn('up', self.kinds())
        self.assertEqual((self.state / 'compose.yaml').read_text(), self.compose_before)
        self.assertEqual(engine.load_settings()['version'], '1.3.0')
        self.assertEqual(self.running, OLD)

    def test_a_switch_that_doesnt_answer_puts_the_old_version_back(self):
        self.install()
        self.answers[NEW] = False
        e = engine.Engine({}, page=True)
        with self.assertRaises(Stop) as caught:
            e.update()
        self.assertEqual(caught.exception.message, update.ROLLED_BACK.format(new='1.4.0', old='1.3.0'))
        self.assertEqual([ev for ev in self.events if ev[0] == 'up'], [('up', NEW), ('up', OLD)])
        self.assertNotIn('restore', self.kinds())                               # the old one ran on the data as it was
        self.assertEqual((self.state / 'compose.yaml').read_text(), self.compose_before)
        s = engine.load_settings()
        self.assertEqual((s['version'], s['image']), ('1.3.0', OLD))
        self.assertNotIn('previous', s)

    def test_an_old_version_that_cant_run_on_the_new_data_gets_the_copy_back(self):
        self.install()
        # the new version doesn't answer, and the old one answers only on the databases from before the update
        self.answers_now = lambda image: image == OLD and 'restore' in self.kinds()
        with self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, update.ROLLED_BACK.format(new='1.4.0', old='1.3.0')
                         + update.WITH_DATABASES)
        restore = next(ev for ev in self.events if ev[0] == 'restore')
        self.assertEqual(restore[1:3], (OLD, 'aiosubtitles_subsync-data'))     # the old image puts the copy back
        self.assertEqual(self.restored, {'users.sqlite3': b'users before', 'subsync.sqlite3': b'store before'})
        kinds = self.kinds()
        self.assertLess(kinds.index('stop'), kinds.index('restore'))           # never under a running service
        self.assertEqual(self.running, OLD)

    def test_a_new_version_that_cant_read_the_databases_is_not_kept(self):
        # /health answers without reading the databases: the switch also asks the service to read them
        self.install()
        self.readable = lambda image: image != NEW
        with self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, update.ROLLED_BACK.format(new='1.4.0', old='1.3.0'))
        self.assertEqual(self.running, OLD)

    def test_an_old_version_that_answers_but_cant_read_the_data_gets_the_copy_back(self):
        # as a test run found: the new version broke the people's database; the old one answered /health on
        # it anyway
        self.install()
        self.answers[NEW] = False
        self.readable = lambda image: image == OLD and 'restore' in self.kinds()
        with self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, update.ROLLED_BACK.format(new='1.4.0', old='1.3.0')
                         + update.WITH_DATABASES)
        self.assertEqual(self.running, OLD)

    def test_a_check_that_held_before_and_fails_after_puts_the_old_one_back(self):
        self.install(route='host')
        self.public_ok = [True, False, True]
        with self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, update.ROLLED_BACK.format(new='1.4.0', old='1.3.0'))
        self.assertEqual(self.running, OLD)

    def test_a_check_that_failed_before_too_doesnt_roll_back(self):
        # a router that never lets this machine reach its own public address: not the new version's doing
        self.install()
        self.public_ok = [False, False]
        self.assertFalse(engine.Engine({}, page=True).update())
        self.assertEqual(self.running, NEW)
        self.assertEqual(engine.load_settings()['version'], '1.4.0')

    def test_the_databases_are_copied_on_both_routes(self):
        for route in ('yourself', 'host'):
            with self.subTest(route=route):
                if self.state.exists():
                    import shutil
                    shutil.rmtree(self.state)
                self.events.clear()
                self.install(route=route)
                e = engine.Engine({}, page=True)
                e.update()
                self.assertIn('cp', self.kinds())
                self.assertTrue(any(r[1].startswith('Saved a copy of the databases: state/backups/databases-')
                                    for r in e.out.rows))
                newest = sorted((self.state / 'backups').glob('databases-*'))[-1]
                self.assertEqual(stat.S_IMODE(os.stat(newest).st_mode), 0o700)
                # Each copy private too (everyone's records on a public instance), whatever docker cp gave it
                self.assertEqual({name: stat.S_IMODE(os.stat(newest / name).st_mode) for name in dockerenv.DATABASES},
                                 dict.fromkeys(dockerenv.DATABASES, 0o600))

    def test_a_failed_copy_stops_before_anything_changes(self):
        self.install()

        def exec_in(name, cmd, stdin=None, timeout=120):
            return 1, '', 'database is locked'
        with mock.patch.object(dockerenv, 'exec_in', exec_in), self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, update.FAILED.format(reason='database is locked', version='1.3.0'))
        self.assertNotIn('check', self.kinds())
        self.assertNotIn('up', self.kinds())

    def test_the_guard_stops_it_before_anything(self):
        self.install()
        self.foreign = [('container', 'aiosubtitles-subsync')]
        with self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, engine.GUARD_STOP.format(kind='container',
                                                                           name='aiosubtitles-subsync'))
        self.assertEqual(self.events, [])

    def test_the_newest_or_an_older_setup_changes_nothing(self):
        self.install(image=NEW, version='1.4.0')
        with self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, update.NEWEST)
        with mock.patch.object(engine, 'PLACE', place(OLD)), mock.patch.object(engine, 'setup_version', lambda: '1.3.0'), \
                self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, update.NEWER_HERE.format(running='1.4.0', setup='1.3.0'))
        self.assertEqual(self.events, [])

    def test_a_rollback_on_request_goes_to_the_previous_version(self):
        self.install(image=NEW, version='1.4.0',
                     previous={'image': OLD, 'version': '1.3.0', 'local': False, 'databases': 'databases-20261113-000000'})
        before = self.state / 'backups' / 'databases-20261113-000000'
        before.mkdir(parents=True)
        (before / 'users.sqlite3').write_bytes(b'before 1.4.0')
        e = engine.Engine({}, page=True)
        self.assertTrue(e.rollback(restore=True))
        self.assertEqual(self.running, OLD)
        restore = next(ev for ev in self.events if ev[0] == 'restore')
        self.assertEqual((restore[1], restore[3]), (OLD, ('users.sqlite3',)))
        s = engine.load_settings()
        self.assertEqual((s['version'], s['image']), ('1.3.0', OLD))
        self.assertNotIn('previous', s)
        self.assertIn('check', self.kinds())                                     # checked before the switch too

    def test_the_copy_a_rollback_needs_is_never_the_one_dropped(self):
        # two failed updates since: four copies with the rollback's own, but the one from before 1.4.0 stays
        self.install(image=NEW, version='1.4.0',
                     previous={'image': OLD, 'version': '1.3.0', 'local': False, 'databases': 'databases-20261113-000000'})
        for stamp in ('20261113-000000', '20261113-010000', '20261113-020000'):
            copy = self.state / 'backups' / f'databases-{stamp}'
            copy.mkdir(parents=True)
            (copy / 'users.sqlite3').write_bytes(stamp.encode())
        self.assertTrue(engine.Engine({}, page=True).rollback(restore=True))
        self.assertEqual(self.restored, {'users.sqlite3': b'20261113-000000'})
        copies = sorted(p.name for p in (self.state / 'backups').iterdir())
        self.assertEqual(len(copies), 3)

    def test_an_update_keeps_the_copy_its_rollback_would_need(self):
        self.install(image=OLD, version='1.3.0',
                     previous={'image': OLD, 'version': '1.2.0', 'local': False, 'databases': 'databases-20261101-000000'})
        for stamp in ('20261101-000000', '20261112-000000', '20261113-000000'):
            (self.state / 'backups' / f'databases-{stamp}').mkdir(parents=True)
        with mock.patch.object(dockerenv, 'check_image', lambda *a, **k: (False, 'no answer')), \
                self.assertRaises(Stop):
            engine.Engine({}, page=True).update()
        self.assertTrue((self.state / 'backups' / 'databases-20261101-000000').is_dir())

    def test_a_rollback_without_the_copy_keeps_the_data(self):
        self.install(image=NEW, version='1.4.0',
                     previous={'image': OLD, 'version': '1.3.0', 'local': False, 'databases': 'databases-x'})
        self.assertTrue(engine.Engine({}, page=True).rollback())
        self.assertNotIn('restore', self.kinds())

    def test_an_install_taken_over_from_setup_1_3_0_keeps_its_volume_through_an_update(self):
        # its volume carries no label and is declared external, so Compose never makes, checks or makes it again
        volume = 'aiosubtitles_subsync-data'
        self.install(external_volume=volume)
        asked = []

        def foreign(project, container, install_id, unlabelled=()):
            asked.append((install_id, set(unlabelled)))
            return []
        with mock.patch.object(dockerenv, 'foreign', foreign):
            self.assertTrue(engine.Engine({}, page=True).update())
        self.assertEqual(asked, [('id-1', {('volume', volume)})])
        compose = (self.state / 'compose.yaml').read_text()
        self.assertIn(f'    image: {NEW}\n', compose)
        self.assertIn(f'\nvolumes:\n  subsync-data: {{external: true, name: "{volume}"}}\n', compose)
        s = engine.load_settings()
        self.assertEqual((s['version'], s['external_volume'], s['previous']['version']), ('1.4.0', volume, '1.3.0'))

    def test_and_through_a_rollback_whose_copy_goes_back_into_it(self):
        volume = 'aiosubtitles_subsync-data'
        self.rollback_with_its_copy(external_volume=volume)
        asked = []

        def foreign(project, container, install_id, unlabelled=()):
            asked.append((install_id, set(unlabelled)))
            return []
        with mock.patch.object(dockerenv, 'foreign', foreign):
            self.assertTrue(engine.Engine({}, page=True).rollback(restore=True))
        self.assertEqual(asked, [('id-1', {('volume', volume)})])
        restore = next(ev for ev in self.events if ev[0] == 'restore')
        self.assertEqual(restore[1:3], (OLD, volume))
        self.assertEqual(self.restored, {'users.sqlite3': b'copy A'})
        self.assertIn(f'\nvolumes:\n  subsync-data: {{external: true, name: "{volume}"}}\n',
                      (self.state / 'compose.yaml').read_text())
        self.assertEqual(engine.load_settings()['external_volume'], volume)

    def test_no_earlier_version_no_rollback(self):
        self.install()
        with self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).rollback()
        self.assertEqual(caught.exception.message, update.NO_ROLLBACK)

    # ------------------------------------------------------------------ what goes wrong, and the copies' files

    def rollback_with_its_copy(self, route='yourself', **more):
        """1.4.0 runs, after an update from 1.3.0 whose copy (copy A) is in state/backups."""
        self.install(route=route, image=NEW, version='1.4.0',
                     previous={'image': OLD, 'version': '1.3.0', 'local': False, 'databases': 'databases-20261113-000000'},
                     **more)
        before = self.state / 'backups' / 'databases-20261113-000000'
        before.mkdir(parents=True)
        (before / 'users.sqlite3').write_bytes(b'copy A')
        return before

    def test_an_old_version_that_reads_only_its_own_copy_gets_it_before_it_starts(self):
        # 1.3.0 can't read the databases as 1.4.0 left them: the reason to put its copy back
        self.rollback_with_its_copy()
        self.readable = lambda image: image == NEW or 'restore' in self.kinds()
        self.assertTrue(engine.Engine({}, page=True).rollback(restore=True))
        self.assertEqual(self.running, OLD)
        self.assertEqual(self.restored, {'users.sqlite3': b'copy A'})
        kinds = self.kinds()
        self.assertLess(kinds.index('restore'), self.events.index(('up', OLD)))     # never started on 1.4.0's data
        self.assertEqual(engine.load_settings()['version'], '1.3.0')

    def test_a_rollback_with_its_copy_that_fails_after_puts_the_data_back_too(self):
        # the copy went back, then the check that held before fails: 1.4.0 runs again on its own data, never on copy A
        for case in ('the check fails', 'the old version fails on its copy'):
            with self.subTest(case=case):
                if self.state.exists():
                    import shutil
                    shutil.rmtree(self.state)
                self.events.clear()
                self.rollback_with_its_copy(route='host')
                if case == 'the check fails':
                    self.public_ok = [True, False]
                else:
                    self.public_ok = [True, True]
                    self.answers[OLD] = False
                with self.assertRaises(Stop) as caught:
                    engine.Engine({}, page=True).rollback(restore=True)
                self.assertEqual(caught.exception.message, update.ROLLED_BACK.format(new='1.3.0', old='1.4.0'))
                restores = [ev for ev in self.events if ev[0] == 'restore']
                self.assertEqual([ev[1] for ev in restores], [OLD, NEW])      # copy A by 1.3.0, then copy B by 1.4.0
                self.assertEqual(self.restored, {'users.sqlite3': b'users before', 'subsync.sqlite3': b'store before'})
                self.assertEqual(self.running, NEW)
                s = engine.load_settings()
                self.assertEqual((s['version'], s['previous']['version']), ('1.4.0', '1.3.0'))

    def test_nothing_is_put_back_under_a_service_that_didnt_stop(self):
        # `compose stop` failing was not looked at, and the copy went into the volume of the running service
        self.rollback_with_its_copy()
        self.compose_fails = {('stop', OLD): 1, ('stop', NEW): 1}
        with self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).rollback(restore=True)
        self.assertEqual(caught.exception.message, update.STILL_DOWN.format(old='1.4.0', container=engine.CONTAINER))
        self.assertEqual([ev for ev in self.events if ev[0] in ('stop', 'restore', 'up')], [('stop', OLD), ('stop', NEW)])
        self.assertEqual(self.running, NEW)                                     # it ran on, as it was
        self.assertEqual(self.waited, [])

    def test_a_start_compose_refuses_is_not_waited_on(self):
        # A failed `compose up` was followed by the whole wait for an answer (90 seconds)
        for fails, message, waited in ((1, update.ROLLED_BACK.format(new='1.4.0', old='1.3.0') + update.WITH_DATABASES,
                                        [NEW, OLD]),
                                       (2, update.STILL_DOWN.format(old='1.3.0', container=engine.CONTAINER), [NEW])):
            with self.subTest(fails=fails):
                if self.state.exists():
                    import shutil
                    shutil.rmtree(self.state)
                self.events.clear()
                self.waited.clear()
                self.install()
                self.answers[NEW] = False
                self.compose_fails = {('up', OLD): fails}          # the old version's start, then its start on the copy
                with self.assertRaises(Stop) as caught:
                    engine.Engine({}, page=True).update()
                self.assertEqual(caught.exception.message, message)
                self.assertEqual(self.waited, waited)                       # never after a start Compose refused
                self.assertIn('restore', self.kinds())                          # the copy is tried when the start fails

    def test_neither_version_answering_says_so(self):
        self.install()
        self.answers = {OLD: False, NEW: False}
        with self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, update.STILL_DOWN.format(old='1.3.0', container=engine.CONTAINER))
        self.assertIn('restore', self.kinds())                                # the copy was tried too
        self.assertEqual(self.running, OLD)

    def test_a_rollback_whose_copy_is_gone_changes_nothing(self):
        self.install(image=NEW, version='1.4.0',
                     previous={'image': OLD, 'version': '1.3.0', 'local': False, 'databases': 'databases-x'})
        elsewhere = Path(self.tmp.name) / 'elsewhere'
        elsewhere.mkdir()
        for copy in (None, elsewhere):            # not there, or there only as a link
            with self.subTest(copy=copy):
                if copy:
                    (self.state / 'backups').mkdir(exist_ok=True)
                    (self.state / 'backups' / 'databases-x').symlink_to(copy)
                with self.assertRaises(Stop) as caught:
                    engine.Engine({}, page=True).rollback(restore=True)
                self.assertEqual(caught.exception.message,
                                 update.FAILED.format(reason='databases-x is not in state/backups', version='1.4.0'))
                self.assertEqual(self.events, [])
        self.assertTrue(engine.Engine({}, page=True).rollback())               # without the copy it goes ahead

    def copying(self, cp):
        """An update whose `docker cp` is `cp(dest)`: (code, out, err)."""
        def run(cmd, timeout=60, stdin=None):
            if cmd[:2] == ['docker', 'cp']:
                self.events.append(('cp', cmd[2]))
                return cp(Path(cmd[3]))
            return 0, '', ''
        return mock.patch.object(dockerenv, 'run', run)

    def test_a_copy_that_fails_on_its_way_out_stops_and_leaves_nothing(self):
        self.install()

        def cp(dest):
            dest.mkdir(parents=True)
            (dest / 'users.sqlite3').write_bytes(b'half')
            return 1, '', 'Error response from daemon: no space left on device\n'
        with self.copying(cp), self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message,
                         update.FAILED.format(reason='Error response from daemon: no space left on device',
                                              version='1.3.0'))
        self.assertNotIn('check', self.kinds())
        self.assertEqual(list((self.state / 'backups').glob('databases-*')), [])   # never one of the copies kept
        self.assertIn('rmtree', self.events[-1][1])                        # the copy inside the container is removed

    def test_links_in_a_copy_are_never_followed(self):
        # the copy is made in the service's container, and setup runs as root with Docker's socket
        self.install()
        outside = Path(self.tmp.name) / 'docker.sock'
        outside.write_text('not to be touched')
        outside.chmod(0o644)
        # The folder link pointed at the test's own folder, and that folder's mode was compared with itself;
        # this one's mode is one setup never gives
        elsewhere = Path(self.tmp.name) / 'elsewhere'
        elsewhere.mkdir()
        elsewhere.chmod(0o751)

        def cp(dest):
            dest.mkdir(parents=True)
            (dest / 'users.sqlite3').symlink_to(outside)
            (dest / 'subsync.sqlite3').write_bytes(b'store before')
            (dest / 'extra').symlink_to(elsewhere)
            return 0, '', ''
        owned = []
        with self.copying(cp), mock.patch.object(engine.os, 'geteuid', lambda: 0), \
                mock.patch.object(engine.os, 'chown', lambda path, uid, gid: owned.append((Path(path), os.path.islink(path)))):
            self.assertTrue(engine.Engine({}, page=True).update())
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)
        copy = next((self.state / 'backups').glob('databases-*'))
        self.assertEqual(sorted(p.name for p in copy.iterdir()), ['subsync.sqlite3'])   # the links are gone
        self.assertEqual([path for path, link in owned if link], [])
        # resolved on both sides: the temporary folder may itself be under a link (macOS's /var is /private/var)
        self.assertFalse({outside.resolve(), elsewhere.resolve()} & {path.resolve() for path, _ in owned})
        self.assertEqual(stat.S_IMODE(elsewhere.stat().st_mode), 0o751)

    def test_a_copy_that_is_a_link_is_refused(self):
        self.install()
        elsewhere = Path(self.tmp.name) / 'elsewhere'
        elsewhere.mkdir(mode=0o755)
        (elsewhere / 'users.sqlite3').write_text('not a copy')
        (elsewhere / 'users.sqlite3').chmod(0o644)

        def cp(dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(elsewhere)
            return 0, '', ''
        with self.copying(cp), self.assertRaises(Stop) as caught:
            engine.Engine({}, page=True).update()
        self.assertEqual(caught.exception.message, "The update didn't go through: the copy of the databases is not a "
                                                   'folder. Version 1.3.0 keeps running.')         # as approved
        self.assertNotIn('check', self.kinds())
        self.assertEqual(list((self.state / 'backups').iterdir()), [])          # the link is gone, its target stays
        self.assertEqual(stat.S_IMODE(elsewhere.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((elsewhere / 'users.sqlite3').stat().st_mode), 0o644)

    def test_a_restore_never_reads_through_a_link(self):
        copy = self.rollback_with_its_copy()
        secret = Path(self.tmp.name) / 'secrets.env'
        secret.write_bytes(b'KEY=topsecret')
        (copy / 'users.sqlite3').unlink()
        (copy / 'users.sqlite3').symlink_to(secret)
        (copy / 'subsync.sqlite3').write_bytes(b'copy A')
        self.assertTrue(engine.Engine({}, page=True).rollback(restore=True))
        self.assertEqual(self.restored, {'subsync.sqlite3': b'copy A'})

    def test_the_copies_belong_to_the_folders_owner(self):
        # a public instance writes no config backups: its first update makes state/backups
        self.install(route='host')
        owned = []
        with mock.patch.object(engine.os, 'geteuid', lambda: 0), \
                mock.patch.object(engine.os, 'chown', lambda path, uid, gid: owned.append((Path(path).name, uid, gid))):
            self.assertTrue(engine.Engine({}, page=True).update())
        owner = place().owner
        copy = next((self.state / 'backups').glob('databases-*')).name
        for name in ('backups', copy, 'users.sqlite3', 'subsync.sqlite3'):
            self.assertIn((name, *owner), owned)


class Backups(unittest.TestCase):
    def test_keep_the_newest_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            backups = Path(tmp)
            for stamp in ('20261101-000000', '20261102-000000', '20261103-000000', '20261104-000000'):
                (backups / f'databases-{stamp}').mkdir(parents=True)
            (backups / 'viewer-1a2b3c4d-20261101-000000.json').write_text('{}')
            update.keep_newest(backups, 'databases-', 3)
            self.assertEqual(sorted(p.name for p in backups.iterdir()),
                             ['databases-20261102-000000', 'databases-20261103-000000', 'databases-20261104-000000',
                              'viewer-1a2b3c4d-20261101-000000.json'])

    def test_only_the_copies_themselves_are_counted_and_removed(self):
        # A link named like a copy made shutil.rmtree raise after a good copy (the update stopped), and a file
        # named databases-… counted as the newest copy, so a real one was removed in its place
        with tempfile.TemporaryDirectory() as tmp:
            backups, elsewhere = Path(tmp) / 'backups', Path(tmp) / 'elsewhere'
            backups.mkdir()
            elsewhere.mkdir()
            (elsewhere / 'users.sqlite3').write_text('kept')
            (backups / 'databases-20200101-000000').symlink_to(elsewhere)
            for stamp in ('20261101-000000', '20261102-000000', '20261103-000000', '20261104-000000'):
                (backups / f'databases-{stamp}').mkdir()
            (backups / 'databases-notes.txt').write_text('mine')
            update.keep_newest(backups, 'databases-', 3, keep={'databases-20261104-000000'})
            self.assertEqual(sorted(p.name for p in backups.iterdir()),
                             ['databases-20200101-000000', 'databases-20261102-000000', 'databases-20261103-000000',
                              'databases-20261104-000000', 'databases-notes.txt'])
            self.assertEqual((elsewhere / 'users.sqlite3').read_text(), 'kept')     # nothing followed through the link


if __name__ == '__main__':
    unittest.main()
