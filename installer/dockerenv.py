"""Docker: is it there, which AIOStreams containers run on this machine, running subsync's compose file, and the
work setup does from inside its container: a helper on the machine's own network, joining AIOStreams'
network, the ownership check, the new version's start check, and putting a database copy back."""
import base64
import io
import json
import secrets
import shutil
import socket
import subprocess
import tarfile
import time

from installer.plan import AIOS_DEFAULT_PORT, DATA, INSTALL_LABEL, SERVICE_USER, VOLUME
from subsync import digits

# docker_problem's (problem, fix) when there is no docker command
NO_DOCKER = ('Docker is not installed.', 'Install Docker (docs.docker.com/engine/install), then run ./setup again.')


def run(cmd, timeout=60, stdin=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def docker_problem():
    """None when Docker and Compose v2 work, else (problem, fix)."""
    if not shutil.which('docker'):
        return NO_DOCKER
    code, _, err = run(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=30)
    if code != 0:
        if 'permission denied' in err.lower():
            return ('Docker is installed, but this user may not use it.',
                    'Run ./setup with sudo, or add yourself to the docker group (sudo usermod -aG docker $USER, then log in again).')
        return 'Docker is installed but not running.', 'Start it (sudo systemctl start docker, or open Docker Desktop), then run ./setup again.'
    if run(['docker', 'compose', 'version', '--short'])[0] != 0:
        return 'Docker Compose v2 is missing.', 'Install the docker-compose-plugin package, then run ./setup again.'
    return None


def free_gb(path='/'):
    """The free space, in GB, of the disk `path` is on; None when it can't be read. Inside setup's container the root
    (the default) is on Docker's own storage, where the images and the service's volume are: on Docker Desktop and
    Colima the virtual machine's disk, not this machine's."""
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def _inspect(ids):
    """`docker inspect` of these containers. Docker exits 1 when one of them is gone (removed since `docker ps`), but
    still prints the others."""
    if not ids:
        return []
    _, out, _ = run(['docker', 'inspect', *ids])
    try:
        found = json.loads(out) if out.strip() else []
    except ValueError:
        return []
    return found if isinstance(found, list) else []


def _port(value):
    """AIOStreams' PORT as its container has it: its default when it is unset, empty or not a whole number written in
    the digits 0 to 9 (digits.whole, the rule for every number setup reads from outside)."""
    return digits.whole(str(value or '').strip()) or AIOS_DEFAULT_PORT


def aios_containers():
    """Running containers that look like AIOStreams: name, networks, ports, env, network mode."""
    code, out, _ = run(['docker', 'ps', '--format', '{{.ID}}\t{{.Image}}\t{{.Names}}'])
    if code != 0:
        return []
    ids = [line.split('\t')[0] for line in out.splitlines()
           if 'aiostreams' in line.split('\t')[1].lower() or 'aiostreams' in line.split('\t')[-1].lower()]
    found = []
    for c in _inspect(ids):
        env = dict(e.split('=', 1) for e in (c.get('Config') or {}).get('Env') or [] if '=' in e)
        if 'subsync' in c['Name'].lower():
            continue
        port = _port(env.get('PORT'))
        ns = c.get('NetworkSettings') or {}
        nets = ns.get('Networks') or {}
        published = []
        for spec, binds in (ns.get('Ports') or {}).items():
            if spec.split('/')[0] == str(port):
                for b in binds or []:
                    published.append((b.get('HostIp') or '0.0.0.0', int(b['HostPort'])))
        found.append({
            'name': c['Name'].lstrip('/'),
            'image': (c.get('Config') or {}).get('Image', ''),
            'port': port,
            'base_url': env.get('BASE_URL', ''),
            'host_network': (c.get('HostConfig') or {}).get('NetworkMode') == 'host',
            'networks': [n for n in nets if n not in ('bridge', 'host', 'none')],
            'ips': [v.get('IPAddress') for v in nets.values() if v.get('IPAddress')],
            'published': published,
        })
    return found


def host_urls(c):
    """Addresses this machine can reach a container's AIOStreams at, best first."""
    urls = []
    if c['host_network']:
        urls.append(f"http://127.0.0.1:{c['port']}")
    for ip, port in c['published']:
        if ip in ('0.0.0.0', '::', ''):
            ip = '127.0.0.1'        # published on every address: this machine's own reaches it
        urls.append(f"http://{f'[{ip}]' if ':' in ip else ip}:{port}")
    urls += [f"http://{ip}:{c['port']}" for ip in c['ips']]   # works on Linux, not on Docker Desktop
    if c['base_url']:
        urls.append(c['base_url'])
    return list(dict.fromkeys(urls))


def port_in_use(port, host='0.0.0.0'):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


def lan_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('192.0.2.1', 9))   # no packet is sent; this only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return ''


def in_vm():
    """Whether Docker runs in a virtual machine of its own, as on a Mac: Docker Desktop (its operating system, as
    `docker info` names it), Colima and the other Lima machines (their names: colima, colima-<profile>, lima-<name>).
    Docker's host network is then the virtual machine's, so no address setup finds there is this machine's."""
    code, out, _ = run(['docker', 'info', '--format', '{{.OperatingSystem}}\t{{.Name}}'], timeout=30)
    system, _, name = out.strip().partition('\t')
    return code == 0 and (system == 'Docker Desktop' or name == 'colima' or name.startswith(('colima-', 'lima-')))


def compose(state_dir, *args, timeout=1800):
    return run(['docker', 'compose', '--project-directory', str(state_dir), '-f', str(state_dir / 'compose.yaml'), *args],
               timeout=timeout)


def container_state(name):
    code, out, _ = run(['docker', 'inspect', '--format', '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}', name])
    return out.strip() if code == 0 else ''


def stopped(name):
    """Whether Docker has the container `name` and it isn't running: it exited, or restarts in a loop."""
    return container_state(name).split()[:1] not in ([], ['running'])


def running(name):
    """Whether the container `name` runs: Docker runs a command in it (exec_in) only then, never while it is stopped,
    restarting in a loop or gone."""
    return container_state(name).split()[:1] == ['running']


def exec_in(name, cmd, stdin=None, timeout=120):
    return run(['docker', 'exec', '-i', name, *cmd], stdin=stdin, timeout=timeout)


# subsync's /health, asked inside its container on the port it listens on there (SUBSYNC_PORT, else 8790): by setup's
# wait for the service and by the new version's start check
HEALTH = ['python', '-c', 'import os,urllib.request;print(urllib.request.urlopen("http://127.0.0.1:%s/health"%'
                          'os.environ.get("SUBSYNC_PORT","8790"),timeout=5).read().decode())']


# ------------------------------------------------------------------------------------------- from inside setup's container
SOCKET = '/var/run/docker.sock'
# The databases a copy holds: subsync's own list (subsync/subsync/hostcmd.py DATABASES; a test keeps them equal)
DATABASES = ('users.sqlite3', 'subsync.sqlite3')

# The helper's script: this machine's network address, and which of the host:port pairs given are taken. A port it
# may not open (below 1024 for its user) is not "taken": only an address already in use is.
PROBE = """import errno, json, socket, sys
def taken(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
        return False
    except OSError as e:
        return e.errno == errno.EADDRINUSE
    finally:
        s.close()
def lan():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return ""
pairs = [a.rsplit(":", 1) for a in sys.argv[1:]]
print(json.dumps({"lan": lan(), "taken": [f"{h}:{p}" for h, p in pairs if taken(h, int(p))]}))
"""


def probe(image, checks):
    """Answers from this machine's own network, by a short-lived helper from setup's own image (on the host's network,
    as the service's user, without Docker's socket, removed at once): {'lan': its address, 'taken': ['host:port']} for
    the (host, port) pairs in `checks`. None when the helper can't run."""
    cmd = ['docker', 'run', '--rm', '--network', 'host', '--user', SERVICE_USER, '--entrypoint', 'python', image,
           '-I', '-c', PROBE] + [f'{h}:{p}' for h, p in checks]
    code, out, _ = run(cmd, timeout=120)
    try:
        return json.loads(out.strip().splitlines()[-1]) if code == 0 else None
    except (ValueError, IndexError):
        return None


def join(network, container):
    """Puts setup's own container on a Docker network (AIOStreams', to reach it by name). True when it is on it."""
    code, _, err = run(['docker', 'network', 'connect', network, container])
    return code == 0 or 'already exists' in err


def _listing(cmd):
    code, out, err = run(cmd)
    if code != 0:
        raise RuntimeError(((err or out).strip().splitlines() or ['docker gave no answer'])[-1])
    return [line.split('\t') + [''] for line in out.splitlines() if line.strip()]


def foreign(project, container, install_id, unlabelled=()):
    """What Docker has under this setup's names (the compose project, the service's container, its volume and network)
    that doesn't carry this install's id: [(kind, name)], empty when everything there is its own. `unlabelled`: the
    (kind, name) this install has without its id (setup labels what it makes since 1.4.0), its own while they carry no
    install label at all. RuntimeError when Docker doesn't answer."""
    label = f'{{{{.Label "{INSTALL_LABEL}"}}}}'
    named = {'container': container, 'volume': f'{project}_{VOLUME}', 'network': f'{project}_default'}
    kinds = (('container', ['docker', 'ps', '-a'], '{{.Names}}'), ('volume', ['docker', 'volume', 'ls'], '{{.Name}}'),
             ('network', ['docker', 'network', 'ls'], '{{.Name}}'))
    found = []
    for kind, base, name in kinds:
        seen = {}
        for filt in (f'label=com.docker.compose.project={project}', f'name={named[kind]}'):
            for row in _listing(base + ['--filter', filt, '--format', f'{name}\t{label}']):
                if filt.startswith('name=') and row[0] != named[kind]:
                    continue        # Docker's name filter matches parts of names too
                seen.setdefault(row[0], row[1])
        found += [(kind, n) for n, owner in seen.items()
                  if owner != install_id and not (owner == '' and (kind, n) in unlabelled)]
    return found


def _remove_checks(project, tag):
    """Removes the start checks' containers (first) and volumes that carry this install's label (`tag`): what a setup
    stopped during a check leaves behind, still running (the page runs an update in a thread that Ctrl-C doesn't
    wait for)."""
    prefix = f'{project}-check-'
    for listing, remove in ((['docker', 'ps', '-a', '--format', '{{.Names}}'], ['docker', 'rm', '-f']),
                            (['docker', 'volume', 'ls', '--format', '{{.Name}}'], ['docker', 'volume', 'rm'])):
        code, out, _ = run(listing + ['--filter', f'label={tag}', '--filter', f'name={prefix}'])
        names = [n for n in out.split() if n.startswith(prefix)] if code == 0 else []   # the filter matches parts too
        if names:
            run(remove + names)


def check_image(image, project, install_id, seconds=90):
    """The new version started once while the old one keeps running: a throwaway container from `image`, with no
    published port, an empty volume of its own, public mode and a throwaway secret, as the service's user. (True, '')
    when /health answers ok within `seconds`, else (False, reason). It and its volume are removed either way, and an
    earlier check's left behind are removed first."""
    name = f'{project}-check-{secrets.token_hex(4)}'
    tag = f'{INSTALL_LABEL}={install_id}'
    _remove_checks(project, tag)
    try:
        run(['docker', 'volume', 'create', '--label', tag, name])
        code, out, err = run(['docker', 'run', '-d', '--name', name, '--label', tag, '--user', SERVICE_USER,
                              '-v', f'{name}:{DATA}', '-e', 'SUBSYNC_PUBLIC=on',
                              '-e', 'SUBSYNC_PUBLIC_BASE=http://127.0.0.1:8790',
                              '-e', f'SUBSYNC_SECRET_KEY={secrets.token_urlsafe(32)}',
                              '-e', f'SUBSYNC_PUBLIC_INVITE={secrets.token_urlsafe(9)}',
                              '-e', 'SUBSYNC_RECHECK_HOURS=0', image], timeout=300)
        if code != 0:
            return False, ((err or out).strip().splitlines() or ['it did not start'])[-1]
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            code, out, _ = run(['docker', 'exec', name] + HEALTH, timeout=20)
            if code == 0 and out.strip() == 'ok':
                return True, ''
            if stopped(name):
                return False, 'it stopped'
            time.sleep(2)
        return False, f'no answer within {seconds} seconds'
    finally:
        run(['docker', 'rm', '-f', name])
        run(['docker', 'volume', 'rm', name])


def databases_tar(files):
    """{name: bytes} as a tar file, base64 text: what RESTORE reads on its standard input."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w') as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            t.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


# Run by the old image, as the service's user, with its data volume at the folder its first argument names: removes
# the databases its second names (JSON) and their leftover -wal/-shm files, then writes the copy's (a database not in
# the copy stays removed: as before the update).
RESTORE = """import base64, io, json, os, sys, tarfile
data, names = sys.argv[1], json.loads(sys.argv[2])
copy = tarfile.open(fileobj=io.BytesIO(base64.b64decode(sys.stdin.read())))
inside = set(copy.getnames())
for name in names:
    for extra in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(os.path.join(data, name + extra))
        except FileNotFoundError:
            pass
    if name in inside:
        tmp = os.path.join(data, name + ".restore")
        with open(tmp, "wb") as f:
            f.write(copy.extractfile(name).read())
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(data, name))
print("restored")
"""


def run_on_volume(image, volume, cmd, timeout=120):
    """`cmd`, as exec_in runs it in the service's container, in a short-lived container of `image` instead (the
    version installed, as the service's user, on the service's volume, removed when it ends): for a service that isn't
    running."""
    return run(['docker', 'run', '--rm', '--user', SERVICE_USER, '--entrypoint', cmd[0], '-v', f'{volume}:{DATA}',
                image, *cmd[1:]], timeout=timeout)


def restore_databases(image, volume, files):
    """Puts a database copy ({name: bytes}) back into the service's volume, with the image of the version that will
    run on it (an older one has no command of its own for this). The service must be stopped. (True, '') or
    (False, reason)."""
    code, out, err = run(['docker', 'run', '--rm', '-i', '--user', SERVICE_USER, '--entrypoint', 'python',
                          '-v', f'{volume}:{DATA}', image, '-I', '-c', RESTORE, DATA, json.dumps(DATABASES)],
                         stdin=databases_tar(files), timeout=600)
    if code == 0 and out.strip().endswith('restored'):
        return True, ''
    return False, ((err or out).strip().splitlines() or ['no answer'])[-1]
