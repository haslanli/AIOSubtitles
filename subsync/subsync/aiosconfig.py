"""The AIOStreams side shared by ./setup and the public sign-up: the rules that build subsync's own sync config from a
viewer's config (no Usenet, no Real-Debrid, no subsync, nothing that starts work in the background), the rules that
place subsync's addon in a viewer's config, and the few AIOStreams API calls both make. Pure functions apart from the
client, which opens every request through the request gate (net.py, realdebrid.py).

Standard library only, so ./setup imports it too.
"""
import base64
import copy
import http.cookiejar
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import net, realdebrid


def _say_wait(seconds):
    print(f'  ..  AIOStreams asks to slow down; waiting {seconds} s', file=sys.stderr, flush=True)


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


def is_subsync_preset(preset, manifest_url=None, names=(SUBSYNC_PRESET_NAME,)):
    """Whether a preset is this subsync addon: a custom addon with this link, or named one of `names` (lower case)."""
    options = preset.get('options') or {}
    if preset.get('type') != 'custom':
        return False
    if manifest_url and options.get('manifestUrl') == manifest_url:
        return True
    return str(options.get('name', '')).strip().lower() in names


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


def subsync_preset(config, manifest_url, metadata=None, name=SUBSYNC_PRESET_NAME):
    return with_defaults({'type': 'custom', 'instanceId': free_instance_id(config, 'ssy'), 'enabled': True,
                          'options': {'name': name, 'manifestUrl': manifest_url,
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


def viewer_change(config, manifest_url, metadata=None, add_opensubtitles=False, name=SUBSYNC_PRESET_NAME):
    """The viewer's config with subsync's addon (an older subsync entry replaced) placed before the other subtitle
    sources and, if asked, OpenSubtitles after it.
    Returns (new config, [plain-language lines]); no lines means nothing to change."""
    new = copy.deepcopy(config)
    presets = new.setdefault('presets', [])
    names = (name.lower(),)
    current = [p for p in presets if is_subsync_preset(p, manifest_url, names)]
    others = [p for p in presets if not is_subsync_preset(p, manifest_url, names)]
    changes = []
    if len(current) == 1 and current[0]['options'].get('manifestUrl') == manifest_url and current[0].get('enabled', True):
        placed = _before_other_subtitles(others, current[0])
        if placed != presets:
            new['presets'] = placed
            changes.append(f'moves "{name}" before your other subtitle sources (apps that list only a few subtitles '
                           'per language keep its entries)')
    else:
        new['presets'] = others
        new['presets'] = _before_other_subtitles(others, subsync_preset(new, manifest_url, metadata, name))
        changes.append(f'updates the subtitle source "{name}" (new address)' if current
                       else f'adds 1 subtitle source: {name} (synced copies of the subtitles you already get)')
    if add_opensubtitles:
        new['presets'].append(opensubtitles_preset(new, metadata))
        changes.append('adds 1 subtitle source: OpenSubtitles (Stremio\'s official one; it lists every language)')
    return new, changes


def without_subsync(config, manifest_url=None, names=(SUBSYNC_PRESET_NAME,)):
    """The viewer's config with subsync's addon taken out (./setup --uninstall; the public "delete my data")."""
    new = copy.deepcopy(config)
    before = len(new.get('presets') or [])
    new['presets'] = [p for p in new.get('presets') or [] if not is_subsync_preset(p, manifest_url, names)]
    return new, before - len(new['presets'])


def sync_config(viewer, manifest_url=None, names=(SUBSYNC_PRESET_NAME,)):
    """The config subsync finds files through: the viewer's sources (so it finds the file being watched) and subtitle
    sources (so it syncs what the viewer sees), without subsync itself, without Usenet, without any way to Real-Debrid
    (realdebrid.without_real_debrid), and with nothing that resolves or queues in the background."""
    new = copy.deepcopy(viewer)
    new['presets'] = [p for p in new.get('presets') or []
                      if not is_subsync_preset(p, manifest_url, names) and p.get('type') not in USENET_PRESETS]
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


# ------------------------------------------------------------------------------------------- the AIOStreams API

UA = 'AIOSubtitles/1.0 (setup)'   # starts with "AIO": stock AIOStreams then includes file details in stream listings


class AiosError(Exception):
    def __init__(self, message, fix=None, code=None):
        super().__init__(message)
        self.message, self.fix, self.code = message, fix, code


def _plain(code, message):
    """AIOStreams error codes, in plain words."""
    code = str(code or '')
    if code in ('USER_INVALID_DETAILS', 'USER_INVALID_PASSWORD', 'USER_NOT_FOUND'):
        return 'That config ID and password don\'t match a config on this AIOStreams.'
    if code == 'USER_INVALID_CONFIG':
        return f'AIOStreams refused the config: {message}'
    if code in ('FORBIDDEN', 'UNAUTHORIZED') or 'not allowed' in (message or '').lower():
        return 'This AIOStreams asks you to sign in before configs can be saved.'
    if code == 'RATE_LIMIT_EXCEEDED' or 'too many' in (message or '').lower():
        return 'AIOStreams is limiting requests from this machine (it allows 10 new configs an hour); try again later.'
    return message or code or 'AIOStreams gave an error without a message.'


class AiosApi:
    """The AIOStreams API calls ./setup and the public sign-up make. Every request passes the request gate
    (net.build_opener: the Real-Debrid guard and, in public mode, the public-address check)."""

    def __init__(self, base, timeout=30, user_agent=UA, on_wait=None, max_timeout=None, retry_wait_cap=60):
        self.base = base.rstrip('/')
        self.timeout = timeout
        self.max_timeout = max_timeout          # no call waits longer than this (the public sign-up's own bound)
        self.retry_wait_cap = retry_wait_cap    # a "too many requests" asking for a longer wait is reported, not waited
        self.user_agent = user_agent
        self.on_wait = on_wait if on_wait is not None else _say_wait
        self.jar = http.cookiejar.CookieJar()
        self.opener = net.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.signed_in = False

    def call(self, method, path, body=None, auth=None, timeout=None, raw=False):
        """One request. A short "too many requests" (stock AIOStreams allows 5 config API calls per 5 seconds) is
        waited out and retried; a long one (10 new configs per hour) is reported."""
        for attempt in range(6):
            status, payload, wait = self._call(method, path, body, auth, timeout, raw)
            if status != 429 or wait is None or wait > self.retry_wait_cap or attempt == 5:
                return status, payload
            if attempt == 0:
                self.on_wait(wait)
            time.sleep(wait)
        return status, payload

    def _call(self, method, path, body, auth, timeout, raw):
        headers = {'User-Agent': self.user_agent, 'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        if auth:
            headers['Authorization'] = 'Basic ' + base64.b64encode(f'{auth[0]}:{auth[1]}'.encode()).decode()
        url = path if path.startswith('http') else self.base + path
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            realdebrid.check(url)
            net.check_scheme(url)
            wait_at_most = timeout or self.timeout
            if self.max_timeout:
                wait_at_most = min(wait_at_most, self.max_timeout)
            with self.opener.open(req, timeout=wait_at_most) as r:
                payload = r.read()
                return r.status, payload if raw else (json.loads(payload) if payload[:1] in (b'{', b'[') else {}), None
        except urllib.error.HTTPError as e:
            payload = e.read() or b''
            wait = None
            if e.code == 429:
                header = e.headers.get('Retry-After') or e.headers.get('RateLimit-Reset') or ''
                wait = int(header) if header.strip().isdigit() else 6
            try:
                return e.code, json.loads(payload), wait
            except ValueError:
                return e.code, {'error': {'code': 'RATE_LIMIT_EXCEEDED' if e.code == 429 else None,
                                          'message': payload[:300].decode('utf-8', 'replace')}}, wait
        except realdebrid.Blocked:
            raise AiosError('Stopped a request to Real-Debrid: AIOSubtitles never uses Real-Debrid.',
                            'Give the address of your AIOStreams, not of Real-Debrid.') from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AiosError(f'Can\'t reach AIOStreams at {self.base} ({getattr(e, "reason", e)}).',
                            'Check the address, and that AIOStreams is running (docker ps).') from None

    @staticmethod
    def _error(status, body, doing):
        err = (body or {}).get('error') or {}
        if isinstance(err, str):
            err = {'message': err}
        message = _plain(err.get('code'), err.get('message') or (body or {}).get('detail'))
        return AiosError(f'{doing}: {message}', code=err.get('code') or status)

    # ------------------------------------------------------------------------------------------- reads

    def status(self):
        status, body = self.call('GET', '/api/v1/status', timeout=15)
        data = (body or {}).get('data') if isinstance(body, dict) else None
        if status != 200 or not data or 'version' not in data:
            raise AiosError(f'{self.base} answered, but not like AIOStreams (HTTP {status}).',
                            'Give the address you open AIOStreams\' configure page at.')
        return data

    def config_exists(self, uuid):
        """Whether a config with this ID exists, asked without its password (stock AIOStreams answers HEAD
        /api/v1/user?uuid= with 200 or 400). None when this AIOStreams can't tell (another answer)."""
        status, _ = self.call('HEAD', '/api/v1/user?' + urllib.parse.urlencode({'uuid': uuid}), timeout=15, raw=True)
        return {200: True, 400: False}.get(status)

    def get_config(self, uuid, password):
        status, body = self.call('GET', '/api/v1/user', auth=(uuid, password))
        if status != 200 or not (body or {}).get('success'):
            raise self._error(status, body, 'Reading the config')
        data = body['data']
        return data['userData'], data['encryptedPassword']

    # ------------------------------------------------------------------------------------------- writes

    def save_config(self, uuid, password, config):
        status, body = self.call('PUT', '/api/v1/user', {'config': config}, auth=(uuid, password), timeout=90)
        if status != 200 or not (body or {}).get('success'):
            raise self._error(status, body, 'Saving the config')

    def create_config(self, config, password):
        status, body = self.call('POST', '/api/v1/user', {'config': config, 'password': password}, timeout=90)
        if status not in (200, 201) or not (body or {}).get('success'):
            raise self._error(status, body, 'Creating the sync config')
        return body['data']['uuid'], body['data']['encryptedPassword']

    def delete_config(self, uuid, password):
        status, body = self.call('DELETE', '/api/v1/user', auth=(uuid, password))
        if status != 200 or not (body or {}).get('success'):
            raise self._error(status, body, 'Removing the sync config')

    def sign_in(self, username, password):
        """AIOStreams' own sign-in (its web page's login). Returns whether that account is an admin."""
        status, body = self.call('POST', '/api/v1/auth/login', {'username': username, 'password': password})
        if status != 200:
            raise self._error(status, body, 'Signing in')
        self.signed_in = True
        return bool(((body or {}).get('data') or {}).get('isAdmin', False))

    def subtitle_cache_setting(self):
        """(key, value, locked) for the per-addon subtitle cache, or None if this AIOStreams has no such setting."""
        status, body = self.call('GET', '/api/v1/dashboard/settings')
        if status != 200:
            raise self._error(status, body, 'Reading AIOStreams\' settings')
        data = (body or {}).get('data') or {}
        items = data.get('settings') or data.get('keys') or data
        if isinstance(items, dict):
            items = [dict(v, key=k) if isinstance(v, dict) else {'key': k, 'value': v} for k, v in items.items()]
        for item in items if isinstance(items, list) else []:
            key = str(item.get('key', ''))
            if item.get('env') == 'SUBTITLE_CACHE_TTL' or key.endswith('subtitle.ttl'):
                return key, item.get('value'), item.get('source') == 'environment'
        return None

    def set_setting(self, key, value):
        status, body = self.call('PATCH', '/api/v1/dashboard/settings', {key: value})
        if status != 200 or (isinstance(body, dict) and body.get('success') is False):
            raise self._error(status, body, 'Changing the subtitle cache setting')

    # ------------------------------------------------------------------------------------------- addon routes

    def addon_get(self, manifest_base, path, timeout=60):
        status, body = self.call('GET', manifest_base + path, timeout=timeout)
        if status != 200:
            raise AiosError(f'AIOStreams answered HTTP {status} for {path.split("/")[1]}.')
        return body
