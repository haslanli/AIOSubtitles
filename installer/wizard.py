#!/usr/bin/env python3
"""AIOSubtitles setup: adds subsync to the AIOStreams you already run. Start it with ./setup.

It installs one thing, the subsync service (in Docker, built on this machine), and makes two config changes in
AIOStreams, each shown to you first and saved only after you say yes:
  1. your config gets one more subtitle source, subsync (a backup copy is saved first);
  2. a second config, made from yours, that subsync uses to find files (no Usenet, no Real-Debrid, nothing in the
     background).
It doesn't install or change AIOStreams itself or AIOMetadata.

  ./setup                  install, or change languages and options (safe to run again)
  ./setup --status         is it running, and what has it done
  ./setup --uninstall      take subsync out of your config and remove it
  ./setup --restore FILE   put a saved copy of your config back
  ./setup --answers FILE [--yes]   answers from KEY=value lines (see docs/answers.example)
"""
import argparse
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from installer import docs, dockerenv, plan  # noqa: E402
from installer.aios import Aios, AiosError  # noqa: E402
from installer.ui import Asker, Stop, bad, box, note, ok, paint, say, step, stop, warn, wrap  # noqa: E402
from subsync import explain, realdebrid  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get('AIOSUBTITLES_STATE') or REPO / 'state')
VERSION = '1.0.0'
CONTAINER = 'aiosubtitles-subsync'
TEST_TITLES = [('movie', 'tt0816692', 'Interstellar'), ('movie', 'tt0133093', 'The Matrix'),
               ('movie', 'tt0110912', 'Pulp Fiction')]


# ------------------------------------------------------------------------------------------- state files

def read_kv(path):
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k, v = line.split('=', 1)
                v = re.sub(r'\s+#.*$', '', v).strip()
                if v:
                    out[k.strip()] = v
    return out


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text)
    tmp.chmod(0o600)
    tmp.replace(path)


def write_kv(path, values, header, explained=False):
    """KEY=value lines under a header; with `explained`, each setting's explanation (explain.py) above its line."""
    body = [f'# {line}' for line in header.splitlines()]
    for k, v in values.items():
        if v in (None, ''):
            continue
        if explained:
            body += [''] + docs.comment(explain.SETTINGS[k][1])
        body.append(f'{k}={v}')
    write_private(path, '\n'.join(body) + '\n')


def load_settings():
    path = STATE / 'settings.json'
    return json.loads(path.read_text()) if path.exists() else {}


def save_settings(s):
    write_private(STATE / 'settings.json', json.dumps(s, indent=2, sort_keys=True) + '\n')


def backup(config, uuid, label='viewer'):
    path = STATE / 'backups' / f'{label}-{uuid[:8]}-{time.strftime("%Y%m%d-%H%M%S", time.gmtime())}.json'
    write_private(path, json.dumps({'uuid': uuid, 'config': config}, indent=2) + '\n')
    (STATE / 'backups').chmod(0o700)
    return path


def short(uuid):
    return f'{uuid[:8]}…' if uuid else '(none)'


# ------------------------------------------------------------------------------------------- the wizard

class Wizard:
    def __init__(self, args):
        self.args = args
        self.answers = read_kv(Path(args.answers)) if args.answers else {}
        self.ask = Asker(self.answers, args.yes)
        self.prev = load_settings()
        self.keys = read_kv(STATE / 'secrets.env')
        self.s = {}            # this run's settings (saved as state/settings.json)
        self.aios = None
        self.status = None

    # 1 -------------------------------------------------------------------------------------------------------
    def check_machine(self):
        step(1, 'This machine')
        problem = dockerenv.docker_problem()
        if problem:
            stop(*problem)
        ok('Docker and Docker Compose work')
        free = dockerenv.free_gb(str(REPO))
        if free is not None and free < 4 and not self.prev.get('installed'):
            stop(f'Only {free:.1f} GB of disk space is free here.', 'subsync needs about 3 GB to build. Free some space and run ./setup again.')
        if free is not None:
            ok(f'{free:.0f} GB of disk space free')

    # 2 -------------------------------------------------------------------------------------------------------
    def find_aios(self):
        step(2, 'Your AIOStreams')
        found = dockerenv.aios_containers()
        container = None
        if len(found) > 1:
            names = [(c['name'], f"{c['name']}  ({c['image']})") for c in found]
            pick = self.ask.choose('AIOS_CONTAINER', 'More than one AIOStreams runs here. Which one do your apps use?',
                                   names, current=self.prev.get('container'))
            container = next(c for c in found if c['name'] == pick)
        elif found:
            container = found[0]
        url = None
        if container and 'AIOS_URL' not in self.answers:
            for candidate in dockerenv.host_urls(container):
                try:
                    status = Aios(candidate, timeout=8).status()
                except AiosError:
                    continue
                url, self.status = candidate, status
                break
            if url:
                ok(f"Found AIOStreams {self.status.get('version', '?')} in the container \"{container['name']}\"")
        if not url:
            if container:
                warn(f"The container \"{container['name']}\" looks like AIOStreams, but I can't reach it from here.")
            url = self.ask.text('AIOS_URL', 'What address do you open AIOStreams at?', current=self.prev.get('aios_url'),
                                default=None if self.prev.get('aios_url') else 'http://127.0.0.1:3000',
                                check=self._check_aios)
        self.aios = Aios(url)
        settings = self.status['settings']
        public = (settings.get('baseUrl') or (container or {}).get('base_url') or url).rstrip('/')
        self.s.update(aios_url=url, aios_public=public, version=VERSION, aios_version=self.status.get('version'))
        if container and container['host_network']:
            self.s.update(mode='host', container=container['name'], networks=[],
                          aios_internal=f"http://127.0.0.1:{container['port']}")
        elif container and container['networks']:
            self.s.update(mode='network', container=container['name'], networks=container['networks'],
                          aios_internal=f"http://{container['name']}:{container['port']}")
        else:
            self.s.update(mode='remote', container=(container or {}).get('name'), networks=[], aios_internal=public)
        ok(f'Viewers reach it at {public}')
        if self.s['mode'] == 'network':
            note(f"subsync will join its Docker network{'s' if len(container['networks']) > 1 else ''}: {', '.join(container['networks'])}")
        elif self.s['mode'] == 'remote':
            note('It doesn\'t run in Docker on this machine, so subsync talks to it over the network.')

    def _check_aios(self, value):
        value = value.strip().rstrip('/')
        if not re.match(r'^https?://', value):
            value = 'http://' + value
        value = re.sub(r'/(stremio|configure|jellyfin)(/.*)?$', '', value)
        try:
            self.status = Aios(value, timeout=15).status()
        except AiosError as e:
            return False, e.message + (f' {e.fix}' if e.fix else ''), value
        return True, f"AIOStreams {self.status.get('version', '?')} answers there", value

    # 3 -------------------------------------------------------------------------------------------------------
    def read_viewer(self):
        step(3, 'The config your apps use')

        def check_id(value):
            uuid = plan.config_id_from(value)
            if not uuid:
                return False, 'That has no config ID in it (it looks like 1a2b3c4d-1234-…).', value
            if self.aios.config_exists(uuid) is False:
                return False, 'There is no config with that ID on this AIOStreams. Check it, or paste your Stremio link.', value
            return True, '', uuid

        def check_password(value):
            try:
                self.viewer, self.viewer_encrypted = self.aios.get_config(self.uuid, value)
            except AiosError as e:
                return False, e.message, value
            return True, 'Config found', value
        current = self.keys.get('VIEWER_UUID')
        while True:
            self.uuid = self.ask.text('AIOS_CONFIG_ID', 'Config ID (or paste your Stremio link)', current=current,
                                      shown=short(current or ''), check=check_id)
            self.password = self.ask.text('AIOS_CONFIG_PASSWORD', 'Its password', secret=True, optional=True,
                                          skip_hint='Enter with nothing typed: give a different config ID',
                                          check=check_password)
            if self.password:
                break
            if self.ask.yes:
                stop('"Its password" needs an answer.', 'Add AIOS_CONFIG_PASSWORD=... to the answers file.')
            current = self.uuid
        names = plan.debrid_names(self.viewer)
        subs = plan.subtitle_sources(self.viewer)
        say()
        (ok if names else warn)('Debrid services: ' + (', '.join(names) if names else 'none'))
        (ok if subs else warn)('Subtitle sources: ' + (', '.join(subs) if subs else 'none yet'))
        if realdebrid.real_debrid_only(self.viewer):
            stop(realdebrid.REAL_DEBRID_ONLY, 'After adding it on AIOStreams\' configure page, run ./setup again.')
        cleaned, ways = realdebrid.without_real_debrid(self.viewer)
        if realdebrid.SERVICE in realdebrid.debrid_in_use(self.viewer):
            ok(f'Real-Debrid is never used: subsync reads through {", ".join(plan.debrid_names(cleaned))} only')
        if ways:
            note('subsync\'s own config leaves out:')
            for line in ways:
                note(f'  {line}')
        problems = plan.sync_config_problems(plan.sync_config(self.viewer))
        if problems:
            stop('subsync can\'t work with this config: ' + '; '.join(problems) + '.',
                 'Add a debrid service other than Real-Debrid (TorBox, ...) on AIOStreams\' configure page, then run '
                 './setup again.')

    # 4 -------------------------------------------------------------------------------------------------------
    def choices(self):
        step(4, 'Your choices')
        prev_langs = ', '.join(n for n, _, _ in [plan._BY[c] for c in self.prev.get('languages', []) if c in plan._BY]) or None

        def check_langs(value):
            langs, error = plan.parse_languages(value)
            return (False, error, value) if error else (True, '', value)
        text = self.ask.text('LANGUAGES', 'Which subtitle languages should be synced?', current=prev_langs, default='English',
                             check=check_langs)
        self.languages = plan.parse_languages(text)[0]
        self.audio = self.ask.yesno('AUDIO', 'Also sync files that carry no subtitles of their own, by listening to the audio?',
                                    self.prev.get('audio', False))
        self.source_keys()
        self.add_os = False
        if not plan.subtitle_sources(self.viewer):
            self.add_os = self.ask.yesno('ADD_OPENSUBTITLES', 'Your config has no subtitle source, so there is nothing to '
                                                              'sync yet. Add OpenSubtitles (free, no account needed)?', True)
        self.public_address()

    def source_keys(self):
        """Optional free keys for subtitle sources subsync searches itself (TheSubtitleDB, OpenSubtitles V3+ and
        Gestdown need none)."""
        for key, label in (('SUBDL_KEY', 'SubDL API key'), ('WYZIE_KEY', 'Wyzie Subs API key')):
            value = self.ask.text(key, f'{label} (optional)', secret=True, optional=True, current=self.keys.get(key))
            if value:
                self.keys[key] = value

    def public_address(self):
        lan = dockerenv.lan_address()
        kind, default = plan.public_default(self.s['aios_public'], lan)
        prev = self.prev.get('public_base')
        if prev:
            kind = 'path' if plan.base_path_of(prev) else 'port'
        url = self.ask.text('PUBLIC_ADDRESS', 'subsync\'s address for your apps', current=prev, default=default,
                            check=plan.check_public)
        kind = 'path' if plan.base_path_of(url) else 'port'
        port = urllib.parse.urlsplit(url).port
        if kind == 'port':
            port = port or (443 if url.startswith('https') else 80)
            bind = '0.0.0.0'
        else:
            port = int(self.prev.get('port') or plan.SUBSYNC_PORT)
            bind = '127.0.0.1'
        if self.s['mode'] == 'host':
            bind = None   # host network: subsync listens on the port itself
        if port != self.prev.get('port') and dockerenv.port_in_use(port, '127.0.0.1' if bind == '127.0.0.1' else '0.0.0.0') \
                and not dockerenv.container_state(CONTAINER):
            stop(f'Port {port} on this machine is already in use.', 'Give another address (a different port) when ./setup asks again.')
        self.s.update(public_base=url, public_kind=kind, port=port, bind=bind)

    # 5 -------------------------------------------------------------------------------------------------------
    def addon_urls(self):
        key = self.keys.get('ADDON_KEY') or plan.new_key()
        self.keys['ADDON_KEY'] = key
        mode = self.s['mode']
        if mode == 'network':
            internal = f'http://{plan.SUBSYNC_HOST}:{plan.SUBSYNC_PORT}'
        elif mode == 'host':
            internal = f"http://127.0.0.1:{self.s['port']}"
        else:
            internal = self.s['public_base']
        self.s['addon_host'] = plan.host_of(internal)
        self.manifest_url = f'{internal}/{key}/manifest.json'

    def review_and_save(self):
        step(5, 'Your AIOStreams configs')
        self.cache = None
        self.addon_urls()
        metadata = {p['ID']: p for p in self.status['settings'].get('presets') or []}
        new_viewer, changes = plan.viewer_change(self.viewer, self.manifest_url, metadata, self.add_os)
        sync = plan.sync_config(new_viewer, self.manifest_url)
        sync_exists = bool(self.keys.get('SYNC_UUID') and self.keys.get('SYNC_PASSWORD'))
        say()
        say(f'  {paint("1", f"Your config {short(self.uuid)}")}')
        if changes:
            for line in changes:
                say(f'    {paint("32", "+")} {line}')
            note('Nothing else in it changes. A copy of it as it is now is saved first.')
        else:
            say('    no change (subsync is already in it)')
        say()
        say(f'  {paint("1", "subsync's own config")} ' + ('(update)' if sync_exists else '(new)'))
        say('    a copy of yours without Usenet, Real-Debrid and subsync, with nothing running in the background,')
        say('    so subsync can find the file you are watching. It never appears in your apps.')
        self.cache = self.offer_cache_setting()
        if not self.ask.yesno('CONFIRM', 'Save these changes?', True):
            stop('Nothing was changed.', 'Run ./setup again when you are ready.')
        self.new_viewer, self.changes = new_viewer, changes
        # subsync's own config first: subsync needs it to start. Yours changes only once subsync answers, since
        # AIOStreams fetches an addon's manifest when a config that lists it is saved.
        self.save_sync(sync, sync_exists)

    def save_viewer(self):
        if self.changes:
            path = backup(self.viewer, self.uuid)
            ok(f'Saved a copy of your config: {path.relative_to(REPO)}')
            self.with_sign_in(lambda: self.aios.save_config(self.uuid, self.password, self.new_viewer))
            check, _ = self.aios.get_config(self.uuid, self.password)
            if not any(plan.is_subsync_preset(p, self.manifest_url) for p in check.get('presets') or []):
                stop('AIOStreams saved the config, but subsync is not in it when read back.',
                     f'Your saved copy is {path.relative_to(REPO)}; ./setup --restore puts it back.')
            ok('Your config now lists subsync' + (' and OpenSubtitles' if self.add_os else ''))
            if self.add_os:   # remembered, so ./setup --uninstall can offer to take it out again
                before = {p.get('instanceId') for p in self.viewer.get('presets') or []}
                added = [p.get('instanceId') for p in check.get('presets') or []
                         if p.get('type') == 'opensubtitles' and p.get('instanceId') not in before]
                if added:
                    self.s['added_opensubtitles'] = added[0]
                    save_settings(self.s)
            self.viewer = check
        else:
            ok('Your config already lists subsync')
        if self.cache:
            self.apply_cache_setting(*self.cache)

    def save_sync(self, sync, exists):
        if exists:
            try:
                self.with_sign_in(lambda: self.aios.save_config(self.keys['SYNC_UUID'], self.keys['SYNC_PASSWORD'], sync))
                _, self.sync_encrypted = self.aios.get_config(self.keys['SYNC_UUID'], self.keys['SYNC_PASSWORD'])
                ok(f"Updated subsync's own config ({short(self.keys['SYNC_UUID'])})")
                return
            except AiosError as e:
                if 'match' not in e.message:
                    raise
                warn('subsync\'s old config is gone from AIOStreams; making a new one.')
        password = secrets.token_urlsafe(24)
        uuid, encrypted = self.with_sign_in(lambda: self.aios.create_config(sync, password))
        self.keys.update(SYNC_UUID=uuid, SYNC_PASSWORD=password)
        self.sync_encrypted = encrypted
        self.write_keys()
        ok(f"Created subsync's own config ({short(uuid)})")

    def with_sign_in(self, action):
        """Some AIOStreams instances only save configs for a signed-in account: ask once, then retry."""
        try:
            return action()
        except AiosError as e:
            if 'sign in' not in e.message or self.aios.signed_in:
                raise
            warn(e.message)
            self.sign_in('to save configs')
            return action()

    def sign_in(self, why):
        user = self.ask.text('AIOS_USERNAME', f'AIOStreams sign-in {why}: username')

        def check(value):
            try:
                self.admin = self.aios.sign_in(user, value)
            except AiosError as e:
                return False, e.message, value
            return True, 'Signed in', value
        self.ask.text('AIOS_PASSWORD', 'Its password', secret=True, check=check)

    def offer_cache_setting(self):
        """AIOStreams keeps each addon's subtitle answer 5 minutes; without that for subsync, a synced copy shows the
        next time a film is opened instead of up to 5 minutes later. Needs AIOStreams' admin sign-in."""
        if not self.ask.yesno('CACHE', 'Turn off AIOStreams\' 5-minute subtitle cache for subsync?', False):
            return None
        if not self.aios.signed_in:
            self.sign_in('(admin)')
        if not getattr(self, 'admin', False):
            warn('That account is not an admin; the setting stays as it is.')
            return None
        found = self.aios.subtitle_cache_setting()
        if not found:
            warn('This AIOStreams version has no subtitle cache setting; nothing to change.')
            return None
        key, value, locked = found
        if locked:
            warn('The setting is fixed by AIOStreams\' environment (SUBTITLE_CACHE_TTL); change it there:')
            note(f"SUBTITLE_CACHE_TTL={','.join(f'{k}:{v}' for k, v in plan.subtitle_cache_value(value, self.s['addon_host']).items())}")
            return None
        return key, value

    def apply_cache_setting(self, key, value):
        new = plan.subtitle_cache_value(value, self.s['addon_host'])
        if self.prev.get('cache_before') is None:
            self.s['cache_before'] = {'key': key, 'value': value}
        self.aios.set_setting(key, new)
        _, after, _ = self.aios.subtitle_cache_setting()
        (ok if after == new else warn)('AIOStreams: no subtitle cache for subsync' if after == new
                                       else 'AIOStreams did not keep the cache setting; synced copies may take 5 minutes.')

    # 6 -------------------------------------------------------------------------------------------------------
    def install(self):
        step(6, 'Installing subsync')
        if self.s['public_kind'] == 'path':
            self.proxy_rule()
        sync_base = f"{self.s['aios_internal']}/stremio/{self.keys['SYNC_UUID']}/{self.sync_encrypted}"
        env = plan.subsync_env({
            'sync_manifest': f'{sync_base}/manifest.json', 'sync_password': self.keys['SYNC_PASSWORD'],
            'public_base': self.s['public_base'],
            'addon_key': self.keys['ADDON_KEY'], 'languages': self.languages,
            'rewrite': plan.rewrite_rule(self.s['aios_public'], self.s['aios_internal']) if self.s['mode'] != 'remote' else '',
            'audio': self.audio, 'subdl_key': self.keys.get('SUBDL_KEY', ''), 'wyzie_key': self.keys.get('WYZIE_KEY', '')})
        if self.s['mode'] == 'host':
            env['SUBSYNC_PORT'] = str(self.s['port'])
        write_kv(STATE / 'subsync.env', env, 'subsync settings, written by ./setup. Private: holds subsync\'s keys.\n'
                 'Every setting, with its explanation and default: the README\'s Settings section.', explained=True)
        (STATE / 'compose.yaml').write_text(plan.compose_yaml({
            'version': VERSION, 'host_network': self.s['mode'] == 'host', 'networks': self.s.get('networks'),
            'bind': self.s.get('bind'), 'port': self.s['port']}))
        self.s.update(languages=[three for _, _, three in self.languages], audio=self.audio,
                      manifest_url=self.manifest_url, installed=self.prev.get('installed') or time.strftime('%Y-%m-%d'))
        for kept in ('cache_before', 'added_opensubtitles'):
            self.s.setdefault(kept, self.prev.get(kept))
        save_settings(self.s)
        self.write_keys()
        image = f'aiosubtitles/subsync:{VERSION}'
        layers = ['docker', 'image', 'inspect', '--format', '{{json .RootFS.Layers}}', image]   # the ID changes on every build
        before = dockerenv.run(layers)[1].strip()
        built = bool(before)
        say()
        say('  Building subsync' + (' (seconds if nothing changed, a few minutes after an update)' if built
                                    else ' (the first time takes a few minutes)') + ' ...')
        code, out, err = dockerenv.compose(STATE, 'build', timeout=3600)
        if code != 0:
            tail = '\n'.join((err or out).strip().splitlines()[-15:])
            stop('Building subsync failed.', tail + '\n\nRun ./setup again after fixing the problem above.')
        after = dockerenv.run(layers)[1].strip()
        ok('Image up to date' if after and after == before else 'Built (its own tests passed during the build)')
        code, out, err = dockerenv.compose(STATE, 'up', '-d', '--remove-orphans', timeout=300)
        if code != 0:
            stop('Starting subsync failed.', '\n'.join((err or out).strip().splitlines()[-10:]))
        if not self.wait_healthy():
            stop('subsync started but does not answer.', f'See its log: docker logs {CONTAINER}')
        ok('subsync is running (it restarts by itself after a reboot)')
        self.save_viewer()

    def wait_healthy(self, seconds=90):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if 'healthy' in dockerenv.container_state(CONTAINER).split()[-1:]:
                return True
            time.sleep(3)
        return False

    def public_health(self):
        try:
            status, body = Aios(self.s['public_base'], timeout=10).call('GET', '/health', raw=True)
        except AiosError:
            return False
        return status == 200 and body.strip() == b'ok'

    def proxy_rule(self):
        if self.public_health():   # a re-run: the rule is in place already
            ok(f"Your web server already reaches subsync at {self.s['public_base']}")
            return
        target_host = f'127.0.0.1:{self.s["port"]}'
        snippets = plan.proxy_snippets(self.s['public_base'], target_host)
        say()
        wrap(f"Your web server needs one rule so {self.s['public_base']}/... reaches subsync. Add it next to your "
             'AIOStreams rule (keep the path as it is; don\'t strip it):')
        for name, lines in snippets.items():
            say(f'    {paint("1", name)}')
            for line in lines:
                say(f'      {line}')
        if self.s['mode'] == 'network':
            note(f'Web server running in Docker on AIOStreams\' network: use {plan.SUBSYNC_HOST}:{plan.SUBSYNC_PORT} instead of {target_host}.')
        self.ask.pause('Add the rule and reload your web server, then press Enter (or press Enter now and add it later):')

    # 7 -------------------------------------------------------------------------------------------------------
    def self_test(self):
        step(7, 'Checking that it works')
        good = True
        # a) the address apps use
        if self.public_health():
            ok(f"Your apps can reach subsync at {self.s['public_base']}")
        else:
            good = False
            warn(f"{self.s['public_base']}/health doesn't answer from this machine.")
            note('If you just added the web server rule, reload the web server; some routers also block a machine')
            note('from reaching its own public address, so check it from your phone too: it should say "ok".')
        # b) AIOStreams asks subsync for subtitles
        before = self.subsync_status().get('subtitle_requests', 0)
        viewer_base = f"{self.s['aios_url']}/stremio/{self.uuid}/{self.viewer_encrypted}"
        reached = False
        for type_, imdb, _ in TEST_TITLES:
            try:
                self.aios.addon_get(viewer_base, f'/subtitles/{type_}/{imdb}.json', timeout=40)
            except AiosError:
                continue
            if self.subsync_status().get('subtitle_requests', 0) > before:
                reached = True
                break
        if reached:
            ok('AIOStreams asks subsync for subtitles')
        else:
            good = False
            bad('AIOStreams did not reach subsync.')
            note(f"It calls {plan.manifest_base(self.manifest_url).rsplit('/', 1)[0]}/…; check that address works from AIOStreams.")
        # c) subsync's config finds files (a search and a cache check; nothing is opened)
        sync_base = f"{self.s['aios_url']}/stremio/{self.keys['SYNC_UUID']}/{self.sync_encrypted}"
        rows, title = [], None
        for type_, imdb, name in TEST_TITLES[:2]:
            try:
                streams = self.aios.addon_get(sync_base, f'/stream/{type_}/{imdb}.json', timeout=90).get('streams') or []
            except AiosError as e:
                warn(e.message)
                continue
            rows = [r for r in streams if readable(r)]
            title = name
            if rows:
                break
        if rows:
            ok(f'subsync\'s config finds {len(rows)} ready debrid file{"s" if len(rows) != 1 else ""} for {title} (a search only)')
        else:
            good = False
            warn('subsync\'s config found no ready debrid file for two well-known films.')
            note('If your apps do show ready files for them, run ./setup --status and send its output with a bug report.')
        # d) one small read, only if the user says yes (Enter answers no)
        if rows:
            if self.ask.yesno('TEST_READ', 'Do the test read now?', False):
                good = self.test_read(rows) and good
            else:
                note('Skipped the test read.')
        return good

    def test_read(self, rows):
        """subsync's own index read of the smallest ready file, with subsync's own cap and size slack. Files listed under
        100 MB are skipped: some addons list a wrong size (1,679 bytes for a 3 GB film), which subsync's size check
        refuses."""
        def size_of(row):
            hints = row.get('streamData') or {}
            return int(hints.get('size') or (row.get('behaviorHints') or {}).get('videoSize') or 0)
        plausible = [r for r in rows if size_of(r) >= 100 * 10 ** 6] or rows
        row = min(plausible, key=lambda r: size_of(r) or float('inf'))
        size = size_of(row)
        cmd = ['python', '-m', 'subsync.selftest'] + (['--size', str(size)] if size else [])
        code, out, err = dockerenv.exec_in(CONTAINER, cmd, stdin=row['url'] + '\n', timeout=180)
        result = {}
        try:
            result = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pass
        used = f"{result.get('bytes', 0) / 1e6:.1f} MB in {result.get('requests', 0)} requests"
        if result.get('ok') and result.get('container'):
            tracks = result.get('text_tracks') or []
            ok(f"Read {used} ({result['container'].upper()}, {size / 1e9:.1f} GB): the file has "
               f"{len(tracks)} text subtitle track{'s' if len(tracks) != 1 else ''}"
               + (f" ({', '.join(sorted({t['language'] or '?' for t in tracks}))})" if tracks else '')
               + (f" and {result['image_tracks']} image one{'s' if result['image_tracks'] != 1 else ''}" if result.get('image_tracks') else ''))
            return True
        if result.get('ok'):
            ok(f"Read works ({used}); {result.get('note')}")
            return True
        if result.get('error') == 'cap':   # subsync works; this one file is beyond what it reads
            ok(f"Read works ({used}); {result.get('detail')}")
            return True
        if result.get('error') == 'refused':
            warn('The test read was refused by the debrid service (HTTP 403 or 429), most likely a limit on your account.')
            note('subsync itself is installed and waits a while after a refusal. Try ./setup again later.')
            return False
        warn("The test read didn't work: " + (result.get('detail') or 'no answer from subsync') + '.')
        note('subsync is installed. Run ./setup again later, or send the output of ./setup --status with a bug report.')
        return False

    def subsync_status(self):
        return subsync_status()

    def write_keys(self):
        self.keys['VIEWER_UUID'] = getattr(self, 'uuid', self.keys.get('VIEWER_UUID'))
        write_kv(STATE / 'secrets.env', self.keys, 'Written by ./setup. Private: subsync\'s keys and its AIOStreams config login.')

    # ---------------------------------------------------------------------------------------------------------
    def summary(self, good):
        cache_off = bool(self.s.get('cache_before'))
        lines = ['subsync is running on this machine and is in your AIOStreams config.', '',
                 f'• You will see "<Language> (Subsync)" next to the usual subtitles ({plan.language_names(self.languages)}) '
                 'while subsync works on a file, and "<Language> (synced)" once a copy timed to that exact file is ready. '
                 'When no copy is clearly closer than the original, nothing is added and the originals stay.',
                 '• Stremio: playing a file starts subsync on it (a few seconds); the synced copy shows the next time '
                 'you open that file' + ('.' if cache_off else ' (after up to 5 minutes, AIOStreams\' subtitle cache).'),
                 '• AIOStreams\' own Jellyfin server: the same, shown as "(synced)".',
                 '• AIOMetadata (stock): it shows plain language names ("English (external)"), and it keeps each '
                 'title\'s subtitle list and text for an hour, also its "no subtitles" answer (see below).']
        box('Done' if good else 'Installed, with warnings (see above)', lines, '1;32' if good else '1;33')
        say()
        say('  Your apps keep the same links: nothing to change in them.')
        say()
        wrap('If you use AIOMetadata: restart it once now, so it asks for subtitles again (its container may have '
             'another name):')
        say('    docker restart aiometadata')
        wrap('and to see synced copies within 5 minutes instead of an hour, add this to its environment:')
        say('    JELLYFIN_SUBTITLE_TTL=300')
        if self.add_os:
            say()
            wrap('Your config had no subtitles before: in Stremio, install the addon once more with the same link, '
                 'so Stremio starts asking it for subtitles.')
        say()
        say(paint('2', '  ./setup again: change languages or options  ·  ./setup --status  ·  ./setup --uninstall'))
        say()


def subsync_status():
    """subsync's /status, asked from inside its container (works whatever its address is)."""
    code, out, _ = dockerenv.exec_in(CONTAINER, ['python', '-c',
        'import os,urllib.request;print(urllib.request.urlopen("http://127.0.0.1:%s/%s/status"%(os.environ.get('
        '"SUBSYNC_PORT","8790"),os.environ["SUBSYNC_ADDON_KEY"]),timeout=10).read().decode())'])
    try:
        return json.loads(out) if code == 0 else {}
    except ValueError:
        return {}


def readable(row):
    """A listed row subsync could read: a ready (cached) debrid file with a link, never a Real-Debrid one."""
    d = row.get('streamData') or {}
    return bool(row.get('url')) and d.get('type') == 'debrid' and (d.get('service') or {}).get('cached') is True \
        and not d.get('library') and realdebrid.row_problem(row) is None


# ------------------------------------------------------------------------------------------- other commands

def cmd_status():
    s, keys = load_settings(), read_kv(STATE / 'secrets.env')
    if not s:
        stop('subsync is not set up here yet.', 'Run ./setup')
    say(paint('1', '  AIOSubtitles · subsync status'))
    state = dockerenv.container_state(CONTAINER)
    (ok if 'healthy' in state.split() else bad)(f'container {CONTAINER}: {state or "not found"}')
    st = subsync_status()
    if st:
        results = (st.get('store') or {}).get('results') or {}
        offered = results.get('synced', 0)
        ok(f"subtitle requests since start: {st.get('subtitle_requests', 0)}; waiting jobs: {st.get('queue', 0)}")
        note(f"synced copies offered: {offered}; subtitles checked: {sum(results.values())} "
             f"({', '.join(f'{v} {k}' for k, v in sorted(results.items())) or 'none yet'})")
        note(f"read today: {st.get('bytes_today', 0) / 1e6:.1f} MB")
        if st.get('backing_off'):
            warn('backing off after a refused read (it waits before reading again)')
    say(f"  AIOStreams: {s.get('aios_public')}  ·  languages: {', '.join(s.get('languages', []))}  ·  audio: {'on' if s.get('audio') else 'off'}")
    say(f"  config: {short(keys.get('VIEWER_UUID', ''))}  ·  subsync's config: {short(keys.get('SYNC_UUID', ''))}")
    say(f'  logs: docker logs {CONTAINER}')
    return 0


def cmd_restore(args, path):
    data = json.loads(Path(path).read_text())
    s = load_settings()
    aios = Aios(s.get('aios_url') or 'http://127.0.0.1:3000')
    ask = Asker(read_kv(Path(args.answers)) if args.answers else {}, args.yes)
    uuid = data['uuid']
    password = ask.text('AIOS_CONFIG_PASSWORD', f'Password of config {short(uuid)}', secret=True)
    if not ask.yesno('CONFIRM', f'Put the copy from {Path(path).name} back into config {short(uuid)}?', False):
        stop('Nothing was changed.')
    current, _ = aios.get_config(uuid, password)
    backup(current, uuid, 'before-restore')
    aios.save_config(uuid, password, data['config'])
    ok('Restored.')
    return 0


def cmd_uninstall(args):
    s, keys = load_settings(), read_kv(STATE / 'secrets.env')
    if not s:
        stop('subsync is not set up here.')
    ask = Asker(read_kv(Path(args.answers)) if args.answers else {}, args.yes)
    aios = Aios(s['aios_url'])
    say(paint('1', '  Removing subsync'))
    uuid = keys.get('VIEWER_UUID')
    password = ask.text('AIOS_CONFIG_PASSWORD', f'Password of your config {short(uuid)}', secret=True)
    viewer, _ = aios.get_config(uuid, password)
    new, removed = plan.without_subsync(viewer, s.get('manifest_url'))
    say(f'  This takes subsync out of your config ({removed} entr{"y" if removed == 1 else "ies"}), deletes subsync\'s own config,')
    say('  and removes the subsync container with its stored copies. Your other subtitle sources stay.')
    added = s.get('added_opensubtitles')
    if added and any(p.get('instanceId') == added for p in new.get('presets') or []):
        if ask.yesno('REMOVE_OPENSUBTITLES', 'setup also added OpenSubtitles to your config. Take that out too?', True):
            new['presets'] = [p for p in new['presets'] if p.get('instanceId') != added]
            removed += 1
    if not ask.yesno('CONFIRM', 'Remove subsync?', False):
        stop('Nothing was changed.')
    if removed:
        path = backup(viewer, uuid)
        aios.save_config(uuid, password, new)
        ok(f'Your config no longer lists subsync (copy before: {path.relative_to(REPO)})')
    if keys.get('SYNC_UUID'):
        try:
            aios.delete_config(keys['SYNC_UUID'], keys['SYNC_PASSWORD'])
            ok('Deleted subsync\'s own config')
        except AiosError as e:
            warn(e.message)
    if (STATE / 'compose.yaml').exists():
        dockerenv.compose(STATE, 'down', '-v', timeout=300)
        ok('Removed the subsync container and its stored copies')
    if s.get('cache_before'):
        note('AIOStreams\' subtitle cache setting for subsync stays; it does nothing without subsync.')
    for name in ('settings.json', 'secrets.env', 'subsync.env', 'compose.yaml'):
        (STATE / name).unlink(missing_ok=True)
    ok('Done.')
    say()
    say('  These stay, in case you set it up again; remove them when you no longer need them:')
    say(f"    • the subsync image (about 1 GB): docker image rm aiosubtitles/subsync:{s.get('version') or VERSION}")
    if s.get('public_kind') == 'path':
        say(f"    • the rule in your web server for {s.get('public_base')}: remove it there and reload the web server")
    say(f'    • this folder, {REPO}, with copies of your config in state/backups/ (./setup --restore FILE puts one back)')
    return 0


# ------------------------------------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(prog='./setup', description='Add subsync (synced subtitles) to your AIOStreams.')
    parser.add_argument('--answers', help='answers from a KEY=value file')
    parser.add_argument('--yes', action='store_true', help='ask nothing: answers file, current values and defaults')
    parser.add_argument('--status', action='store_true', help='is it running, and what has it done')
    parser.add_argument('--uninstall', action='store_true', help='take subsync out of your config and remove it')
    parser.add_argument('--restore', metavar='FILE', help='put a saved copy of your config back')
    args = parser.parse_args(argv)
    try:
        if args.status:
            return cmd_status()
        if args.restore:
            return cmd_restore(args, args.restore)
        if args.uninstall:
            return cmd_uninstall(args)
        say()
        say(paint('1', '  AIOSubtitles · subsync setup'))
        wrap('subsync makes copies of your subtitles timed to the exact file you play, and lists them next to the '
             'originals as "<Language> (synced)". This adds it to the AIOStreams you already run. Each question says '
             'what it does; press Enter to accept what is shown.')
        w = Wizard(args)
        if w.prev:
            say(paint('2', '  Already set up here: your answers are kept unless you change them.'))
        w.check_machine()
        w.find_aios()
        w.read_viewer()
        w.choices()
        w.review_and_save()
        w.install()
        good = w.self_test()
        w.summary(good)
        return 0 if good else 3
    except Stop as e:
        say()
        bad(e.message)
        for line in (e.fix or '').strip().splitlines():
            say(f'      {line}')
        say()
        return 1
    except AiosError as e:
        say()
        bad(e.message)
        if e.fix:
            say(f'      {e.fix}')
        say()
        return 1


if __name__ == '__main__':
    sys.exit(main())
