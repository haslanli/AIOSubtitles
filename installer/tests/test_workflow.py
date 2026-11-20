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
from unittest import mock

from subsync import settings, ui

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
        sums, installs = set(), set()
        for name, text in self.files.items():
            steps = text.split('\n      - ')
            setups = [i for i, step in enumerate(steps) if step.startswith('uses: docker/setup-buildx-action@')]
            self.assertTrue(setups, name)
            for i in setups:
                # Each buildx set-up comes right after the step that downloads buildx v0.39.0 and checks it
                # (the check came after the set-up, which had run the binary by then), and fetches none of its own
                install = steps[i - 1]
                self.assertTrue(install.startswith('name: Install buildx, checked against its published sums\n'), name)
                self.assertIn('url="https://github.com/docker/buildx/releases/download/v0.39.0/buildx-v0.39.0.linux-'
                              '$arch"', install)
                self.assertNotRegex(steps[i], r'\n\s+version:', name)
                found = re.findall(r'^          BUILDX_(AMD64|ARM64): ([0-9a-f]{64})$', install, re.M)
                self.assertEqual([arch for arch, _ in found], ['AMD64', 'ARM64'], name)
                sums.add(tuple(found))
                installs.add(install)
        self.assertEqual(len(sums), 1, sums)                     # the same sums wherever buildx is set up
        self.assertEqual(len(installs), 1)                       # and the same step: one copy can't drift
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
        # NOTICE.md and LICENSE, which the image carries, were left out: every file or folder the Dockerfile
        # copies from the repository (VERSION is the version itself) is in the rule, and nothing else but .dockerignore
        rule = re.search(r'! git diff --quiet "v\$version" HEAD -- (.+); then', self.main).group(1).split()
        copied = set()
        for line in (ROOT / 'subsync' / 'Dockerfile').read_text(encoding='utf-8').splitlines():
            words = line.split()
            if words[:1] == ['COPY'] and not any(w.startswith('--from=') for w in words):
                copied |= {w.split('/')[0] + ('/' if '/' in w else '') for w in words[1:-1] if not w.startswith('--')}
        self.assertEqual(copied, {'subsync/', 'installer/', 'VERSION', 'NOTICE.md', 'LICENSE'})
        self.assertEqual(sorted(copied - {'VERSION'} | {'.dockerignore'}), sorted(rule))

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


class ChecksJobPython(unittest.TestCase):
    """The checks job runs this suite on the runner's own python3, Python 3.12 on ubuntu-24.04 (the oldest the README
    names for these tests): what setup builds must not need what Python 3.13 added."""

    def test_setups_settings_count_processors_without_process_cpu_count(self):
        # Settings (every Engine builds them) counted processors with os.process_cpu_count, new in 3.13, so on
        # the runner every test that built an Engine stopped with AttributeError, and nothing was published
        self.assertIn('    runs-on: ubuntu-24.04\n', blocks(read('image.yml'), 'jobs')['checks'])
        real = os.__dict__.pop('process_cpu_count', None)
        try:
            with mock.patch.object(os, 'cpu_count', lambda: 3), \
                    mock.patch.object(settings, 'CPU_MAX_FILE', '/nonexistent/cpu.max'), \
                    mock.patch.object(settings, 'CFS_DIR', '/nonexistent'):
                self.assertEqual(settings.available_cpus(), 3)
                self.assertEqual(settings.Settings({}).syncs_at_once, 3)            # SUBSYNC_SYNCS_AT_ONCE=auto
        finally:
            if real is not None:
                os.process_cpu_count = real


@unittest.skipUnless(shutil.which('bash') and shutil.which('git'), 'needs bash and git')
class VersionRules(unittest.TestCase):
    """The checks job's version rules, run in bash on a throwaway repository whose v1.2.0 tag is the last release (real
    git; nothing leaves this machine)."""

    def setUp(self):
        step = read('image.yml').split('- name: Version rules', 1)[1].split('\n  test:', 1)[0]
        self.script = run_scripts(step)[0]
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        self.git('init', '-q')
        for name in ('VERSION', 'NOTICE.md', 'LICENSE', 'README.md', '.dockerignore', 'subsync/run.py',
                     'installer/setup.py'):
            self.change(name, '1.2.0\n' if name == 'VERSION' else 'first\n')
        self.git('tag', 'v1.2.0')

    def git(self, *args):
        """git with none of the developer's own git settings (commit signing, hooks, ...)."""
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'init.defaultBranch=main',
                        *args], cwd=self.repo, check=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=60,
                       env=env)

    def change(self, name, text='changed\n'):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', name)

    def rules(self, ref, ref_name=''):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', REF=ref, REF_NAME=ref_name)
        done = subprocess.run(['bash', '-c', self.script], cwd=self.repo, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, env=env)
        return done.returncode, done.stdout.strip().splitlines()[-1:]

    RAISE = (1, ["::error::The image's files changed since v1.2.0: raise VERSION"])

    def test_a_change_to_the_image_needs_a_new_version_on_a_pull_request_too(self):
        # A pull request was never checked, so its merge then failed main's run and blocked every publish
        for name in ('subsync/run.py', 'installer/setup.py', '.dockerignore'):
            with self.subTest(name=name):
                self.change(name, f'{name} changed\n')
                self.assertEqual(self.rules('refs/heads/main'), self.RAISE)
                self.assertEqual(self.rules('refs/pull/7/merge'), self.RAISE)

    def test_the_notices_the_image_carries_count_as_its_files(self):
        # The image carries NOTICE.md and LICENSE (/opt/aiosubtitles/licences/), so a change there is a change
        # of the image
        for name in ('NOTICE.md', 'LICENSE'):
            with self.subTest(name=name):
                self.change(name, f'{name} changed\n')
                self.assertEqual(self.rules('refs/pull/7/merge'), self.RAISE)
                self.assertEqual(self.rules('refs/heads/main'), self.RAISE)

    def test_other_files_and_a_new_version_pass(self):
        self.change('README.md')
        self.assertEqual(self.rules('refs/pull/7/merge'), (0, ['VERSION 1.2.0']))
        self.change('subsync/run.py')
        self.change('VERSION', '1.3.0\n')                    # no release tag of it yet
        self.assertEqual(self.rules('refs/pull/7/merge'), (0, ['VERSION 1.3.0']))
        self.assertEqual(self.rules('refs/heads/main'), (0, ['VERSION 1.3.0']))

    def test_a_tag_is_v_and_the_version_or_its_release_candidate(self):
        self.change('subsync/run.py')                         # a tag is checked by its name only
        self.assertEqual(self.rules('refs/tags/v1.2.0', 'v1.2.0'), (0, ['VERSION 1.2.0']))
        self.assertEqual(self.rules('refs/tags/v1.2.0-rc.1', 'v1.2.0-rc.1'), (0, ['VERSION 1.2.0']))
        self.assertEqual(self.rules('refs/tags/v1.2.1', 'v1.2.1'),
                         (1, ["::error::The tag doesn't match VERSION (1.2.0)"]))
        self.change('VERSION', '1.2\n')
        self.assertEqual(self.rules('refs/heads/main'), (1, ['::error::VERSION must be three numbers, like 1.3.0']))


class BuildxSumCheck(unittest.TestCase):
    """The step that downloads buildx v0.39.0 and installs it as Docker's plugin only when it has the published sums,
    run in bash with a stand-in for curl that writes a file of ours."""

    def setUp(self):
        if not (shutil.which('bash') and shutil.which('sha256sum')):
            self.skipTest('needs bash and sha256sum')
        step = read('image-build.yml').split('- name: Install buildx, checked against its published sums', 1)[1]
        self.script = run_scripts(step.split('\n      - ', 1)[0])[0]
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        (self.folder / 'temp').mkdir()
        self.release = self.folder / 'release'
        self.release.write_bytes(b'buildx as published')
        self.sum = subprocess.run(['sha256sum', str(self.release)], capture_output=True, text=True, check=True,
                                  stdin=subprocess.DEVNULL).stdout.split()[0]
        self.plugin = self.folder / 'config' / 'cli-plugins' / 'docker-buildx'

    def install(self, arch, amd64, arm64, download=True, config=True):
        """(exit code, last line); curl's stand-in notes the address asked for, then writes the release where -o says
        (or fails as curl does)."""
        curl = ('curl() { local out=; while [ $# -gt 1 ]; do [ "$1" = -o ] && out="$2"; shift; done; '
                'printf "%s" "$1" > "$RUNNER_TEMP/asked"; [ "$DOWNLOAD" = yes ] || return 22; cp "$RELEASE" "$out"; }')
        env = dict(PATH=os.environ.get('PATH', '/usr/bin:/bin'), RUNNER_ARCH=arch, RUNNER_TEMP=str(self.folder / 'temp'),
                   HOME=str(self.folder), BUILDX_AMD64=amd64, BUILDX_ARM64=arm64, RELEASE=str(self.release),
                   DOWNLOAD='yes' if download else 'no')
        if config:
            env['DOCKER_CONFIG'] = str(self.folder / 'config')
        done = subprocess.run(['bash', '-c', curl + '\n' + self.script], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, env=env)
        return done.returncode, done.stdout.strip().splitlines()[-1:]

    def asked(self):
        return (self.folder / 'temp' / 'asked').read_text()

    def test_the_published_binary_is_installed_as_dockers_plugin(self):
        release = 'https://github.com/docker/buildx/releases/download/v0.39.0/buildx-v0.39.0.linux-'
        self.assertEqual(self.install('X64', self.sum, '0' * 64), (0, [f'{self.plugin}: buildx v0.39.0 as published']))
        self.assertEqual(self.asked(), release + 'amd64')
        self.assertEqual(self.plugin.read_bytes(), b'buildx as published')
        self.assertEqual(self.plugin.stat().st_mode & 0o777, 0o755)
        self.plugin.unlink()
        self.assertEqual(self.install('ARM64', '0' * 64, self.sum), (0, [f'{self.plugin}: buildx v0.39.0 as published']))
        self.assertEqual(self.asked(), release + 'arm64')
        self.assertEqual(self.plugin.read_bytes(), b'buildx as published')
        default = self.folder / '.docker' / 'cli-plugins' / 'docker-buildx'      # Docker's own folder without DOCKER_CONFIG
        self.assertEqual(self.install('X64', self.sum, '0' * 64, config=False),
                         (0, [f'{default}: buildx v0.39.0 as published']))
        self.assertEqual(default.read_bytes(), b'buildx as published')

    def test_any_other_binary_stops_the_job_and_is_never_installed(self):
        code, out = self.install('X64', '0' * 64, self.sum)       # the arm64 sum on an amd64 runner
        self.assertEqual(code, 1)
        file = self.folder / 'temp' / 'buildx-v0.39.0.linux-amd64'
        self.assertEqual(out, [f'::error::{file} is not buildx v0.39.0 as published (sha256 {self.sum})'])
        self.assertFalse(self.plugin.exists())

    def test_an_unknown_runner_or_no_download_stops_the_job(self):
        self.assertEqual(self.install('X86', self.sum, self.sum), (1, ['::error::No published buildx sum for X86']))
        self.assertEqual(self.install('X64', self.sum, self.sum, download=False),
                         (1, ["::error::Couldn't download buildx v0.39.0"]))
        self.assertFalse(self.plugin.exists())


if __name__ == '__main__':
    unittest.main()
