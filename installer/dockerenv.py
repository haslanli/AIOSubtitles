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


def run(cmd, timeout=60, stdin=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, '', str(e)


def docker_problem():
    """None when Docker and Compose v2 work, else (problem, fix)."""
    if not shutil.which('docker'):
        return 'Docker is not installed.', 'Install Docker (docs.docker.com/engine/install), then run ./setup again.'
    code, _, err = run(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=30)
    if code != 0:
        if 'permission denied' in err.lower():
            return ('Docker is installed, but this user may not use it.',
                    'Run ./setup with sudo, or add yourself to the docker group (sudo usermod -aG docker $USER, then log in again).')
        return 'Docker is installed but not running.', 'Start it (sudo systemctl start docker, or open Docker Desktop), then run ./setup again.'
    code, out, _ = run(['docker', 'compose', 'version', '--short'])
    if code != 0:
        return 'Docker Compose v2 is missing.', 'Install the docker-compose-plugin package, then run ./setup again.'
    return None


def free_gb(path='/'):
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def _inspect(ids):
    if not ids:
        return []
    code, out, _ = run(['docker', 'inspect', *ids])
    return json.loads(out) if code == 0 and out.strip() else []


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
        port = int(env.get('PORT', '3000') or 3000)
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
        urls.append(f"http://{'127.0.0.1' if ip in ('0.0.0.0', '::', '') else ip}:{port}")
    urls += [f"http://{ip}:{c['port']}" for ip in c['ips']]   # works on Linux, not on Docker Desktop
    if c['base_url']:
        urls.append(c['base_url'])
    seen = []
    return [u for u in urls if not (u in seen or seen.append(u))]


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


def compose(state_dir, *args, timeout=1800):
    return run(['docker', 'compose', '--project-directory', str(state_dir), '-f', str(state_dir / 'compose.yaml'), *args],
               timeout=timeout)


def container_state(name):
    code, out, _ = run(['docker', 'inspect', '--format', '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}', name])
    return out.strip() if code == 0 else ''


def exec_in(name, cmd, stdin=None, timeout=120):
    return run(['docker', 'exec', '-i', name, *cmd], stdin=stdin, timeout=timeout)


# ------------------------------------------------------------------------------------------- from inside setup's container
SOCKET = '/var/run/docker.sock'
INSTALL_LABEL = 'org.aiosubtitles.install'
SERVICE_USER = '10001:10001'
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


def foreign(project, container, install_id):
    """What Docker has under this setup's names (the compose project, the service's container, its volume and network)
    that doesn't carry this install's id: [(kind, name)], empty when everything there is its own. RuntimeError when
    Docker doesn't answer."""
    label = f'{{{{.Label "{INSTALL_LABEL}"}}}}'
    named = {'container': container, 'volume': f'{project}_subsync-data', 'network': f'{project}_default'}
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
        found += [(kind, n) for n, owner in seen.items() if owner != install_id]
    return found


def _health_command(port=8790):
    return ['python', '-c', f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{port}/health', "
                            "timeout=5).read().decode())"]


def check_image(image, project, install_id, seconds=90):
    """The new version started once while the old one keeps running: a throwaway container from `image`, with no
    published port, an empty volume of its own, public mode and a throwaway secret, as the service's user. (True, '')
    when /health answers ok within `seconds`, else (False, reason). It and its volume are removed either way."""
    name = f'{project}-check-{secrets.token_hex(4)}'
    tag = f'{INSTALL_LABEL}={install_id}'
    try:
        run(['docker', 'volume', 'create', '--label', tag, name])
        code, out, err = run(['docker', 'run', '-d', '--name', name, '--label', tag, '--user', SERVICE_USER,
                              '-v', f'{name}:/data', '-e', 'SUBSYNC_PUBLIC=on',
                              '-e', 'SUBSYNC_PUBLIC_BASE=http://127.0.0.1:8790',
                              '-e', f'SUBSYNC_SECRET_KEY={secrets.token_urlsafe(32)}',
                              '-e', f'SUBSYNC_PUBLIC_INVITE={secrets.token_urlsafe(9)}',
                              '-e', 'SUBSYNC_RECHECK_HOURS=0', image], timeout=300)
        if code != 0:
            return False, ((err or out).strip().splitlines() or ['it did not start'])[-1]
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            code, out, _ = run(['docker', 'exec', name] + _health_command(), timeout=20)
            if code == 0 and out.strip() == 'ok':
                return True, ''
            if container_state(name).split()[:1] not in ([], ['running']):
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


# Run by the old image, as the service's user, with its data volume at /data: removes the databases and their
# leftover -wal/-shm files, then writes the copy's (a database not in the copy stays removed: as before the update).
RESTORE = """import base64, io, os, sys, tarfile
data = "/data"
names = ("users.sqlite3", "subsync.sqlite3")
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


def restore_databases(image, volume, files):
    """Puts a database copy ({name: bytes}) back into the service's volume, with the image of the version that will
    run on it (an older one has no command of its own for this). The service must be stopped. (True, '') or
    (False, reason)."""
    code, out, err = run(['docker', 'run', '--rm', '-i', '--user', SERVICE_USER, '--entrypoint', 'python',
                          '-v', f'{volume}:/data', image, '-I', '-c', RESTORE], stdin=databases_tar(files), timeout=600)
    if code == 0 and out.strip().endswith('restored'):
        return True, ''
    return False, ((err or out).strip().splitlines() or ['no answer'])[-1]
