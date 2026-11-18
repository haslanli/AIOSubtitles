"""The image workflow's rules (.github/workflows/), read as text: no YAML package is needed.

Every outside action is pinned to a full commit SHA with its version beside it; the token can write packages only in
the jobs that publish, and those run only for a push to main or a release tag, after every test has passed; nothing
but the run's own GITHUB_TOKEN is used; event values reach scripts only through env.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from subsync import ui

ROOT =Path(__file__).resolve().parents[2]
FLOWS = ROOT / '.github' / 'workflows'
PUBLISHERS = ('publish-amd64', 'publish-arm64', 'manifest')
ON_PUSH_ONLY = ("github.event_name == 'push' && (github.ref == 'refs/heads/main' || "
                "startsWith(github.ref, 'refs/tags/v'))")
PIN = re.compile(r'^\s*(?:-\s+)?uses:\s+([\w.-]+/[\w.-]+)@([0-9a-f]{40}) # v\d+\.\d+\.\d+$')
LOCAL = re.compile(r'^\s*(?:-\s+)?uses:\s+\./\.github/workflows/[\w-]+\.yml$')
# a YAML block scalar's header: | or >, its indentation and chomping indicators in either order, and a comment
BLOCK = re.compile(r'^[|>]([1-9]?[+-]?|[+-]?[1-9]?)\s*(#.*)?$')


def read(name):
    return (FLOWS / name).read_text()


def workflows(folder):
    """The files GitHub runs as workflows in `folder`: every .yml and .yaml file, dot-named ones too."""
    return sorted(p.name for p in folder.iterdir() if p.suffix in ('.yml', '.yaml'))


def blocks(text, parent):
    """The blocks under a top-level key (`jobs:` or `on:`), by their two-space-indented names."""
    out, current, inside = {}, None, False
    for line in text.splitlines():
        if not line.startswith(' ') and line.strip():
            inside = line.rstrip() == f'{parent}:'
            current = None
            continue
        m = re.match(r'^  ([\w-]+):\s*$', line)
        if inside and m:
            current = m.group(1)
            out[current] = []
        elif inside and current:
            out[current].append(line)
    return {k: '\n'.join(v) for k, v in out.items()}


def run_scripts(text):
    """The text of every `run:` step (one line or a block)."""
    scripts, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r'^(\s*)(?:-\s+)?run:\s*(.*)$', line)
        if not m:
            continue
        if not BLOCK.match(m.group(2)):
            scripts.append(m.group(2))
            continue
        indent, body = len(m.group(1)), []
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                break
            body.append(nxt)
        scripts.append('\n'.join(body))
    return scripts


class WorkflowRules(unittest.TestCase):
    def setUp(self):
        self.main, self.build = read('image.yml'), read('image-build.yml')
        self.files = {'image.yml': self.main, 'image-build.yml': self.build}

    def test_only_these_two_files(self):
        self.assertEqual(workflows(FLOWS), ['image-build.yml', 'image.yml'])

    def test_every_file_github_would_run_counts_and_nothing_else(self):
        # Finder's .DS_Store there once failed the test above; a dot-named workflow still counts
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('image.yml', 'other.yaml', '.hidden.yml', '.DS_Store', 'image.yml~', '.image.yml.swp'):
                (Path(tmp) / name).write_text('')
            self.assertEqual(workflows(Path(tmp)), ['.hidden.yml', 'image.yml', 'other.yaml'])

    def test_every_action_pinned_to_a_commit(self):
        for name, text in self.files.items():
            for line in text.splitlines():
                if 'uses:' in line:
                    self.assertTrue(PIN.match(line) or LOCAL.match(line), f'{name}: not pinned: {line.strip()}')

    def test_only_well_known_actions(self):
        allowed = {'actions/checkout', 'docker/setup-buildx-action', 'docker/login-action', 'docker/metadata-action',
                   'docker/build-push-action'}
        for text in self.files.values():
            for line in text.splitlines():
                m = PIN.match(line)
                if m:
                    self.assertIn(m.group(1), allowed)

    def test_same_action_same_pin(self):
        pins = {}
        for text in self.files.values():
            for line in text.splitlines():
                m = PIN.match(line)
                if m:
                    pins.setdefault(m.group(1), set()).add(m.group(2))
        self.assertTrue(all(len(v) == 1 for v in pins.values()), pins)

    def test_triggers(self):
        on = blocks(self.main, 'on')
        self.assertEqual(sorted(on), ['pull_request', 'push'])
        self.assertIn('branches: [main]', on['push'])
        self.assertIn("tags: ['v[0-9]+.[0-9]+.[0-9]+', 'v[0-9]+.[0-9]+.[0-9]+-rc.[0-9]+']", on['push'])
        self.assertIn('branches: [main]', on['pull_request'])
        self.assertIn('on:\n  workflow_call:', self.build)
        for text in self.files.values():
            for word in ('pull_request_target', 'workflow_run', 'workflow_dispatch', 'schedule:', 'repository_dispatch'):
                self.assertNotIn(word, text)

    def test_token_reads_only_except_where_it_publishes(self):
        self.assertIn('\npermissions:\n  contents: read\n\n', self.main)
        self.assertNotIn('permissions:', self.build)          # each call gets its caller's
        for job, body in blocks(self.main, 'jobs').items():
            self.assertIn('    permissions:\n      contents: read', body, job)
            self.assertEqual('packages: write' in body, job in PUBLISHERS, job)
            for scope in ('id-token', 'attestations', 'actions:', 'contents: write', 'write-all'):
                self.assertNotIn(scope, body, job)

    def test_publishing_waits_for_every_test(self):
        jobs = blocks(self.main, 'jobs')
        self.assertEqual(sorted(jobs), ['checks', 'manifest', 'publish-amd64', 'publish-arm64', 'test'])
        for job in ('publish-amd64', 'publish-arm64'):
            self.assertIn('    needs: [checks, test]', jobs[job])
            self.assertIn(f'    if: {ON_PUSH_ONLY}', jobs[job])
            self.assertIn('      publish: true', jobs[job])
        self.assertIn('    needs: [publish-amd64, publish-arm64]', jobs['manifest'])
        self.assertIn('      publish: false', jobs['test'])
        self.assertNotIn('needs:', jobs['test'])
        self.assertNotIn('needs:', jobs['checks'])
        self.assertIn('runner: ubuntu-24.04\n', jobs['test'])
        self.assertIn('runner: ubuntu-24.04-arm\n', jobs['test'])

    def test_pushes_only_in_the_publish_phase(self):
        steps = self.build.split('\n      - ')[1:]
        self.assertGreater(len(steps), 5)
        for step in steps:
            if 'push=true' in step or 'login-action' in step:
                self.assertIn('if: inputs.publish', step)
            if step.startswith(('name: Build and test', 'name: Start it once')):
                self.assertIn('!inputs.publish', step)
        self.assertIn('push-by-digest=true', self.build)
        self.assertIn('provenance: mode=max', self.build)
        self.assertIn('sbom: generator=docker/buildkit-syft-scanner:', self.build)

    def test_only_the_runs_own_token(self):
        for name, text in self.files.items():
            for expression in re.findall(r'\$\{\{(.*?)\}\}', text):        # GitHub's secrets, not Python's module
                for found in re.findall(r'\bsecrets\b\s*(?:\.\s*\w+|\[)?', expression):
                    self.assertEqual(found.replace(' ', ''), 'secrets.GITHUB_TOKEN', name)
            self.assertNotIn('secrets: inherit', text, name)
            self.assertEqual(text.count('secrets.GITHUB_TOKEN'), text.count('login-action@'), name)

    def test_checkouts_keep_no_credentials(self):
        for name, text in self.files.items():
            self.assertEqual(text.count('actions/checkout@'), text.count('persist-credentials: false'), name)

    def test_every_job_has_a_time_limit(self):
        for name, text in self.files.items():
            for job, body in blocks(text, 'jobs').items():
                if '    uses: ./' not in body:            # a called workflow's job sets its own
                    self.assertRegex(body, r'\n    timeout-minutes: \d+', f'{name}: {job}')

    def test_event_values_reach_scripts_through_env(self):
        for name, text in self.files.items():
            for script in run_scripts(text):
                self.assertNotIn('${{', script, name)

    def test_a_block_script_is_read_whatever_its_header(self):
        # a header with a chomping or indentation indicator, or a comment, once made only the header the script
        body = '          echo "${{ github.event.pull_request.title }}"'
        for header in ('|', '>', '|-', '>-', '|+', '>+', '|2', '|2-', '>-2', '| # a note', '>-  # a note'):
            with self.subTest(header=header):
                text = '    steps:\n      - run: ' + header + '\n' + body + '\n      - name: Next\n'
                self.assertEqual(run_scripts(text), [body])
        self.assertEqual(run_scripts('      - run: echo one # a note\n'), ['echo one # a note'])

    def test_no_workflow_artifacts(self):
        self.assertIn('\n      DOCKER_BUILD_RECORD_UPLOAD: false', self.build)    # build-push-action uploads one otherwise
        for text in self.files.values():
            self.assertNotIn('upload-artifact', text)

    def test_pinned_tools(self):
        self.assertIn('version: v0.39.0', self.build)
        self.assertRegex(self.build, r'image=moby/buildkit:v[\d.]+@sha256:[0-9a-f]{64}')
        self.assertRegex(self.build, r'buildkit-syft-scanner:[\d.]+@sha256:[0-9a-f]{64}')
        for text in self.files.values():
            self.assertNotIn('-latest', text)

    def test_no_register_notes(self):
        for name, text in self.files.items():
            self.assertNotRegex(text, r'\bAD\d+|\bS4-\d+', name)


    # -- the setup inside the image ---------------------------------------------------------------
    def test_the_image_builds_from_the_repository_root(self):
        self.assertNotIn('context: subsync', self.build)
        self.assertEqual(self.build.count('          context: .\n          file: subsync/Dockerfile\n'), 2)

    def test_release_candidates_publish_only_their_own_tag(self):
        rules = self.main.split('- name: Version rules', 1)[1].split('\n  test:', 1)[0]
        self.assertIn(r'if [ "$REF_NAME" != "v$version" ] && ! [[ "$REF_NAME" =~ ^v${version//./\\.}-rc\.[0-9]+$ ]]; then',
                      rules)
        # {{major}}.{{minor}} and latest never come from a pre-release (docker/metadata-action; the newest check
        # looks at plain vX.Y.Z tags only)
        self.assertIn("grep -E '^v[0-9]+\\.[0-9]+\\.[0-9]+$'", self.main)

    def test_the_version_rule_covers_every_input_of_the_image(self):
        self.assertIn('! git diff --quiet "v$version" HEAD -- subsync/ installer/ .dockerignore', self.main)

    def test_the_start_check_loads_the_typefaces_the_pages_use(self):
        # the check keeps its own list of the files, as a check independent of the image's code; it names the same ones
        check = self.build.split('- name: Start it once and check that it answers', 1)[1].split('\n      - name:', 1)[0]
        loops = re.findall(r'^\s*for font in (.+); do$', check, re.M)
        self.assertEqual([sorted(names.split()) for names in loops], [sorted(ui.FONT_FILES)])

    def test_the_start_check_runs_setups_own_checks(self):
        check = self.build.split('- name: Start it once and check that it answers', 1)[1].split('\n      - name:', 1)[0]
        self.assertIn('[ "$(docker run --rm "$IMAGE" setup --version)" = "$(tr -d \'[:space:]\' < VERSION)" ]', check)
        self.assertIn('out="$(docker run --rm "$IMAGE" setup --status 2>&1 || true)"', check)
        self.assertIn('grep -qF "Setup needs Docker\'s socket" <<< "$out"', check)
        self.assertIn("docker exec aiosubtitles-check grep -E '^Uid:\\s+10001\\s' /proc/1/status", check)


class StartCheckGreps(unittest.TestCase):
    """The start check's header and leak greps, run in bash with stand-ins for curl and docker that write more after
    the line grep looks for. Piped into `grep -q` under pipefail, a command still writing when grep stops at its match
    dies of SIGPIPE and fails the pipeline: the header check then fails and the leak check passes."""

    SECRET = '-Ab' + 'c' * 40           # token_urlsafe can start with '-': grep must not read it as an option
    MORE = "head -c 2000000 /dev/zero | tr '\\0' x; echo"       # far more than a pipe holds

    def setUp(self):
        if not shutil.which('bash'):
            self.skipTest('needs bash')
        check = read('image-build.yml').split('- name: Start it once and check that it answers', 1)[1]
        self.lines = [line.strip() for line in check.split('\n      - name:', 1)[0].splitlines()]

    def command(self, first, last):
        """The check's lines from the one that contains `first` (or, with none, the last one alone) to the one that
        contains `last`."""
        end = next(i for i, line in enumerate(self.lines) if last in line)
        start = next((i for i, line in enumerate(self.lines[:end + 1]) if first in line), end)
        return '\n'.join(self.lines[start:end + 1])

    def run_check(self, stand_in, command, **env):
        script = '\n'.join(['set -euo pipefail', 'fail() { echo "FAIL: $1"; exit 3; }', 'secret="$SECRET"', stand_in,
                            command, 'echo PASSED'])
        done = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, env=dict(PATH=os.environ.get('PATH', '/usr/bin:/bin'),
                                                                 SECRET=self.SECRET, **env))
        return done.stdout.strip().splitlines()[-1:]

    def test_a_secret_in_the_log_is_always_found(self):
        leak = self.command('logs="$(docker logs', 'The secret is in the log')
        docker = f'docker() {{ printf "%s\\n" "$FIRST"; {self.MORE}; }}'
        self.assertEqual(self.run_check(docker, leak, FIRST=f'started, key={self.SECRET}'),
                         ['FAIL: The secret is in the log'])
        self.assertEqual(self.run_check(docker, leak, FIRST='started'), ['PASSED'])
        self.assertEqual(self.run_check('docker() { return 1; }', leak), ['PASSED'])      # no log: nothing in it

    def test_the_security_header_is_found_however_much_follows_it(self):
        header = self.command('curl -fsS -D -', 'security header')
        curl = f'curl() {{ printf "%s\\r\\n" "HTTP/1.1 200 OK" "$CSP"; {self.MORE}; }}'
        self.assertEqual(self.run_check(curl, header, CSP="content-security-policy: default-src 'none'"), ['PASSED'])
        self.assertEqual(self.run_check(curl, header, CSP='x-other: 1'),
                         ['FAIL: The sign-up page has no security header'])
        self.assertEqual(self.run_check('curl() { return 22; }', header),
                         ['FAIL: The sign-up page has no security header'])

    def test_no_check_pipes_into_a_quiet_grep(self):
        for name in ('image.yml', 'image-build.yml'):
            for script in run_scripts(read(name)):
                for line in script.splitlines():
                    if not line.lstrip().startswith('#'):
                        self.assertNotRegex(line, r'\|\s*grep\s+(-\w+\s+)*-\w*q', name)


if __name__ == '__main__':
    unittest.main()
