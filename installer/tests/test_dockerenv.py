"""Setup's Docker work from inside its container: the helper on the machine's network, joining AIOStreams'
network, the ownership check, the new version's start check, and putting a database copy back."""
import ast
import base64
import contextlib
import errno
import io
import json
import os
import socket
import tarfile
import unittest
from pathlib import Path
from unittest import mock

from installer import dockerenv, plan

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


def publishes(arg):
    """Whether one `docker run` argument publishes a port, in any spelling: -p, -p8790:8790, -P, short flags joined
    (-dp, -dP), --publish, --publish=8790:8790, --publish-all."""
    if arg.startswith('--'):
        return arg.split('=', 1)[0] in ('--publish', '--publish-all')
    if arg.startswith('-'):
        for flag in arg[1:]:
            if flag in 'pP':
                return True
            if flag not in 'dit':          # a flag that takes a value: the rest of the argument is that value
                return False
    return False


def container(cid, name, image, env=(), host_network=False, networks=None, ports=None):
    """A container as `docker inspect` describes it (the fields setup reads)."""
    return {'Id': cid, 'Name': f'/{name}', 'Config': {'Image': image, 'Env': ['PATH=/usr/bin', *env]},
            'HostConfig': {'NetworkMode': 'host' if host_network else 'bridge'},
            'NetworkSettings': {'Networks': networks or {}, 'Ports': ports or {}}}


class Discovery(unittest.TestCase):
    """Which running containers are AIOStreams, and where this machine reaches each, from Docker's own answers."""

    PS = ('aaa111\tghcr.io/viren070/aiostreams:latest\taiostreams\n'
          'bbb222\tregistry.example/aio:2\tmy-aiostreams\n'
          'ccc333\taiosubtitles:local-1.4.0\taiostreams-subsync\n'
          'ddd444\tnginx:1\tweb\n')
    CONTAINERS = {
        'aaa111': container('aaa111', 'aiostreams', 'ghcr.io/viren070/aiostreams:latest', ['PORT=3000'],
                            host_network=True, networks={'host': {'IPAddress': ''}}),
        'bbb222': container('bbb222', 'my-aiostreams', 'registry.example/aio:2',
                            ['PORT=', 'BASE_URL=https://aio.example.com'],
                            networks={'bridge': {'IPAddress': '172.17.0.3'},
                                      'aio_default': {'IPAddress': '172.18.0.2'}},
                            ports={'3000/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '3001'},
                                                {'HostIp': '::', 'HostPort': '3001'},
                                                {'HostIp': '::1', 'HostPort': '3002'}],
                                   '8080/tcp': [{'HostIp': '0.0.0.0', 'HostPort': '8080'}], '9000/tcp': None}),
        'ccc333': container('ccc333', 'aiostreams-subsync', 'aiosubtitles:local-1.4.0',
                            networks={'aio_default': {'IPAddress': '172.18.0.3'}}),
    }

    def docker(self, gone=(), env=None):
        """`docker ps` and `docker inspect` as Docker answers them; `gone`: ids removed since `docker ps`."""
        def inspect(cmd):
            found = [self.CONTAINERS[i] for i in cmd[2:] if i not in gone]
            if env:
                found = [dict(c, Config=dict(c['Config'], Env=env)) for c in found]
            return (1 if gone else 0), json.dumps(found), ''.join(f'Error: No such object: {i}\n' for i in gone)
        return FakeDocker([(['docker', 'ps'], (0, self.PS, '')), (['docker', 'inspect'], inspect)])

    def test_aiostreams_containers_and_what_setup_needs_of_each(self):
        docker = self.docker()
        with mock.patch.object(dockerenv, 'run', docker):
            found = dockerenv.aios_containers()
        self.assertEqual(found, [
            {'name': 'aiostreams', 'image': 'ghcr.io/viren070/aiostreams:latest', 'port': 3000, 'base_url': '',
             'host_network': True, 'networks': [], 'ips': [], 'published': []},
            {'name': 'my-aiostreams', 'image': 'registry.example/aio:2', 'port': 3000,
             'base_url': 'https://aio.example.com', 'host_network': False, 'networks': ['aio_default'],
             'ips': ['172.17.0.3', '172.18.0.2'], 'published': [('0.0.0.0', 3001), ('::', 3001), ('::1', 3002)]},
        ])                                    # by image or name; never a subsync, nor a port other than AIOStreams'
        inspected = [c for c, _ in docker.calls if c[:2] == ['docker', 'inspect']]
        self.assertEqual(inspected, [['docker', 'inspect', 'aaa111', 'bbb222', 'ccc333']])     # never web

    def test_a_port_that_isnt_a_number_is_aiostreams_default(self):
        with mock.patch.object(dockerenv, 'run', self.docker(env=['PORT=${PORT}'])):
            self.assertEqual([c['port'] for c in dockerenv.aios_containers()], [3000, 3000])
        with mock.patch.object(dockerenv, 'run', self.docker(env=['PORT=3005'])):
            self.assertEqual([c['port'] for c in dockerenv.aios_containers()], [3005, 3005])

    def test_a_container_gone_since_docker_ps_leaves_the_others(self):
        with mock.patch.object(dockerenv, 'run', self.docker(gone=('aaa111',))):
            self.assertEqual([c['name'] for c in dockerenv.aios_containers()], ['my-aiostreams'])

    def test_docker_not_answering_finds_none(self):
        with mock.patch.object(dockerenv, 'run', FakeDocker([(['docker', 'ps'], (1, '', 'Cannot connect'))])):
            self.assertEqual(dockerenv.aios_containers(), [])
        with mock.patch.object(dockerenv, 'run', FakeDocker([(['docker', 'ps'], (0, self.PS, '')),
                                                             (['docker', 'inspect'], (1, '', 'Cannot connect'))])):
            self.assertEqual(dockerenv.aios_containers(), [])

    def test_the_addresses_this_machine_reaches_it_at_best_first(self):
        with mock.patch.object(dockerenv, 'run', self.docker()):
            host, bridged = dockerenv.aios_containers()
        self.assertEqual(dockerenv.host_urls(host), ['http://127.0.0.1:3000'])
        self.assertEqual(dockerenv.host_urls(bridged), ['http://127.0.0.1:3001', 'http://[::1]:3002',
                                                        'http://172.17.0.3:3000', 'http://172.18.0.2:3000',
                                                        'https://aio.example.com'])
        both = dict(bridged, host_network=True, port=3001, ips=[], base_url='http://127.0.0.1:3001')
        self.assertEqual(dockerenv.host_urls(both), ['http://127.0.0.1:3001', 'http://[::1]:3002'])   # once each


class DockerProblem(unittest.TestCase):
    def problem(self, which='/usr/bin/docker', info=(0, '29.0.0', ''), compose=(0, '5.7.1', '')):
        docker = FakeDocker([(['docker', 'info'], info), (['docker', 'compose'], compose)])
        with mock.patch.object(dockerenv.shutil, 'which', lambda name: which), \
                mock.patch.object(dockerenv, 'run', docker):
            return dockerenv.docker_problem()

    def test_docker_and_compose_working_is_no_problem(self):
        self.assertIsNone(self.problem())

    def test_each_problem_says_its_fix(self):
        self.assertEqual(self.problem(which=None), ('Docker is not installed.', 'Install Docker '
                                                   '(docs.docker.com/engine/install), then run ./setup again.'))
        denied = (1, '', 'permission denied while trying to connect to the Docker daemon socket')
        self.assertEqual(self.problem(info=denied)[0], 'Docker is installed, but this user may not use it.')
        self.assertIn('sudo usermod -aG docker $USER', self.problem(info=denied)[1])
        self.assertEqual(self.problem(info=(1, '', 'Cannot connect to the Docker daemon'))[0],
                         'Docker is installed but not running.')
        self.assertEqual(self.problem(compose=(1, '', "docker: 'compose' is not a docker command."))[0],
                         'Docker Compose v2 is missing.')


class PortInUse(unittest.TestCase):
    def test_a_port_something_listens_on_is_in_use(self):
        busy = socket.socket()
        busy.bind(('127.0.0.1', 0))
        busy.listen()
        port = busy.getsockname()[1]
        try:
            self.assertTrue(dockerenv.port_in_use(port, '127.0.0.1'))
        finally:
            busy.close()
        free = socket.socket()
        free.bind(('127.0.0.1', 0))
        port = free.getsockname()[1]
        free.close()
        self.assertFalse(dockerenv.port_in_use(port, '127.0.0.1'))


class OneDefinition(unittest.TestCase):
    """Each name setup's Docker work shares with the compose file it writes, and with subsync, is defined once."""

    def test_the_install_names_are_plans(self):
        for name in ('INSTALL_LABEL', 'SERVICE_USER', 'VOLUME', 'DATA'):
            with self.subTest(name=name):
                self.assertIs(getattr(dockerenv, name), getattr(plan, name))

    def test_the_databases_are_subsyncs_own_list(self):
        source = Path(__file__).resolve().parents[2] / 'subsync' / 'subsync' / 'hostcmd.py'
        tree = ast.parse(source.read_text())       # read, not imported: subsync's settings need numpy
        value = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                     and any(getattr(t, 'id', None) == 'DATABASES' for t in node.targets))
        self.assertEqual(ast.literal_eval(value), dockerenv.DATABASES)


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
        refused = []

        class Unprivileged(socket.socket):
            """A port below 1024 is refused (EACCES), as for the helper's user, whoever runs the tests: as root (the
            image build's test run) port 1 would bind, and the refusal would never be checked."""

            def bind(self, address):
                if address[1] < 1024:
                    refused.append(address)
                    raise OSError(errno.EACCES, os.strerror(errno.EACCES))
                super().bind(address)

        out = io.StringIO()
        busy = socket.socket()
        busy.bind(('127.0.0.1', 0))
        busy.listen()
        port = busy.getsockname()[1]
        try:
            with contextlib.redirect_stdout(out), mock.patch('sys.argv', ['-c', f'127.0.0.1:{port}', '127.0.0.1:1']), \
                    mock.patch.object(socket, 'socket', Unprivileged):
                exec(compile(dockerenv.PROBE, 'probe', 'exec'), {'__name__': '__main__'})
        finally:
            busy.close()
        found = json.loads(out.getvalue())
        self.assertEqual(found['taken'], [f'127.0.0.1:{port}'])        # port 1 is refused, but isn't taken
        self.assertEqual(refused, [('127.0.0.1', 1)])

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
    """What Docker has under setup's names that this install didn't make."""

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
        self.assertEqual([a for a in start if publishes(a)], [])
        self.assertNotIn('docker.sock', ' '.join(start))
        self.assertIn('SUBSYNC_PUBLIC=on', start)
        self.assertEqual(start[-1], IMAGE)
        self.assertIn(['docker', 'rm', '-f', name], cmds)
        self.assertIn(['docker', 'volume', 'rm', name], cmds)
        secret = next(a for a in start if a.startswith('SUBSYNC_SECRET_KEY='))
        self.assertGreaterEqual(len(secret.split('=', 1)[1]), 43)

    def test_any_spelling_of_a_published_port_is_seen(self):
        # the check above once looked for a "-p" argument only
        for args in (['-p', '8790:8790'], ['-p8790:8790'], ['-P'], ['-dp', '8790:8790'], ['-dP'],
                     ['--publish', '8790:8790'], ['--publish=8790:8790'], ['--publish-all']):
            with self.subTest(args=args):
                self.assertTrue(any(publishes(a) for a in args))
        for arg in ('-d', '-it', '--pull', '-v', '-eSUBSYNC_PUBLIC=on', 'SUBSYNC_PUBLIC=on', '--label', 'p'):
            with self.subTest(arg=arg):
                self.assertFalse(publishes(arg))

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

    def test_a_version_that_doesnt_start_fails_with_dockers_reason_and_leaves_nothing(self):
        docker = FakeDocker([(['docker', 'run', '-d'], (125, '', 'Unable to find image\ndocker: manifest unknown.\n'))])
        with mock.patch.object(dockerenv, 'run', docker), mock.patch.object(dockerenv.time, 'sleep', lambda s: None):
            good, reason = dockerenv.check_image(IMAGE, 'aiosubtitles', 'id-1')
        self.assertEqual((good, reason), (False, 'docker: manifest unknown.'))
        cmds = [c[:3] for c, _ in docker.calls]
        self.assertNotIn(['docker', 'exec'], [c[:2] for c in cmds])
        self.assertIn(['docker', 'rm', '-f'], cmds)
        self.assertIn(['docker', 'volume', 'rm'], cmds)

    def test_a_version_that_stops_fails_at_once(self):
        docker = FakeDocker([(['docker', 'exec'], (1, '', 'container is not running')),
                             (['docker', 'inspect'], (0, 'exited ', ''))])
        with mock.patch.object(dockerenv, 'run', docker), mock.patch.object(dockerenv.time, 'sleep', lambda s: None):
            good, reason = dockerenv.check_image(IMAGE, 'aiosubtitles', 'id-1')
        self.assertEqual((good, reason), (False, 'it stopped'))
        self.assertEqual(len([c for c, _ in docker.calls if c[:2] == ['docker', 'exec']]), 1)   # no wait for the rest
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
        self.assertEqual(cmd[-2:], ['/data', '["users.sqlite3", "subsync.sqlite3"]'])    # where, and which databases
        for name in dockerenv.DATABASES:
            self.assertNotIn(name, dockerenv.RESTORE)                       # given, never a copy of the list
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
            with mock.patch('sys.stdin', io.StringIO(payload)), mock.patch('sys.stdout', io.StringIO()), \
                    mock.patch('sys.argv', ['-c', tmp, json.dumps(dockerenv.DATABASES)]):
                exec(compile(dockerenv.RESTORE, 'restore', 'exec'), {'__name__': '__main__'})
            self.assertEqual(sorted(p.name for p in data.iterdir()), ['subsync.sqlite3'])   # not in the copy: gone
            self.assertEqual((data / 'subsync.sqlite3').read_text(), 'old')
            self.assertEqual(oct((data / 'subsync.sqlite3').stat().st_mode & 0o777), '0o600')


if __name__ == '__main__':
    unittest.main()
