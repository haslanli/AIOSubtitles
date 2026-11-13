"""The work of AIOSubtitles' setup, step by step, without asking or printing anything itself.

Two front ends drive it: the setup page (installer/web.py), which puts each step's answers
in and shows what comes back as rows, and an answers file (./setup --answers FILE), which runs every step at once
and prints as the terminal setup did. Asking is `self.ask`: a question with no answer yet raises Problem(missing=True)
for the page and stops an answers-file run; a wrong answer raises Problem at its field. Output is `self.out`.

It installs one thing, the subsync service (in Docker, built on this machine), and makes two config changes in
AIOStreams, each shown first and saved only after a yes:
  1. your config gets one more subtitle source, subsync (a backup copy is saved first);
  2. a second config, made from yours, that subsync uses to find files (no Usenet, no Real-Debrid, nothing in the
     background).
It doesn't install or change AIOStreams itself or AIOMetadata.
"""
import json
import os
import re
import secrets
import sys
import textwrap
import time
import urllib.parse
from pathlib import Path

from installer import docs, dockerenv, hosting, limits, plan, ui, update
from installer.aios import Aios, AiosError  # noqa: F401  (tests reach the client through here)
from installer.ui import Asker, Stop, stop
from subsync import explain, realdebrid

REPO = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get('AIOSUBTITLES_STATE') or REPO / 'state')
CONTAINER = 'aiosubtitles-subsync'
TEST_TITLES = [('movie', 'tt0816692', 'Interstellar'), ('movie', 'tt0133093', 'The Matrix'),
               ('movie', 'tt0110912', 'Pulp Fiction')]
# the Real-Debrid stop's way out: on the page and in a terminal
RD_WAY_OUT_PAGE = "After adding it on AIOStreams' configure page, go back one step and continue again."
RD_WAY_OUT = "After adding it on AIOStreams' configure page, run ./setup again."


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


def write_kv(path, values, header):
    body = [f'# {line}' for line in header.splitlines()] + [f'{k}={v}' for k, v in values.items() if v not in (None, '')]
    write_private(path, '\n'.join(body) + '\n')


ENV_HEADER = ('subsync settings. ./setup rewrites the lines it sets from your answers and keeps every line you add.\n'
              'Private: holds subsync\'s keys. Every setting, with its explanation and default: the README\'s Settings section.')
NOT_A_SETTING = 'Not a subsync setting: kept as you wrote it.'


def write_env(path, values):
    """state/subsync.env: each line with its setting's explanation (explain.py) above it. Empty values are written: an
    empty value is a setting too."""
    body = [f'# {line}' for line in ENV_HEADER.splitlines()]
    for k, v in values.items():
        body += [''] + docs.comment(explain.SETTINGS[k][1] if k in explain.SETTINGS else NOT_A_SETTING) + [f'{k}={v}']
    write_private(path, '\n'.join(body) + '\n')


def env_values():
    path = STATE / 'subsync.env'
    return plan.parse_env(path.read_text() if path.exists() else '')


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


def current_version():
    """The version of the code in this folder (its VERSION file)."""
    return update.running(REPO)


def compose_for(s, version):
    """state/compose.yaml for the setup saved in `s` (state/settings.json), with that version's image."""
    if s.get('route') == 'host':
        return hosting.compose_yaml(version, s['port'])
    return plan.compose_yaml({'version': version, 'host_network': s.get('mode') == 'host', 'networks': s.get('networks'),
                              'bind': s.get('bind'), 'port': s['port']})


def short(uuid):
    return f'{uuid[:8]}…' if uuid else '(none)'


# ------------------------------------------------------------------------------------------- asking and output

class Problem(Exception):
    """An answer the page must ask for (`missing`) or ask again (with `message`), at the field `field`."""

    def __init__(self, field, message='', missing=False):
        super().__init__(message)
        self.field, self.message, self.missing = field, message, missing


class PageAsk:
    """The page's answers: what the page sent, else the current value or the default. Nothing is printed: a good
    check's message becomes a row, a wrong answer raises Problem at its field, an unanswered required one raises
    Problem(missing=True), so the page shows that question."""

    def __init__(self, answers, out):
        self.answers, self.out = answers, out

    def text(self, key, question, *, current=None, default=None, secret=False, optional=False, check=None,
             shown=None, skip_hint=None):
        (explain.QUESTIONS.get(key) or explain.SETTINGS[key])    # every question has its one explanation
        given = self.answers.get(key)
        value = (given or '').strip() if isinstance(given, str) else given
        if not value:
            value = current or default or ''
        if not value:
            if optional:
                return ''
            raise Problem(key, f'"{question}" needs an answer.', missing=given is None)
        if check:
            good, message, value = check(value)
            if not good:
                raise Problem(key, message)
            if message:
                self.out.ok(message)
        return value

    def yesno(self, key, question, default):
        value = str(self.answers.get(key, '')).strip().lower()
        if value in ('y', 'yes', 'on', 'true', '1'):
            return True
        if value in ('n', 'no', 'off', 'false', '0'):
            return False
        return default

    def choose(self, key, question, options, current=None):
        values = [v for v, _ in options]
        value = self.answers.get(key)
        if value in values:
            return value
        if current in values:
            return current
        raise Problem(key, '', missing=True)

    def pause(self, question):
        pass


class Terminal:
    """Output as the terminal setup printed it."""
    say, wrap, ok, warn, bad, note, step, box, paint = map(staticmethod, (ui.say, ui.wrap, ui.ok, ui.warn, ui.bad, ui.note,
                                                                          ui.step, ui.box, ui.paint))

    def working(self, text):
        ui.say(f'  {text}')

    def changes(self, sections):
        for heading, lines, after in sections:
            ui.say()
            ui.say(f'  {ui.paint("1", heading)}')
            for sign, line in lines:
                ui.say(f'    {ui.paint("32", sign)} {line}')
            if lines:
                ui.note(after)
            else:
                for part in textwrap.wrap(after, ui.WIDTH - 2):
                    ui.say(f'    {part}')


class Rows:
    """Output for the page: check rows (ok, warn, bad), notes under the row before them, and progress lines."""

    def __init__(self):
        self.rows = []

    def _add(self, kind, text):
        self.rows.append([kind, text, None])

    def ok(self, text):
        self._finish()
        self._add('ok', text)

    def warn(self, text):
        self._finish()
        self._add('warn', text)

    def bad(self, text):
        self._finish()
        self._add('bad', text)

    def note(self, text):
        if self.rows and self.rows[-1][0] in ('ok', 'warn', 'bad'):
            last = self.rows[-1]
            last[2] = f'{last[2]} {text.strip()}' if last[2] else text.strip()
        else:
            self._add('note', text.strip())

    def working(self, text):
        self._finish()
        self._add('now', text)

    def _finish(self):
        for row in self.rows:
            if row[0] == 'now':
                row[0] = 'done'

    def say(self, text=''):
        pass

    wrap = say

    def step(self, number, title, total=None):
        pass

    def box(self, *a, **k):
        pass

    def paint(self, code, text):
        return text

    def changes(self, sections):
        pass


# ------------------------------------------------------------------------------------------- the steps

class Engine:
    def __init__(self, answers=None, page=False, out=None):
        self.answers = dict(answers or {})
        self.page = page
        self.out = out or (Rows() if page else Terminal())
        self.ask = PageAsk(self.answers, self.out) if page else Asker(self.answers, yes=True)
        self.prev = load_settings()
        self.keys = read_kv(STATE / 'secrets.env')
        self.s = {}            # this run's settings (saved as state/settings.json)
        self.aios = None
        self.status = None
        self.limit_values = {}

    # what the page shows filled in on a re-run ------------------------------------------------------------------
    def prefill(self):
        """The answers kept from the last setup, as the page fills them in (never a password or a key)."""
        p, env = self.prev, env_values()
        filled = {'installed': bool(p.get('installed'))}
        if p.get('aios_url'):
            filled['AIOS_URL'] = p['aios_url']
        if self.keys.get('VIEWER_UUID'):
            filled['AIOS_CONFIG_ID'] = self.keys['VIEWER_UUID']
        names = [plan._BY[c][0] for c in p.get('languages', []) if c in plan._BY]
        if names:
            filled['LANGUAGES'] = ', '.join(names)
        filled['AUDIO'] = 'yes' if p.get('audio') else 'no'
        audio = self.known_audio_budget()
        if audio:
            filled['AUDIO_BUDGET'] = audio
        if p.get('public_base'):
            filled['PUBLIC_ADDRESS'] = p['public_base']
        filled[limits.DAILY.setting] = limits.current(limits.DAILY, env)
        return filled

    # 1 -------------------------------------------------------------------------------------------------------
    def check_machine(self):
        self.out.step(1, 'This machine', self.HOST_STEPS if getattr(self, 'route', None) == 'host' else None)
        problem = dockerenv.docker_problem()
        if problem:
            stop(*problem)
        self.out.ok('Docker and Docker Compose work')
        free = dockerenv.free_gb(str(REPO))
        if free is not None and free < 4 and not self.prev.get('installed'):
            stop(f'Only {free:.1f} GB of disk space is free here.', 'subsync needs about 3 GB to build. Free some space and run ./setup again.')
        if free is not None:
            self.out.ok(f'{free:.0f} GB of disk space free')

    # 2 -------------------------------------------------------------------------------------------------------
    def find_aios(self):
        self.out.step(2, 'Your AIOStreams')
        found = self.found = dockerenv.aios_containers()
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
                self.out.ok(f"Found AIOStreams {self.status.get('version', '?')} in the container \"{container['name']}\"")
        if not url:
            if container:
                self.out.warn(f"The container \"{container['name']}\" looks like AIOStreams, but I can't reach it from here.")
            url = self.ask.text('AIOS_URL', 'What address do you open AIOStreams at?', current=self.prev.get('aios_url'),
                                default=None if (self.prev.get('aios_url') or self.page) else 'http://127.0.0.1:3000',
                                check=self._check_aios)
        self.aios = Aios(url)
        settings = self.status['settings']
        public = (settings.get('baseUrl') or (container or {}).get('base_url') or url).rstrip('/')
        self.s.update(aios_url=url, aios_public=public, version=current_version(), aios_version=self.status.get('version'))
        if container and container['host_network']:
            self.s.update(mode='host', container=container['name'], networks=[],
                          aios_internal=f"http://127.0.0.1:{container['port']}")
        elif container and container['networks']:
            self.s.update(mode='network', container=container['name'], networks=container['networks'],
                          aios_internal=f"http://{container['name']}:{container['port']}")
        else:
            self.s.update(mode='remote', container=(container or {}).get('name'), networks=[], aios_internal=public)
        self.out.ok(f'Viewers reach it at {public}')
        if self.s['mode'] == 'network':
            self.out.note(f"subsync will join its Docker network{'s' if len(container['networks']) > 1 else ''}: {', '.join(container['networks'])}")
        elif self.s['mode'] == 'remote':
            self.out.note('It doesn\'t run in Docker on this machine, so subsync talks to it over the network.')

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
        self.out.step(3, 'The config your apps use')

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
        self.uuid = self.ask.text('AIOS_CONFIG_ID', 'Config ID (or paste your Stremio link)', current=current,
                                  shown=short(current or ''), check=check_id)
        self.password = self.ask.text('AIOS_CONFIG_PASSWORD', 'Its password', secret=True, check=check_password)
        names = plan.debrid_names(self.viewer)
        subs = plan.subtitle_sources(self.viewer)
        self.out.say()
        (self.out.ok if names else self.out.warn)('Debrid services: ' + (', '.join(names) if names else 'none'))
        (self.out.ok if subs else self.out.warn)('Subtitle sources: ' + (', '.join(subs) if subs else 'none yet'))
        if realdebrid.real_debrid_only(self.viewer):
            raise Stop(realdebrid.REAL_DEBRID_ONLY, RD_WAY_OUT, page_fix=RD_WAY_OUT_PAGE)
        cleaned, ways = realdebrid.without_real_debrid(self.viewer)
        if realdebrid.SERVICE in realdebrid.debrid_in_use(self.viewer):
            self.out.ok(f'Real-Debrid is never used: subsync reads through {", ".join(plan.debrid_names(cleaned))} only')
        if ways:
            self.out.note('subsync\'s own config leaves out:')
            for line in ways:
                self.out.note(f'  {line}')
        problems = plan.sync_config_problems(plan.sync_config(self.viewer))
        if problems:
            stop('subsync can\'t work with this config: ' + '; '.join(problems) + '.',
                 'Add a debrid service other than Real-Debrid (TorBox, ...) on AIOStreams\' configure page, then run '
                 './setup again.')

    # 4 -------------------------------------------------------------------------------------------------------
    def choices(self):
        self.out.step(4, 'Your choices')
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

    def known_audio_budget(self):
        """The last answer to the audio limit question, or a value set by hand in state/subsync.env before; None."""
        by_hand = env_values().get('SUBSYNC_AUDIO_BUDGET_GB')
        return self.prev.get('audio_budget') or plan.audio_budget(by_hand or '', allowed_too=by_hand)

    def ask_audio_budget(self):
        """The most the audio route may read of one file, in GB: 0.5, 1 (recommended), 2 or 4. Enter keeps the last
        answer, or a value set by hand in state/subsync.env before."""
        current = self.known_audio_budget()

        def check(value):
            chosen = plan.audio_budget(value, allowed_too=current)
            return (True, '', chosen) if chosen else (False, 'Type 0.5, 1, 2 or 4 (GB per file).', value)
        return self.ask.text('AUDIO_BUDGET', 'How much of each file may the audio route read, in GB: 0.5, 1, 2 or 4?',
                             current=current, default='1', check=check)

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
            raise Problem('PUBLIC_ADDRESS', f'Port {port} on this machine is already in use.')
        self.s.update(public_base=url, public_kind=kind, port=port, bind=bind)

    # 5 -------------------------------------------------------------------------------------------------------
    def limits(self):
        """The limits a personal install asks: the daily total and, with the audio route on, the audio limit (no language
        limit: on your own install you choose your languages yourself)."""
        self.out.step(5, 'Limits')
        lim = limits.DAILY

        def check(value):
            chosen, error = limits.check(lim, value)
            return (False, error, value) if error else (True, '', chosen)
        self.limit_values = {lim.setting: self.ask.text(lim.setting, lim.label, current=limits.current(lim, env_values()),
                                                        check=check)}
        self.audio_budget = self.ask_audio_budget() if self.audio else None
        if self.audio_budget is None:      # audio off: the last answer (or a value set by hand) is kept for later
            self.audio_budget_kept = self.known_audio_budget()

    # 6 -------------------------------------------------------------------------------------------------------
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

    def plan_changes(self):
        """What saving would change in the two configs, as (heading, [(sign, line)], note) sections. Changes nothing."""
        self.addon_urls()
        metadata = {p['ID']: p for p in self.status['settings'].get('presets') or []}
        self.new_viewer, self.changes = plan.viewer_change(self.viewer, self.manifest_url, metadata, self.add_os)
        self.sync = plan.sync_config(self.new_viewer, self.manifest_url)
        self.sync_exists = bool(self.keys.get('SYNC_UUID') and self.keys.get('SYNC_PASSWORD'))
        viewer = (f'Your config {short(self.uuid)}', [('+', line) for line in self.changes],
                  'Nothing else in it changes. A copy of it as it is now is saved first.' if self.changes
                  else 'no change (subsync is already in it)')
        sync = ("subsync's own config " + ('(update)' if self.sync_exists else '(new)'), [],
                'a copy of yours without Usenet, Real-Debrid and subsync, with nothing running in the background, '
                'so subsync can find the file you are watching. It never appears in your apps.')
        return [viewer, sync]

    def review_and_save(self):
        self.out.step(6, 'Your AIOStreams configs')
        self.cache = None
        self.out.changes(self.plan_changes())
        self.cache = self.offer_cache_setting()
        if not self.ask.yesno('CONFIRM', 'Save these changes?', True):
            stop('Nothing was changed.', 'Run ./setup again when you are ready.')
        # subsync's own config first: subsync needs it to start. Yours changes only once subsync answers, since
        # AIOStreams fetches an addon's manifest when a config that lists it is saved.
        self.save_sync(self.sync, self.sync_exists)

    def save_viewer(self):
        if self.changes:
            path = backup(self.viewer, self.uuid)
            self.out.ok(f'Saved a copy of your config: {path.relative_to(REPO)}')
            self.with_sign_in(lambda: self.aios.save_config(self.uuid, self.password, self.new_viewer))
            check, _ = self.aios.get_config(self.uuid, self.password)
            if not any(plan.is_subsync_preset(p, self.manifest_url) for p in check.get('presets') or []):
                stop('AIOStreams saved the config, but subsync is not in it when read back.',
                     f'Your saved copy is {path.relative_to(REPO)}; ./setup --restore puts it back.')
            self.out.ok('Your config now lists subsync' + (' and OpenSubtitles' if self.add_os else ''))
            if self.add_os:   # remembered, so ./setup --uninstall can offer to take it out again
                before = {p.get('instanceId') for p in self.viewer.get('presets') or []}
                added = [p.get('instanceId') for p in check.get('presets') or []
                         if p.get('type') == 'opensubtitles' and p.get('instanceId') not in before]
                if added:
                    self.s['added_opensubtitles'] = added[0]
                    save_settings(self.s)
            self.viewer = check
        else:
            self.out.ok('Your config already lists subsync')
        if self.cache:
            self.apply_cache_setting(*self.cache)

    def save_sync(self, sync, exists):
        if exists:
            try:
                self.with_sign_in(lambda: self.aios.save_config(self.keys['SYNC_UUID'], self.keys['SYNC_PASSWORD'], sync))
                _, self.sync_encrypted = self.aios.get_config(self.keys['SYNC_UUID'], self.keys['SYNC_PASSWORD'])
                self.out.ok(f"Updated subsync's own config ({short(self.keys['SYNC_UUID'])})")
                return
            except AiosError as e:
                if 'match' not in e.message:
                    raise
                self.out.warn('subsync\'s old config is gone from AIOStreams; making a new one.')
        password = secrets.token_urlsafe(24)
        uuid, encrypted = self.with_sign_in(lambda: self.aios.create_config(sync, password))
        self.keys.update(SYNC_UUID=uuid, SYNC_PASSWORD=password)
        self.sync_encrypted = encrypted
        self.write_keys()
        self.out.ok(f"Created subsync's own config ({short(uuid)})")

    def with_sign_in(self, action):
        """Some AIOStreams instances only save configs for a signed-in account: ask once, then retry."""
        try:
            return action()
        except AiosError as e:
            if 'sign in' not in e.message or self.aios.signed_in:
                raise
            self.out.warn(e.message)
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
            self.out.warn('That account is not an admin; the setting stays as it is.')
            return None
        found = self.aios.subtitle_cache_setting()
        if not found:
            self.out.warn('This AIOStreams version has no subtitle cache setting; nothing to change.')
            return None
        key, value, locked = found
        if locked:
            self.out.warn('The setting is fixed by AIOStreams\' environment (SUBTITLE_CACHE_TTL); change it there:')
            self.out.note(f"SUBTITLE_CACHE_TTL={','.join(f'{k}:{v}' for k, v in plan.subtitle_cache_value(value, self.s['addon_host']).items())}")
            return None
        return key, value

    def apply_cache_setting(self, key, value):
        new = plan.subtitle_cache_value(value, self.s['addon_host'])
        if self.prev.get('cache_before') is None:
            self.s['cache_before'] = {'key': key, 'value': value}
        self.aios.set_setting(key, new)
        _, after, _ = self.aios.subtitle_cache_setting()
        (self.out.ok if after == new else self.out.warn)('AIOStreams: no subtitle cache for subsync' if after == new
                                                         else 'AIOStreams did not keep the cache setting; synced copies may take 5 minutes.')

    # 7 -------------------------------------------------------------------------------------------------------
    def install(self):
        self.out.step(7, 'Installing subsync')
        if self.s['public_kind'] == 'path':
            self.proxy_rule()
        sync_base = f"{self.s['aios_internal']}/stremio/{self.keys['SYNC_UUID']}/{self.sync_encrypted}"
        env = plan.subsync_env({
            'sync_manifest': f'{sync_base}/manifest.json', 'sync_password': self.keys['SYNC_PASSWORD'],
            'public_base': self.s['public_base'],
            'addon_key': self.keys['ADDON_KEY'], 'languages': self.languages,
            'rewrite': plan.rewrite_rule(self.s['aios_public'], self.s['aios_internal']) if self.s['mode'] != 'remote' else '',
            'audio': self.audio, 'audio_budget': self.audio_budget,
            'subdl_key': self.keys.get('SUBDL_KEY', ''), 'wyzie_key': self.keys.get('WYZIE_KEY', '')})
        if self.s['mode'] == 'host':
            env['SUBSYNC_PORT'] = str(self.s['port'])
        env.update(limits.env_lines([limits.DAILY], self.limit_values))
        env, kept = plan.merge_env(env, env_values(), owned=limits.names([limits.DAILY]))
        write_env(STATE / 'subsync.env', env)
        if kept:
            self.out.ok(f"Kept the lines of state/subsync.env that ./setup doesn't set: {', '.join(kept)}")
        (STATE / 'compose.yaml').write_text(compose_for(self.s, current_version()))
        self.s.update(languages=[three for _, _, three in self.languages], audio=self.audio,
                      audio_budget=self.audio_budget or getattr(self, 'audio_budget_kept', None),   # for when it's on again
                      manifest_url=self.manifest_url, installed=self.prev.get('installed') or time.strftime('%Y-%m-%d'))
        for kept in ('cache_before', 'added_opensubtitles'):
            self.s.setdefault(kept, self.prev.get(kept))
        save_settings(self.s)
        self.write_keys()
        self.build_and_start('subsync', 'subsync is running (it restarts by itself after a reboot)')
        self.save_viewer()

    def build_and_start(self, name, running):
        image = f'aiosubtitles/subsync:{current_version()}'
        layers = ['docker', 'image', 'inspect', '--format', '{{json .RootFS.Layers}}', image]   # the ID changes on every build
        before = dockerenv.run(layers)[1].strip()
        built = bool(before)
        self.out.say()
        self.out.working(f'Building {name}' + (' (seconds if nothing changed, a few minutes after an update)' if built
                                                else ' (the first time takes a few minutes)') + ' ...')
        code, out, err = dockerenv.compose(STATE, 'build', timeout=3600)
        if code != 0:
            tail = '\n'.join((err or out).strip().splitlines()[-15:])
            stop(f'Building {name} failed.', tail + '\n\nRun ./setup again after fixing the problem above.')
        after = dockerenv.run(layers)[1].strip()
        self.out.ok('Image up to date' if after and after == before else 'Built (its own tests passed during the build)')
        code, out, err = dockerenv.compose(STATE, 'up', '-d', '--remove-orphans', timeout=300)
        if code != 0:
            stop(f'Starting {name} failed.', '\n'.join((err or out).strip().splitlines()[-10:]))
        if not self.wait_healthy():
            stop(f'{name} started but does not answer.', f'See its log: docker logs {CONTAINER}')
        self.out.ok(running)

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
            self.out.ok(f"Your web server already reaches subsync at {self.s['public_base']}")
            return
        target_host = f'127.0.0.1:{self.s["port"]}'
        self.web_server_rule = plan.proxy_snippets(self.s['public_base'], target_host)
        self.rule_intro = (f"Your web server needs one rule so {self.s['public_base']}/... reaches subsync. Add it next to "
                           'your AIOStreams rule (keep the path as it is; don\'t strip it):')
        self.out.say()
        self.out.wrap(self.rule_intro)
        for name, lines in self.web_server_rule.items():
            self.out.say(f'    {self.out.paint("1", name)}')
            for line in lines:
                self.out.say(f'      {line}')
        if self.s['mode'] == 'network':
            self.out.note(f'Web server running in Docker on AIOStreams\' network: use {plan.SUBSYNC_HOST}:{plan.SUBSYNC_PORT} instead of {target_host}.')
        self.ask.pause('Add the rule and reload your web server, then press Enter (or press Enter now and add it later):')

    # 8 -------------------------------------------------------------------------------------------------------
    def self_test(self):
        rows = self.self_test_checks()
        return self.offer_test_read(rows) and self.good

    def self_test_checks(self):
        """The checks that read nothing: the address apps use, AIOStreams asking subsync, subsync's config finding
        files (a search only). Returns the ready rows a test read could take."""
        self.out.step(8, 'Checking that it works')
        self.good = True
        # a) the address apps use
        if self.public_health():
            self.out.ok(f"Your apps can reach subsync at {self.s['public_base']}")
        else:
            self.good = False
            self.out.warn(f"{self.s['public_base']}/health doesn't answer from this machine.")
            self.out.note('If you just added the web server rule, reload the web server; some routers also block a machine')
            self.out.note('from reaching its own public address, so check it from your phone too: it should say "ok".')
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
            self.out.ok('AIOStreams asks subsync for subtitles')
        else:
            self.good = False
            self.out.bad('AIOStreams did not reach subsync.')
            self.out.note(f"It calls {plan.manifest_base(self.manifest_url).rsplit('/', 1)[0]}/…; check that address works from AIOStreams.")
        # c) subsync's config finds files (a search and a cache check; nothing is opened)
        sync_base = f"{self.s['aios_url']}/stremio/{self.keys['SYNC_UUID']}/{self.sync_encrypted}"
        rows, title = [], None
        for type_, imdb, name in TEST_TITLES[:2]:
            try:
                streams = self.aios.addon_get(sync_base, f'/stream/{type_}/{imdb}.json', timeout=90).get('streams') or []
            except AiosError as e:
                self.out.warn(e.message)
                continue
            rows = [r for r in streams if readable(r)]
            title = name
            if rows:
                break
        if rows:
            self.out.ok(f'subsync\'s config finds {len(rows)} ready debrid file{"s" if len(rows) != 1 else ""} for {title} (a search only)')
        else:
            self.good = False
            self.out.warn('subsync\'s config found no ready debrid file for two well-known films.')
            self.out.note('If your apps do show ready files for them, run ./setup --status and send its output with a bug report.')
        self.ready_rows = rows
        return rows

    def offer_test_read(self, rows):
        """d) one small read, only if the user says yes (the default is no)."""
        if not rows:
            return True
        if self.ask.yesno('TEST_READ', 'Do the test read now?', False):
            return self.test_read(rows)
        self.out.note('Skipped the test read.')
        return True

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
            self.out.ok(f"Read {used} ({result['container'].upper()}, {size / 1e9:.1f} GB): the file has "
                        f"{len(tracks)} text subtitle track{'s' if len(tracks) != 1 else ''}"
                        + (f" ({', '.join(sorted({t['language'] or '?' for t in tracks}))})" if tracks else '')
                        + (f" and {result['image_tracks']} image one{'s' if result['image_tracks'] != 1 else ''}" if result.get('image_tracks') else ''))
            return True
        if result.get('ok'):
            self.out.ok(f"Read works ({used}); {result.get('note')}")
            return True
        if result.get('error') == 'cap':   # subsync works; this one file is beyond what it reads
            self.out.ok(f"Read works ({used}); {result.get('detail')}")
            return True
        if result.get('error') == 'refused':
            self.out.warn('The test read was refused by the debrid service (HTTP 403 or 429), most likely a limit on your account.')
            self.out.note('subsync itself is installed and waits a while after a refusal. Try ./setup again later.')
            return False
        self.out.warn("The test read didn't work: " + (result.get('detail') or 'no answer from subsync') + '.')
        self.out.note('subsync is installed. Run ./setup again later, or send the output of ./setup --status with a bug report.')
        return False

    def subsync_status(self):
        return subsync_status()

    def write_keys(self):
        self.keys['VIEWER_UUID'] = getattr(self, 'uuid', self.keys.get('VIEWER_UUID'))
        write_kv(STATE / 'secrets.env', self.keys, 'Written by ./setup. Private: subsync\'s keys and its AIOStreams config login.')

    # ---------------------------------------------------------------------------------------------------------
    def summary_parts(self, good):
        """The closing screen: (title, paragraphs, [(kind, text)] after them), kind 'p' or 'code'."""
        cache_off = bool(self.s.get('cache_before'))
        lines = ['subsync is running on this machine and is in your AIOStreams config.',
                 f'You will see "<Language> (Subsync)" next to the usual subtitles ({plan.language_names(self.languages)}) '
                 'while subsync works on a file, and "<Language> (synced)" once a copy timed to that exact file is ready. '
                 'When no copy is clearly closer than the original, nothing is added and the originals stay.',
                 'Stremio: playing a file starts subsync on it (a few seconds); the synced copy shows the next time '
                 'you open that file' + ('.' if cache_off else ' (after up to 5 minutes, AIOStreams\' subtitle cache).'),
                 'AIOStreams\' own Jellyfin server: the same, shown as "(synced)".',
                 'AIOMetadata (stock): it shows plain language names ("English (external)"), and it keeps each '
                 'title\'s subtitle list and text for an hour, also its "no subtitles" answer (see below).']
        after = [('p', 'Your apps keep the same links: nothing to change in them.'),
                 ('p', 'If you use AIOMetadata: restart it once now, so it asks for subtitles again (its container may '
                       'have another name):'),
                 ('code', 'docker restart aiometadata'),
                 ('p', 'and to see synced copies within 5 minutes instead of an hour, add this to its environment:'),
                 ('code', 'JELLYFIN_SUBTITLE_TTL=300')]
        if self.add_os:
            after.append(('p', 'Your config had no subtitles before: in Stremio, install the addon once more with the '
                               'same link, so Stremio starts asking it for subtitles.'))
        return ('Done' if good else 'Installed, with warnings (see above)'), lines, after

    def summary(self, good):
        title, lines, after = self.summary_parts(good)
        self.out.box(title, [lines[0], ''] + ['• ' + line for line in lines[1:]], '1;32' if good else '1;33')
        for kind, text in after:
            self.out.say()
            if kind == 'code':
                self.out.say(f'    {text}')
            else:
                self.out.wrap(text)
        self.out.say()
        self.out.say(self.out.paint('2', '  ./setup again: change languages or options  ·  ./setup --status  ·  ./setup --uninstall'))
        self.out.say()

    def run_answers(self):
        """Every step with the answers file's answers (or the current values and defaults): ./setup --answers."""
        if self.choose_route() == 'host':
            return self.run_hosting()
        self.check_machine()
        self.find_aios()
        self.read_viewer()
        self.choices()
        self.limits()
        self.review_and_save()
        self.install()
        good = self.self_test()
        self.summary(good)
        return good

    # ------------------------------------------------------------------------------------- the hosting route
    ROUTES = [('yourself', 'Install it for yourself'), ('host', 'Host a public instance')]
    HOST_STEPS = 7

    def choose_route(self):
        """Install it for yourself, or host a public instance: the last setup's route on a re-run."""
        self.route = self.ask.choose('ROUTE', 'How do you want to use it?', self.ROUTES,
                                     current=self.prev.get('route') or 'yourself')
        return self.route

    def host_address(self):
        self.out.step(2, 'Its web address', self.HOST_STEPS)
        env = env_values()
        kept = self.prev.get('public_base') if self.prev.get('route') == 'host' else None
        self.address = self.ask.text('HOST_ADDRESS', 'Web address', current=kept, check=hosting.check_address)
        self.trusted_proxy = self.ask.text('SUBSYNC_TRUSTED_PROXY', 'Trusted proxy',
                                           current=env.get('SUBSYNC_TRUSTED_PROXY'), default='gateway')
        port = int(self.prev.get('port') or hosting.PORT)
        if port != self.prev.get('port') and dockerenv.port_in_use(port, '127.0.0.1') \
                and not dockerenv.container_state(CONTAINER):
            stop(f'Port {port} on this machine is already in use.')
        self.s.update(route='host', public_base=self.address, port=port, version=current_version())

    def host_signups(self):
        """The invite code (suggested on a first setup; emptied, anyone may sign up) and the secret, made once."""
        self.out.step(3, 'Sign-ups', self.HOST_STEPS)
        env = env_values()
        if 'SUBSYNC_PUBLIC_INVITE' in self.answers:
            self.invite = (self.answers['SUBSYNC_PUBLIC_INVITE'] or '').strip()
        elif 'SUBSYNC_PUBLIC_INVITE' in env or self.prev.get('route') == 'host':
            self.invite = env.get('SUBSYNC_PUBLIC_INVITE', '')
        else:
            self.invite = getattr(self, 'suggested_invite', None) or hosting.suggested_invite()
            self.suggested_invite = self.invite
        self.secret = env.get('SUBSYNC_SECRET_KEY') or getattr(self, 'secret', None)
        self.secret_new = not env.get('SUBSYNC_SECRET_KEY')
        if not self.secret:
            self.secret = hosting.new_secret()

    def host_choices(self):
        self.out.step(4, 'Your choices', self.HOST_STEPS)
        for key, label in (('SUBDL_KEY', 'SubDL API key'), ('SUBSYNC_SUBSOURCE_KEY', 'SubSource API key')):
            value = self.ask.text(key, f'{label} (optional)', secret=True, optional=True, current=self.keys.get(key))
            if value:
                self.keys[key] = value

    def host_limits(self):
        """Every limit, in its three groups, the current value or subsync's default filled in."""
        self.out.step(5, 'Limits', self.HOST_STEPS)
        env = env_values()
        self.limit_values = {}
        for lim in hosting.LIMITS:
            def check(value, lim=lim):
                chosen, error = limits.check(lim, value)
                return (False, error, value) if error else (True, '', chosen)
            self.limit_values[lim.setting] = self.ask.text(lim.setting, lim.label, current=limits.current(lim, env),
                                                           check=check)

    def host_install(self):
        self.out.step(6, 'Installing AIOSubtitles', self.HOST_STEPS)
        if self.secret_new:
            self.out.ok('Made the secret key and kept it in state/subsync.env, readable only by your user.')
        answers = {'HOST_ADDRESS': self.address, 'SUBSYNC_TRUSTED_PROXY': self.trusted_proxy,
                   'SUBSYNC_PUBLIC_INVITE': self.invite, 'SUBDL_KEY': self.keys.get('SUBDL_KEY', ''),
                   'SUBSYNC_SUBSOURCE_KEY': self.keys.get('SUBSYNC_SUBSOURCE_KEY', '')}
        env, kept = plan.merge_env(hosting.public_env(answers, self.secret, self.limit_values), env_values(),
                                   owned=hosting.OWNED)
        write_env(STATE / 'subsync.env', env)
        self.secret_new = False
        if kept:
            self.out.ok(f"Kept the lines of state/subsync.env that ./setup doesn't set: {', '.join(kept)}")
        (STATE / 'compose.yaml').write_text(compose_for(self.s, current_version()))
        self.s.update(installed=self.prev.get('installed') or time.strftime('%Y-%m-%d'))
        save_settings(self.s)
        self.write_keys()
        self.build_and_start('AIOSubtitles', 'AIOSubtitles is running (it restarts by itself after a reboot)')
        self.web_server_rule = None
        if not self.public_health_at(self.address):
            self.web_server_rule = hosting.web_server_rule(self.address, f"127.0.0.1:{self.s['port']}")
            self.rule_intro = (f'Your web server needs one rule so {self.address} reaches AIOSubtitles on this machine. '
                               'Add it with the https certificate your web server has for that name, and reload the '
                               'web server:')
            self.out.say()
            self.out.wrap(self.rule_intro)
            for name, lines in self.web_server_rule.items():
                self.out.say(f'    {self.out.paint("1", name)}')
                for line in lines:
                    self.out.say(f'      {line}')

    def public_health_at(self, address):
        try:
            status, _, body = hosting.fetch(address + '/health')
        except OSError:
            return False
        return status == 200 and body.strip() == b'ok'

    def host_check(self):
        self.out.step(7, 'Checking that it works', self.HOST_STEPS)
        self.good = hosting.check(self.address, self.out)
        return self.good

    def host_summary_parts(self, good):
        lines = [f'AIOSubtitles is running on this machine as a public instance at {self.address}.',
                 ('Share the address and the invite code with the people you want to use it: each of them signs up '
                  'there with their own AIOStreams link.') if self.invite else
                 ('Share the address with the people you want to use it: each of them signs up there with their own '
                  'AIOStreams link.'),
                 'Keep the folder state/ in your backups: it holds the secret key, and without it everyone has to '
                 'sign up again.']
        return ('Done' if good else 'Installed, with warnings (see above)'), lines, []

    def run_hosting(self):
        self.check_machine()
        self.host_address()
        self.host_signups()
        self.host_choices()
        self.host_limits()
        self.host_install()
        good = self.host_check()
        title, lines, _ = self.host_summary_parts(good)
        self.out.box(title, [lines[0], ''] + ['• ' + line for line in lines[1:]], '1;32' if good else '1;33')
        self.out.say()
        self.out.say(self.out.paint('2', '  ./setup again: change your answers  ·  ./setup --status  ·  ./setup --uninstall'))
        self.out.say()
        return good

    # ------------------------------------------------------------------------------------- after setup
    def status_parts(self):
        """What ./setup --status prints and the page after setup shows: check rows [(kind, text, more)] and details
        [(label, value)]."""
        s, keys = self.prev, self.keys
        if not s:
            stop('subsync is not set up here yet.', 'Run ./setup')
        state = dockerenv.container_state(CONTAINER)
        rows = [('ok' if 'healthy' in state.split() else 'bad', f'container {CONTAINER}: {state or "not found"}', None)]
        if s.get('route') == 'host':
            code, out, _ = dockerenv.exec_in(CONTAINER, ['python', '-m', 'subsync.hostcmd', 'status'])
            try:
                st = json.loads(out) if code == 0 else {}
            except ValueError:
                st = {}
            if st:
                rows.append(('ok', f"people signed up: {st.get('people', 0)} of {st.get('max_people', 0)}",
                             f"read today: {st.get('read_today', 0) / 1e9:.1f} GB of {st.get('daily_budget', 0) / 1e9:.0f} GB"))
            return rows, [('logs', f'docker logs {CONTAINER}')]
        st = subsync_status()
        if st:
            results = (st.get('store') or {}).get('results') or {}
            rows.append(('ok', f"subtitle requests since start: {st.get('subtitle_requests', 0)}; waiting jobs: {st.get('queue', 0)}",
                         f"synced copies offered: {results.get('synced', 0)}; subtitles checked: {sum(results.values())} "
                         f"({', '.join(f'{v} {k}' for k, v in sorted(results.items())) or 'none yet'}) "
                         f"read today: {st.get('bytes_today', 0) / 1e6:.1f} MB"))
            if st.get('backing_off'):
                rows.append(('warn', 'backing off after a refused read (it waits before reading again)', None))
        audio = f"on, up to {s.get('audio_budget') or '1'} GB per file" if s.get('audio') else 'off'
        details = [('AIOStreams', s.get('aios_public')), ('languages', ', '.join(s.get('languages', []))),
                   ('audio', audio), ('config', short(keys.get('VIEWER_UUID', ''))),
                   ("subsync's config", short(keys.get('SYNC_UUID', ''))), ('logs', f'docker logs {CONTAINER}')]
        return rows, details

    def remove(self):
        """Takes subsync out of the config your apps use, deletes subsync's own config and removes the container with
        its stored copies (./setup --uninstall, and Remove subsync on the page). `self.stays`: what is left."""
        s, keys = self.prev, self.keys
        if not s:
            stop('subsync is not set up here.')
        aios = Aios(s['aios_url'])
        self.out.say(self.out.paint('1', '  Removing subsync'))
        uuid = keys.get('VIEWER_UUID')

        def check_password(value):
            try:
                self.viewer, _ = aios.get_config(uuid, value)
            except AiosError as e:
                return False, e.message, value
            return True, '', value
        password = self.ask.text('AIOS_CONFIG_PASSWORD', f'Password of your config {short(uuid)}', secret=True,
                                 check=check_password)
        new, removed = plan.without_subsync(self.viewer, s.get('manifest_url'))
        self.out.say(f'  This takes subsync out of your config ({removed} entr{"y" if removed == 1 else "ies"}), deletes subsync\'s own config,')
        self.out.say('  and removes the subsync container with its stored copies. Your other subtitle sources stay.')
        added = s.get('added_opensubtitles')
        if added and any(p.get('instanceId') == added for p in new.get('presets') or []):
            if self.ask.yesno('REMOVE_OPENSUBTITLES', 'setup also added OpenSubtitles to your config. Take that out too?', True):
                new['presets'] = [p for p in new['presets'] if p.get('instanceId') != added]
                removed += 1
        if not self.ask.yesno('CONFIRM', 'Remove subsync?', False):
            stop('Nothing was changed.')
        if removed:
            path = backup(self.viewer, uuid)
            aios.save_config(uuid, password, new)
            self.out.ok(f'Your config no longer lists subsync (copy before: {path.relative_to(REPO)})')
        if keys.get('SYNC_UUID'):
            try:
                aios.delete_config(keys['SYNC_UUID'], keys['SYNC_PASSWORD'])
                self.out.ok('Deleted subsync\'s own config')
            except AiosError as e:
                self.out.warn(e.message)
        if (STATE / 'compose.yaml').exists():
            dockerenv.compose(STATE, 'down', '-v', timeout=300)
            self.out.ok('Removed the subsync container and its stored copies')
        if s.get('cache_before'):
            self.out.note('AIOStreams\' subtitle cache setting for subsync stays; it does nothing without subsync.')
        for name in ('settings.json', 'secrets.env', 'subsync.env', 'compose.yaml'):
            (STATE / name).unlink(missing_ok=True)
        self.out.ok('Done.')
        self.stays = [f"the subsync image (about 1 GB): docker image rm aiosubtitles/subsync:{s.get('version') or current_version()}"]
        if s.get('public_kind') == 'path':
            self.stays.append(f"the rule in your web server for {s.get('public_base')}: remove it there and reload the web server")
        self.stays.append(f'this folder, {REPO}, with copies of your config in state/backups/ (./setup --restore FILE puts one back)')
        self.out.say()
        self.out.say('  These stay, in case you set it up again; remove them when you no longer need them:')
        for line in self.stays:
            self.out.say(f'    • {line}')

    def remove_host(self):
        """Stops a public instance and removes its container with everyone's records and stored copies, once the box
        saying so is ticked; the sync configs it made on people's AIOStreams stay (opening a record needs their link)."""
        s = self.prev
        if not s:
            stop('subsync is not set up here.')
        if not self.ask.yesno('CONFIRM', "Delete everyone's records and stored copies", False):
            raise Problem('CONFIRM', '', missing=True)
        if (STATE / 'compose.yaml').exists():
            dockerenv.compose(STATE, 'down', '-v', timeout=300)
            self.out.ok('Removed the subsync container and its stored copies')
        for name in ('settings.json', 'secrets.env', 'subsync.env', 'compose.yaml'):
            (STATE / name).unlink(missing_ok=True)
        self.out.ok('Done.')
        self.stays = [f"the subsync image (about 1 GB): docker image rm aiosubtitles/subsync:{s.get('version') or current_version()}",
                      f"the rule in your web server for {s.get('public_base')}: remove it there and reload the web server",
                      f'this folder, {REPO}, with copies of your config in state/backups/ (./setup --restore FILE puts one back)']

    # ------------------------------------------------------------------------------------- updates
    def update(self, restart=None):
        """The first half of an update, on the code running now: the new version ready, a public instance's
        databases copied, the folder fast-forwarded; then setup starts again on the new code (`restart(old)`)."""
        ready = update.look(REPO)
        if ready.how == 'newest':
            raise Stop(update.NEWEST)
        if ready.how == 'not-git':
            raise Stop(update.NOT_GIT)
        if ready.how == 'error':
            raise Stop(update.LOOK_FAILED.format(reason=ready.reason))
        old_version = self.prev.get('version') or current_version()
        if self.prev.get('route') == 'host':
            self.copy_databases(old_version)
        self.out.working(update.FETCHING.format(version=ready.version))
        try:
            old = update.fast_forward(REPO)
        except update.UpdateStopped as e:
            raise Stop(e.message) from None
        self.out.working(update.RESTARTING)
        (restart or self._restart)(old)

    @staticmethod
    def _restart(old):
        os.execv(sys.executable, [sys.executable, '-I', str(REPO / 'installer' / 'wizard.py'), '--update-finish', old])

    def finish_update(self, old):
        """The second half, on the new code: build the new version while the old one keeps running, switch, check. A
        failed build or start puts the folder and compose.yaml back and starts the old version as it was."""
        self.s = s = load_settings()
        new_version = current_version()
        old_version = s.get('version') or update.version_at(REPO, old)
        host = s.get('route') == 'host'
        path = STATE / 'compose.yaml'
        before = path.read_text()
        try:
            path.write_text(compose_for(s, new_version))
            self.build_and_start('AIOSubtitles' if host else 'subsync',
                                 'AIOSubtitles is running (it restarts by itself after a reboot)' if host else
                                 'subsync is running (it restarts by itself after a reboot)')
        except Stop as e:
            update.put_back(REPO, old)
            path.write_text(before)
            dockerenv.compose(STATE, 'up', '-d', '--remove-orphans', timeout=300)
            raise Stop(update.FAILED.format(reason=e.message.rstrip('.'), version=old_version)) from None
        s['version'] = new_version
        save_settings(s)
        if host:
            return hosting.check(s['public_base'], self.out)
        if self.public_health():
            self.out.ok(f"Your apps can reach subsync at {s['public_base']}")
            return True
        self.out.warn(f"{s['public_base']}/health doesn't answer from this machine.")
        return False

    def copy_databases(self, old_version):
        """A checked copy of a public instance's two databases, out of its container, the newest three kept."""
        stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        inside = f'/data/before-update-{stamp}'
        code, out, err = dockerenv.exec_in(CONTAINER, ['python', '-m', 'subsync.hostcmd', 'copy-databases', inside],
                                           timeout=600)
        dest = STATE / 'backups' / f'databases-{stamp}'
        if code == 0:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.parent.chmod(0o700)
            code, out, err = dockerenv.run(['docker', 'cp', f'{CONTAINER}:{inside}', str(dest)], timeout=600)
        dockerenv.exec_in(CONTAINER, ['python', '-c', f'import shutil; shutil.rmtree({inside!r}, ignore_errors=True)'])
        if code != 0:
            reason = ((err or out).strip().splitlines() or ['no answer'])[-1]
            raise Stop(update.FAILED.format(reason=reason.rstrip('.'), version=old_version))
        dest.chmod(0o700)
        for path in dest.iterdir():
            path.chmod(0o600)
        update.keep_newest(STATE / 'backups', 'databases-', 3)
        self.out.ok(update.SAVED_DATABASES.format(path=dest.relative_to(REPO)))


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
