"""Where setup runs: what setup reads about its own container, and the./setup it writes into the
image route's folder."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from installer import dockerenv, where

DIGEST = 'sha256:' + 'b' * 64


def inspect(image='ghcr.io/haslanli/aiosubtitles:latest', network='bridge', mounts=None, ports=None):
    return {'Id': 'c0ffee' * 10 + 'c0ff', 'Image': 'sha256:' + 'e' * 64,
            'Config': {'Image': image},
            'HostConfig': {'NetworkMode': network},
            'NetworkSettings': {
                'Ports': {'8789/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8899'}]} if ports is None else ports,
                'Networks': {} if network == 'host' else {'bridge': {'Gateway': '172.17.0.1', 'IPAddress': '172.17.0.2'}}},
            'Mounts': mounts if mounts is not None else [
                {'Type': 'bind', 'Source': '/var/run/docker.sock', 'Destination': '/var/run/docker.sock'},
                {'Type': 'bind', 'Source': '/home/me/aiosubtitles', 'Destination': '/folder'}]}


PULLED = {'Id': 'sha256:' + 'e' * 64, 'RepoDigests': ['ghcr.io/haslanli/aiosubtitles@' + DIGEST]}
BUILT = {'Id': 'sha256:' + 'f' * 64, 'RepoDigests': [], 'RepoTags': ['aiosubtitles:local-1.4.0']}


class Place(unittest.TestCase):
    def test_the_image_route_pins_the_image_it_runs_from(self):
        p = where.place_from(inspect(), PULLED, '1.4.0', 'image', (1000, 1000))
        self.assertEqual(p.route, 'image')
        self.assertEqual(p.image, 'ghcr.io/haslanli/aiosubtitles:1.4.0@' + DIGEST)
        self.assertFalse(p.local)
        self.assertEqual(p.run_as, 'ghcr.io/haslanli/aiosubtitles:latest')
        self.assertEqual(p.network, 'bridge')
        self.assertEqual(p.page_port, 8899)
        self.assertEqual(p.gateway, '172.17.0.1')
        self.assertEqual(p.host_folder, '/home/me/aiosubtitles')
        self.assertEqual(p.owner, (1000, 1000))

    def test_a_release_candidate_keeps_its_own_name_for_the_next_run(self):
        p = where.place_from(inspect(image='ghcr.io/haslanli/aiosubtitles:1.4.0-rc.1'), PULLED, '1.4.0', 'image',
                             (0, 0))
        self.assertEqual(p.run_as, 'ghcr.io/haslanli/aiosubtitles:1.4.0-rc.1')
        self.assertEqual(p.image, 'ghcr.io/haslanli/aiosubtitles:1.4.0@' + DIGEST)

    def test_the_source_route_runs_its_local_build(self):
        mounts = [{'Type': 'bind', 'Source': '/srv/AIOSubtitles/state', 'Destination': '/folder/state'}]
        p = where.place_from(inspect(image='aiosubtitles:local-1.4.0', network='host', mounts=mounts, ports={}),
                             BUILT, '1.4.0', 'source', (1000, 1000))
        self.assertEqual((p.route, p.image, p.local, p.network), ('source', 'aiosubtitles:local-1.4.0', True, 'host'))
        self.assertEqual(p.host_folder, '/srv/AIOSubtitles')
        self.assertIsNone(p.page_port)
        self.assertEqual(p.gateway, '')

    def test_a_local_build_with_a_digest_is_still_local(self):
        # Docker 29's image store gives a local build a digest too; it must never be looked for on a registry
        built = {'Id': 'sha256:' + 'f' * 64, 'RepoDigests': ['aiosubtitles@' + DIGEST]}
        p = where.place_from(inspect(image='aiosubtitles:local-1.4.0'), built, '1.4.0', 'image', (0, 0))
        self.assertTrue(p.local)
        self.assertEqual(p.image, 'aiosubtitles:local-1.4.0@' + DIGEST)      # its own tag, pinned to this build
        self.assertNotIn('--pull', where.setup_script(p))

    def test_no_port_for_the_page_is_none(self):
        p = where.place_from(inspect(ports={}), PULLED, '1.4.0', 'image', (0, 0))
        self.assertIsNone(p.page_port)

    def test_what_is_missing_from_the_docker_run_command(self):
        self.assertEqual(where.missing(inspect(), 'image'), [])
        self.assertEqual(where.missing(inspect(mounts=[]), 'image'), [where.NO_FOLDER])
        self.assertEqual(where.missing(inspect(mounts=[{'Type': 'bind', 'Source': '/x', 'Destination': '/folder/state'}]),
                                       'source'), [])


class Discover(unittest.TestCase):
    def test_setup_reads_its_own_container_through_the_socket(self):
        calls = []

        def run(cmd, timeout=60, stdin=None):
            calls.append(cmd)
            if cmd[:2] == ['docker', 'inspect']:
                return 0, json.dumps([inspect()]), ''
            if cmd[:3] == ['docker', 'image', 'inspect']:
                return 0, json.dumps([PULLED]), ''
            return 1, '', 'unexpected'
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(dockerenv, 'run', run), \
                mock.patch.object(where, 'FOLDER', Path(tmp)):
            p = where.discover('1.4.0', {'HOSTNAME': 'c0ffeec0ffee'})
            self.assertEqual(p.owner, (os.stat(tmp).st_uid, os.stat(tmp).st_gid))
        self.assertEqual(calls[0], ['docker', 'inspect', 'c0ffeec0ffee'])
        self.assertEqual(calls[1], ['docker', 'image', 'inspect', 'sha256:' + 'e' * 64])
        self.assertEqual(p.container, 'c0ffee' * 10 + 'c0ff')


class SetupScript(unittest.TestCase):
    def place(self, **kw):
        return where.place_from(inspect(**kw), PULLED, '1.4.0', 'image', (os.getuid(), os.getgid()))

    def test_the_folders_setup_runs_the_same_command(self):
        text = where.setup_script(self.place())
        self.assertTrue(text.startswith('#!/bin/sh\n' + where.SCRIPT_MARK))
        self.assertIn('exec docker run --rm -i $t --pull always -v /var/run/docker.sock:/var/run/docker.sock '
                      '-v "$PWD:/folder" -p 127.0.0.1:8899:8789 ghcr.io/haslanli/aiosubtitles:latest setup "$@"\n',
                      text)

    def test_it_runs_in_any_shell_and_needs_a_terminal_only_for_the_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, bin_ = Path(tmp) / 'aiosubtitles', Path(tmp) / 'bin'
            folder.mkdir()
            bin_.mkdir()
            (bin_ / 'docker').write_text('#!/bin/sh\necho "$PWD|$*"\n')
            (bin_ / 'docker').chmod(0o755)
            where.write_setup_script(self.place(), folder)
            self.assertEqual(oct((folder / 'setup').stat().st_mode & 0o777), '0o755')
            done = subprocess.run(['sh', str(folder / 'setup'), '--status'], capture_output=True, text=True,
                                  timeout=30, stdin=subprocess.DEVNULL, cwd=tmp,
                                  env={'PATH': f'{bin_}:/usr/bin:/bin'})
        line = done.stdout.strip()
        self.assertTrue(line.startswith(f'{folder}|run --rm -i --pull always'), line)   # no -t without a terminal
        self.assertTrue(line.endswith('setup --status'))

    def test_a_setup_file_it_didnt_write_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'setup').write_text('#!/usr/bin/env bash\n# AIOSubtitles setup: the source folder\n')
            self.assertFalse(where.write_setup_script(self.place(), folder))
            self.assertIn('the source folder', (folder / 'setup').read_text())
            (folder / 'setup').write_text('#!/bin/sh\n' + where.SCRIPT_MARK + '\nold\n')
            self.assertTrue(where.write_setup_script(self.place(), folder))
            self.assertIn('-p 127.0.0.1:8899:8789', (folder / 'setup').read_text())

    def test_a_local_image_is_never_pulled(self):
        mounts = [{'Type': 'bind', 'Source': '/x', 'Destination': '/folder'}]
        p = where.place_from(inspect(image='aiosubtitles:local-1.4.0', mounts=mounts), BUILT, '1.4.0', 'image', (0, 0))
        self.assertNotIn('--pull', where.setup_script(p))


if __name__ == '__main__':
    unittest.main()
