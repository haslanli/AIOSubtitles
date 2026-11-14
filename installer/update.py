"""Updates, only when the person asks: the page after setup, or ./setup --update. Nothing looks for a new version in the
background and nothing updates by itself.

A new version comes as a newer image: on the image route ./setup fetches the newest release before setup starts; on
the source route ./setup --update fetches the folder's newer commit with git and builds its image first (the folder's
./setup script, which uses the git words below). Setup then compares its own version with the one the service runs,
and the update (installer/engine.py) copies the databases, checks the new version while the old one keeps running,
switches, checks again, and puts the old version back when the new one doesn't answer.
"""
import re
import shutil
from collections import namedtuple
from pathlib import Path

Ready = namedtuple('Ready', 'version how')     # how: ready (an update), newest, newer-here (this machine runs newer)

# the words
RUNNING = 'This machine runs version {version}.'
READY = 'Version {version} is ready.'
ABOUT = ('Updating saves a copy of the databases, checks the new version while the old one keeps running, then '
         "switches: AIOSubtitles is offline for a few seconds. If the new version doesn't answer, the old one is put "
         'back.')
ABOUT_HOSTING = ABOUT
NEWEST = 'This is the newest version.'
NEWER_HERE = ('This machine runs version {running}, newer than this setup ({setup}): from here, only a rollback can be '
              'made.')
FAILED = "The update didn't go through: {reason}. Version {version} keeps running."
SAVED_DATABASES = 'Saved a copy of the databases: {path}'
COPYING = 'Copying the databases'
CHECKING = 'Checking version {version} while version {old} keeps running'
CHECKED = 'The new version answers'
SWITCHING = 'Switching to version {version}'
SWITCHED = 'Switched to version {version}'
ROLLED_BACK = "Version {new} didn't answer: version {old} is running again"
WITH_DATABASES = ', with the databases from before the update'
STILL_DOWN = "Version {old} didn't start again either. Its log: docker logs {container}"
KEPT = 'Version {version} is kept for a rollback: ./setup --rollback.'
OLD_IMAGES = 'Older images: docker image rm {image} frees about 1 GB.'
NO_ROLLBACK = 'There is no earlier version to go back to.'
ROLLBACK = 'Roll back to version {version}'
ROLLBACK_HEADING = 'Rollback'
ROLLBACK_ABOUT = ('Goes back to the version before the last update, with the same copy of the databases and '
                  'the same checks as an update.')
RESTORE_QUESTION = 'Also put back the databases from before version {version} was installed ({name})?'
RESTORE_HELP = ('Everything since that update is lost: sign-ups, records and stored copies made after it. '
                'Without this, the earlier version runs on the databases as they are now.')
# the source route's ./setup --update (git, in the folder's own script; a test keeps the script's words equal)
LOOK_FAILED = "Couldn't look for a new version: {reason}."
NOT_GIT = ("This folder wasn't downloaded with git, so it can't fetch a new version: download the new version, copy "
           "this folder's state/ into it, and run ./setup there.")
CHANGED = 'This folder has changes of its own, so the update stopped before changing anything: {files}.'


def _parts(version):
    return tuple(int(n) for n in re.findall(r'\d+', (version or '0').split('-', 1)[0])[:3])


def compare(settings, image, version):
    """Whether the setup running now (its `image` and `version`) is an update for the service in `settings`."""
    running, here = _parts(settings.get('version')), _parts(version)
    if here > running:
        return Ready(version, 'ready')
    if here < running:
        return Ready(version, 'newer-here')
    if settings.get('image') and settings.get('image') != image:
        return Ready(version, 'ready')        # another build of the same version (a release candidate before it)
    return Ready(version, 'newest')


def running(repo):
    """The version of the code in this folder (its VERSION file)."""
    path = Path(repo) / 'VERSION'
    return path.read_text().strip() if path.exists() else '1.0.0'


def keep_newest(folder, prefix, n, keep=()):
    """Leaves `n` entries whose names start with `prefix` (their names end with the time they were made): those named
    in `keep` (a rollback needs them), then the newest."""
    folder = Path(folder)
    if not folder.exists():
        return
    found = sorted(p for p in folder.iterdir() if p.name.startswith(prefix))
    kept = [p for p in found if p.name in keep]
    rest = [p for p in found if p.name not in keep]
    room = max(n - len(kept), 0)
    for path in rest[:-room] if room else rest:
        shutil.rmtree(path) if path.is_dir() else path.unlink()
