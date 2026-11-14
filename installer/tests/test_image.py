"""The image setup runs from: built from the repository's root, Docker's own programs pinned in it, the tests
of both parts run in its build without leaving anything behind, and its entry script: setup as root, the service
never as root."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = REPO / 'subsync' / 'Dockerfile'
ENTRY = REPO / 'installer' / 'image-entry.sh'
SHA = r'sha256:[0-9a-f]{64}'


def instruction(text, start):
    """The whole instruction that starts with `start`, its continued lines included."""
    lines = text[text.index('\n' + start) + 1:].splitlines()
    out = []
    for line in lines:
        out.append(line)
        if not line.rstrip().endswith('\\'):
            break
    return '\n'.join(out)


class Dockerfile(unittest.TestCase):
    def setUp(self):
        self.text = DOCKERFILE.read_text()
        self.lines = [line for line in self.text.splitlines() if not line.lstrip().startswith('#')]
        self.final = self.text.split('\nFROM python@', 1)[1]          # the image's own stage

    def test_it_builds_from_the_repository_root(self):
        copies = [line for line in self.lines if line.startswith('COPY ') and '--from=' not in line]
        self.assertTrue(copies)
        for line in copies:
            sources = line.split()[1:-1]
            sources = [s for s in sources if not s.startswith('--')]
            for source in sources:
                self.assertTrue((REPO / source.rstrip('*').rstrip('/')).exists() or '*' in source, line)
        self.assertIn('COPY subsync/requirements.txt ', self.text)
        self.assertIn('COPY subsync/subsync ./subsync', self.text)
        self.assertIn('COPY installer/*.py /opt/aiosubtitles/installer/', self.text)
        self.assertIn('COPY VERSION /opt/aiosubtitles/VERSION', self.text)
        self.assertIn('WORKDIR /opt/aiosubtitles/subsync', self.text)

    def test_dockers_own_programs_are_pinned(self):
        self.assertRegex(self.text, r'\nFROM docker:29\.9\.3-cli@' + SHA + ' AS dockercli\n')
        for arch, name in (('amd64', 'x86_64'), ('arm64', 'aarch64')):
            self.assertRegex(self.text, rf'\nFROM scratch AS compose-{arch}\nADD --chmod=755 --checksum={SHA} '
                                        rf'https://github\.com/docker/compose/releases/download/v5\.7\.1/'
                                        rf'docker-compose-linux-{name} /docker-compose\n')
        # a build without --platform (./setup's own) leaves TARGETARCH empty: no FROM may depend on it, and the
        # image's own architecture picks its Compose inside a step that mounts both (only that one becomes a layer)
        for line in self.lines:
            if line.startswith('FROM '):
                self.assertNotIn('TARGETARCH', line)
        pick = instruction(self.final, 'RUN --mount=type=bind,from=compose-amd64')
        self.assertIn('--mount=type=bind,from=compose-arm64,target=/compose/arm64', pick)
        self.assertIn('install -D -m 755 "/compose/$(dpkg --print-architecture)/docker-compose" '
                      '/usr/local/lib/docker/cli-plugins/docker-compose', pick)
        self.assertIn('COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker\n', self.final)
        self.assertNotIn('COPY --from=compose', self.final)

    def test_both_test_suites_run_in_the_build_and_leave_nothing_behind(self):
        run = instruction(self.final, 'RUN --mount=type=bind,source=.,target=/src')
        self.assertIn('-s /src/subsync/tests -t /src/subsync/tests', run)
        self.assertIn('-s /src/installer/tests -t /src', run)
        self.assertIn('TMPDIR="$t"', run)
        self.assertIn('rm -rf "$t"', run)
        self.assertIn('test -z "$(ls -A /tmp)"', run)                 # nothing a test made stays in the image
        self.assertNotIn('COPY subsync/tests', self.text)              # the tests never become a layer

    def test_the_image_starts_as_root_only_for_setup(self):
        self.assertNotRegex(self.final, r'\nUSER ')                    # root by default: setup needs the socket
        self.assertIn('ENTRYPOINT ["/opt/aiosubtitles/entry"]', self.final)
        self.assertIn('CMD ["python", "-u", "-m", "subsync"]', self.final)
        self.assertIn('groupadd --system --gid 10001 subsync && useradd --system --uid 10001 --gid 10001', self.final)
        self.assertIn('COPY --chmod=755 installer/image-entry.sh /opt/aiosubtitles/entry', self.final)

    def test_the_build_context_leaves_out_what_isnt_needed(self):
        ignore = (REPO / '.dockerignore').read_text().split()
        for name in ('.git', 'state', '**/__pycache__', '**/*.pyc', '**/.DS_Store'):
            self.assertIn(name, ignore)


class Entry(unittest.TestCase):
    """The entry script with stand-ins for id, setpriv and python on its PATH."""

    def run_entry(self, *args, uid=0, setpriv_works=True):
        with tempfile.TemporaryDirectory() as tmp:
            bin_ = Path(tmp)
            log = bin_ / 'log'
            stubs = {'id': f'echo {uid}',
                     'setpriv': ('echo "setpriv $*" >> "$LOG"; while [ "$1" != -- ]; do shift; done; shift; '
                                 '[ "$1" = true ] && exit 0; echo "ran $* as 10001" >> "$LOG"') if setpriv_works
                     else 'echo "setpriv: setresuid failed: Operation not permitted" >&2; exit 1',
                     'python': 'echo "python $* state=$AIOSUBTITLES_STATE image=$AIOSUBTITLES_IN_IMAGE" >> "$LOG"'}
            for name, body in stubs.items():
                path = bin_ / name
                path.write_text(f'#!/bin/sh\n{body}\n')
                path.chmod(0o755)
            env = {'PATH': f'{bin_}:/usr/bin:/bin', 'LOG': str(log)}
            done = subprocess.run(['sh', str(ENTRY), *args], capture_output=True, text=True, timeout=30, env=env,
                                  stdin=subprocess.DEVNULL)
            return done.returncode, log.read_text() if log.exists() else '', done.stderr

    def test_setup_runs_setup_with_the_folders_state(self):
        code, log, _ = self.run_entry('setup', '--version')
        self.assertEqual(code, 0)
        # -u: without a terminal (docker run -d, a pipe) Python would hold the page's address back
        self.assertEqual(log, 'python -I -u /opt/aiosubtitles/installer/wizard.py --version state=/folder/state '
                              'image=1\n')

    def test_the_service_started_as_root_drops_to_its_own_user(self):
        code, log, _ = self.run_entry('python', '-u', '-m', 'subsync')
        self.assertEqual(code, 0)
        self.assertIn('ran python -u -m subsync as 10001', log)
        self.assertIn('setpriv --reuid=10001 --regid=10001 --clear-groups -- python -u -m subsync', log)
        self.assertNotIn('python -u -m subsync state=', log)           # never run directly as root

    def test_the_service_never_runs_as_root_when_it_cant_drop(self):
        code, log, err = self.run_entry('python', '-u', '-m', 'subsync', setpriv_works=False)
        self.assertNotEqual(code, 0)
        self.assertEqual(log, '')
        self.assertIn('user: "10001:10001"', err)

    def test_the_service_started_as_its_user_runs_as_it_is(self):
        code, log, _ = self.run_entry('python', '-u', '-m', 'subsync', uid=10001)
        self.assertEqual(code, 0)
        self.assertEqual(log, 'python -u -m subsync state= image=\n')

    def test_the_script_is_plain_sh(self):
        text = ENTRY.read_text()
        self.assertTrue(text.startswith('#!/bin/sh\n'))
        self.assertIsNone(re.search(r'\[\[|\bfunction\b|\$\{[^}]*//', text))   # no bash-only syntax


if __name__ == '__main__':
    unittest.main()
