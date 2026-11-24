"""The AIOStreams side shared by ./setup and the public sign-up: the rules that build subsync's own sync config from a
viewer's config (no Usenet, no Real-Debrid, no subsync, nothing that starts work in the background), the rules that
place subsync's addon in a viewer's config, and the few AIOStreams API calls both make. Pure functions apart from the
client, which opens every request through the request gate (net.py, realdebrid.py).

Standard library only, so ./setup imports it too.
"""
import base64
import copy
import http.client
import http.cookiejar
import json
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import digits, langs, net, realdebrid
from .version import user_agent


def _say_wait(seconds):
    print(f'  ..  AIOStreams asks to slow down; waiting {seconds} s', file=sys.stderr, flush=True)


SUBSYNC_PRESET_NAME = 'subsync'
# How long AIOStreams waits for the addons this module adds, in ms as a config writes it. subsync answers a
# subtitle list from what it has (with pending entries on, after at most SUBSYNC_PENDING_WAIT_MS, 1 s by default), so
# one that doesn't answer holds the viewer's subtitle list back 2 s at most; OpenSubtitles gets AIOStreams' own default
SUBSYNC_ADDON_TIMEOUT_MS = 2000
OPENSUBTITLES_TIMEOUT_MS = 7000

# AIOStreams preset types that give subtitles (v2.37.0). A custom addon gives subtitles when its resources say so.
SUBTITLE_PRESETS = {'opensubtitles', 'opensubtitles-v3-plus', 'subsource', 'subdl', 'subhero', 'aiosubtitle'}
# Usenet sources: left out of the sync config, so subsync's listings never spend an indexer's daily API calls.
USENET_PRESETS = {'newznab', 'nzbhydra', 'easynews', 'easynews-search', 'easynewsPlus', 'easynewsPlusPlus',
                  'usenet-streamer', 'streamnzb', 'davex'}
USENET_SERVICES = {'stremio_nntp', 'nzbdav', 'aiostreams', 'altmount', 'easynews', 'stremthru_newz'}
# Addons whose own Sources choice decides whether they search Usenet indexers beside torrent ones (AIOStreams' Prowlarr
# searches both, whatever its services, unless that choice leaves one out): (its torrent value, its Usenet value)
SOURCES_CHOICE = {'prowlarr': ('torrent', 'usenet')}

# Usenet, when a person turns it on: a second config, the Usenet config, with only their Usenet searches
# and the Usenet services that serve the requested part of a file: AIOStreams' own Usenet streaming, NzbDAV and AltMount
# (only through AIOStreams' own proxy: without it their links need the WebDAV password, and often point into a home
# network), Easynews, and TorBox for the Usenet copies it has ready. Never Stremio's NNTP streams (the player reads
# Usenet itself; there is no link to read) and never a separate Usenet addon.
USENET_READABLE = ('aiostreams', 'nzbdav', 'altmount', 'easynews', 'torbox')   # also the order they are named in
USENET_NAMES = {'aiostreams': 'AIOStreams\' own Usenet streaming', 'nzbdav': 'NzbDAV', 'altmount': 'AltMount',
                'easynews': 'Easynews', 'torbox': 'TorBox Usenet'}
PROXIED_ONLY = ('nzbdav', 'altmount')
# The Usenet config's addons and, of the services above, the ones each can use (AIOStreams v2.35.8's presets)
USENET_ADDONS = {'newznab': {'torbox', 'nzbdav', 'altmount', 'aiostreams'},
                 'nzbhydra': {'torbox', 'nzbdav', 'altmount', 'aiostreams'},
                 'prowlarr': {'torbox', 'nzbdav', 'altmount', 'aiostreams'},
                 'easynews-search': {'easynews'}}
# On top of SYNC_SETTINGS: only ready copies (a TorBox release it would first download is never listed), and an indexer's
# error shown as a row of the listing, so AIOSubtitles stops asking that person's indexers for the day
USENET_SETTINGS = {'excludeUncached': True, 'hideErrors': False}
USENET_DROPPED = ('excludeUncachedFromAddons', 'excludeUncachedFromServices', 'excludeUncachedFromStreamTypes',
                  'excludeUncachedMode')
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

# The languages offered, as ISO 639-2/B codes (what subsync and Matroska use), in the order they are shown; their names
# and ISO 639-1 codes come from langs.py
OFFERED = ('eng', 'spa', 'fre', 'ger', 'ita', 'por', 'rus', 'ukr', 'pol', 'dut', 'swe', 'nor', 'dan', 'fin', 'cze',
           'hun', 'rum', 'bul', 'hrv', 'srp', 'gre', 'tur', 'ara', 'heb', 'per', 'hin', 'ind', 'tha', 'vie', 'jpn',
           'kor', 'chi')
LANGUAGES = [(langs.NAMES[three], langs.lang2(three), three) for three in OFFERED]   # (name, ISO 639-1, ISO 639-2/B)
# (name, code) pairs: what the language slots offer (ui.language_slots), on the site's pages and the setup page
LANGUAGE_OPTIONS = [(name, three) for name, _, three in LANGUAGES]
# what parse_languages understands: each language's name (any case) and codes, ISO 639-2's terminologic codes (deu,
# fra, …) and "farsi"
_BY = {key: lang for lang in LANGUAGES for key in (lang[0].lower(), lang[1], lang[2])}
_BY.update({code: _BY[three] for code, three in langs.TERMINOLOGIC.items() if three in OFFERED})
_BY['farsi'] = _BY['per']
# an AIOStreams config ID (searched for in setup's pasted text; whole, with fullmatch, in a link's path)
UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)


# The most languages a person may choose: the ceiling of SUBSYNC_MAX_LANGUAGES and what the setup
# keeps. The pages name a place for each (ui.ORDINALS); the texts say it in words (LIMIT_WORDS, explain.NUMBER_WORDS)
MAX_LANGUAGES = 4
LIMIT_WORDS = {1: 'One', 2: 'Two', 3: 'Three', 4: 'Four'}


def parse_languages(text, limit=MAX_LANGUAGES):
    """'English, ru' -> ([('English','en','eng'), ('Russian','ru','rus')], None) or ([], 'what is wrong'). At most
    `limit` languages (1 to MAX_LANGUAGES: the public instance's SUBSYNC_MAX_LANGUAGES; the setup keeps the most)."""
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
    if len(found) > limit:
        if limit == 1:
            return [], 'One language at most.'
        return [], f'{LIMIT_WORDS[limit]} languages at most (each one is a separate sync for every file).'
    return found, None


def language_names(langs):
    return ', '.join(name for name, _, _ in langs)


def names_of_codes(codes):
    """The names of the languages these codes stand for, in their order; a code it doesn't know is left out."""
    return [_BY[code][0] for code in codes if code in _BY]


def config_id_from(text):
    """A config ID typed as is, or found in a pasted Stremio/Jellyfin link (…/stremio/<id>/…)."""
    m = UUID_RE.search(text or '')
    return m.group(0).lower() if m else None


MAX_ALIAS = 200   # longer than any alias AIOStreams makes (a person's own: 64 characters)


def alias_from(text):
    """The alias of a pasted alias link (…/stremio/u/<alias>/…), as written: AIOStreams looks an instance-wide alias up
    as it is, case included. None for anything else, an empty or overlong alias included."""
    path = urllib.parse.urlsplit((text or '').strip()).path
    _, sep, rest = path.partition('/stremio/u/')
    alias = urllib.parse.unquote(rest.split('/')[0]) if sep else ''
    return alias if alias and len(alias) <= MAX_ALIAS and not re.search(r'[\x00-\x1f\x7f]', alias) else None


def install_link(instance, uuid, encrypted):
    """A config's install link. It holds the config's encrypted password: never logged or shown."""
    return f'{instance}/stremio/{uuid}/{encrypted}/manifest.json'


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
    """Unset options take the preset's own defaults, as AIOStreams' configure page would fill them in. The list comes
    from AIOStreams' status answer: options of another shape (not a list, an option without a text id) are passed
    over."""
    options = ((metadata or {}).get(preset['type']) or {}).get('OPTIONS')
    for option in options if isinstance(options, list) else []:
        if isinstance(option, dict) and isinstance(option.get('id'), str) and option.get('default') is not None:
            preset['options'].setdefault(option['id'], option['default'])
    return preset


def subsync_preset(config, manifest_url, metadata=None, name=SUBSYNC_PRESET_NAME):
    return with_defaults({'type': 'custom', 'instanceId': free_instance_id(config, 'ssy'), 'enabled': True,
                          'options': {'name': name, 'manifestUrl': manifest_url,
                                      'resources': ['subtitles'], 'timeout': SUBSYNC_ADDON_TIMEOUT_MS}}, metadata)


def opensubtitles_preset(config, metadata=None):
    """Stremio's official OpenSubtitles (v3): no account, no language setting (it lists every language). The
    community OpenSubtitles V3+ answered in 16-23 s on 2026-11-09, past AIOStreams' 7 s default."""
    return with_defaults({'type': 'opensubtitles', 'instanceId': free_instance_id(config, 'ost'), 'enabled': True,
                          'options': {'name': 'OpenSubtitles', 'timeout': OPENSUBTITLES_TIMEOUT_MS,
                                      'resources': ['subtitles']}}, metadata)


def _before_other_subtitles(presets, preset):
    """presets with `preset` placed before the first other subtitle source (at the end when there is none).
    AIOStreams lists subtitles in the order of the config, and players keep only the first few per language
    (AIOMetadata: 3), so subsync's entries must come first to be seen."""
    for i, p in enumerate(presets):
        if is_subtitle_preset(p):
            return presets[:i] + [preset] + presets[i:]
    return presets + [preset]


def without_builtin_twins(config, presets):
    """(config, names): the config without the subtitle addons of these preset types, those AIOSubtitles' public
    service asks itself (extra.cleanable_presets, of srclimits.CLEANABLE_PRESETS: Stremio's OpenSubtitles,
    OpenSubtitles V3+, SubDL, SubSource), and their names."""
    new = copy.deepcopy(config)
    kept, gone = [], []
    for p in new.get('presets') or []:
        if isinstance(p, dict) and p.get('type') in presets:
            gone.append((p.get('options') or {}).get('name') or p['type'])
        else:
            kept.append(p)
    new['presets'] = kept
    return new, gone


def viewer_change(config, manifest_url, metadata=None, add_opensubtitles=False, name=SUBSYNC_PRESET_NAME,
                  clean_up=()):
    """The viewer's config with subsync's addon (an older subsync entry replaced) placed before the other subtitle
    sources and, if asked, OpenSubtitles after it; with `clean_up` (the preset types of the subtitle addons
    AIOSubtitles asks itself, extra.cleanable_presets) without those addons. Returns (new config, [plain-language
    lines]); no lines means nothing to change."""
    removed = []
    if clean_up:
        config, removed = without_builtin_twins(config, clean_up)
    new = copy.deepcopy(config)
    presets = new.setdefault('presets', [])
    names = (name.lower(),)
    current = [p for p in presets if is_subsync_preset(p, manifest_url, names)]
    others = [p for p in presets if not is_subsync_preset(p, manifest_url, names)]
    changes = []
    if len(current) == 1 and current[0]['options'].get('manifestUrl') == manifest_url and current[0].get('enabled', True):
        # in place when no other subtitle source comes before it: only the subtitle sources' order matters, so stream
        # addons around it change nothing
        if any(is_subtitle_preset(p) for p in presets[:presets.index(current[0])]):
            new['presets'] = _before_other_subtitles(others, current[0])
            changes.append(f'moves "{name}" before your other subtitle sources (apps that list only a few subtitles '
                           'per language keep its entries)')
    else:
        new['presets'] = others
        new['presets'] = _before_other_subtitles(others, subsync_preset(new, manifest_url, metadata, name))
        same = [p for p in current if p['options'].get('manifestUrl') == manifest_url]
        if not current:
            changes.append(f'adds 1 subtitle source: {name} (synced copies of the subtitles you already get)')
        elif not same:
            changes.append(f'updates the subtitle source "{name}" (new address)')
        else:   # its address was there already: say what really changes
            if not any(p.get('enabled', True) for p in same):
                changes.append(f'switches the subtitle source "{name}" back on')
            if len(current) > 1:
                extra = len(current) - 1
                changes.append(f'removes {extra} extra entr{"y" if extra == 1 else "ies"} of the subtitle source '
                               f'"{name}"')
    if add_opensubtitles:
        new['presets'].append(opensubtitles_preset(new, metadata))
        changes.append('adds 1 subtitle source: OpenSubtitles (Stremio\'s official one; it lists every language)')
    if removed:
        changes.append(f'removes {len(removed)} subtitle addon{"" if len(removed) == 1 else "s"} {name} now searches '
                       f'itself: ' + ", ".join(removed))
    return new, changes


def without_subsync(config, manifest_url=None, names=(SUBSYNC_PRESET_NAME,)):
    """The viewer's config with subsync's addon taken out (./setup --uninstall; the public "delete my data")."""
    new = copy.deepcopy(config)
    before = len(new.get('presets') or [])
    new['presets'] = [p for p in new.get('presets') or [] if not is_subsync_preset(p, manifest_url, names)]
    return new, before - len(new['presets'])


def _within_services(config):
    """The config's addons, each addon's own Services choice cut down to the services still on the config's list, and
    an addon left with none left out. AIOStreams refuses a config whose addon names a service it doesn't have, and an
    empty choice too; the sync config leaves services out (Usenet, Real-Debrid), so their names come out of
    each addon as well. An addon whose choice names only services that are there is left as it is."""
    have = {s.get('id') for s in config.get('services') or []}
    kept = []
    for preset in config.get('presets') or []:
        chosen = (preset.get('options') or {}).get('services')
        if isinstance(chosen, list):
            rest = [s for s in chosen if s in have]
            if rest != chosen:
                if not rest:
                    continue
                preset['options']['services'] = rest
        kept.append(preset)
    return kept


def _one_kind(presets, kind):
    """The addons, each one with its own Sources choice (SOURCES_CHOICE: Prowlarr) set to search `kind` only ('torrent'
    in the sync config, 'usenet' in the Usenet config), and one set to the other kind only left out."""
    kept = []
    for preset in presets:
        values = SOURCES_CHOICE.get(preset.get('type'))
        if values:
            wanted = values[0] if kind == 'torrent' else values[1]
            options = preset.setdefault('options', {})
            chosen = options.get('sources')
            if isinstance(chosen, list) and chosen and wanted not in [str(c).lower() for c in chosen]:
                continue
            options['sources'] = [wanted]
        kept.append(preset)
    return kept


def _with_sync_settings(new):
    for key, value in SYNC_SETTINGS.items():
        if isinstance(value, dict):
            new[key] = {**(new.get(key) or {}), **value}
        else:
            new[key] = value
    for key in ('uuid', 'accessKey', 'precacheSelector', 'precacheSingleStream'):
        new.pop(key, None)
    return new


def sync_config(viewer, manifest_url=None, names=(SUBSYNC_PRESET_NAME,)):
    """The config subsync finds files through: the viewer's sources (so it finds the file being watched) and subtitle
    sources (so it syncs what the viewer sees), without subsync itself, without Usenet (no Usenet addon or service, and
    Prowlarr set to its torrent indexers: _one_kind), without any way to Real-Debrid (realdebrid.without_real_debrid),
    with no addon naming a service it left out (_within_services), and with nothing that resolves or queues in the
    background."""
    new = copy.deepcopy(viewer)
    new['presets'] = [p for p in new.get('presets') or []
                      if not is_subsync_preset(p, manifest_url, names) and p.get('type') not in USENET_PRESETS]
    new['services'] = [s for s in new.get('services') or [] if s.get('id') not in USENET_SERVICES]
    new, _ = realdebrid.without_real_debrid(new)
    new['presets'] = _one_kind(_within_services(new), 'torrent')
    return _with_sync_settings(new)


def _readable_services(services):
    """The Usenet services of a service list that AIOSubtitles reads through (USENET_READABLE), switched on; NzbDAV and
    AltMount only through AIOStreams' own proxy (their credentials hold the address AIOStreams gave them)."""
    kept = []
    for s in services or []:
        if s.get('id') not in USENET_READABLE or not s.get('enabled', True):
            continue
        if s.get('id') in PROXIED_ONLY and not ((s.get('credentials') or {}).get('aiostreamsAuth')):
            continue
        kept.append(s)
    return kept


def _usable(config):
    """The config's addons without one that has no service it can use on the config's list (AIOStreams refuses a config
    where Newznab, Prowlarr or Easynews Search has none, whatever their own choice says)."""
    have = {s.get('id') for s in config.get('services') or []}
    kept = []
    for preset in config.get('presets') or []:
        can = USENET_ADDONS.get(preset.get('type'))
        if can is not None:
            chosen = (preset.get('options') or {}).get('services')
            if not (set(chosen) if isinstance(chosen, list) else have) & have & can:
                continue
        kept.append(preset)
    return kept


def usenet_sync_config(viewer, manifest_url=None, names=(SUBSYNC_PRESET_NAME,), subtitles=False):
    """The Usenet config: the viewer's Usenet searches (USENET_ADDONS, Prowlarr set to its Usenet
    indexers) and the Usenet services AIOSubtitles reads through (_readable_services), nothing else; with `subtitles`
    also the viewer's subtitle sources, for a setup that has no sync config. Made by the sync config's steps
    (no way to Real-Debrid, each addon's own Services choice cut to this list, _within_services), plus: an addon with no
    service it can use left out (_usable); only ready copies listed and errors shown (USENET_SETTINGS)."""
    new = copy.deepcopy(viewer)
    new['presets'] = [p for p in new.get('presets') or [] if not is_subsync_preset(p, manifest_url, names)
                      and (p.get('type') in USENET_ADDONS or (subtitles and is_subtitle_preset(p)))]
    new['services'] = _readable_services(new.get('services'))
    new, _ = realdebrid.without_real_debrid(new)
    new['presets'] = _one_kind(_within_services(new), 'usenet')
    new['presets'] = _usable(new)
    new = _with_sync_settings(new)
    new.update(USENET_SETTINGS)
    for key in USENET_DROPPED:
        new.pop(key, None)
    if isinstance(new.get('hideErrorsForResources'), list):
        new['hideErrorsForResources'] = [r for r in new['hideErrorsForResources'] if r != 'stream']
    return new


def usenet_route(viewer, manifest_url=None, names=(SUBSYNC_PRESET_NAME,)):
    """(services, why): the Usenet services the viewer's Usenet files would be read through, in USENET_READABLE's order
    (TorBox only when a kept Usenet search can use it), and None; or ([], why there are none): 'proxy' (NzbDAV or
    AltMount not through AIOStreams' proxy), 'nntp' (only Stremio's NNTP streams) or 'none' (no Usenet
    search AIOSubtitles uses)."""
    usenet = usenet_sync_config(viewer, manifest_url, names)
    have = {s.get('id') for s in usenet['services']}
    used = set()
    for preset in usenet['presets']:
        chosen = (preset.get('options') or {}).get('services')
        used |= (set(chosen) if isinstance(chosen, list) else have) & have & USENET_ADDONS[preset['type']]
    if used:
        return [s for s in USENET_READABLE if s in used], None
    theirs = {s.get('id') for s in viewer.get('services') or [] if s.get('enabled', True)}
    if theirs & set(PROXIED_ONLY):
        return [], 'proxy'
    if 'stremio_nntp' in theirs:
        return [], 'nntp'
    return [], 'none'


def usenet_names(services):
    """'NzbDAV', 'NzbDAV and TorBox Usenet', 'AIOStreams' own Usenet streaming, NzbDAV and TorBox Usenet'."""
    names = [USENET_NAMES[s] for s in services if s in USENET_NAMES]
    return names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1] if names else ''


# Where Usenet files aren't read: said only by the public sign-up and its daily check (./setup reads Usenet files too)
NO_SERVICE = 'no debrid service (AIOSubtitles reads files from debrid services only, not Usenet or torrents)'
# Where Usenet files are read too
NO_SERVICE_USENET = ('no debrid or Usenet service AIOSubtitles can read (it reads ready debrid files, and Usenet files '
                     'through services that stream part of a file; not torrents, not Stremio\'s NNTP streams)')


def sync_config_problems(sync, usenet=False):
    """Why the sync config can't find any file subsync could read (empty = it can). `usenet`: Usenet files are read too
    where this is said."""
    problems = []
    if not realdebrid.debrid_in_use(sync):
        problems.append(NO_SERVICE_USENET if usenet else NO_SERVICE)
    if not stream_sources(sync):
        problems.append('no stream source other than Usenet ones')
    return problems


# ------------------------------------------------------------------------------------------- the AIOStreams API

UA = user_agent('setup')   # starts with "AIO": stock AIOStreams then includes file details in stream listings

# The client's fixed values: waits in seconds, sizes in bytes
CALL_TIMEOUT = 30            # a call's wait when neither the call nor the client names its own
QUICK_TIMEOUT = 15           # the status, whether a config exists, an alias: small answers given at once
WRITE_TIMEOUT = 90           # saving or making a config (AIOStreams checks every addon in it)
ADDON_TIMEOUT = 60           # an addon route of a config: its manifest, a stream or subtitle listing
SETUP_CHECK_TIMEOUT = 40     # ./setup's last check: a subtitle listing through the person's config, the Usenet config's
                             # manifest
SETUP_SEARCH_TIMEOUT = 90    # ./setup's last check: a stream listing through the sync config, whose search of every
                             # source and cache check take longest
TRIES = 6                    # tries at a call AIOStreams asks to slow down
SLOW_DOWN_WAIT = 6           # the wait for a "too many requests" that names none (stock AIOStreams: a 5-second window)
RETRY_WAIT_CAP = 60          # the longest wait asked for that is waited out; a longer one is reported
PUBLIC_RETRY_WAIT_CAP = 5    # the public sign-up's (a person is waiting on the page)
PUBLIC_TIMEOUT = 30          # public mode's longest wait for any call, from its start to its whole answer: the sign-up,
                             # the settings page and the daily check (signup.public_api)
MAX_ANSWER = 16 * 1024 * 1024   # the largest AIOStreams answer read (a config, a stream or subtitle listing)
MAX_REFUSAL = 64 * 1024      # the most of an alias lookup's refusal read
ERROR_EXCERPT = 300          # the most kept of AIOStreams' own words for an error, in characters (_one_line)
READ_SIZE = 64 * 1024        # an answer is read this much at a time, at most

BAD_ANSWER = 'BAD_ANSWER'    # AiosError.code: an answer AIOStreams doesn't give (not JSON of the call's shape, too
                             # large, a redirect of a call that carries a password)
UNREADABLE = 'UNREADABLE'    # AiosError.code: an answer that came but isn't whole JSON (cut short by a proxy, not
                             # UTF-8): AIOStreams was reached
UNREACHABLE = 'UNREACHABLE'  # AiosError.code: no answer at all (a name not found, a connection refused or timed out, a
                             # request the address rules stop): "Can't reach AIOStreams at …"
TOO_SLOW = 'TOO_SLOW'        # AiosError.code: asked, but its whole answer didn't come within the call's wait:
                             # AIOStreams was reached, so it isn't UNREACHABLE
NOT_AIOSTREAMS = 'NOT_AIOSTREAMS'   # AiosError.code: something answered the status call, but not as AIOStreams does
REAL_DEBRID = 'REAL_DEBRID'  # AiosError.code: a request to Real-Debrid, stopped before it was sent
ALIAS_UNKNOWN = 'ALIAS_UNKNOWN'      # AiosError.code: this AIOStreams doesn't know an alias (alias_config_id)
ALIAS_NO_CONFIG = 'ALIAS_NO_CONFIG'  # AiosError.code: its answer for an alias holds no config ID on this AIOStreams
# A "too many requests" of any call but making a config: AIOStreams' short limit (5 calls in 5 seconds)
LIMITED = 'AIOStreams is limiting requests from this machine; try again in a few minutes.'


class AiosError(Exception):
    """`code`: AIOStreams' own error code (or the HTTP status of a refusal without one), or aiosconfig's for the errors
    it finds itself (UNREACHABLE, TOO_SLOW, NOT_AIOSTREAMS, BAD_ANSWER, UNREADABLE, REAL_DEBRID, ALIAS_UNKNOWN,
    ALIAS_NO_CONFIG). `kind`: what an AIOStreams refusal means as aiosconfig read it (_reading; None for any other
    error). Callers decide by these two, never by the message's words, which may quote AIOStreams. `detail`:
    AIOStreams' own words for a refusal, when it gave some, wherever in its answer (shown by the public sign-up for a
    refused config,). `answer`: for a refusal without words, what names it: its code, or
    "HTTP <status>" for an answer read for its status alone; None otherwise. `reason`: the message
    without the step it names, for the public pages, which name their own."""

    def __init__(self, message, fix=None, code=None, detail=None, reason=None, kind=None, answer=None):
        super().__init__(message)
        self.message, self.fix, self.code, self.detail, self.kind = message, fix, code, detail, kind
        self.answer = answer
        self.reason = reason or message


ALIAS_REDIRECTS = (301, 302, 303, 307, 308)
# AIOStreams' codes for a config ID and password that open no config: a wrong password, or a config that is gone
WRONG_PASSWORD_CODES = ('USER_INVALID_DETAILS', 'USER_INVALID_PASSWORD', 'USER_NOT_FOUND')
CODE_WORD = re.compile(r'[A-Z][A-Z0-9_]{0,63}')   # an error code as AIOStreams and aiosconfig write them

# What an AIOStreams refusal means (AiosError.kind), read once from its code, its own words and its HTTP status: for
# the sentence the error gets (_plain) and for the callers that act on it (needs_sign_in, is_limited)
NO_CONFIG = 'NO_CONFIG'              # the config ID and password open no config: a wrong password, or a config gone
CONFIG_REFUSED = 'CONFIG_REFUSED'    # AIOStreams refused the config itself (its reason: AiosError.detail)
SIGN_IN = 'SIGN_IN'                  # it makes and saves configs only for a signed-in account
LIMITING = 'LIMITING'                # it is limiting requests from this machine


def _reading(code, message, status=None):
    """The kind of an AIOStreams refusal, from its code (`code`), its own words (`message`) and its HTTP status, or
    None: a wrong password or a refused config by its code alone, whatever the words; a limit by its status (429, "too
    many requests") or its code, whatever the words, else by its words; a sign-in by its code or its words."""
    code, words = str(code or ''), (message or '').lower()
    if code in WRONG_PASSWORD_CODES:
        return NO_CONFIG
    if code == 'USER_INVALID_CONFIG':
        return CONFIG_REFUSED
    if status == 429 or code == 'RATE_LIMIT_EXCEEDED':
        return LIMITING
    if code in ('FORBIDDEN', 'UNAUTHORIZED') or 'not allowed' in words:
        return SIGN_IN
    if 'too many' in words:
        return LIMITING
    return None


def needs_sign_in(e):
    """Whether an AiosError is AIOStreams asking for a signed-in account, as its message then says: told by AIOStreams'
    code and own words, never by the message's wording."""
    return e.kind == SIGN_IN


def is_limited(e):
    """Whether an AiosError is AIOStreams limiting requests as aiosconfig read it, its message then saying so: told by
    AIOStreams' code and own words, never by the message's wording."""
    return e.kind == LIMITING


def _plain(code, message, making=False, status=None):
    """AIOStreams error codes, in plain words. `making`: the call made a config, which AIOStreams limits per hour; its
    limit on every other call lasts seconds. `status`: the refusal's HTTP status (_reading)."""
    kind = _reading(code, message, status)
    if kind == NO_CONFIG:
        return 'That config ID and password don\'t match a config on this AIOStreams.'
    if kind == CONFIG_REFUSED:
        return f'AIOStreams refused the config: {message}'
    if kind == SIGN_IN:
        return 'This AIOStreams asks you to sign in before configs can be saved.'
    if kind == LIMITING:
        if making:
            return ('AIOStreams is limiting requests from this machine (it allows 10 new configs an hour); try again '
                    'later.')
        return LIMITED
    return message or str(code or '') or 'AIOStreams gave an error without a message.'


_BREAKS = re.compile(r'[\x00-\x1f\x7f-\x9f\u2028\u2029]+')   # control characters and line separators


def _one_line(words):
    """AIOStreams' own words for an error as one short line: each run of control characters or line
    separators one space, at most ERROR_EXCERPT characters, so quoting them in the log, a record or a page never
    starts a line of its own nor carries megabytes. None stays None."""
    if words is None:
        return None
    words = _BREAKS.sub(' ', words).strip()
    return words if len(words) <= ERROR_EXCERPT else words[:ERROR_EXCERPT - 1] + '…'


def _loads(data):
    """json.loads, with an answer nested too deep to decode (RecursionError) refused as JSON it can't read, a
    ValueError like any other."""
    try:
        return json.loads(data)
    except RecursionError:
        raise ValueError('JSON nested too deep') from None


def _error_code(body):
    """AIOStreams' error code in an answer's body ({"error": {"code": …}}), or None."""
    try:
        err = _loads(body).get('error')
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    return err.get('code') if isinstance(err, dict) else None


def _read(answer, limit):
    """An answer's body (a response, or an HTTPError carrying one), read a piece at a time and only until more than
    `limit` bytes are in (the caller refuses or cuts a longer one); the call's bound (net.Deadline) ends one that keeps
    coming. A body that ends before its Content-Length raises IncompleteRead."""
    parts, size = [], 0
    while size <= limit:
        part = answer.read1(READ_SIZE)
        if not part:
            missing = getattr(answer, 'length', None)
            if missing:
                raise http.client.IncompleteRead(b''.join(parts), missing)
            break
        parts.append(part)
        size += len(part)
    return b''.join(parts)


class AiosApi:
    """The AIOStreams API calls ./setup and the public sign-up make. Every request passes the request gate
    (net.build_opener: the Real-Debrid guard and, in public mode, the public-address check), and every failure, of the
    network or of an answer's shape, comes back as an AiosError."""

    def __init__(self, base, timeout=CALL_TIMEOUT, user_agent=UA, on_wait=None, max_timeout=None,
                 retry_wait_cap=RETRY_WAIT_CAP, tries=TRIES):
        self.base = base.rstrip('/')
        self.timeout = timeout
        # the longest any call waits (the public sign-up's own bound), from its start to its whole answer: its
        # connections, any redirect, its status line, headers and body (net.Deadline)
        self.max_timeout = max_timeout
        self.retry_wait_cap = retry_wait_cap    # a "too many requests" asking for a longer wait is reported, not waited
        self.tries = tries                      # tries at a call AIOStreams asks to slow down
        self.user_agent = user_agent
        self.on_wait = on_wait if on_wait is not None else _say_wait
        self.jar = http.cookiejar.CookieJar()
        self.opener = net.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        # for calls that carry a password: a redirect is never followed (urllib would send the password on to wherever
        # it points, plain http included)
        self.keyed_opener = net.build_opener(urllib.request.HTTPCookieProcessor(self.jar), net.NoRedirects())
        self.signed_in = False

    def call(self, method, path, body=None, auth=None, timeout=None, raw=False):
        """One request. A short "too many requests" (stock AIOStreams allows 5 config API calls per 5 seconds) is
        waited out and retried; a long one (10 new configs per hour) is reported."""
        for attempt in range(self.tries):
            status, payload, wait = self._call(method, path, body, auth, timeout, raw)
            if wait is None or wait > self.retry_wait_cap or attempt == self.tries - 1:
                return status, payload
            if attempt == 0:
                self.on_wait(wait)
            time.sleep(wait)

    def _wait_at_most(self, timeout=None):
        wait = timeout or self.timeout
        return min(wait, self.max_timeout) if self.max_timeout else wait

    def _call(self, method, path, body, auth, timeout, raw):
        """(status, answer, the wait a "too many requests" asks for or None); a refusal's answer is None when it isn't
        JSON (a proxy's error page): nothing of it is read but its status. The call's wait bounds the whole
        request (net.Deadline): a server that sends its answer a byte now and then fails it as too slow."""
        headers = {'User-Agent': self.user_agent, 'Accept': 'application/json'}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        if auth:
            headers['Authorization'] = 'Basic ' + base64.b64encode(f'{auth[0]}:{auth[1]}'.encode()).decode()
        url = path if path.startswith('http') else self.base + path
        wait_at_most = self._wait_at_most(timeout)
        bound = net.Deadline(wait_at_most)
        try:
            # made inside the try: urllib refuses an address without a scheme here, with a ValueError
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            with bound:
                try:
                    answer = realdebrid.urlopen(req, wait_at_most, opener=self.keyed_opener if auth else self.opener)
                except urllib.error.HTTPError as e:
                    answer = e          # a refusal is an answer too: read, and closed, the same way
                try:
                    if auth and 300 <= answer.status < 400:
                        raise self._bad_answer(answer.status)
                    payload = _read(answer, MAX_ANSWER)
                finally:
                    answer.close()
            if bound.passed:            # its connection was shut at the bound: what came may be cut short
                raise TimeoutError('timed out')
            if len(payload) > MAX_ANSWER:
                raise self._bad_answer(answer.status)
            if not isinstance(answer, urllib.error.HTTPError):
                return answer.status, payload if raw else self._json(payload), None
            wait = self._asked_wait(answer.headers) if answer.status == 429 else None
            try:
                return answer.status, _loads(payload), wait
            except ValueError:
                return answer.status, None, wait
        except realdebrid.Blocked:
            raise self._blocked() from None
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            raise self._no_answer(e, bound) from None

    def _no_answer(self, e, bound):
        """The AiosError for a request that got no whole answer (`bound`, the call's net.Deadline: whether it ended the
        request, and the connections made under it): a connection that failed (urllib's URLError: none made, or the
        request not sent) can't reach AIOStreams, unless the bound ended it after a connection was made (a redirect's
        next hop, after the first one answered): AIOStreams was reached, so it took too long, as a request sent whose
        answer didn't come within the call's wait; an answer cut short came, so AIOStreams was reached
."""
        if isinstance(e, urllib.error.URLError) and not (bound.passed and bound.made):
            return self._unreachable(TimeoutError('timed out') if bound.passed else e)
        if bound.passed or isinstance(e, TimeoutError):
            return AiosError(f'AIOStreams at {self.base} took too long to answer.', 'Try again in a few minutes.',
                             code=TOO_SLOW)
        if isinstance(e, http.client.IncompleteRead):
            return self._unreadable()
        return self._unreachable(e)

    def _asked_wait(self, headers):
        """The seconds a "too many requests" asks to wait (Retry-After, else RateLimit-Reset); SLOW_DOWN_WAIT, never
        more than this client's cap, when it names none, so a short limit is waited out on the public service too."""
        headers = headers or {}
        value = digits.whole((headers.get('Retry-After') or headers.get('RateLimit-Reset') or '').strip())
        return value if value is not None else min(SLOW_DOWN_WAIT, self.retry_wait_cap)

    def _blocked(self):
        return AiosError('Stopped a request to Real-Debrid: AIOSubtitles never uses Real-Debrid.',
                         'Give the address of your AIOStreams, not of Real-Debrid.', code=REAL_DEBRID)

    def _unreachable(self, e):
        # http.client's own errors are named by their kind only: some quote the address, whose path can hold a config's
        # encrypted password
        reason = type(e).__name__ if isinstance(e, http.client.HTTPException) else getattr(e, 'reason', e)
        return AiosError(f'Can\'t reach AIOStreams at {self.base} ({reason}).',
                         'Check the address, and that AIOStreams is running (docker ps).', code=UNREACHABLE)

    def _bad_answer(self, status):
        return AiosError(f'{self.base} answered in a way AIOSubtitles doesn\'t know (HTTP {status}).',
                         'Give the address you open AIOStreams\' configure page at.', code=BAD_ANSWER)

    def _json(self, payload):
        """A successful answer's JSON ({} for one that isn't JSON at all); one that starts as JSON but can't be read
        whole is UNREADABLE: it came, so AIOStreams was reached."""
        if payload[:1] not in (b'{', b'['):
            return {}
        try:
            return _loads(payload)
        except ValueError:      # cut short, not UTF-8, or nested too deep
            raise self._unreadable() from None

    def _unreadable(self):
        return AiosError(f'{self.base} answered, but its answer isn\'t the JSON AIOStreams sends (it may have been cut '
                         'short).',
                         'Try again; if it keeps happening, check the web server or proxy in front of AIOStreams.',
                         code=UNREADABLE)

    def _object(self, status, body):
        """An answer's JSON object ({} for an empty answer); any other answer is BAD_ANSWER."""
        if not body:
            return {}
        if not isinstance(body, dict):
            raise self._bad_answer(status)
        return body

    def _error(self, status, body, doing, making=False):
        """The AiosError for a call AIOStreams refused, in plain words (BAD_ANSWER for an answer that isn't
        AIOStreams'). `body` None: the answer is read for its HTTP status alone (one that isn't JSON, such as a proxy's
        error page,; a server error of the status call), so none of it is quoted. `making`: the call
        made a config (_plain)."""
        if body is None:
            answer, kind = f'HTTP {status}', _reading(None, None, status)
            message = _plain(None, None, making, status) if kind else f'AIOStreams answered with an error ({answer}).'
            return AiosError(f'{doing}: {message}', code=status, reason=message, kind=kind, answer=answer)
        if body and not isinstance(body, dict):
            return self._bad_answer(status)
        body = body or {}
        err = body.get('error') or {}
        if isinstance(err, str):
            err = {'message': err}
        if not isinstance(err, dict):
            return self._bad_answer(status)
        code, said, detail = err.get('code'), err.get('message'), body.get('detail')
        # AIOStreams' own words, in its error or beside it
        words = (_one_line(said) if isinstance(said, str) else None) or (
            _one_line(detail) if isinstance(detail, str) else None)
        message = _plain(code, words, making, status)
        return AiosError(f'{doing}: {message}', code=code or status, detail=words, reason=message,
                         kind=_reading(code, words, status),
                         answer=code if not words and isinstance(code, str) and CODE_WORD.fullmatch(code) else None)

    def _succeeded(self, status, body, doing, ok=(200,), making=False):
        """The answer of a call AIOStreams says succeeded; raises its refusal (_error) otherwise."""
        if status in ok and isinstance(body, dict) and body.get('success'):
            return body
        raise self._error(status, body, doing, making)

    # ------------------------------------------------------------------------------------------- reads

    def status(self):
        """AIOStreams' status answer's data (its version, its presets' settings). A limit or a server error is
        reported as one, by its HTTP status alone: an AIOStreams that is busy or restarting is at a right address
; any other answer that isn't AIOStreams' status is NOT_AIOSTREAMS."""
        status, body = self.call('GET', '/api/v1/status', timeout=QUICK_TIMEOUT)
        if status == 429 or 500 <= status <= 599:
            raise self._error(status, None, 'Reading AIOStreams\' status')
        data = body.get('data') if isinstance(body, dict) else None
        if status != 200 or not isinstance(data, dict) or 'version' not in data:
            raise AiosError(f'{self.base} answered, but not like AIOStreams (HTTP {status}).',
                            'Give the address you open AIOStreams\' configure page at.', code=NOT_AIOSTREAMS)
        return data

    def config_exists(self, uuid):
        """Whether a config with this ID exists, asked without its password (stock AIOStreams answers HEAD
        /api/v1/user?uuid= with 200 or 400). None when this AIOStreams can't tell (another answer)."""
        status, _ = self.call('HEAD', '/api/v1/user?' + urllib.parse.urlencode({'uuid': uuid}), timeout=QUICK_TIMEOUT,
                              raw=True)
        return {200: True, 400: False}.get(status)

    def alias_config_id(self, alias):
        """The config ID behind an alias link (…/stremio/u/<alias>/…): AIOStreams answers it, without a password, with
        a redirect to the config's full link. The redirect is read, never followed, and only the ID is kept: the rest of
        that link holds the config's encrypted password and is dropped at once (never logged, stored or shown). A short
        "too many requests" is waited out and asked once more, as `call` does. Raises AiosError: code
        ALIAS_UNKNOWN when this AIOStreams doesn't know the alias, ALIAS_NO_CONFIG when its answer holds no config ID on
        this AIOStreams, RATE_LIMIT_EXCEEDED while it limits requests, the HTTP status of its server error (500 to 599),
        and UNREACHABLE (the usual "Can't reach" message) when it can't be reached."""
        status, location, body, headers = self._ask_alias(alias)
        if status == 429 and (wait := self._asked_wait(headers)) <= self.retry_wait_cap:
            self.on_wait(wait)
            time.sleep(wait)
            status, location, body, headers = self._ask_alias(alias)
        if status in ALIAS_REDIRECTS and location:
            found = self._config_id_in(location)
            if found:
                return found
        elif status == 404 or (status == 400 and _error_code(body) == 'USER_INVALID_DETAILS'):
            raise AiosError('This AIOStreams doesn\'t know that alias.', code=ALIAS_UNKNOWN)
        elif status == 429:
            raise AiosError(LIMITED, code='RATE_LIMIT_EXCEEDED', kind=LIMITING)
        elif 500 <= status <= 599:
            raise AiosError(f'AIOStreams answered HTTP {status} for the alias link; try again in a few minutes.',
                            code=status)
        raise AiosError('This AIOStreams gave no config for that alias.', code=ALIAS_NO_CONFIG)

    def _ask_alias(self, alias):
        """One request for an alias, its redirect never followed: (status, the redirect's target, a refusal's body,
        the answer's headers)."""
        url = f'{self.base}/stremio/u/{urllib.parse.quote(alias, safe="")}/manifest.json'
        wait_at_most = min(self._wait_at_most(), QUICK_TIMEOUT)
        location, body = None, b''
        bound = net.Deadline(wait_at_most)
        try:
            req = urllib.request.Request(url, method='GET', headers={'User-Agent': self.user_agent,
                                                                     'Accept': 'application/json'})
            with bound:
                try:
                    with realdebrid.urlopen(req, wait_at_most, opener=net.build_opener(net.NoRedirects())) as r:
                        status, headers = r.status, r.headers
                        if bound.passed:    # shut at the bound mid-head: what came reads as a whole status line
                            raise TimeoutError('timed out')
                except urllib.error.HTTPError as e:
                    if bound.passed:        # the same for a refusal: its code never came whole
                        e.close()
                        raise TimeoutError('timed out') from None
                    status, headers = e.code, e.headers
                    location = headers.get('Location') if headers else None
                    # only a refusal's body is read: a redirect's would hold the encrypted password, and may never come
                    try:
                        body = _read(e, MAX_REFUSAL)[:MAX_REFUSAL] if status == 400 else b''
                    except (OSError, ValueError, http.client.HTTPException):
                        body = b''
                    finally:
                        e.close()
            if bound.passed:            # a refusal cut short at the bound: its reason never came whole
                body = b''
        except realdebrid.Blocked:
            raise self._blocked() from None
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            raise self._no_answer(e, bound) from None
        return status, location, body, headers

    def _config_id_in(self, location):
        """The config ID in a redirect's target (/stremio/<id>/<encrypted password>/…), only on this AIOStreams' own
        host (a relative target, or one naming that host); None otherwise."""
        target = urllib.parse.urlsplit(location)
        if target.netloc and (target.hostname or '').lower() != (urllib.parse.urlsplit(self.base).hostname or '').lower():
            return None
        _, sep, rest = target.path.partition('/stremio/')
        pieces = rest.split('/')
        if sep and len(pieces) >= 3 and UUID_RE.fullmatch(pieces[0]) and pieces[1]:
            return pieces[0].lower()
        return None

    def get_config(self, uuid, password):
        """(the config, its encrypted password)."""
        status, body = self.call('GET', '/api/v1/user', auth=(uuid, password))
        data = self._succeeded(status, body, 'Reading the config').get('data')
        if not (isinstance(data, dict) and isinstance(data.get('userData'), dict)
                and isinstance(data.get('encryptedPassword'), str)):
            raise self._bad_answer(status)
        return data['userData'], data['encryptedPassword']

    # ------------------------------------------------------------------------------------------- writes

    def save_config(self, uuid, password, config):
        status, body = self.call('PUT', '/api/v1/user', {'config': config}, auth=(uuid, password),
                                 timeout=WRITE_TIMEOUT)
        self._succeeded(status, body, 'Saving the config')

    def create_config(self, config, password):
        """(the new config's ID, its encrypted password)."""
        status, body = self.call('POST', '/api/v1/user', {'config': config, 'password': password},
                                 timeout=WRITE_TIMEOUT)
        data = self._succeeded(status, body, 'Creating the sync config', ok=(200, 201), making=True).get('data')
        if not (isinstance(data, dict) and isinstance(data.get('uuid'), str)
                and isinstance(data.get('encryptedPassword'), str)):
            raise self._bad_answer(status)
        return data['uuid'], data['encryptedPassword']

    def delete_config(self, uuid, password):
        status, body = self.call('DELETE', '/api/v1/user', auth=(uuid, password))
        self._succeeded(status, body, 'Removing the sync config')

    def sign_in(self, username, password):
        """AIOStreams' own sign-in (its web page's login). Returns whether that account is an admin."""
        status, body = self.call('POST', '/api/v1/auth/login', {'username': username, 'password': password})
        if status != 200:
            raise self._error(status, body, 'Signing in')
        self.signed_in = True
        return bool(self._object(status, self._object(status, body).get('data')).get('isAdmin', False))

    def subtitle_cache_setting(self):
        """(key, value, locked) for the per-addon subtitle cache, or None if this AIOStreams has no such setting."""
        status, body = self.call('GET', '/api/v1/dashboard/settings')
        if status != 200:
            raise self._error(status, body, 'Reading AIOStreams\' settings')
        data = self._object(status, self._object(status, body).get('data'))
        items = data.get('settings') or data.get('keys') or data
        if isinstance(items, dict):
            items = [dict(v, key=k) if isinstance(v, dict) else {'key': k, 'value': v} for k, v in items.items()]
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            key = str(item.get('key', ''))
            if item.get('env') == 'SUBTITLE_CACHE_TTL' or key.endswith('subtitle.ttl'):
                return key, item.get('value'), item.get('source') == 'environment'
        return None

    def set_setting(self, key, value):
        status, body = self.call('PATCH', '/api/v1/dashboard/settings', {key: value})
        if status != 200 or (isinstance(body, dict) and body.get('success') is False):
            raise self._error(status, body, 'Changing the subtitle cache setting')

    # ------------------------------------------------------------------------------------------- addon routes

    def addon_get(self, manifest_base, path, timeout=ADDON_TIMEOUT):
        """An addon route's JSON object (a manifest, a stream or subtitle listing; {} for an answer that isn't JSON);
        any other answer is BAD_ANSWER, a refusal has its HTTP status as its code."""
        status, body = self.call('GET', manifest_base + path, timeout=timeout)
        if status != 200:
            raise AiosError(f'AIOStreams answered HTTP {status} for {path.split("/")[1]}.', code=status)
        return self._object(status, body)
