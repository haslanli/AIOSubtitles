"""What ./setup changes, as pure functions (no network, no Docker), so each rule is tested on its own.

Nothing here talks to AIOStreams; wizard.py reads the config, calls these, shows the result and saves it only after
the user says yes.
"""
import copy
import ipaddress
import re
import secrets
import urllib.parse

from subsync import realdebrid

SUBSYNC_PORT = 8790
SUBSYNC_HOST = 'aiosubtitles-subsync'   # its name on AIOStreams' Docker network(s)
SUBSYNC_PRESET_NAME = 'subsync'

# AIOStreams preset types that give subtitles (v2.37.0). A custom addon gives subtitles when its resources say so.
SUBTITLE_PRESETS = {'opensubtitles', 'opensubtitles-v3-plus', 'subsource', 'subdl', 'subhero', 'aiosubtitle'}
# Usenet sources: left out of the sync config, so subsync's listings never spend an indexer's daily API calls.
USENET_PRESETS = {'newznab', 'nzbhydra', 'easynews', 'easynews-search', 'easynewsPlus', 'easynewsPlusPlus',
                  'usenet-streamer', 'streamnzb', 'davex'}
USENET_SERVICES = {'stremio_nntp', 'nzbdav', 'aiostreams', 'altmount', 'easynews', 'stremthru_newz'}
# The sync config: no Usenet, no Real-Debrid, no subsync, and nothing that starts work in the background, so a read of
# a listed link opens that one file and nothing else (no failover to the next row, no preload, no pre-caching).
SYNC_SETTINGS = {
    'failover': {'enabled': False, 'precacheFailover': False},
    'preloadStreams': {'enabled': False},
    'precacheNextEpisode': False,
    'serviceWrap': {'enabled': False},
    'cacheAndPlay': {'enabled': False},
    'checkOwned': False,
}

# name, ISO 639-1, ISO 639-2/B (what subsync and Matroska use)
LANGUAGES = [
    ('English', 'en', 'eng'), ('Spanish', 'es', 'spa'), ('French', 'fr', 'fre'), ('German', 'de', 'ger'),
    ('Italian', 'it', 'ita'), ('Portuguese', 'pt', 'por'), ('Russian', 'ru', 'rus'), ('Ukrainian', 'uk', 'ukr'),
    ('Polish', 'pl', 'pol'), ('Dutch', 'nl', 'dut'), ('Swedish', 'sv', 'swe'), ('Norwegian', 'no', 'nor'),
    ('Danish', 'da', 'dan'), ('Finnish', 'fi', 'fin'), ('Czech', 'cs', 'cze'), ('Hungarian', 'hu', 'hun'),
    ('Romanian', 'ro', 'rum'), ('Bulgarian', 'bg', 'bul'), ('Croatian', 'hr', 'hrv'), ('Serbian', 'sr', 'srp'),
    ('Greek', 'el', 'gre'), ('Turkish', 'tr', 'tur'), ('Arabic', 'ar', 'ara'), ('Hebrew', 'he', 'heb'),
    ('Persian', 'fa', 'per'), ('Hindi', 'hi', 'hin'), ('Indonesian', 'id', 'ind'), ('Thai', 'th', 'tha'),
    ('Vietnamese', 'vi', 'vie'), ('Japanese', 'ja', 'jpn'), ('Korean', 'ko', 'kor'), ('Chinese', 'zh', 'chi'),
]
_BY = {}
for _name, _two, _three in LANGUAGES:
    for _k in (_name.lower(), _two, _three):
        _BY[_k] = (_name, _two, _three)
_BY.update({'deu': _BY['de'], 'fra': _BY['fr'], 'nld': _BY['nl'], 'ces': _BY['cs'], 'ell': _BY['el'],
            'ron': _BY['ro'], 'zho': _BY['zh'], 'fas': _BY['fa'], 'farsi': _BY['fa']})
UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)


def parse_languages(text):
    """'English, ru' -> ([('English','en','eng'), ('Russian','ru','rus')], None) or ([], 'what is wrong')."""
    found, unknown = [], []
    for part in re.split(r'[,;/\s]+', text or ''):
        if not part:
            continue
        lang = _BY.get(part.strip().lower())
        if lang is None:
            unknown.append(part)
        elif lang not in found:
            found.append(lang)
    if unknown:
        return [], f'Not a language I know: {", ".join(unknown)}. Use names like English, Spanish, Russian.'
    if not found:
        return [], 'Name at least one language.'
    if len(found) > 4:
        return [], 'Four languages at most (each one is a separate sync for every file).'
    return found, None


def language_names(langs):
    return ', '.join(name for name, _, _ in langs)


def config_id_from(text):
    """A config ID typed as is, or found in a pasted Stremio/Jellyfin link (…/stremio/<id>/…)."""
    m = UUID_RE.search(text or '')
    return m.group(0).lower() if m else None


def new_key(nbytes=18):
    return secrets.token_urlsafe(nbytes).replace('-', 'x').replace('_', 'y')


# ------------------------------------------------------------------------------------------- reading a config

def debrid_names(config):
    """The names of the debrid services a config uses (its service list and its addons' links), in a fixed order."""
    used = realdebrid.debrid_in_use(config)
    return [name for service, name in realdebrid.DEBRID_SERVICES.items() if service in used]


def is_subtitle_preset(preset):
    if preset.get('type') in SUBTITLE_PRESETS:
        return True
    resources = (preset.get('options') or {}).get('resources') or []
    return preset.get('type') == 'custom' and 'subtitles' in resources


def is_subsync_preset(preset, manifest_url=None):
    options = preset.get('options') or {}
    if preset.get('type') != 'custom':
        return False
    if manifest_url and options.get('manifestUrl') == manifest_url:
        return True
    return str(options.get('name', '')).strip().lower() == SUBSYNC_PRESET_NAME


def subtitle_sources(config, manifest_url=None):
    """Names of the subtitle sources a viewer gets, subsync left out."""
    return [str((p.get('options') or {}).get('name') or p.get('type')) for p in config.get('presets') or []
            if p.get('enabled', True) and is_subtitle_preset(p) and not is_subsync_preset(p, manifest_url)]


def stream_sources(config):
    return [p for p in config.get('presets') or []
            if p.get('enabled', True) and not is_subtitle_preset(p) and p.get('type') not in USENET_PRESETS
            and 'stream' in ((p.get('options') or {}).get('resources') or ['stream'])]


# ------------------------------------------------------------------------------------------- the changes

def free_instance_id(config, wanted):
    taken = {p.get('instanceId') for p in config.get('presets') or []}
    if wanted not in taken:
        return wanted
    for i in range(1, 1000):
        candidate = f'{wanted[:2]}{i}'
        if candidate not in taken:
            return candidate
    return new_key(3)[:4]


def with_defaults(preset, metadata):
    """Unset options take the preset's own defaults, as AIOStreams' configure page would fill them in."""
    for option in (metadata or {}).get(preset['type'], {}).get('OPTIONS', []):
        if option.get('default') is not None:
            preset['options'].setdefault(option['id'], option['default'])
    return preset


def subsync_preset(config, manifest_url, metadata=None):
    return with_defaults({'type': 'custom', 'instanceId': free_instance_id(config, 'ssy'), 'enabled': True,
                          'options': {'name': SUBSYNC_PRESET_NAME, 'manifestUrl': manifest_url,
                                      'resources': ['subtitles'], 'timeout': 2000}}, metadata)


def opensubtitles_preset(config, metadata=None):
    """Stremio's official OpenSubtitles (v3): no account, no language setting (it lists every language). The
    community OpenSubtitles V3+ answered in 16-23 s on 2026-11-09, past AIOStreams' 7 s default."""
    return with_defaults({'type': 'opensubtitles', 'instanceId': free_instance_id(config, 'ost'), 'enabled': True,
                          'options': {'name': 'OpenSubtitles', 'timeout': 7000, 'resources': ['subtitles']}}, metadata)


def _before_other_subtitles(presets, preset):
    """presets with `preset` placed before the first other subtitle source (at the end when there is none).
    AIOStreams lists subtitles in the order of the config, and players keep only the first few per language
    (AIOMetadata: 3), so subsync's entries must come first to be seen."""
    for i, p in enumerate(presets):
        if is_subtitle_preset(p):
            return presets[:i] + [preset] + presets[i:]
    return presets + [preset]


def viewer_change(config, manifest_url, metadata=None, add_opensubtitles=False):
    """The viewer's config with subsync's addon (an older subsync entry replaced) placed before the other subtitle
    sources and, if asked, OpenSubtitles after it.
    Returns (new config, [plain-language lines]); no lines means nothing to change."""
    new = copy.deepcopy(config)
    presets = new.setdefault('presets', [])
    current = [p for p in presets if is_subsync_preset(p, manifest_url)]
    others = [p for p in presets if not is_subsync_preset(p, manifest_url)]
    changes = []
    if len(current) == 1 and current[0]['options'].get('manifestUrl') == manifest_url and current[0].get('enabled', True):
        placed = _before_other_subtitles(others, current[0])
        if placed != presets:
            new['presets'] = placed
            changes.append('moves "subsync" before your other subtitle sources (apps that list only a few subtitles '
                           'per language keep its entries)')
    else:
        new['presets'] = others
        new['presets'] = _before_other_subtitles(others, subsync_preset(new, manifest_url, metadata))
        changes.append('updates the subtitle source "subsync" (new address)' if current
                       else 'adds 1 subtitle source: subsync (synced copies of the subtitles you already get)')
    if add_opensubtitles:
        new['presets'].append(opensubtitles_preset(new, metadata))
        changes.append('adds 1 subtitle source: OpenSubtitles (Stremio\'s official one; it lists every language)')
    return new, changes


def without_subsync(config, manifest_url=None):
    """The viewer's config with subsync's addon taken out (./setup --uninstall)."""
    new = copy.deepcopy(config)
    before = len(new.get('presets') or [])
    new['presets'] = [p for p in new.get('presets') or [] if not is_subsync_preset(p, manifest_url)]
    return new, before - len(new['presets'])


def sync_config(viewer, manifest_url=None):
    """The config subsync finds files through: the viewer's sources (so it finds the file being watched) and subtitle
    sources (so it syncs what the viewer sees), without subsync itself, without Usenet, without any way to Real-Debrid
    (realdebrid.without_real_debrid), and with nothing that resolves or queues in the background."""
    new = copy.deepcopy(viewer)
    new['presets'] = [p for p in new.get('presets') or []
                      if not is_subsync_preset(p, manifest_url) and p.get('type') not in USENET_PRESETS]
    new['services'] = [s for s in new.get('services') or [] if s.get('id') not in USENET_SERVICES]
    new, _ = realdebrid.without_real_debrid(new)
    for key, value in SYNC_SETTINGS.items():
        if isinstance(value, dict):
            new[key] = {**(new.get(key) or {}), **value}
        else:
            new[key] = value
    for key in ('uuid', 'accessKey', 'precacheSelector', 'precacheSingleStream'):
        new.pop(key, None)
    return new


def sync_config_problems(sync):
    """Why the sync config can't find any file subsync could read (empty = it can)."""
    problems = []
    if not realdebrid.debrid_in_use(sync):
        problems.append('no debrid service (subsync reads files from debrid services only, not Usenet or torrents)')
    if not stream_sources(sync):
        problems.append('no stream source other than Usenet ones')
    return problems


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
        'SUBSYNC_SOURCE_MANIFEST': s['sync_manifest'],
        'SUBSYNC_SOURCE_PASSWORD': s['sync_password'],
        'SUBSYNC_PUBLIC_BASE': s['public_base'],
        'SUBSYNC_BASE_PATH': base_path_of(s['public_base']),
        'SUBSYNC_ADDON_KEY': s['addon_key'],
        'SUBSYNC_LANGUAGES': ','.join(three for _, _, three in s['languages']),
        'SUBSYNC_URL_REWRITE': s.get('rewrite', ''),
        'SUBSYNC_AUDIO_ROUTE': 'on' if s['audio'] else 'off',
        'SUBSYNC_DAILY_BUDGET_GB': str(s.get('daily_budget_gb', 50)),
        'SUBSYNC_SUBDL_KEY': s.get('subdl_key', ''),
        'SUBSYNC_WYZIE_KEY': s.get('wyzie_key', ''),
    }
    return {k: v for k, v in env.items() if v not in (None, '')}


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
