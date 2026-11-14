"""What ./setup changes, as pure functions (no network, no Docker), so each rule is tested on its own.

Nothing here talks to AIOStreams; wizard.py reads the config, calls these, shows the result and saves it only after
the user says yes.
"""
import ipaddress
import math
import re
import urllib.parse

from subsync import explain
from subsync.aiosconfig import (  # noqa: F401  (the rules live in subsync, shared with the public sign-up)
    SUBSYNC_PRESET_NAME, SUBTITLE_PRESETS, USENET_PRESETS, USENET_SERVICES, SYNC_SETTINGS, LANGUAGES, UUID_RE,
    parse_languages, language_names, config_id_from, new_key, debrid_names, is_subtitle_preset, is_subsync_preset,
    subtitle_sources, stream_sources, free_instance_id, with_defaults, subsync_preset, opensubtitles_preset,
    _before_other_subtitles, viewer_change, without_subsync, sync_config, sync_config_problems, usenet_route,
    usenet_sync_config, _BY)

SUBSYNC_PORT = 8790
SUBSYNC_HOST = 'aiosubtitles-subsync'   # its name on AIOStreams' Docker network(s)


# ------------------------------------------------------------------------------------------- addresses

def host_of(url):
    return (urllib.parse.urlsplit(url).hostname or '').lower()


def is_local_host(host):
    if host in ('localhost', ''):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_private_host(host):
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return host.endswith(('.local', '.lan', '.home', '.internal')) or '.' not in host


def public_default(aios_public, lan_ip, port=SUBSYNC_PORT):
    """Where viewers' apps fetch subsync's files, and how: ('path', url) behind the same web address as AIOStreams
    (one rule in the user's web server), or ('port', url) on this machine's own port."""
    parts = urllib.parse.urlsplit(aios_public or '')
    host = (parts.hostname or '').lower()
    if parts.scheme == 'https' and not is_private_host(host):
        return 'path', f'{parts.scheme}://{parts.netloc}/subsync'
    if is_local_host(host):
        host = lan_ip or '127.0.0.1'
    return 'port', f'http://{host}:{port}'


def check_public(url):
    """(good, message, normalized) for an address typed in by the user."""
    url = (url or '').strip().rstrip('/')
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return False, 'Type a full address that starts with http:// or https://', url
    if parts.query or parts.fragment:
        return False, 'Leave out anything after ? or #.', url
    return True, '', url


def base_path_of(url):
    return urllib.parse.urlsplit(url).path.rstrip('/')


def rewrite_rule(aios_public, aios_internal):
    """Stream links carry AIOStreams' public address; subsync on the same machine reads them over the Docker network."""
    a, b = (aios_public or '').rstrip('/'), (aios_internal or '').rstrip('/')
    return f'{a}={b}' if a and b and a != b else ''


def manifest_base(manifest_url):
    return manifest_url[: -len('/manifest.json')] if manifest_url.endswith('/manifest.json') else manifest_url.rstrip('/')


def subtitle_cache_value(current, host):
    """AIOStreams' per-addon subtitle cache setting with subsync's host set to -1 (no cache), the rest kept."""
    value = {}
    if isinstance(current, dict):
        value.update(current)
    elif isinstance(current, str):
        for part in current.split(','):
            k, _, v = part.partition(':')
            if k.strip() and v.strip().lstrip('-').isdigit():
                value[k.strip()] = int(v)
    elif isinstance(current, (int, float)):
        value['*'] = int(current)
    value.setdefault('*', 300)
    value[host] = -1
    return value


# ------------------------------------------------------------------------------------------- subsync's files

def subsync_env(s):
    """The service's settings. `s` holds the answers and what was found (see wizard.py); everything else keeps
    subsync's own defaults."""
    env = {
        'SUBSYNC_SOURCE_MANIFEST': s.get('sync_manifest', ''),     # none for a config read through Usenet only
        'SUBSYNC_SOURCE_PASSWORD': s.get('sync_password', ''),
        'SUBSYNC_USENET': 'on' if s.get('usenet') else 'off',
        'SUBSYNC_USENET_MANIFEST': s.get('usenet_manifest', '') if s.get('usenet') else '',
        'SUBSYNC_USENET_PASSWORD': s.get('usenet_password', '') if s.get('usenet') else '',
        'SUBSYNC_PUBLIC_BASE': s['public_base'],
        'SUBSYNC_BASE_PATH': base_path_of(s['public_base']),
        'SUBSYNC_ADDON_KEY': s['addon_key'],
        'SUBSYNC_LANGUAGES': ','.join(three for _, _, three in s['languages']),
        'SUBSYNC_URL_REWRITE': s.get('rewrite', ''),
        'SUBSYNC_AUDIO_ROUTE': 'on' if s['audio'] else 'off',
        'SUBSYNC_AUDIO_BUDGET_GB': s.get('audio_budget') if s['audio'] else '',
        'SUBSYNC_SUBDL_KEY': s.get('subdl_key', ''),
        'SUBSYNC_WYZIE_KEY': s.get('wyzie_key', ''),
    }
    return {k: v for k, v in env.items() if v not in (None, '')}


AUDIO_BUDGETS = ('0.5', '1', '2', '4')   # GB per file the audio route may read: ./setup's choices (explain.py)


def _gigabytes(text):
    """'1 GB', '1gb', '1.0' -> '1'; '.5' -> '0.5'; None when it isn't a positive number."""
    value = (text or '').strip().lower()
    if value.endswith('gb'):
        value = value[:-2].strip()
    try:
        number = float(value)
    except ValueError:
        return None
    if not (math.isfinite(number) and number > 0):
        return None
    return str(int(number)) if number == int(number) else str(number)


def audio_budget(text, allowed_too=None):
    """An answer to ./setup's audio limit question as written in subsync.env ('0.5', '1', '2' or '4'), or None.
    `allowed_too`: a value set by hand before, kept when it is given again."""
    value = _gigabytes(text)
    if value is not None and (value in AUDIO_BUDGETS or (allowed_too is not None and value == _gigabytes(allowed_too))):
        return value
    return None


ENV_LINE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$')


def parse_env(text):
    """The KEY=value lines of an env file, empty values included (an empty value is a setting too). Comment and other
    lines are skipped; in an unquoted value, a " #" starts a comment, as Docker Compose reads it."""
    out = {}
    for line in (text or '').splitlines():
        m = ENV_LINE.match(line)
        if m:
            value = m.group(2)
            out[m.group(1)] = value if value.startswith(('"', "'")) else re.sub(r'\s+#.*$', '', value).strip()
    return out


def merge_env(written, current, owned=()):
    """state/subsync.env: ./setup's lines (`written`: the settings it owns, explain.WRITTEN_BY_SETUP, and the limits it
    asked, `owned`), then every other line of the current file as the user wrote it. A limit it asked whose answer is
    subsync's default has no line, so a later default applies. Returns (values, names kept from the current file)."""
    owned = set(explain.WRITTEN_BY_SETUP) | set(owned)
    kept = [k for k in current if k not in owned]
    return {**written, **{k: current[k] for k in kept}}, kept


def compose_yaml(s):
    """state/compose.yaml: subsync only, on AIOStreams' Docker network(s) when it runs on this machine."""
    lines = [
        '# Written by ./setup (AIOSubtitles). Run ./setup again to change it; edits here are replaced.',
        'name: aiosubtitles',
        'services:',
        '  subsync:',
        '    build: ../subsync',
        f"    image: aiosubtitles/subsync:{s['version']}",
        '    container_name: aiosubtitles-subsync',
        '    restart: unless-stopped',
        '    env_file: [subsync.env]',
        '    volumes: ["subsync-data:/data"]',
        '    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}',
    ]
    if s.get('host_network'):
        lines.append('    network_mode: host')
    else:
        if s.get('bind'):
            lines.append(f"    ports: [\"{s['bind']}:{s['port']}:{SUBSYNC_PORT}\"]")
        lines.append('    networks:')
        lines.append('      default: {}')
        for i, _ in enumerate(s.get('networks') or []):
            lines.append(f'      aios{i}: {{aliases: [{SUBSYNC_HOST}]}}')
    if not s.get('host_network') and s.get('networks'):
        lines.append('networks:')
        for i, name in enumerate(s['networks']):
            lines.append(f'  aios{i}: {{external: true, name: "{name}"}}')
    lines += ['volumes:', '  subsync-data: {}', '']
    return '\n'.join(lines)


def proxy_snippets(public_base, target):
    """What to add to the user's web server so <public>/subsync/... reaches subsync with the path unchanged."""
    path = base_path_of(public_base) or '/'
    host = urllib.parse.urlsplit(public_base).hostname
    return {
        'Caddy': [f'{host} {{', f'    reverse_proxy {path}/* {target}', '    # ... your existing lines stay as they are', '}'],
        'nginx': [f'location {path}/ {{', f'    proxy_pass http://{target};', '}'],
        'Traefik': [f'rule: Host(`{host}`) && PathPrefix(`{path}/`)', f'service: http://{target}  (no StripPrefix)'],
    }
