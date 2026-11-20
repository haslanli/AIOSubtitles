"""What ./setup changes, as pure functions (no network, no Docker), so each rule is tested on its own.

Nothing here talks to AIOStreams; installer/engine.py reads the config, calls these, shows the result through its two
front ends (the setup page, installer/web.py, and the terminal, installer/wizard.py) and saves it only after the user
says yes.
"""
import ipaddress
import math
import os
import re
import urllib.parse

from subsync import digits, explain
from subsync.aiosconfig import (  # noqa: F401  (the rules live in subsync, shared with the public sign-up)
    USENET_SERVICES, SYNC_SETTINGS, LANGUAGES, parse_languages, language_names, names_of_codes, config_id_from,
    alias_from, new_key, debrid_names, is_subtitle_preset, is_subsync_preset, subtitle_sources, subsync_preset,
    viewer_change, without_subsync, sync_config, sync_config_problems, usenet_route, usenet_sync_config)

SUBSYNC_PORT = 8790
SUBSYNC_HOST = 'aiosubtitles-subsync'   # its name on AIOStreams' Docker network(s)
AIOS_DEFAULT_PORT = 3000   # AIOStreams' own default port (its PORT setting)
AIOS_DEFAULT_URL = f'http://127.0.0.1:{AIOS_DEFAULT_PORT}'   # AIOStreams' own default address on its machine


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
    return 'port', f"http://{f'[{host}]' if ':' in host else host}:{port}"     # an IPv6 address in its brackets


def has_usable_port(parts):
    """Whether a split address has no port or one from 1 to 65535 (`.port` raises ValueError for letters or a number
    out of range, which urlsplit itself lets through)."""
    try:
        return parts.port != 0
    except ValueError:
        return False


# an address with a space (or another blank) inside: urlsplit lets it through, and Python's http.client then refuses it
NO_SPACES = 'Leave out the spaces.'


def has_space(text):
    return any(c.isspace() for c in text)


def check_public(url):
    """(good, message, normalized) for an address typed in by the user."""
    url = (url or '').strip().rstrip('/')
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or not has_usable_port(parts):
        return False, 'Type a full address that starts with http:// or https://', url
    if parts.query or parts.fragment:
        return False, 'Leave out anything after ? or #.', url
    if has_space(url):
        return False, NO_SPACES, url
    return True, '', url


def base_path_of(url):
    return urllib.parse.urlsplit(url).path.rstrip('/')


def rewrite_rule(aios_public, aios_internal):
    """Stream links carry AIOStreams' public address; subsync on the same machine reads them over the Docker network."""
    a, b = (aios_public or '').rstrip('/'), (aios_internal or '').rstrip('/')
    return f'{a}={b}' if a and b and a != b else ''


def subtitle_cache_value(current, host):
    """AIOStreams' per-addon subtitle cache setting with subsync's host set to -1 (no cache), the rest kept."""
    value = {}
    if isinstance(current, dict):
        value.update(current)
    elif isinstance(current, str):
        for part in current.split(','):
            k, _, v = part.partition(':')
            v = v.strip()
            number = digits.whole(v.removeprefix('-'))     # never "²", "--5" or "１"
            if k.strip() and number is not None:
                value[k.strip()] = -number if v.startswith('-') else number
    elif isinstance(current, (int, float)):
        value['*'] = int(current)
    value.setdefault('*', 300)
    value[host] = -1
    return value


# ------------------------------------------------------------------------------------------- subsync's files

def subsync_env(s):
    """The service's settings. `s` holds the answers and what was found (see engine.py's install); everything else keeps
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
        # When it starts, written only for "as soon as a file is opened" (pick is subsync's default)
        'SUBSYNC_AUDIO_WHEN': 'open' if s['audio'] and s.get('audio_when') == 'open' else '',
        'SUBSYNC_AUDIO_BUDGET_GB': s.get('audio_budget') if s['audio'] else '',
        'SUBSYNC_SUBDL_KEY': s.get('subdl_key', ''),
        'SUBSYNC_WYZIE_KEY': s.get('wyzie_key', ''),
        'SUBSYNC_TMDB_KEY': s.get('tmdb_key', ''),
    }
    return {k: v for k, v in env.items() if v not in (None, '')}


AUDIO_BUDGETS = ('0.5', '1', '2', '4')   # GB per file the audio route may read: ./setup's choices (explain.py)
AUDIO_BUDGET_WORDS = ', '.join(AUDIO_BUDGETS[:-1]) + ' or ' + AUDIO_BUDGETS[-1]     # as questions and messages say them

# The personal route's audio question, one of three answers (its explanation: explain.QUESTIONS['AUDIO'])
AUDIO_QUESTION = 'Sync files that have no subtitles of their own, from their audio?'
AUDIO_ANSWERS = (('no', 'No'), ('pick', 'Yes, when I pick the file'), ('open', 'Yes, as soon as a file is opened'))
# setup also took yes or no here before, as answers files may still say
_YES, _NO = ('y', 'yes', 'on', 'true'), ('n', 'no', 'off', 'false')
# A number for AUDIO in an answers file: the yes/no question took 1 for yes and 0 for no, and the terminal's numbers
# now start with 1 = No, so a number could mean either; it is refused rather than read either way (engine.ask_audio)
AUDIO_NUMBER_REFUSED = ('AUDIO in an answers file takes no, pick or open, not a number: the question had two answers '
                        'before (1 was yes), so a number could mean either.')


def audio_answer(text):
    """An answer to the audio question in words: no, pick or open; the earlier yes/no question's yes reads as pick
    (when I pick the file, what it did). None otherwise (a number is the terminal's way to choose: Asker.choose)."""
    value = str(text or '').strip().lower()
    if value in ('pick', 'open'):
        return value
    return 'pick' if value in _YES else 'no' if value in _NO else None


# A switch in state/subsync.env is on with one of these words, as subsync reads it (subsync/subsync/settings.py _on;
# a test keeps them equal). Typed answers take y too (ui.Asker.yesno); AUDIO's yes takes no number (_YES above).
ON = ('1', 'on', 'true', 'yes')


def is_on(value):
    """A switch of state/subsync.env, read as subsync reads it."""
    return str(value).strip().lower() in ON


def audio_when_of(env):
    """When the audio route starts as state/subsync.env has it, read as subsync reads it: SUBSYNC_AUDIO_WHEN, else
    the setting it replaced (SUBSYNC_AUDIO_ON_PICK: anything but on means open); pick otherwise."""
    when = str(env.get('SUBSYNC_AUDIO_WHEN') or '').strip().lower()
    if when in ('pick', 'open'):
        return when
    return 'pick' if is_on(env.get('SUBSYNC_AUDIO_ON_PICK', 'on')) else 'open'


def _gigabytes(text):
    """'1 GB', '1gb', '1.0' -> '1'; '.5' -> '0.5'; None when it isn't a positive number."""
    value = (text or '').strip().lower()
    if value.endswith('gb'):
        value = value[:-2].strip()
    number = digits.decimal(value)
    if number is None or not (math.isfinite(number) and number > 0):
        return None
    return str(int(number)) if number == int(number) else str(number)


def audio_budget(text, allowed_too=None):
    """An answer to ./setup's audio limit question as written in subsync.env ('0.5', '1', '2' or '4'), or None.
    `allowed_too`: a value set by hand before, kept when it is given again."""
    value = _gigabytes(text)
    if value is not None and (value in AUDIO_BUDGETS or (allowed_too is not None and value == _gigabytes(allowed_too))):
        return value
    return None


ENV_LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')


def _between_quotes(value):
    """A quoted value's text, found as Docker Compose finds it: the value starts with ' or ", the next one of the same
    closes it, and nothing but a comment follows. None when it isn't quoted that way."""
    quote = value[:1]
    end = value.find(quote, 1) if quote in ('"', "'") else -1
    if end < 0:
        return None
    rest = value[end + 1:].strip()
    return value[1:end] if not rest or rest.startswith('#') else None


def parse_env(text, as_written=False):
    """The KEY=value lines of an env file (an answers file, state/subsync.env, state/secrets.env) as Docker Compose
    reads them: an `export ` before the name and spaces round the = are allowed, a quoted value is the text between
    its quotes as it is, and in an unquoted value a " #" starts a comment. Empty values are included (an empty value
    is a setting too); comment and other lines are skipped; a $ is never substituted. A value that starts with a quote
    it doesn't close is kept as written. `as_written`: every quoted value as the line has it (its quotes, and a comment
    after them), for the lines setup writes back as they were."""
    out = {}
    for line in (text or '').splitlines():
        m = ENV_LINE.match(line)
        if not m:
            continue
        value = m.group(2)
        if value.startswith(('"', "'")):
            inside = _between_quotes(value)
            out[m.group(1)] = value if as_written or inside is None else inside
        else:
            out[m.group(1)] = re.sub(r'\s+#.*$', '', value).strip()
    return out


def merge_env(written, current, owned=()):
    """state/subsync.env: ./setup's lines (`written`: the settings it owns, explain.WRITTEN_BY_SETUP, and the limits it
    asked, `owned`), then every other line of the current file as the user wrote it. A limit it asked whose answer is
    subsync's default has no line, so a later default applies. Returns (values, names kept from the current file)."""
    owned = set(explain.WRITTEN_BY_SETUP) | set(owned)
    kept = [k for k in current if k not in owned]
    return {**written, **{k: current[k] for k in kept}}, kept


def project():
    """The compose project's name: aiosubtitles (an isolated test sets its own with AIOSUBTITLES_PROJECT)."""
    return os.environ.get('AIOSUBTITLES_PROJECT') or 'aiosubtitles'


def container_name():
    return f'{project()}-subsync'


INSTALL_LABEL = 'org.aiosubtitles.install'    # on everything setup makes: what its ownership check looks for
SERVICE_USER = '10001:10001'    # the service's user and group: the image's own, which owns DATA (subsync/Dockerfile)
VOLUME = 'subsync-data'         # the service's volume; Docker names it <project>_subsync-data
DATA = '/data'                  # where it is in the service's container (subsync's SUBSYNC_DATA in the image)


def compose_yaml(s):
    """state/compose.yaml: subsync only, on AIOStreams' Docker network(s) when it runs on this machine. It runs the
    image setup itself runs from (`s['image']`, pinned by digest; `local`: built on this machine, never pulled), as its
    own user, never with Docker's socket, and labels the container, its volume and its own network with the install's
    id (`s['install_id']`). The volume an install setup 1.3.0 or before made keeps (`s['external_volume']`, its name;
    Docker can't label a volume it has) is declared external instead, so Compose never creates, checks or recreates
    it."""
    label = f'{{{INSTALL_LABEL}: "{s["install_id"]}"}}'
    lines = [
        '# Written by ./setup (AIOSubtitles). Run ./setup again to change it; edits here are replaced.',
        f'name: {project()}',
        'services:',
        '  subsync:',
        f"    image: {s['image']}",
        f"    pull_policy: {'never' if s.get('local') else 'missing'}",
        f'    container_name: {container_name()}',
        f'    user: "{SERVICE_USER}"',
        f'    labels: {label}',
        '    restart: unless-stopped',
        '    env_file: [subsync.env]',
        f'    volumes: ["{VOLUME}:{DATA}"]',
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
        lines.append('networks:')
        lines.append(f'  default: {{labels: {label}}}')
        for i, name in enumerate(s.get('networks') or []):
            lines.append(f'  aios{i}: {{external: true, name: "{name}"}}')
    kept = s.get('external_volume')
    volume = f'{{external: true, name: "{kept}"}}' if kept else f'{{labels: {label}}}'
    lines += ['volumes:', f'  {VOLUME}: {volume}', '']
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
