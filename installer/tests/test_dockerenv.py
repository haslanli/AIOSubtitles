"""Setup's Docker work from inside its container: the helper on the machine's network, joining AIOStreams'
network, the ownership check (OD18), the new version's start check, and putting a database copy back."""
import base64
import io
import json
import tarfile
import unittest
from unittest import mock

from installer import dockerenv

IMAGE = 'ghcr.io/haslanli/aiosubtitles:1.4.0@sha256:' + 'a' * 64


class FakeDocker:
    """Answers `docker ...` commands from a table of (prefix, (code, out, err)); records every command."""

    def __init__(self, answers=()):
        self.answers, self.calls = list(answers), []

    def __call__(self, cmd, timeout=60, stdin=None):
        self.calls.append((cmd, stdin))
        for prefix, answer in self.answers:
            if cmd[:len(prefix)] == prefix:
                return answer(cmd) if callable(answer) else answer
        return 0, '', ''


class Probe(unittest.TestCase):
    def test_the_helper_runs_on_the_machines_network_without_the_socket(self):
        docker = FakeDocker([(['docker', 'run'], (0, json.dumps({'lan': '192.168.1.5', 'taken': ['0.0.0.0:8790']}), ''))])
        with mock.patch.object(dockerenv, 'run', docker):
            found = dockerenv.probe(IMAGE, [('127.0.0.1', 8790), ('0.0.0.0', 8790)])
        self.assertEqual(found, {'lan': '192.168.1.5', 'taken': ['0.0.0.0:8790']})
        cmd = docker.calls[0][0]
        self.assertEqual(cmd[:9], ['docker', 'run', '--rm', '--network', 'host', '--user', '10001:10001',
                                   '--entrypoint', 'python'])
        self.assertNotIn('docker.sock', ' '.join(cmd))
        self.assertEqual(cmd[-2:], ['127.0.0.1:8790', '0.0.0.0:8790'])
        self.assertIn(IMAGE, cmd)

    def test_the_helpers_script_tells_a_taken_port_from_a_low_one(self):
        out = io.StringIO()
        import contextlib
        import socket
        busy = socket.socket()
        busy.bind(('127.0.0.1', 0))
        busy.listen()
        port = busy.getsockname()[1]
        try:
            with contextlib.redirect_stdout(out), mock.patch('sys.argv', ['-c', f'127.0.0.1:{port}', '127.0.0.1:1']):
                exec(compile(dockerenv.PROBE, 'probe', 'exec'), {'__name__': '__main__'})
        finally:
            busy.close()
        found = json.loads(out.getvalue())
        self.assertEqual(found['taken'], [f'127.0.0.1:{port}'])        # port 1 needs root here, but isn't taken

    def test_no_answer_is_none(self):
        with mock.patch.object(dockerenv, 'run', FakeDocker([(['docker', 'run'], (125, '', 'no such image'))])):
            self.assertIsNone(dockerenv.probe(IMAGE, []))


class Join(unittest.TestCase):
    def test_setup_joins_aiostreams_network(self):
        docker = FakeDocker()
        with mock.patch.object(dockerenv, 'run', docker):
            self.assertTrue(dockerenv.join('aio_default', 'c0ffee'))
        self.assertEqual(docker.calls[0][0], ['docker', 'network', 'connect', 'aio_default', 'c0ffee'])

    def test_already_on_it_is_fine(self):
        docker = FakeDocker([(['docker', 'network'], (1, '', 'Error response from daemon: endpoint with name x already '
                                                              'exists in network aio_default'))])
        with mock.patch.object(dockerenv, 'run', docker):
            self.assertTrue(dockerenv.join('aio_default', 'c0ffee'))
        docker = FakeDocker([(['docker', 'network'], (1, '', 'Error response from daemon: network aio_x not found'))])
        with mock.patch.object(dockerenv, 'run', docker):
            self.assertFalse(dockerenv.join('aio_x', 'c0ffee'))


class Guard(unittest.TestCase):
    """What Docker has under setup's names that this install didn't make (OD18)."""

    def docker(self, containers='', volumes='', networks=''):
        def listing(text):
            return lambda cmd: (0, text, '')
        return FakeDocker([(['docker', 'ps'], listing(containers)), (['docker', 'volume', 'ls'], listing(volumes)),
                           (['docker', 'network', 'ls'], listing(networks))])

    def test_nothing_there_is_nothing_foreign(self):
        with mock.patch.object(dockerenv, 'run', self.docker()):
            self.assertEqual(dockerenv.foreign('aiosubtitles', 'aiosubtitles-subsync', 'id-1'), [])

    def test_its_own_things_are_fine(self):
        docker = self.docker('aiosubtitles-subsync\tid-1\naiosubtitles-check-1a2b\tid-1\n',
                             'aiosubtitles_subsync-data\tid-1\n', 'aiosubtitles_default\tid-1\n')
        with mock.patch.object(dockerenv, 'run', docker):
            self.assertEqual(dockerenv.foreign('aiosubtitles', 'aiosubtitles-subsync', 'id-1'), [])

    def test_anything_without_its_id_is_foreign(self):
        # the public instance on the Oracle box: made by hand, under the same project name, no label at all
        docker = self.docker('aiosubtitles-subsync\t\naiosubtitles-caddy-1\t\n', 'aiosubtitles_subsync-data\t\n',
                             'aiosubtitles_default\tid-other\n')
        with mock.patch.object(dockerenv, 'run', docker):
            found = dockerenv.foreign('aiosubtitles', 'aiosubtitles-subsync', 'id-1')
        self.assertEqual(found, [('container', 'aiosubtitles-subsync'), ('container', 'aiosubtitles-caddy-1'),
                                 ('volume', 'aiosubtitles_subsync-data'), ('network', 'aiosubtitles_default')])

    def test_it_looks_by_project_and_by_name(self):
        docker = self.docker()
        with mock.patch.object(dockerenv, 'run', docker):
            dockerenv.foreign('aiosubtitles', 'aiosubtitles-subsync', 'id-1')
        cmds = [' '.join(c) for c, _ in docker.calls]
        self.assertIn('label=com.docker.compose.project=aiosubtitles', ' '.join(cmds))
        self.assertTrue(any('name=aiosubtitles-subsync' in c for c in cmds if c.startswith('docker ps')))
        self.assertTrue(any('name=aiosubtitles_subsync-data' in c for c in cmds if c.startswith('docker volume')))
        self.assertTrue(any('name=aiosubtitles_default' in c for c in cmds if c.startswith('docker network')))

    def test_a_similar_name_is_not_its_name(self):
        def by_name(text):        # Docker's name filter matches parts of names; the project label matches nothing
            return lambda cmd: (0, text if any(a.startswith('name=') for a in cmd) else '', '')
        docker = FakeDocker([(['docker', 'ps'], by_name('aiosubtitles-subsync-old\t\n')),
                             (['docker', 'volume', 'ls'], by_name('aiosubtitles_subsync-data-2\t\n')),
                             (['docker', 'network', 'ls'], by_name(''))])
        with mock.patch.object(dockerenv, 'run', docker):
            self.assertEqual(dockerenv.foreign('aiosubtitles', 'aiosubtitles-subsync', 'id-1'), [])

    def test_docker_not_answering_stops_it(self):
        docker = FakeDocker([(['docker', 'ps'], (1, '', 'Cannot connect to the Docker daemon'))])
        with mock.patch.object(dockerenv, 'run', docker), self.assertRaises(RuntimeError):
            dockerenv.foreign('aiosubtitles', 'aiosubtitles-subsync', 'id-1')


class CheckImage(unittest.TestCase):
    """The new version started once beside the old one: no port, an empty volume of its own, public mode."""

    def test_a_version_that_answers_passes_and_leaves_nothing(self):
        docker = FakeDocker([(['docker', 'exec'], (0, 'ok', ''))])
        with mock.patch.object(dockerenv, 'run', docker), mock.patch.object(dockerenv.time, 'sleep', lambda s: None):
            good, reason = dockerenv.check_image(IMAGE, 'aiosubtitles', 'id-1')
        self.assertTrue(good, reason)
        cmds = [c for c, _ in docker.calls]
        start = next(c for c in cmds if c[:3] == ['docker', 'run', '-d'])
        name = start[start.index('--name') + 1]
        self.assertTrue(name.startswith('aiosubtitles-check-'))
        self.assertIn('org.aiosubtitles.install=id-1', start)
        self.assertEqual(start[start.index('--user') + 1], '10001:10001')
        self.assertNotIn('-p', start)
        self.assertNotIn('docker.sock', ' '.join(start))
        self.assertIn('SUBSYNC_PUBLIC=on', start)
        self.assertEqual(start[-1], IMAGE)
        self.assertIn(['docker', 'rm', '-f', name], cmds)
        self.assertIn(['docker', 'volume', 'rm', name], cmds)
        secret = next(a for a in start if a.startswith('SUBSYNC_SECRET_KEY='))
        self.assertGreaterEqual(len(secret.split('=', 1)[1]), 43)

    def test_a_version_that_never_answers_fails_and_leaves_nothing(self):
        docker = FakeDocker([(['docker', 'exec'], (1, '', 'connection refused')),
                             (['docker', 'inspect'], (0, 'running', ''))])
        clock = iter(range(0, 1000, 5))
        with mock.patch.object(dockerenv, 'run', docker), mock.patch.object(dockerenv.time, 'sleep', lambda s: None), \
                mock.patch.object(dockerenv.time, 'monotonic', lambda: next(clock)):
            good, reason = dockerenv.check_image(IMAGE, 'aiosubtitles', 'id-1', seconds=60)
        self.assertFalse(good)
        cmds = [c[:3] for c, _ in docker.calls]
        self.assertIn(['docker', 'rm', '-f'], cmds)
        self.assertIn(['docker', 'volume', 'rm'], cmds)


class Restore(unittest.TestCase):
    def test_the_old_image_puts_the_copy_back_as_the_services_user(self):
        docker = FakeDocker([(['docker', 'run'], (0, 'restored', ''))])
        with mock.patch.object(dockerenv, 'run', docker):
            good, _ = dockerenv.restore_databases('aiosubtitles:local-1.3.0', 'aiosubtitles_subsync-data',
                                                  {'users.sqlite3': b'U', 'subsync.sqlite3': b'S'})
        self.assertTrue(good)
        cmd, stdin = docker.calls[0]
        self.assertEqual(cmd[:8], ['docker', 'run', '--rm', '-i', '--user', '10001:10001', '--entrypoint', 'python'])
        self.assertIn('aiosubtitles_subsync-data:/data', cmd)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(stdin))) as t:
            self.assertEqual(sorted(t.getnames()), ['subsync.sqlite3', 'users.sqlite3'])

    def test_the_restore_script_removes_leftovers_and_writes_the_copy(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            for name in ('users.sqlite3', 'users.sqlite3-wal', 'subsync.sqlite3', 'subsync.sqlite3-shm'):
                (data / name).write_text('new')
            payload = dockerenv.databases_tar({'subsync.sqlite3': b'old'})
            with mock.patch('sys.stdin', io.StringIO(payload)), mock.patch('sys.stdout', io.StringIO()):
                exec(compile(dockerenv.RESTORE.replace('"/data"', repr(tmp)), 'restore', 'exec'),
                     {'__name__': '__main__'})
            self.assertEqual(sorted(p.name for p in data.iterdir()), ['subsync.sqlite3'])   # not in the copy: gone
            self.assertEqual((data / 'subsync.sqlite3').read_text(), 'old')
            self.assertEqual(oct((data / 'subsync.sqlite3').stat().st_mode & 0o777), '0o600')


if __name__ == '__main__':
    unittest.main()
