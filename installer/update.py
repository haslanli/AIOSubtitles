"""Updates, only when the person asks: the page after setup, or
./setup --update. Nothing looks for a new version in the background and nothing updates by itself.

A folder cloned with git asks its origin whether there is a newer commit (git fetch: nothing else is contacted) and
reads the new version's number from its VERSION file. The update fast-forwards the folder (it stops before changing
anything if files were changed by hand), then setup starts again on the new code and builds the new version while the
old one keeps running (installer/engine.py); a failed build puts the folder back here.
"""
import os
import shutil
import subprocess
from collections import namedtuple
from pathlib import Path

Ready = namedtuple('Ready', 'version how reason')     # how: git (a newer version), newest, not-git, error

# the words
RUNNING = 'This machine runs version {version}.'
READY = 'Version {version} is ready.'
ABOUT = ('Updating builds subsync again from the new version and restarts it; your answers, keys and stored copies '
         'stay.')
ABOUT_HOSTING = ('Updating builds AIOSubtitles again from the new version and restarts it; your answers, keys, '
                 'sign-ups and stored copies stay, and a copy of its databases is saved first.')
NEWEST = 'This is the newest version.'
LOOK_FAILED = "Couldn't look for a new version: {reason}."
NOT_GIT = ("This folder wasn't downloaded with git, so it can't fetch a new version: download the new version, copy "
           "this folder's state/ into it, and run ./setup there.")
CHANGED = 'This folder has changes of its own, so the update stopped before changing anything: {files}.'
FAILED = "The update didn't go through: {reason}. Version {version} keeps running."
FETCHING = 'Fetching version {version}'
RESTARTING = 'Starting setup again on the new version'
SAVED_DATABASES = 'Saved a copy of the databases: {path}'


class UpdateStopped(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _git(repo, *args, timeout=120):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='', SSH_ASKPASS='')
    try:
        done = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def _reason(err):
    lines = [line.strip() for line in (err or '').splitlines() if line.strip()]
    return (lines[-1] if lines else 'git gave no reason').rstrip('.')


def running(repo):
    """The version of the code in this folder."""
    path = Path(repo) / 'VERSION'
    return path.read_text().strip() if path.exists() else '1.0.0'


def version_at(repo, commit):
    code, out, _ = _git(repo, 'show', f'{commit}:VERSION')
    return out if code == 0 and out else '1.0.0'


def look(repo):
    """Whether a newer version is ready: Ready(version, 'git', None), or how it isn't (newest, not-git, error)."""
    if not (Path(repo) / '.git').exists():
        return Ready(None, 'not-git', None)
    code, upstream, err = _git(repo, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
    if code:
        return Ready(None, 'error', _reason(err))
    code, _, err = _git(repo, 'fetch', '--quiet', upstream.split('/', 1)[0])
    if code:
        return Ready(None, 'error', _reason(err))
    _, head, _ = _git(repo, 'rev-parse', 'HEAD')
    _, theirs, _ = _git(repo, 'rev-parse', '@{u}')
    if head == theirs:
        return Ready(None, 'newest', None)
    return Ready(version_at(repo, '@{u}'), 'git', None)


def changed_files(repo):
    """Files in git that were changed by hand here (state/ is not in git, so it never counts)."""
    _, out, _ = _git(repo, 'diff', '--name-only', 'HEAD')
    return [line.strip() for line in out.splitlines() if line.strip()]


def fast_forward(repo):
    """Moves the folder to the new version; returns the commit it was on. Changes nothing when it can't go ahead."""
    files = changed_files(repo)
    if files:
        raise UpdateStopped(CHANGED.format(files=', '.join(files)))
    _, old, _ = _git(repo, 'rev-parse', 'HEAD')
    code, _, err = _git(repo, 'merge', '--ff-only', '--quiet', '@{u}')
    if code:
        raise UpdateStopped(FAILED.format(reason=_reason(err), version=running(repo)))
    return old


def put_back(repo, old):
    """The folder back on the commit it was on before the update."""
    _git(repo, 'reset', '--keep', old)


def keep_newest(folder, prefix, n):
    """Leaves the newest `n` entries whose names start with `prefix` (their names end with the time they were made)."""
    folder = Path(folder)
    if not folder.exists():
        return
    found = sorted(p for p in folder.iterdir() if p.name.startswith(prefix))
    for path in found[:-n] if n else found:
        shutil.rmtree(path) if path.is_dir() else path.unlink()
