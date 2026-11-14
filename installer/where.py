"""Where setup runs: always inside the AIOSubtitles image, on one of two routes.

  image   `docker run ... setup` in the folder the person made: the folder at /folder, its files in /folder/state, and
          a./setup written into it that runs the same command again;
  source  the source folder's own ./setup, which builds the image here first: only its state/ at /folder/state.

Setup reads its own container through Docker's socket: the image it runs from (the service then runs exactly that
image, pinned by digest), the machine's port for the page, the folder's path on the machine and its owner. On the
machine's own network (the source route on Linux) setup works as it did outside a container; on Docker's bridge
network it reaches the machine through its gateway.
"""
import json
import os
import posixpath
import socket
from collections import namedtuple
from pathlib import Path

from installer import dockerenv

MOUNT = '/folder'          # where the folder is in setup's container
FOLDER = Path(MOUNT)
PAGE_PORT = 8789          # the setup page's port inside the container
SCRIPT_MARK = '# AIOSubtitles setup, written by setup:'
# what the docker run command lacks (README, Install)
NO_SOCKET = ('Setup needs Docker\'s socket to start and update AIOSubtitles: add -v '
             '/var/run/docker.sock:/var/run/docker.sock to the docker run command (README, Install).')
NO_FOLDER = ('Setup needs a folder for its files: add -v "$PWD:/folder" to the docker run command, in the folder you '
             'made for it (README, Install).')
NO_PAGE_PORT = ('The setup page needs a port on this machine: add -p 127.0.0.1:8789:8789 to the docker run command '
                '(README, Install), or give every answer with --answers FILE --yes.')

Place = namedtuple('Place', 'route network container image local run_as page_port host_folder owner gateway')


def _repo(name):
    """'ghcr.io/x/y:tag' -> 'ghcr.io/x/y' (a registry's port stays)."""
    name = name.split('@', 1)[0]
    last = name.rsplit('/', 1)[-1]
    return name[: len(name) - len(last)] + last.split(':', 1)[0]


def _registry(repo):
    """Whether an image's name starts with a registry (ghcr.io/..., localhost:5000/...)."""
    first = repo.split('/', 1)[0] if '/' in repo else ''
    return bool(first) and ('.' in first or ':' in first or first == 'localhost')


def place_from(info, image, version, route, owner):
    """The Place from `docker inspect` of setup's own container (`info`) and of its image (`image`)."""
    run_as = (info.get('Config') or {}).get('Image') or ''
    digests = image.get('RepoDigests') or []
    mine = [d for d in digests if _repo(d) == _repo(run_as)] or digests
    repo, digest = mine[0].split('@', 1) if mine else (_repo(run_as), '')
    if run_as.startswith('sha256:') and mine:
        run_as = f'{repo}:{version}'
    # a name without a registry (aiosubtitles:local-1.4.0) is a build of this machine's: never pulled. Docker 29's
    # image store gives such a build a digest too: it pins this very build, under its own tag
    local = not _registry(repo)
    if not digest:
        pinned = run_as
    elif local:
        pinned = f"{run_as.split('@', 1)[0]}@{digest}"
    else:
        pinned = f'{repo}:{version}@{digest}'
    network = 'host' if (info.get('HostConfig') or {}).get('NetworkMode') == 'host' else 'bridge'
    settings = info.get('NetworkSettings') or {}
    page_port = None
    if network == 'bridge':
        binds = (settings.get('Ports') or {}).get(f'{PAGE_PORT}/tcp') or []
        binds = sorted(binds, key=lambda b: b.get('HostIp') != '127.0.0.1')
        if binds:
            page_port = int(binds[0]['HostPort'])
    nets = settings.get('Networks') or {}
    gateway = '' if network == 'host' else ((nets.get('bridge') or next(iter(nets.values()), {})).get('Gateway') or '')
    target = MOUNT if route == 'image' else f'{MOUNT}/state'
    source = next((m.get('Source') for m in info.get('Mounts') or [] if m.get('Destination') == target), '') or ''
    host_folder = source if route == 'image' else posixpath.dirname(source.rstrip('/'))
    return Place(route, network, info.get('Id', ''), pinned, local, run_as, page_port, host_folder, tuple(owner),
                 gateway)


def missing(info, route):
    """What setup's own container lacks to keep its files: [NO_FOLDER] or []."""
    target = MOUNT if route == 'image' else f'{MOUNT}/state'
    return [] if any(m.get('Destination') == target for m in info.get('Mounts') or []) else [NO_FOLDER]


def discover(version, environ=None):
    """The Place of this setup, read through the socket. Raises RuntimeError with the reason when it can't."""
    environ = os.environ if environ is None else environ
    route = 'source' if environ.get('AIOSUBTITLES_ROUTE') == 'source' else 'image'
    me = environ.get('HOSTNAME') or socket.gethostname()
    code, out, err = dockerenv.run(['docker', 'inspect', me])
    if code != 0:
        raise RuntimeError(((err or out).strip().splitlines() or ['docker inspect failed'])[-1])
    info = json.loads(out)[0]
    lacking = missing(info, route)
    if lacking:
        raise RuntimeError(lacking[0])
    code, out, err = dockerenv.run(['docker', 'image', 'inspect', info['Image']])
    image = json.loads(out)[0] if code == 0 and out.strip() else {}
    folder = FOLDER if route == 'image' else FOLDER / 'state'
    st = os.stat(folder)
    return place_from(info, image, version, route, (st.st_uid, st.st_gid))


def setup_script(place):
    """The image route's ./setup: the same docker run line, on this folder, with the port and image it was run with."""
    pull = '' if place.local else ' --pull always'
    port = place.page_port or PAGE_PORT
    return ('#!/bin/sh\n'
            f'{SCRIPT_MARK} runs setup again on this folder (./setup --help lists the options).\n'
            '# Setup rewrites this file; edits here are replaced.\n'
            'cd "$(dirname "$0")" || exit 1\n'
            't=; [ -t 0 ] && [ -t 1 ] && t=-t\n'
            f'exec docker run --rm -i $t{pull} -v /var/run/docker.sock:/var/run/docker.sock -v "$PWD:/folder" '
            f'-p 127.0.0.1:{port}:{PAGE_PORT} {place.run_as} setup "$@"\n')


def write_setup_script(place, folder=None):
    """Writes the folder's ./setup (the image route only), owned by the folder's owner. A setup file setup didn't
    write is never replaced. True when the file is setup's own."""
    folder = Path(folder or FOLDER)
    path = folder / 'setup'
    if path.exists():
        lines = path.read_text(errors='replace').split('\n')
        if len(lines) < 2 or not lines[1].startswith(SCRIPT_MARK):
            return False
    text = setup_script(place)
    if not path.exists() or path.read_text() != text:
        tmp = folder / '.setup.tmp'
        tmp.write_text(text)
        tmp.chmod(0o755)
        if os.geteuid() == 0:
            os.chown(tmp, *place.owner)
        tmp.replace(path)
    return True
