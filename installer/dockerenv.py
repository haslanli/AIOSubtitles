"""Docker: is it there, which AIOStreams containers run on this machine, and running subsync's compose file."""
import json
import shutil
import socket
import subprocess


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
