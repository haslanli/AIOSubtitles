"""The image setup runs from: built from the repository's root, Docker's own programs pinned in it, the tests
of both parts run in its build without leaving anything behind, and its entry script: setup as root, the service
never as root."""
import fnmatch
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import installer  # noqa: F401  (its __init__ puts subsync's folder on the import path)
import licences   # subsync/licences.py, a step of the image's build
from subsync import explain, ui

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


def copied(path, source):
    """Whether a COPY `source` (a file, a folder, or a pattern whose * stays within one folder, as Docker's do) takes
    the file at `path`, both relative to the repository."""
    source = source.rstrip('/')
    if (REPO / source).is_dir():
        return path.startswith(source + '/')
    parts, pattern = path.split('/'), source.split('/')
    return len(parts) == len(pattern) and all(fnmatch.fnmatchcase(p, q) for p, q in zip(parts, pattern))


def matches(path, rule):
    """Whether a .dockerignore `rule` leaves out the file at `path`: "**/" then a name for any folder or file of that
    name, else the folders or files the rule names from the root."""
    parts = path.split('/')
    if rule.startswith('**/'):
        return any(fnmatch.fnmatchcase(part, rule[3:]) for part in parts)
    pattern = rule.split('/')
    return len(parts) >= len(pattern) and all(fnmatch.fnmatchcase(p, q) for p, q in zip(parts, pattern))


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

    def test_every_font_file_the_pages_serve_is_fetched_with_a_pinned_sum(self):
        # the typefaces and their licence the pages serve from static/fonts (setup's page too: installer/web.py), each
        # from github.com/google/fonts at one pinned revision, the build stopping unless it has its pinned SHA-256
        added = re.findall(r'^ADD --chmod=644 --checksum=sha256:[0-9a-f]{64} \\\n\s+(\S+) \\\n\s+'
                           r'\./subsync/static/fonts/(\S+)$', self.final, re.M)
        self.assertEqual(sorted(name for _, name in added), sorted(ui.FONT_FILES + ('OFL.txt',)))
        for url, _ in added:
            self.assertRegex(url, r'^https://raw\.githubusercontent\.com/google/fonts/[0-9a-f]{40}/ofl/')
        self.assertEqual(len({url.split('/')[5] for url, _ in added}), 1)          # one revision for all three

    def test_the_build_context_leaves_out_what_isnt_needed(self):
        ignore = (REPO / '.dockerignore').read_text().split()
        for name in ('.git', 'state', '**/__pycache__', '**/*.pyc', '**/.DS_Store'):
            self.assertIn(name, ignore)

    def test_the_python_base_is_said_to_be_the_one_the_wheels_are_for(self):
        # The base image is pinned by digest alone, and requirements.txt's wheels are for one CPython (cp313):
        # the line above its FROM says which, and the two agree
        said = re.search(r'^# CPython (\d+)\.(\d+)\b.*\nFROM python@' + SHA + '$', self.text, re.M)
        self.assertIsNotNone(said)
        wheels = set(re.findall(r'\bcp(\d)(\d+)\b', (REPO / 'subsync' / 'requirements.txt').read_text()))
        self.assertEqual(wheels, {said.groups()})

    def test_every_file_setup_needs_is_copied_into_the_image(self):
        # The build's installer suite imports the build context's installer (-t /src), never the image's own
        # copy, which holds only what the COPY lines name (installer/*.py: its top-level modules): a subfolder or a data
        # file setup reads would pass the build's tests and be missing from the image
        sources = [source for line in self.lines if line.startswith('COPY ') and '--from=' not in line
                   for source in line.split()[1:-1] if not source.startswith('--')]
        ignore = (REPO / '.dockerignore').read_text().split()
        for path in sorted(p.relative_to(REPO).as_posix() for p in (REPO / 'installer').rglob('*') if p.is_file()):
            if path.startswith('installer/tests/') or any(matches(path, rule) for rule in ignore):
                continue
            with self.subTest(path=path):
                self.assertTrue(any(copied(path, source) for source in sources), sources)


class Notice(unittest.TestCase):
    """NOTICE.md names every third-party part the image ships, and the image carries it with the licences' texts.
    Docker's two programs and public mode's three packages had no row, and the image carried neither NOTICE.md nor
    LICENSE."""

    def setUp(self):
        self.notice = (REPO / 'NOTICE.md').read_text()
        self.parts = [line.split(' | ', 1)[0].removeprefix('| ') for line in self.notice.splitlines()
                      if line.startswith('| ') and not line.startswith('| Part ')]
        self.text = DOCKERFILE.read_text()

    def has_row(self, part):
        """Whether a row's Part cell is `part`, in any case: the whole cell, so a pin of pycparser 3.1 never takes the
        row of pycparser 3.12 for its own."""
        return part.lower() in {row.lower() for row in self.parts}

    def test_every_pinned_package_has_its_row(self):
        pins = re.findall(r'^([A-Za-z0-9_.-]+)==(\S+)', (REPO / 'subsync' / 'requirements.txt').read_text(), re.M)
        self.assertTrue(pins)
        for name, version in pins:
            with self.subTest(package=name):
                self.assertTrue(self.has_row(f"{name.replace('-', ' ')} {version}"), self.parts)

    def test_every_program_copied_into_the_image_has_its_row(self):
        # the stages a program is copied from: a new one needs its row (and its line here)
        self.assertEqual(set(re.findall(r'^COPY --from=(\S+) ', self.text, re.M)), {'alass', 'dockercli'})
        alass = re.search(r'cargo install alass-cli --version (\S+) ', self.text).group(1)
        docker = re.search(r'^FROM docker:(\S+)-cli@', self.text, re.M).group(1)
        compose = set(re.findall(r'/docker/compose/releases/download/v(\S+)/docker-compose-', self.text))
        self.assertEqual(len(compose), 1)
        for part in (f'alass-cli {alass}', f'Docker CLI {docker} (Docker, Inc.)', f'Docker Compose {compose.pop()} '
                                                                                 '(Docker, Inc.)'):
            with self.subTest(part=part):
                self.assertTrue(self.has_row(part), self.parts)

    def test_the_new_rows_are_the_approved_ones(self):
        for row in ("| cryptography 50.0.2 | Apache-2.0 or BSD-3-Clause | public mode only: encrypts each person's "
                    'record |',
                    '| cffi 2.1.1 | MIT | needed by cryptography |',
                    '| pycparser 3.12 | BSD-3-Clause | needed by cffi |',
                    "| Docker CLI 29.9.3 (Docker, Inc.) | Apache-2.0 | from Docker's official image docker:29.9.3-cli, "
                    'pinned by digest; used by setup only, never by the service |',
                    "| Docker Compose 5.7.1 (Docker, Inc.) | Apache-2.0 | Docker's release, checked by SHA-256; used "
                    'by setup only |',
                    '| Python 3.13 base image, with its Debian packages | PSF; the Debian packages under their own '
                    'licences | |'):
            self.assertIn('\n' + row + '\n', self.notice)
        self.assertNotIn('| Python 3.13 base image | PSF | |', self.notice)

    def test_the_sites_it_searches_itself_are_named(self):
        # It named only the sources in your AIOStreams config; by default AIOSubtitles also searches
        # OpenSubtitles (twice), TheSubtitleDB and Gestdown itself, and with their keys SubDL, SubSource and Wyzie Subs
        self.assertEqual(explain.SETTINGS['SUBSYNC_EXTRA_SOURCES'][0], 'osv3,osv3plus,tsdb,gestdown')
        self.assertIn('Subtitles come from the sources in your own AIOStreams config (for example OpenSubtitles) and '
                      'from the sites AIOSubtitles searches itself (OpenSubtitles, TheSubtitleDB and Gestdown; with '
                      'their keys, SubDL, SubSource and Wyzie Subs), under their own terms.',
                      ' '.join(self.notice.split()))

    def test_the_image_carries_the_notices_and_the_licences(self):
        self.assertIn("|\n\nThe image carries this file and these licences' texts in /opt/aiosubtitles/licences/.\n\n",
                      self.notice)                                          # under the table
        final = self.text.split('\nFROM python@', 1)[1]
        # The folder held NOTICE.md, LICENSE and the Apache-2.0 text only; licences.py puts the text of
        # every licence the table names there, Apache-2.0's too, from the files its step finds in the image
        self.assertIn('\nCOPY NOTICE.md LICENSE /opt/aiosubtitles/licences/\n'
                      'RUN --mount=type=bind,source=subsync,target=/src python -I -B /src/licences.py\n', final)
        self.assertNotIn('cp /usr/share/common-licenses/', final)
        self.assertGreater(final.index('/src/licences.py'), final.index('./subsync/static/fonts/OFL.txt'))
        self.assertLess(final.index('/src/licences.py'), final.index('--mount=type=bind,source=.,target=/src'))
        self.assertEqual(licences.FOLDER, Path('/opt/aiosubtitles/licences'))
        self.assertIn('\nWORKDIR /opt/aiosubtitles/subsync\n', final)
        self.assertEqual(licences.FONTS, Path('/opt/aiosubtitles/subsync/subsync/static/fonts'))
        self.assertEqual(licences.REQUIREMENTS, REPO / 'subsync' / 'requirements.txt')


# Stand-ins for licence texts, each with its real heading or clause (the words licences.WORDS looks for)
MIT = 'MIT License\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\n'
BSD = 'Redistribution and use in source and binary forms\n    * Neither the name of the copyright holder nor\n'
APACHE = '\n                                 Apache License\n                           Version 2.0, January 2004\n'
DEBIAN = {'Apache-2.0': APACHE,
          'GPL-3': '                    GNU GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007\n',
          'GPL-2': '                    GNU GENERAL PUBLIC LICENSE\n                       Version 2, June 1991\n',
          'LGPL-2.1': '                  GNU LESSER GENERAL PUBLIC LICENSE\n                       Version 2.1, '
                      'February 1999\n'}
PYTHON = 'TERMS AND CONDITIONS FOR USING PYTHON\n\nPYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n'
OFL = ('This Font Software is licensed under the SIL Open Font License, Version 1.1.\n\n'
       'SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007\n')
# Each pinned package's licence files ({info}: its .dist-info folder) where its wheel puts them: numpy's beside its code
# too, onnxruntime's only there (its .dist-info has none)
WHEELS = {'numpy': {'{info}/licenses/LICENSE.txt': BSD, '{info}/licenses/numpy/ma/LICENSE': BSD,
                    'numpy/ma/LICENSE': BSD},
          'onnxruntime': {'onnxruntime/LICENSE': MIT},
          'silero-vad': {'{info}/licenses/LICENSE': MIT},
          'cryptography': {'{info}/licenses/LICENSE': 'Apache-2.0 or BSD-3-Clause\n',
                           '{info}/licenses/LICENSE.APACHE': APACHE, '{info}/licenses/LICENSE.BSD': BSD},
          'cffi': {'{info}/licenses/LICENSE': MIT},
          'pycparser': {'{info}/licenses/LICENSE': BSD}}
# And files of theirs that are no licence file, which the folder doesn't get
OTHERS = {'onnxruntime': {'onnxruntime/ThirdPartyNotices.txt': MIT, 'onnxruntime/__init__.py': ''},
          'cryptography': {'{info}/sboms/sbom.json': '{}\n', '{info}/WHEEL': 'Wheel-Version: 1.0\n'}}


class Licences(unittest.TestCase):
    """subsync/licences.py, the build's step that puts the text of every licence NOTICE.md names in the image's licence
    folder, run on stand-ins of what it reads in the image: Debian's texts, Python's, the typefaces'
    and the licence files of requirements.txt's packages where their wheels put them."""

    def setUp(self):
        self.stand_in()

    def stand_in(self):
        """A new stand-in of what the step reads, and of the folder as the COPY before it leaves it."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        self.folder, self.debian, self.stdlib, self.fonts, self.site = (
            root / name for name in ('licences', 'common-licenses', 'python3.13', 'fonts', 'site-packages'))
        self.write(self.folder / 'NOTICE.md', (REPO / 'NOTICE.md').read_text())
        self.write(self.folder / 'LICENSE', (REPO / 'LICENSE').read_text())
        for name, text in DEBIAN.items():
            self.write(self.debian / name, text)
        self.write(self.stdlib / 'LICENSE.txt', PYTHON)
        self.write(self.fonts / 'OFL.txt', OFL)
        pins = dict(re.findall(r'^([A-Za-z0-9_.-]+)==(\S+)', licences.REQUIREMENTS.read_text(), re.M))
        self.assertEqual(set(pins), set(WHEELS))
        self.texts = set()                                    # the packages' licence files, as site-packages has them
        for name, version in pins.items():
            info = f"{name.replace('-', '_')}-{version}.dist-info"
            texts = {path.format(info=info): text for path, text in WHEELS[name].items()}
            files = texts | {path.format(info=info): text for path, text in OTHERS.get(name, {}).items()}
            files[f'{info}/METADATA'] = f'Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n'
            for path, text in files.items():
                self.write(self.site / path, text)
            self.write(self.site / info / 'RECORD', ''.join(f'{path},,\n' for path in [*files, f'{info}/RECORD']))
            self.texts |= set(texts)

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def collect(self):
        licences.collect(folder=self.folder, debian=self.debian, fonts=self.fonts, stdlib=self.stdlib,
                         path=[str(self.site)])

    def test_the_folder_gets_the_text_of_every_licence_the_table_names(self):
        self.collect()
        found = {path.relative_to(self.folder).as_posix() for path in self.folder.rglob('*') if path.is_file()}
        self.assertEqual(found, {'NOTICE.md', 'LICENSE', 'Apache-2.0.txt', 'GPL-3.0.txt', 'GPL-2.0.txt', 'LGPL-2.1.txt',
                                 'Python.txt', 'OFL.txt'} | {f'site-packages/{path}' for path in self.texts})
        self.assertIn('site-packages/onnxruntime/LICENSE', found)
        for name, source in (('GPL-3.0.txt', self.debian / 'GPL-3'), ('Python.txt', self.stdlib / 'LICENSE.txt'),
                             ('site-packages/numpy/ma/LICENSE', self.site / 'numpy' / 'ma' / 'LICENSE')):
            self.assertEqual((self.folder / name).read_bytes(), source.read_bytes())
        self.assertEqual(licences.named((REPO / 'NOTICE.md').read_text()),
                         {'GPL-3.0', 'MIT', 'Apache-2.0', 'SIL Open Font Licence 1.1', 'BSD-3-Clause', 'LGPL-2.1',
                          'GPL-2.0', 'PSF'})

    def test_a_text_missing_from_the_base_image_stops_the_build(self):
        (self.debian / 'GPL-3').unlink()
        self.assertRaises(FileNotFoundError, self.collect)

    def test_the_build_stops_unless_every_licence_has_its_text_there(self):
        notice = (REPO / 'NOTICE.md').read_text()
        for what, change, named in (
                ('a package without a licence file', lambda: self.write(
                    self.site / 'pycparser-3.12.dist-info' / 'RECORD', 'pycparser-3.12.dist-info/METADATA,,\n'),
                 'pycparser'),
                ('a package that is not installed', lambda: shutil.rmtree(self.site / 'cffi-2.1.1.dist-info'), 'cffi'),
                ("a text without its licence's words", lambda: self.write(self.debian / 'GPL-2', APACHE), 'GPL-2.0'),
                ('a licence with no text known', lambda: self.write(self.folder / 'NOTICE.md', notice.replace(
                    '| numpy 2.5.4 | BSD-3-Clause |', '| numpy 2.5.4 | Zlib |')), 'Zlib')):
            with self.subTest(what=what):
                self.stand_in()
                change()
                with self.assertRaises(SystemExit) as stop:
                    self.collect()
                self.assertIn(named, str(stop.exception.code))


class Entry(unittest.TestCase):
    """The entry script with stand-ins for id, setpriv and python on its PATH."""

    def run_entry(self, *args, uid=0, setpriv_works=True, folder=None):
        """`folder`: the entry script with the person's folder there instead of /folder; the python stand-in then
        writes down the folder it runs in."""
        with tempfile.TemporaryDirectory() as tmp:
            bin_ = Path(tmp)
            log = bin_ / 'log'
            entry = ENTRY
            stubs = {'id': f'echo {uid}',
                     'setpriv': ('echo "setpriv $*" >> "$LOG"; while [ "$1" != -- ]; do shift; done; shift; '
                                 '[ "$1" = true ] && exit 0; echo "ran $* as 10001" >> "$LOG"') if setpriv_works
                     else 'echo "setpriv: setresuid failed: Operation not permitted" >&2; exit 1',
                     'python': 'echo "python $* state=$AIOSUBTITLES_STATE image=$AIOSUBTITLES_IN_IMAGE" >> "$LOG"'}
            if folder is not None:
                entry = bin_ / 'entry'
                entry.write_text(ENTRY.read_text().replace('/folder', str(folder)))
                stubs['python'] = 'echo "in $(pwd -P)" >> "$LOG"'
            for name, body in stubs.items():
                path = bin_ / name
                path.write_text(f'#!/bin/sh\n{body}\n')
                path.chmod(0o755)
            env = {'PATH': f'{bin_}:/usr/bin:/bin', 'LOG': str(log)}
            done = subprocess.run(['sh', str(entry), *args], capture_output=True, text=True, timeout=30, env=env,
                                  stdin=subprocess.DEVNULL, cwd=bin_)
            return done.returncode, log.read_text() if log.exists() else '', done.stderr

    def test_setup_runs_setup_with_the_folders_state(self):
        code, log, _ = self.run_entry('setup', '--version')
        self.assertEqual(code, 0)
        # -u: without a terminal (docker run -d, a pipe) Python would hold the page's address back
        self.assertEqual(log, 'python -I -u /opt/aiosubtitles/installer/wizard.py --version state=/folder/state '
                              'image=1\n')

    def test_setup_runs_in_the_persons_folder(self):
        # the README's ./setup --restore state/backups/FILE and --answers FILE: paths in the person's folder, which
        # is /folder in setup's container (the image's own folder, WORKDIR, would be the wrong place)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve() / 'folder'
            (folder / 'state').mkdir(parents=True)
            code, log, _ = self.run_entry('setup', '--restore', 'state/backups/viewer-1a2b3c4d.json', folder=folder)
            self.assertEqual((code, log), (0, f'in {folder}\n'))
            # no folder mounted (docker run ... setup --version): setup still runs, where it was started
            code, log, _ = self.run_entry('setup', '--version', folder=Path(tmp).resolve() / 'none')
            self.assertEqual(code, 0)
            self.assertTrue(log.startswith('in ') and not log.startswith(f'in {Path(tmp).resolve()}/none'), log)

    def test_the_service_started_as_root_drops_to_its_own_user(self):
        code, log, _ = self.run_entry('python', '-u', '-m', 'subsync')
        self.assertEqual(code, 0)
        self.assertIn('ran python -u -m subsync as 10001', log)
        self.assertIn('setpriv --reuid=10001 --regid=10001 --clear-groups --no-new-privs -- python -u -m subsync', log)
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
