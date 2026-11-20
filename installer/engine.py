"""The work of AIOSubtitles' setup, step by step, without asking or printing anything itself.

Two front ends drive it: the setup page (installer/web.py), which puts each step's answers
in and shows what comes back as rows, and the terminal (./setup --answers FILE, ./setup --uninstall), which runs every
step at once and prints as the terminal setup did. Asking is `self.ask`: a question with no answer yet raises
Problem(missing=True) for the page, is asked in a terminal, and stops a run with --yes; a wrong answer raises Problem
at its field. Output is `self.out`.

It installs one thing, the subsync service (in Docker, from the image setup itself runs from), and makes two config
changes in
AIOStreams, each shown first and saved only after a yes:
  1. your config gets one more subtitle source, subsync (a backup copy is saved first);
  2. a second config, made from yours, that subsync uses to find files (no Usenet, no Real-Debrid, nothing in the
     background), and with Usenet on a third, the Usenet config, with only your Usenet searches and services; a
     config with no debrid service other than Real-Debrid gets only the Usenet config.
It doesn't install or change AIOStreams itself or AIOMetadata.
"""
import functools
import json
import os
import re
import secrets
import shutil
import stat
import textwrap
import time
import urllib.parse
from pathlib import Path

from installer import docs, dockerenv, hosting, limits, plan, screens, ui, update, where
from installer.aios import Aios, AiosError  # noqa: F401  (tests reach the client through here)
from installer.aios import WRONG_PASSWORD_CODES, needs_sign_in
from installer.ui import Asker, Stop, stop
from subsync import digits, explain, origlang, realdebrid
from subsync.settings import Settings

REPO = Path(__file__).resolve().parent.parent      # the code (/opt/aiosubtitles in the image)
STATE = Path(os.environ.get('AIOSUBTITLES_STATE') or REPO / 'state')
CONTAINER = plan.container_name()
PLACE = None       # where.Place: where this setup runs, read at start-up (installer/wizard.py)
GUARD_STOP = ("Setup stopped without changing anything: Docker has a {kind} named {name} that this setup didn't make. "
              "If it belongs to another AIOSubtitles, run that one's setup; if not, rename or remove it yourself.")
# a volume's own: removing it may delete an AIOSubtitles' stored copies, or a public instance's records
GUARD_STOP_VOLUME = ("Setup stopped without changing anything: Docker has a volume named {name} that this setup "
                     "didn't make. It may hold an AIOSubtitles' stored copies (on a public instance, everyone's "
                     "records): if it belongs to another AIOSubtitles, run that one's setup; remove it only once you "
                     "are sure nothing in it is needed.")
TAKEN_OVER = 'Took over the install an earlier setup (1.3.0 or before) made here; its stored copies are kept.'
CANT_REACH = ("Setup runs in a container, so it can't reach an AIOStreams that only listens on this machine's "
              '127.0.0.1. Give its address on your network, or use the source route (README).')
STARTING = 'Starting {name}'
# subsync's /health, asked inside its container on the port it listens on there (SUBSYNC_PORT, else 8790)
HEALTH = ['python', '-c', 'import os,urllib.request;print(urllib.request.urlopen("http://127.0.0.1:%s/health"%'
                          'os.environ.get("SUBSYNC_PORT","8790"),timeout=5).read().decode())']
TEST_TITLES = [('movie', 'tt0816692', 'Interstellar'), ('movie', 'tt0133093', 'The Matrix'),
               ('movie', 'tt0110912', 'Pulp Fiction')]
# The stops about the config, each with its way out on the page (where Continue reads the config again:
# installer/web.py) and in a terminal. The Real-Debrid stop's:
RD_WAY_OUT_PAGE = "After adding it on AIOStreams' configure page, press Continue again: setup reads the config again."
RD_WAY_OUT = "After adding it on AIOStreams' configure page, run ./setup again."
# A config with no debrid service but Usenet, answered no to the Usenet question
NO_USENET = "subsync can't work with this config without Usenet: it has no debrid service other than Real-Debrid."
NO_USENET_WAY_OUT_PAGE = ("Answer yes to the Usenet question, or add a debrid service (TorBox, …) on AIOStreams' "
                          "configure page, then press Continue again.")
NO_USENET_WAY_OUT = ("Answer yes to the Usenet question, or add a debrid service (TorBox, …) on AIOStreams' configure "
                     "page, then run ./setup again.")
# A config whose own config for subsync can't work, with no Usenet route either
NO_DEBRID_WAY_OUT_PAGE = ("Add a debrid service other than Real-Debrid (TorBox, …) on AIOStreams' configure page, "
                          "then press Continue again.")
NO_DEBRID_WAY_OUT = ("Add a debrid service other than Real-Debrid (TorBox, ...) on AIOStreams' configure page, then "
                     "run ./setup again.")
# AIOStreams elsewhere, reached over plain http: the config's password would cross the network unencrypted
PLAIN_HTTP = ("AIOStreams at {address} uses plain http, so the config's password would go over the network "
              "unencrypted. Use its https address if it has one.")
KEY_REMOVED = 'Removed the saved {name}'
# a new TMDB key, checked with TMDB once (refused, it is asked again with the site's words, explain.PUBLIC_TEXTS)
TMDB_ACCEPTED = 'TMDB accepts this key'
TMDB_UNCHECKED = "TMDB didn't answer, so the key wasn't checked; it is saved as you typed it."
# The closing title when a check warned: the terminal printed the warnings above it, the page shows them under it
WARNED = 'Installed, with warnings'
WARNED_ABOVE = 'Installed, with warnings (see above)'
# Docker failing to take the service down: nothing of setup's is deleted, so removing it again finishes the job
REMOVE_FAILED = "Removing the subsync container failed; setup's files were kept, so removing it again finishes the job."
# Setup never turns an installed folder into the other route: it is removed first
ROUTE_STOPS = {
    'yourself': ("A public instance runs here; setup can't turn it into an install for yourself.",
                 "Run ./setup --uninstall first (it deletes everyone's records), then ./setup again."),
    'host': ("subsync is installed here for yourself; setup can't turn it into a public instance.",
             'Run ./setup --uninstall first, then ./setup again.'),
}
# The status line's words for the outcomes a check of a subtitle has (store.stats), in the order shown
OUTCOMES = {'synced': 'synced', 'in_sync': 'already in sync', 'keep_original': 'original kept'}
# an alias link at the config question
ALIAS_UNKNOWN = "This AIOStreams doesn't know that alias. Check the link, or paste the config ID or the full install link."
ALIAS_NO_CONFIG = ("Setup couldn't get the config from that alias link. Paste the config ID, or the full link: open the "
                   "alias link in a browser and copy the address it takes you to.")


# ------------------------------------------------------------------------------------------- state files

def read_kv(path):
    """The non-empty values of a KEY=value file setup wrote (state/secrets.env), read as plan.parse_env reads every
    env and answers file."""
    return {k: v for k, v in plan.parse_env(path.read_text() if path.exists() else '').items() if v}


def own(path):
    """Setup runs as root in its container: what it writes in the folder belongs to the folder's owner."""
    if PLACE and os.geteuid() == 0:
        try:
            os.chown(path, *PLACE.owner)
        except OSError:
            pass


def shown(path):
    """A path in the folder as messages show it: state/backups/…."""
    return str(Path(path).relative_to(STATE.parent))


def host_folder():
    """The folder's path on this machine."""
    return (PLACE.host_folder if PLACE and PLACE.host_folder else None) or str(STATE.parent)


def write_whole(path, text, mode):
    """`path` written whole or not at all: a temporary file beside it, renamed over it once written, so a full disk or
    a setup stopped half way leaves the last one as it was, never a short one."""
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text)
    tmp.chmod(mode)
    own(tmp)
    tmp.replace(path)


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    own(path.parent)
    write_whole(path, text, 0o600)


# What ends a line where a value is written: every line break str.splitlines() knows (plan.parse_env reads the files
# back with it; Docker Compose ends a line at \n), and NUL. A value holding one, from AIOStreams' answers say, would add
# lines, and so settings, of its own.
LINE_BREAK = re.compile(r'[\x00\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]')


def one_line_each(path, values):
    """Stops before `path` is written when a value would break its line."""
    for k, v in values.items():
        if LINE_BREAK.search(str(v)):
            raise Stop(f'Setup stopped before writing {shown(path)}: the value for {k} has a line break in it.')


def write_kv(path, values, header):
    values = {k: v for k, v in values.items() if v not in (None, '')}
    one_line_each(path, values)
    body = [f'# {line}' for line in header.splitlines()] + [f'{k}={v}' for k, v in values.items()]
    write_private(path, '\n'.join(body) + '\n')


ENV_HEADER = ('subsync settings. ./setup rewrites the lines it sets from your answers and keeps every line you add.\n'
              'Private: holds subsync\'s keys. Every setting, with its explanation and default: the README\'s Settings section.')
NOT_A_SETTING = 'Not a subsync setting: kept as you wrote it.'


def write_env(path, values):
    """state/subsync.env: each line with its setting's explanation (explain.py) above it. Empty values are written: an
    empty value is a setting too."""
    one_line_each(path, values)
    body = [f'# {line}' for line in ENV_HEADER.splitlines()]
    for k, v in values.items():
        body += [''] + docs.comment(explain.SETTINGS[k][1] if k in explain.SETTINGS else NOT_A_SETTING) + [f'{k}={v}']
    write_private(path, '\n'.join(body) + '\n')


def env_values(as_written=False):
    """state/subsync.env's values, read as Compose reads them (plan.parse_env); `as_written`: quoted ones as the file
    has them, for the lines setup keeps."""
    path = STATE / 'subsync.env'
    return plan.parse_env(path.read_text() if path.exists() else '', as_written=as_written)


def env_settings():
    """subsync's settings as state/subsync.env starts it, for the questions that state one (explain.setup_help); a
    number or an on/off setting written wrong there reads as its default (Settings.written_wrong)."""
    return Settings(env_values())


def load_settings():
    path = STATE / 'settings.json'
    return json.loads(path.read_text()) if path.exists() else {}


def save_settings(s):
    write_private(STATE / 'settings.json', json.dumps(s, indent=2, sort_keys=True) + '\n')


def backup(config, uuid, label='viewer'):
    path = STATE / 'backups' / f'{label}-{uuid[:8]}-{time.strftime("%Y%m%d-%H%M%S", time.gmtime())}.json'
    write_private(path, json.dumps({'uuid': uuid, 'config': config}, indent=2) + '\n')
    return path


# A copy of the databases is made inside the service's container, so a link in it is never followed: setup runs as root
# with Docker's socket, and a link could point it at anything it can reach.
def real_folder(path):
    """Whether `path` is a folder itself, not a link to one."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def plain_databases(folder):
    """The names of the databases in a copy's folder that are plain files; none when the folder isn't a real one."""
    if not real_folder(folder):
        return set()
    return {p.name for p in Path(folder).iterdir()
            if p.name in dockerenv.DATABASES and stat.S_ISREG(p.lstat().st_mode)}


def remove_entry(path):
    """Removes a file, a link (never what it points to) or a folder with all in it; nothing when there is none."""
    try:
        folder = stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return
    shutil.rmtree(path) if folder else os.unlink(path)


def setup_version():
    """This setup's own version (its image's VERSION file)."""
    return update.running(REPO)


def compose_for(s):
    """state/compose.yaml for the setup saved in `s` (state/settings.json): its image, pinned, and its install id."""
    if s.get('route') == 'host':
        return hosting.compose_yaml(s['image'], s['install_id'], s['port'], local=s.get('local'),
                                    external_volume=s.get('external_volume'))
    return plan.compose_yaml({'image': s['image'], 'local': s.get('local'), 'install_id': s['install_id'],
                              'host_network': s.get('mode') == 'host', 'networks': s.get('networks'),
                              'bind': s.get('bind'), 'port': s['port'], 'external_volume': s.get('external_volume')})


def write_compose(s):
    write_whole(STATE / 'compose.yaml', compose_for(s), 0o644)


def bridge():
    """Setup on Docker's bridge network (the image route): the machine is reached through the helper and gateway."""
    return bool(PLACE and PLACE.network == 'bridge')


def port_taken(port, host):
    if bridge():
        found = dockerenv.probe(PLACE.image, [(host, port)])
        return bool(found) and f'{host}:{port}' in found.get('taken', [])
    return dockerenv.port_in_use(port, host)


def lan_address():
    if bridge():
        return (dockerenv.probe(PLACE.image, []) or {}).get('lan', '')
    return dockerenv.lan_address()


def saved_aios(s):
    """The AIOStreams of the setup saved in `s` (state/settings.json), as this run reaches it. On the bridge network each
    ./setup runs in a new container, which joins AIOStreams' Docker network again before asking it by name; AIOStreams'
    own address is used when the saved one doesn't answer."""
    if bridge() and s.get('mode') == 'network' and s.get('networks'):
        dockerenv.join(s['networks'][0], PLACE.container)
    for url in dict.fromkeys(u for u in (s.get('aios_url'), s.get('aios_public')) if u):
        try:
            Aios(url, timeout=8).status()
        except AiosError:
            continue
        return Aios(url)
    return Aios(s.get('aios_url') or plan.AIOS_DEFAULT_URL)


def aios_settings(status):
    """The settings in AIOStreams' status answer, read as the public sign-up reads them: none when an AIOStreams (an
    older or a forked one) sends none."""
    settings = status.get('settings')
    return settings if isinstance(settings, dict) else {}


def made_before_labels(s):
    """Whether the setup saved in `s` (state/settings.json) is an install setup 1.3.0 or before made: installed, with
    no install id. Setup labels what it makes since 1.4.0, so that install's container, volume and network carry no
    label; setup's steps take it over once (Engine.own_image)."""
    return bool(s.get('installed')) and not s.get('install_id')


def data_volume(s):
    """The service's volume as Docker names it: the one an install setup 1.3.0 or before made, kept as it is, or the
    one its compose file makes."""
    return s.get('external_volume') or f'{plan.project()}_{plan.VOLUME}'


def volume_gone(name):
    """Whether Docker has no volume named `name` (removed by hand since, or state/ brought to another machine)."""
    code, _, err = dockerenv.run(['docker', 'volume', 'inspect', name])
    return code != 0 and 'no such volume' in err.lower()


def unlabelled_own(s):
    """What the install saved in `s` has under setup's names without its label: all three while setup 1.3.0 or before
    made it, then the volume kept from it."""
    if made_before_labels(s):
        return {('container', CONTAINER), ('volume', data_volume(s)), ('network', f'{plan.project()}_default')}
    return {('volume', s['external_volume'])} if s.get('external_volume') else set()


def guard(install_id, unlabelled=()):
    """Stops before any change when Docker has something under setup's names that this install didn't make
    (`unlabelled`: what it has without its label, unlabelled_own)."""
    try:
        found = dockerenv.foreign(plan.project(), CONTAINER, install_id, unlabelled=unlabelled)
    except RuntimeError as e:
        raise Stop(str(e)) from None
    if found:
        kind, name = found[0]
        raise Stop(GUARD_STOP_VOLUME.format(name=name) if kind == 'volume' else GUARD_STOP.format(kind=kind, name=name))


def new_install_id():
    return secrets.token_hex(8)


def image_handle(image):
    """How messages name an image to remove: its short ID (a pinned name is one long word that no page can wrap), or
    the name when Docker doesn't know it."""
    code, out, _ = dockerenv.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image])
    found = out.strip().split(':')[-1] if code == 0 else ''
    return found[:12] if re.fullmatch(r'[0-9a-f]{64}', found) else image


def short(uuid):
    return f'{uuid[:8]}…' if uuid else '(none)'


def config_shown(value):
    """A config ID, or a link with one, as setup shows it: its ID's first 8 characters (a pasted install link holds the
    config's encrypted password); an alias link only as one."""
    return 'an alias link' if plan.alias_from(value) else short(plan.config_id_from(value) or '')


def last_lines(out, err):
    """What Docker said last, as a failed start or removal shows it."""
    return '\n'.join((err or out).strip().splitlines()[-10:])


# ------------------------------------------------------------------------------------------- asking and output

class Problem(Exception):
    """An answer the page must ask for (`missing`) or ask again (with `message`), at the field `field`."""

    def __init__(self, field, message='', missing=False):
        super().__init__(message)
        self.field, self.message, self.missing = field, message, missing


class PageAsk:
    """The page's answers: what the page sent, else the current value or the default. Nothing is printed: a good
    check's message becomes a row, a wrong answer raises Problem at its field, an unanswered required one raises
    Problem(missing=True), so the page shows that question. An optional answer sent empty (its "Remove the saved key"
    box) removes the current one: '' is returned."""

    def __init__(self, answers, out):
        self.answers, self.out = answers, out

    def text(self, key, question, *, current=None, default=None, secret=False, optional=False, check=None,
             shown=None, skip_hint=None):
        (explain.QUESTIONS.get(key) or explain.SETTINGS[key])    # every question has its one explanation
        given = self.answers.get(key)
        if optional and current and given == '':
            return ''
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
    """Output for the page: check rows (ok, warn, bad), notes under the row before them in the same step, and progress
    lines."""

    def __init__(self):
        self.rows = []
        self.step_start = 0     # where the step being run starts: a note never joins an earlier step's row

    def begin(self):
        """A step starts here: returns where its rows start."""
        self.step_start = len(self.rows)
        return self.step_start

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
        if len(self.rows) > self.step_start and self.rows[-1][0] in ('ok', 'warn', 'bad'):
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

    def step(self, number, title, total):
        pass

    def box(self, *a, **k):
        pass

    def paint(self, code, text):
        return text

    def changes(self, sections):
        pass


# ------------------------------------------------------------------------------------------- the steps

def keys_kept_unless_done(step):
    """A step that may change the keys (an optional key typed, or the saved one removed) keeps the change only when it
    goes through: refused at a later answer, it leaves them as they were, so the page asks again with the saved key and
    its "Remove the saved key" box, and an answer changed back keeps the key."""
    @functools.wraps(step)
    def run(self, *args, **kwargs):
        before = dict(self.keys)
        try:
            return step(self, *args, **kwargs)
        except BaseException:
            self.keys.clear()
            self.keys.update(before)
            raise
    return run


class Engine:
    def __init__(self, answers=None, page=False, out=None, yes=True):
        """`answers`: the page's, or the answers file's lines (None: no file given). `yes`: in a terminal, nothing is
        asked (./setup --yes); False asks what `answers` leaves out."""
        self.answers = dict(answers or {})
        self.page = page
        self.out = out or (Rows() if page else Terminal())
        self.env_settings = env_settings()      # the questions that state a setting say this install's value
        self.ask = PageAsk(self.answers, self.out) if page else Asker(answers, yes=yes, settings=self.env_settings)
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
        names = plan.names_of_codes(p.get('languages', []))
        if names:
            filled['LANGUAGES'] = ', '.join(names)
        filled['AUDIO'] = self.known_audio()
        filled['USENET'] = 'yes' if p.get('usenet') else 'no'
        audio = self.known_audio_budget()
        if audio:
            filled['AUDIO_BUDGET'] = audio
        if p.get('public_base'):
            filled['PUBLIC_ADDRESS'] = p['public_base']
        filled[limits.DAILY.setting] = limits.current(limits.DAILY, env)
        return filled

    def show_step(self, step_id, route='yourself'):
        """A step's heading in a terminal, "Step n of N · title": its route's steps as the page's rail lists them."""
        titles, ids = (screens.HOSTING, screens.HOSTING_IDS) if route == 'host' else \
            (screens.PERSONAL, screens.PERSONAL_IDS)
        i = ids.index(step_id)
        self.out.step(i + 1, titles[i], len(ids))

    # 1 -------------------------------------------------------------------------------------------------------
    def check_machine(self):
        self.show_step('machine', getattr(self, 'route', None))
        problem = dockerenv.docker_problem()
        if problem:
            stop(*problem)
        # before any question: nothing under setup's names that this install didn't make
        self.install_id = self.prev.get('install_id') or getattr(self, 'install_id', None) or new_install_id()
        guard(self.install_id, unlabelled_own(self.prev))
        self.out.ok('Docker and Docker Compose work')
        free = dockerenv.free_gb(str(STATE.parent))
        if free is None:
            return
        if free < screens.DISK_GB:          # a warning only: setup goes on
            self.out.warn(f'Only {free:.1f} GB of disk space free: AIOSubtitles needs about {screens.DISK_GB} GB.')
        else:
            self.out.ok(f'{free:.0f} GB of disk space free')

    # 2 -------------------------------------------------------------------------------------------------------
    def find_aios(self):
        self.show_step('aios')
        container, url = self.reach_aios()
        settings = aios_settings(self.status)
        public = (settings.get('baseUrl') or (container or {}).get('base_url') or url).rstrip('/')
        self.s.update(aios_url=url, aios_public=public, version=setup_version(), aios_version=self.status.get('version'))
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

    def reach_aios(self):
        """AIOStreams as this machine reaches it: its container (which one, when there are several), else the address
        asked. Sets self.aios and self.status; returns (the container or None, the address). ./setup --restore finds
        it this way too when no address is saved."""
        found = self.found = dockerenv.aios_containers()
        container = None
        if len(found) > 1:
            names = [(c['name'], f"{c['name']}  ({c['image']})") for c in found]
            pick = self.ask.choose('AIOS_CONTAINER', screens.LABELS['AIOS_CONTAINER'], names,
                                   current=self.prev.get('container'))
            container = next(c for c in found if c['name'] == pick)
        elif found:
            container = found[0]
        url = None
        # the container's own addresses first, even with an address given (a re-run's page fills in the last one): on
        # the bridge network trying them joins AIOStreams' network, which that address may need
        if container:
            for candidate in self.aios_urls(container):
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
            url = self.ask.text('AIOS_URL', screens.LABELS['AIOS_URL'], current=self.prev.get('aios_url'),
                                default=None if (self.prev.get('aios_url') or self.page or bridge())
                                else plan.AIOS_DEFAULT_URL, check=self._check_aios)
        if urllib.parse.urlsplit(url).scheme == 'http' and not plan.is_private_host(plan.host_of(url)):
            self.out.warn(PLAIN_HTTP.format(address=url))      # before the config's password is asked
        self.aios = Aios(url)
        return container, url

    def aios_urls(self, c):
        """Addresses setup can reach a container's AIOStreams at, best first. On the bridge network setup joins
        AIOStreams' Docker network and asks it by name, or reaches the machine through the gateway."""
        if not bridge():
            return dockerenv.host_urls(c)
        urls, gateway = [], PLACE.gateway
        if c['networks'] and dockerenv.join(c['networks'][0], PLACE.container):
            urls.append(f"http://{c['name']}:{c['port']}")
        if c['host_network'] and gateway:
            urls.append(f"http://{gateway}:{c['port']}")
        urls += [f'http://{gateway}:{port}' for ip, port in c['published'] if ip in ('0.0.0.0', '::', '') and gateway]
        if c['base_url']:
            urls.append(c['base_url'])
        return list(dict.fromkeys(urls))

    def _check_aios(self, value):
        """AIOStreams answering at the address typed. One without http:// or https:// is tried with https first unless
        its host is local or private, then with http: the status check sends no password."""
        value = value.strip().rstrip('/')
        if re.match(r'^https?://', value):
            tries = [value]
        elif plan.is_private_host(plan.host_of('http://' + value)):
            tries = ['http://' + value]
        else:
            tries = ['https://' + value, 'http://' + value]
        for address in tries:
            found = self._aios_at(address)
            if found[0]:
                break
        return found

    def _aios_at(self, value):
        value = re.sub(r'/(stremio|configure|jellyfin)(/.*)?$', '', value)
        parts = urllib.parse.urlsplit(value)
        # 127.0.0.1 is setup's own container on the bridge network: the machine is its gateway. An address whose port
        # can't be one goes to the client as typed, which refuses it as it does on every route.
        gateway = bridge() and plan.is_local_host(plan.host_of(value)) and plan.has_usable_port(parts)
        if gateway:
            value = urllib.parse.urlunsplit(parts._replace(netloc=f'{PLACE.gateway}:{parts.port or 80}'))
        try:
            self.status = Aios(value, timeout=15).status()
        except AiosError as e:
            if gateway:
                return False, CANT_REACH, value
            return False, e.message + (f' {e.fix}' if e.fix else ''), value
        return True, f"AIOStreams {self.status.get('version', '?')} answers there", value

    # 3 -------------------------------------------------------------------------------------------------------
    def read_viewer(self):
        self.show_step('config')

        def check_id(value):
            alias = plan.alias_from(value)       # an alias link (…/stremio/u/<name>/…) is looked up here
            if alias:
                try:
                    uuid = self.aios.alias_config_id(alias)
                except AiosError as e:
                    return False, {'ALIAS_UNKNOWN': ALIAS_UNKNOWN, 'ALIAS_NO_CONFIG': ALIAS_NO_CONFIG}.get(
                        e.code, e.message + (f' {e.fix}' if e.fix else '')), value
            else:
                uuid = plan.config_id_from(value)
            if not uuid:
                return False, 'That has no config ID in it (it looks like 1a2b3c4d-1234-…).', value
            if self.aios.config_exists(uuid) is False:
                return False, 'There is no config with that ID on this AIOStreams. Check it, or paste your install link.', value
            return True, '', uuid

        found = {}

        def check_password(value):
            try:
                found['config'] = self.aios.get_config(config_id, value)
            except AiosError as e:
                return False, e.message, value
            return True, 'Config found', value
        current = self.keys.get('VIEWER_UUID')
        config_id = self.ask.text('AIOS_CONFIG_ID', screens.LABELS['AIOS_CONFIG_ID'], current=current,
                                  shown=config_shown, check=check_id)
        password = self.ask.text('AIOS_CONFIG_PASSWORD', screens.LABELS['AIOS_CONFIG_PASSWORD'], secret=True,
                                 check=check_password)
        # the config changes only once both are right: an ID whose password is wrong leaves the last one as it was
        self.uuid, self.password = config_id, password
        self.viewer, self.viewer_encrypted = found['config']
        names = plan.debrid_names(self.viewer)
        subs = plan.subtitle_sources(self.viewer)
        self.out.say()
        (self.out.ok if names else self.out.warn)('Debrid services: ' + (', '.join(names) if names else 'none'))
        (self.out.ok if subs else self.out.warn)('Subtitle sources: ' + (', '.join(subs) if subs else 'none yet'))
        # Usenet: a config with no debrid service but a Usenet route goes on to the Usenet question
        self.usenet_services, self.usenet_why = plan.usenet_route(self.viewer)
        self.has_sync = not realdebrid.real_debrid_only(self.viewer) and \
            not plan.sync_config_problems(plan.sync_config(self.viewer))
        if realdebrid.real_debrid_only(self.viewer) and not self.usenet_services:
            raise Stop(realdebrid.REAL_DEBRID_ONLY, RD_WAY_OUT, page_fix=RD_WAY_OUT_PAGE)
        cleaned, ways = realdebrid.without_real_debrid(self.viewer)
        if realdebrid.SERVICE in realdebrid.debrid_in_use(self.viewer) and plan.debrid_names(cleaned):
            self.out.ok(f'Real-Debrid is never used: subsync reads through {", ".join(plan.debrid_names(cleaned))} only')
        if ways:
            self.out.note('subsync\'s own config leaves out:')
            for line in ways:
                self.out.note(f'  {line}')
        problems = plan.sync_config_problems(plan.sync_config(self.viewer), usenet=True)
        if problems and not self.usenet_services:
            raise Stop('subsync can\'t work with this config: ' + '; '.join(problems) + '.', NO_DEBRID_WAY_OUT,
                       page_fix=NO_DEBRID_WAY_OUT_PAGE)

    # 4 -------------------------------------------------------------------------------------------------------
    @keys_kept_unless_done
    def choices(self):
        self.show_step('choices')
        prev_langs = ', '.join(plan.names_of_codes(self.prev.get('languages', []))) or None

        def check_langs(value):
            langs, error = plan.parse_languages(value)
            return (False, error, value) if error else (True, '', value)
        text = self.ask.text('LANGUAGES', screens.LABELS['LANGUAGES'], current=prev_langs, default='English',
                             check=check_langs)
        self.languages = plan.parse_languages(text)[0]
        self.audio_when = self.ask_audio()
        self.audio = self.audio_when != 'no'
        self.usenet = False
        if self.usenet_services:
            self.usenet = self.ask.yesno('USENET', screens.LABELS['USENET'], self.prev.get('usenet', False))
        elif self.answers.get('USENET', '').strip().lower() in ('yes', 'y', 'on') and self.usenet_why:
            self.out.note(explain.SETUP_USENET_WHY[self.usenet_why])
        if not self.has_sync and not self.usenet:
            if realdebrid.real_debrid_only(self.viewer):
                raise Stop(realdebrid.REAL_DEBRID_ONLY, RD_WAY_OUT, page_fix=RD_WAY_OUT_PAGE)
            raise Stop(NO_USENET, NO_USENET_WAY_OUT, page_fix=NO_USENET_WAY_OUT_PAGE)
        self.source_keys()
        self.add_os = False
        if not plan.subtitle_sources(self.viewer):
            self.add_os = self.ask.yesno('ADD_OPENSUBTITLES', screens.LABELS['ADD_OPENSUBTITLES'], True)
        self.public_address()

    def known_audio(self):
        """The current answer to the audio question: no, pick or open. An install from before the question was
        three answers (audio on) reads as state/subsync.env starts it: pick, or open set by hand there
        (SUBSYNC_AUDIO_WHEN=open, or the setting it replaced, SUBSYNC_AUDIO_ON_PICK=off)."""
        if not self.prev.get('audio'):
            return 'no'
        when = self.prev.get('audio_when')
        return when if when in ('pick', 'open') else plan.audio_when_of(env_values())

    def ask_audio(self):
        """Files with no subtitles of their own: no, when I pick the file, or as soon as a file is opened. An
        answers file may still say yes or no, as the question was before: yes is when I pick the file. A number there
        is refused (plan.AUDIO_NUMBER_REFUSED): with --yes setup stops, else the question is asked; typed in the
        terminal, a number is its option as listed."""
        given = self.ask.answers.get('AUDIO')
        if given is not None and digits.whole(str(given).strip()) is not None:
            if getattr(self.ask, 'yes', False):
                stop(plan.AUDIO_NUMBER_REFUSED)
            self.out.warn(plan.AUDIO_NUMBER_REFUSED)
            del self.ask.answers['AUDIO']
        elif given is not None:
            self.ask.answers['AUDIO'] = plan.audio_answer(given) or given
        return self.ask.choose('AUDIO', screens.LABELS['AUDIO'], plan.AUDIO_ANSWERS, current=self.known_audio())

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
            return (True, '', chosen) if chosen else (False, limits.NOT_AN_AUDIO_LIMIT, value)
        return self.ask.text('AUDIO_BUDGET', f'How much of each file may the audio route read, in GB: '
                                             f'{plan.AUDIO_BUDGET_WORDS}?', current=current, default='1', check=check)

    def source_keys(self):
        """Optional free keys for subtitle sources subsync searches itself (TheSubtitleDB, OpenSubtitles V3+ and
        Gestdown need none)."""
        for key in ('SUBDL_KEY', 'WYZIE_KEY'):
            self.optional_key(key, screens.LABELS[key])
        self.tmdb_key_question(screens.LABELS['TMDB_KEY'])

    def tmdb_key_question(self, label):
        """The original-language lookup's optional TMDB key, with TMDB's notice under it (its terms ask for it; the page
        shows it under the key's field, screens._tmdb, so it is no row of the step there). A new key is checked with
        TMDB first (check_tmdb_key)."""
        value = self.optional_key('TMDB_KEY', label, check=self.check_tmdb_key)
        if not self.page:
            self.out.note(explain.TMDB_NOTICE)
        return value

    def check_tmdb_key(self, value):
        """A TMDB key given in this run, checked with TMDB once, with one request (origlang.check_key, as the site's
        settings page checks a person's own key, at the TMDB address subsync uses): refused, it is asked again, and not
        kept as the answer (the page's box is empty again: left empty, the step goes on without it); when TMDB doesn't
        answer, it is kept with a warning. The saved key, and one checked before in this run, aren't checked again."""
        if value in (self.keys.get('TMDB_KEY'), getattr(self, 'tmdb_checked', None)):
            return True, '', value
        url = env_values().get('SUBSYNC_TMDB_URL')
        accepted = origlang.check_key(Settings({'SUBSYNC_TMDB_URL': url} if url else {}), value)
        if accepted is False:
            self.ask.answers.pop('TMDB_KEY', None)
            return False, explain.PUBLIC_TEXTS['tmdb_refused'], value
        self.tmdb_checked = value
        if accepted is None:
            self.out.warn(TMDB_UNCHECKED)
            return True, '', value
        return True, TMDB_ACCEPTED, value

    def optional_key(self, key, label, check=None):
        """An optional key: a new one replaces the saved one, no answer keeps it, and the answer that removes it (the
        page's "Remove the saved key" box, remove typed in a terminal, KEY= in an answers file) removes it. `check`: the
        question's check of an answer (ui.Asker.text). Returns the key in use, or ''."""
        saved = self.keys.get(key)
        value = self.ask.text(key, label, secret=True, optional=True, current=saved, check=check)
        if value:
            self.keys[key] = value
        elif saved:
            del self.keys[key]
            self.out.ok(KEY_REMOVED.format(name=screens.LABELS[key].removesuffix(' (optional)')))
        return value

    def address_default(self):
        """subsync's address for your apps when there is no answer to keep or given (plan.public_default): it holds this
        machine's LAN address only when AIOStreams' own address is local. That address is looked up only then: on the
        bridge network the lookup starts a helper container (setup's own container has an address of its own there,
        which phones can't reach)."""
        local = plan.is_local_host(plan.host_of(self.s['aios_public']))
        return plan.public_default(self.s['aios_public'], lan_address() if local else '')[1]

    def public_address(self):
        prev = self.prev.get('public_base')
        # The default is used or shown only without a last answer to keep and, on the page, without one sent (the page
        # fills in the same one, address_default)
        answered = self.page and str(self.ask.answers.get('PUBLIC_ADDRESS') or '').strip()
        default = None if prev or answered else self.address_default()
        url = self.ask.text('PUBLIC_ADDRESS', screens.LABELS['PUBLIC_ADDRESS'], current=prev, default=default,
                            check=plan.check_public)
        kind = 'path' if plan.base_path_of(url) else 'port'
        port = urllib.parse.urlsplit(url).port
        if kind == 'port':
            port = port or (443 if url.startswith('https') else 80)
            bind = '0.0.0.0'
        else:
            # behind your web server: the last path address's port, never a port address's own (80, say: the one your
            # web server needs)
            kept = self.prev.get('port') if self.prev.get('public_kind') == 'path' else None
            port = int(kept or plan.SUBSYNC_PORT)
            bind = '127.0.0.1'
        if self.s['mode'] == 'host':
            bind = None   # host network: subsync listens on the port itself, at SUBSYNC_BIND (install)
        if self.port_in_use(port, '127.0.0.1' if bind == '127.0.0.1' else '0.0.0.0'):
            raise Problem('PUBLIC_ADDRESS', f'Port {port} on this machine is already in use.')
        self.s.update(public_base=url, public_kind=kind, port=port, bind=bind)

    def port_in_use(self, port, host):
        """Whether `port` on this machine (at `host`) is taken by anything but this install's own container, which holds
        its last port while it exists (both routes)."""
        held = self.prev.get('port') if dockerenv.container_state(CONTAINER) else None
        return port != held and port_taken(port, host)

    # 5 -------------------------------------------------------------------------------------------------------
    def limits(self):
        """The limits a personal install asks: the daily total and, with the audio route on, the audio limit (no language
        limit: on your own install you choose your languages yourself)."""
        self.show_step('limits')
        lim = limits.DAILY

        def check(value):
            chosen, error = limits.check(lim, value)
            return (False, error, value) if error else (True, '', chosen)
        self.limit_values = {lim.setting: self.ask.text(lim.setting, lim.label, current=limits.current(lim, env_values()),
                                                        check=check)}
        if self.usenet:
            lim = limits.USENET
            self.limit_values[lim.setting] = self.ask.text(lim.setting, lim.label,
                                                           current=limits.current(lim, env_values()), check=check)
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
        metadata = {p['ID']: p for p in aios_settings(self.status).get('presets') or []
                    if isinstance(p, dict) and 'ID' in p}
        self.new_viewer, self.changes = plan.viewer_change(self.viewer, self.manifest_url, metadata, self.add_os)
        self.sync = plan.sync_config(self.new_viewer, self.manifest_url)
        self.sync_exists = bool(self.keys.get('SYNC_UUID') and self.keys.get('SYNC_PASSWORD'))
        self.usenet_sync = plan.usenet_sync_config(self.new_viewer, self.manifest_url, subtitles=not self.has_sync) \
            if self.usenet else None
        self.usenet_exists = bool(self.keys.get('USENET_UUID') and self.keys.get('USENET_PASSWORD'))
        viewer = (f'Your config {short(self.uuid)}', [('+', line) for line in self.changes],
                  'Nothing else in it changes. A copy of it as it is now is saved first.' if self.changes
                  else 'no change (subsync is already in it)')
        sections = [viewer]
        if self.has_sync:
            sections.append(("subsync's own config " + ('(update)' if self.sync_exists else '(new)'), [],
                             'a copy of yours without Real-Debrid, Usenet and subsync, with nothing running in the '
                             'background, so subsync can find the file you are watching. It never appears in your '
                             'apps.'))
        if self.usenet:
            sections.append(("subsync's Usenet config " + ('(update)' if self.usenet_exists else '(new)'), [],
                             'only your Usenet searches and services, with nothing running in the background, so subsync '
                             'can find a file you play from Usenet when your debrid service doesn\'t have it. It never '
                             'appears in your apps.'))
        return sections

    def review_and_save(self):
        self.show_step('review')
        self.cache = None
        self.out.changes(self.plan_changes())
        self.cache = self.offer_cache_setting()
        if not self.ask.yesno('CONFIRM', 'Save these changes?', True):
            stop('Nothing was changed.', 'Run ./setup again when you are ready.')
        # subsync's own config first: subsync needs it to start. Yours changes only once subsync answers, since
        # AIOStreams fetches an addon's manifest when a config that lists it is saved.
        if self.has_sync:
            self.save_sync(self.sync, self.sync_exists)
        else:
            self.drop_config('SYNC', "Deleted subsync's own config")
        if self.usenet:
            self.save_sync(self.usenet_sync, self.usenet_exists, kind='USENET')
        else:
            self.drop_config('USENET', "Deleted subsync's Usenet config")

    def drop_config(self, kind, done):
        """A config of subsync's this setup no longer uses (Usenet turned off, or no debrid service any more): deleted
        on AIOStreams, its keys forgotten."""
        if not self.keys.get(f'{kind}_UUID'):
            return
        try:
            self.with_sign_in(lambda: self.aios.delete_config(self.keys[f'{kind}_UUID'], self.keys[f'{kind}_PASSWORD']))
            self.out.ok(done)
        except AiosError as e:
            if str(e.code) not in WRONG_PASSWORD_CODES:     # else already gone: nothing to delete
                raise
        self.keys.pop(f'{kind}_UUID', None)
        self.keys.pop(f'{kind}_PASSWORD', None)
        self.write_keys()

    def save_viewer(self):
        if self.changes:
            path = backup(self.viewer, self.uuid)
            self.out.ok(f'Saved a copy of your config: {shown(path)}')
            self.with_sign_in(lambda: self.aios.save_config(self.uuid, self.password, self.new_viewer))
            check, _ = self.aios.get_config(self.uuid, self.password)
            if not any(plan.is_subsync_preset(p, self.manifest_url) for p in check.get('presets') or []):
                stop('AIOStreams saved the config, but subsync is not in it when read back.',
                     f'Your saved copy is {shown(path)}; ./setup --restore puts it back.')
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

    def save_sync(self, sync, exists, kind='SYNC'):
        """subsync's own config (`kind` 'USENET': its Usenet config), saved into the one it has or made new."""
        name = "subsync's Usenet config" if kind == 'USENET' else "subsync's own config"
        uuid_key, password_key = f'{kind}_UUID', f'{kind}_PASSWORD'
        if exists:
            try:
                self.with_sign_in(lambda: self.aios.save_config(self.keys[uuid_key], self.keys[password_key], sync))
                _, encrypted = self.aios.get_config(self.keys[uuid_key], self.keys[password_key])
                setattr(self, f'{kind.lower()}_encrypted', encrypted)
                self.out.ok(f"Updated {name} ({short(self.keys[uuid_key])})")
                return
            except AiosError as e:
                if str(e.code) not in WRONG_PASSWORD_CODES:     # else gone: a new one is made
                    raise
                self.out.warn(f'{name[0].upper()}{name[1:]} is gone from AIOStreams; making a new one.'
                              if kind == 'USENET' else 'subsync\'s old config is gone from AIOStreams; making a new one.')
        password = secrets.token_urlsafe(24)
        uuid, encrypted = self.with_sign_in(lambda: self.aios.create_config(sync, password))
        self.keys.update({uuid_key: uuid, password_key: password})
        setattr(self, f'{kind.lower()}_encrypted', encrypted)
        self.write_keys()
        self.out.ok(f"Created {name} ({short(uuid)})")

    def with_sign_in(self, action):
        """Some AIOStreams instances only save configs for a signed-in account: ask once, then retry."""
        try:
            return action()
        except AiosError as e:
            if not needs_sign_in(e) or self.aios.signed_in:
                raise
            self.out.warn(e.message)
            self.sign_in(screens.SIGN_IN_TO_SAVE)
            return action()

    def sign_in(self, question):
        user = self.ask.text('AIOS_USERNAME', question)

        def check(value):
            try:
                self.admin = self.aios.sign_in(user, value)
            except AiosError as e:
                return False, e.message, value
            return True, 'Signed in', value
        self.ask.text('AIOS_PASSWORD', screens.LABELS['AIOS_PASSWORD'], secret=True, check=check)

    def offer_cache_setting(self):
        """AIOStreams keeps each addon's subtitle answer 5 minutes; without that for subsync, a synced copy shows the
        next time a film is opened instead of up to 5 minutes later. Needs AIOStreams' admin sign-in."""
        if not self.ask.yesno('CACHE', screens.LABELS['CACHE'], False):
            return None
        if not self.aios.signed_in:
            self.sign_in(screens.SIGN_IN_ADMIN)
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
        save_settings(self.s)      # the record of the change, which later runs' closing text and removing read
        _, after, _ = self.aios.subtitle_cache_setting()
        (self.out.ok if after == new else self.out.warn)('AIOStreams: no subtitle cache for subsync' if after == new
                                                         else 'AIOStreams did not keep the cache setting; synced copies may take 5 minutes.')

    # 7 -------------------------------------------------------------------------------------------------------
    def install(self):
        self.show_step('install')
        if self.s['public_kind'] == 'path':
            self.proxy_rule()
        def link(kind):
            if not self.keys.get(f'{kind}_UUID'):
                return ''
            return (f"{self.s['aios_internal']}/stremio/{self.keys[f'{kind}_UUID']}/"
                    f"{getattr(self, f'{kind.lower()}_encrypted')}/manifest.json")
        env = plan.subsync_env({
            'sync_manifest': link('SYNC'), 'sync_password': self.keys.get('SYNC_PASSWORD', ''),
            'usenet': self.usenet, 'usenet_manifest': link('USENET') if self.usenet else '',
            'usenet_password': self.keys.get('USENET_PASSWORD', '') if self.usenet else '',
            'public_base': self.s['public_base'],
            'addon_key': self.keys['ADDON_KEY'], 'languages': self.languages,
            'rewrite': plan.rewrite_rule(self.s['aios_public'], self.s['aios_internal']) if self.s['mode'] != 'remote' else '',
            'audio': self.audio, 'audio_when': self.audio_when, 'audio_budget': self.audio_budget,
            'subdl_key': self.keys.get('SUBDL_KEY', ''), 'wyzie_key': self.keys.get('WYZIE_KEY', ''),
            'tmdb_key': self.keys.get('TMDB_KEY', '')})
        if self.s['mode'] == 'host':
            env['SUBSYNC_PORT'] = str(self.s['port'])
            if self.s['public_kind'] == 'path':     # reached through your web server only (AIOStreams at 127.0.0.1)
                env['SUBSYNC_BIND'] = '127.0.0.1'
        asked = limits.personal(audio=False, usenet=self.usenet)
        env.update(limits.env_lines(asked, self.limit_values))
        # the setting SUBSYNC_AUDIO_WHEN replaced goes: the audio answer carries it (known_audio)
        env, kept = plan.merge_env(env, env_values(as_written=True),
                                   owned=limits.names([limits.DAILY, limits.USENET]) + ('SUBSYNC_AUDIO_ON_PICK',))
        write_env(STATE / 'subsync.env', env)
        if kept:
            self.out.ok(f"Kept the lines of state/subsync.env that ./setup doesn't set: {', '.join(kept)}")
        self.own_image()
        guard(self.s['install_id'], unlabelled_own(self.prev))
        write_compose(self.s)
        if self.audio:
            self.s['audio_when'] = self.audio_when
        self.s.update(languages=[three for _, _, three in self.languages], audio=self.audio, usenet=self.usenet,
                      audio_budget=self.audio_budget or getattr(self, 'audio_budget_kept', None),   # for when it's on again
                      manifest_url=self.manifest_url, installed=self.prev.get('installed') or time.strftime('%Y-%m-%d'))
        for kept in ('cache_before', 'added_opensubtitles'):
            self.s.setdefault(kept, self.prev.get(kept))
        save_settings(self.s)
        self.write_keys()
        self.start('subsync', 'subsync is running (it restarts by itself after a reboot)')
        if made_before_labels(self.prev):
            self.out.ok(TAKEN_OVER)
        self.write_setup_script()
        self.save_viewer()

    def own_image(self):
        """This install's id (kept from the last setup) and the image the service runs: setup's own, pinned. An install
        setup 1.3.0 or before made is taken over here: it gets an id, and its volume, which Docker can't label, is kept
        as it is (recorded in state/settings.json, declared external in its compose file) while Docker has it; once it
        is gone, Compose makes the usual labelled one instead (an external volume must exist). Its container and network
        are made again with the label when it starts."""
        self.s['install_id'] = self.prev.get('install_id') or getattr(self, 'install_id', None) or new_install_id()
        kept = data_volume(self.prev) if made_before_labels(self.prev) or self.prev.get('external_volume') else None
        if kept and not volume_gone(kept):
            self.s['external_volume'] = kept
        else:
            self.s.pop('external_volume', None)
        self.s['version'] = setup_version()
        if PLACE:
            self.s.update(image=PLACE.image, local=PLACE.local)
        else:
            self.s.setdefault('image', self.prev.get('image') or f'aiosubtitles:local-{setup_version()}')
            self.s.setdefault('local', True)

    def write_setup_script(self):
        """The image route's folder gets its ./setup."""
        if PLACE and PLACE.route == 'image':
            where.write_setup_script(PLACE, STATE.parent)

    def start(self, name, running):
        """The service from compose.yaml, up and answering. Nothing is built here: the image is setup's own."""
        self.out.say()
        self.out.working(STARTING.format(name=name))
        code, out, err = dockerenv.compose(STATE, 'up', '-d', '--remove-orphans', timeout=300)
        if code != 0:
            stop(f'Starting {name} failed.', last_lines(out, err))
        if not self.wait_healthy():
            stop(f'{name} started but does not answer.', f'See its log: docker logs {CONTAINER}')
        self.out.ok(running)

    def wait_healthy(self, seconds=90):
        """Whether the service answers within `seconds`: its /health asked inside its container every 2 seconds, as
        the new version's start check asks. Docker's own health check is no use here: on Docker Engine 23 and 24 its
        first probe runs only after 30 seconds."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            code, out, _ = dockerenv.exec_in(CONTAINER, HEALTH, timeout=20)
            if code == 0 and out.strip() == 'ok':
                return True
            time.sleep(2)
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
        # the rule's note, under it in the terminal and on the page (which shows the rule's words, not as rows)
        self.rule_note = (f'Web server running in Docker on AIOStreams\' network: use {plan.SUBSYNC_HOST}:'
                          f'{plan.SUBSYNC_PORT} instead of {target_host}.') if self.s['mode'] == 'network' else None
        self.out.say()
        self.out.wrap(self.rule_intro)
        for name, lines in self.web_server_rule.items():
            self.out.say(f'    {self.out.paint("1", name)}')
            for line in lines:
                self.out.say(f'      {line}')
        if self.rule_note:
            self.out.say(self.out.paint('2', f'     {self.rule_note}'))     # as a note prints, with the rule's lines
        self.ask.pause('Add the rule and reload your web server, then press Enter (or press Enter now and add it later):')

    # 8 -------------------------------------------------------------------------------------------------------
    def self_test(self):
        rows = self.self_test_checks()
        return self.offer_test_read(rows) and self.good

    def self_test_checks(self):
        """The checks that read nothing: the address apps use, AIOStreams asking subsync, subsync's config finding
        files (a search only). Returns the ready rows a test read could take."""
        self.show_step('check')
        self.good = True
        # a) the address apps use
        if self.public_health():
            self.out.ok(f"Your apps can reach subsync at {self.s['public_base']}")
        else:
            self.good = False
            self.out.warn(hosting.NO_ANSWER.format(address=self.s['public_base']))
            for line in hosting.NO_ANSWER_NOTES:
                self.out.note(line)
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
        # d) subsync's Usenet config answers: its manifest only, no search, so no indexer call is spent
        if getattr(self, 'usenet', False) and self.keys.get('USENET_UUID'):
            usenet_base = f"{self.s['aios_url']}/stremio/{self.keys['USENET_UUID']}/{self.usenet_encrypted}"
            try:
                self.aios.addon_get(usenet_base, '/manifest.json', timeout=40)
                self.out.ok("subsync's Usenet config answers (no search made, to spare your indexer's daily calls)")
            except AiosError:
                self.good = False
                self.out.warn("subsync's Usenet config doesn't answer. Run ./setup again, or send the output of "
                              "./setup --status with a bug report.")
        # c) subsync's config finds files (a search and a cache check; nothing is opened); none with Usenet only
        if not self.keys.get('SYNC_UUID'):
            self.ready_rows = []
            return []
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
        if self.ask.yesno('TEST_READ', screens.LABELS['TEST_READ'], False):
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
        if result.get('error') == 'unreadable':   # an index subsync can't parse: maybe a fault of subsync's
            self.out.note('subsync is installed. If many of your files get no synced copy, send the output of ./setup '
                          '--status with a bug report.')
        else:
            self.out.note('subsync is installed. Run ./setup again later, or send the output of ./setup --status '
                          'with a bug report.')
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
                 'When no copy is clearly closer than the original, no synced copy is added: the originals stay, and '
                 'the one that fits the file best is listed once as "<Language> (original)".',
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
        return self.closing_title(good), lines, after

    def closing_title(self, good):
        return 'Done' if good else WARNED if self.page else WARNED_ABOVE

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
    ROUTES = [(value, title) for value, title, _ in screens.WAYS]

    def choose_route(self):
        """Install it for yourself, or host a public instance. Where it is installed, its route, not asked again: an
        answer for the other one stops, since setup never turns one into the other (it is removed first)."""
        installed = (self.prev.get('route') or 'yourself') if self.prev.get('installed') else None
        if installed and 'ROUTE' not in self.answers:
            self.route = installed
        else:
            self.route = self.ask.choose('ROUTE', screens.LABELS['ROUTE'], self.ROUTES,
                                         current=self.prev.get('route') or 'yourself')
        if installed and self.route != installed:
            raise Stop(*ROUTE_STOPS[self.route])
        return self.route

    def host_address(self):
        self.show_step('address', 'host')
        env = env_values()
        kept = self.prev.get('public_base') if self.prev.get('route') == 'host' else None
        self.address = self.ask.text('HOST_ADDRESS', screens.LABELS['HOST_ADDRESS'], current=kept,
                                     check=hosting.check_address)
        self.trusted_proxy = self.ask.text('SUBSYNC_TRUSTED_PROXY', screens.LABELS['SUBSYNC_TRUSTED_PROXY'],
                                           current=env.get('SUBSYNC_TRUSTED_PROXY'), default=hosting.TRUSTED_PROXY)
        # a public instance keeps its own port on a re-run; a personal install's (80, say) is never taken over. The port
        # is checked unless this install's own container holds it.
        port = int(self.prev.get('port') or hosting.PORT) if self.prev.get('route') == 'host' else hosting.PORT
        if self.port_in_use(port, '127.0.0.1'):
            stop(f'Port {port} on this machine is already in use.')
        self.s.update(route='host', public_base=self.address, port=port, version=setup_version())

    def host_signups(self):
        """The invite code (suggested on a first setup; emptied, anyone may sign up) and the secret, made once."""
        self.show_step('signups', 'host')
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

    @keys_kept_unless_done
    def host_choices(self):
        self.show_step('hostchoices', 'host')
        for key in ('SUBDL_KEY', 'SUBSYNC_SUBSOURCE_KEY'):
            self.optional_key(key, screens.LABELS[key])
        # the original-language lookup: a TMDB key for everyone, or (with none) each person's own, or Wikidata alone
        self.host_tmdb_personal = False
        if self.tmdb_key_question(screens.TMDB_FOR_EVERYONE):
            self.ask.answers.pop('SUBSYNC_TMDB_PERSONAL', None)     # a key for everyone: never each person's too
        else:
            kept = plan.is_on(env_values().get('SUBSYNC_TMDB_PERSONAL', 'off'))
            self.host_tmdb_personal = self.ask.yesno('SUBSYNC_TMDB_PERSONAL', screens.LABELS['SUBSYNC_TMDB_PERSONAL'],
                                                     kept)
        kept = plan.is_on(env_values().get('SUBSYNC_USENET', 'off'))
        self.host_usenet = self.ask.yesno('SUBSYNC_USENET', screens.LABELS['SUBSYNC_USENET'], kept)
        kept = plan.is_on(env_values().get('SUBSYNC_AUDIO_ROUTE', 'off'))
        self.host_audio = self.ask.yesno('SUBSYNC_AUDIO_ROUTE', screens.LABELS['SUBSYNC_AUDIO_ROUTE'], kept)

    def host_limits(self):
        """Every limit, in its three groups, the current value or subsync's default filled in."""
        self.show_step('hostlimits', 'host')
        env = env_values()
        self.limit_values = {}
        for lim in limits.hosting(getattr(self, 'host_usenet', True), getattr(self, 'host_audio', False)):
            def check(value, lim=lim):
                chosen, error = limits.check(lim, value, places=self.places())
                return (False, error, value) if error else (True, '', chosen)
            self.limit_values[lim.setting] = self.ask.text(lim.setting, lim.label, current=limits.current(lim, env),
                                                           check=check)

    def places(self):
        """The files synced at a time as answered (asked before the audio syncs at a time): its number, or with
        auto this machine's processors (subsync's container runs without a processor limit); None before it is asked."""
        value = self.limit_values.get('SUBSYNC_SYNCS_AT_ONCE')
        if value == 'auto':
            return os.cpu_count() or 1
        return digits.whole(value) if value else None

    def host_install(self):
        self.show_step('hostinstall', 'host')
        if self.secret_new:
            self.out.ok('Made the secret key and kept it in state/subsync.env, readable only by your user.')
        answers = {'HOST_ADDRESS': self.address, 'SUBSYNC_TRUSTED_PROXY': self.trusted_proxy,
                   'SUBSYNC_PUBLIC_INVITE': self.invite, 'SUBDL_KEY': self.keys.get('SUBDL_KEY', ''),
                   'SUBSYNC_SUBSOURCE_KEY': self.keys.get('SUBSYNC_SUBSOURCE_KEY', ''),
                   'TMDB_KEY': self.keys.get('TMDB_KEY', '')}
        env = hosting.public_env(answers, self.secret, self.limit_values, usenet=getattr(self, 'host_usenet', False),
                                 audio=getattr(self, 'host_audio', False),
                                 tmdb_personal=getattr(self, 'host_tmdb_personal', False))
        env, kept = plan.merge_env(env, env_values(as_written=True), owned=hosting.OWNED)
        write_env(STATE / 'subsync.env', env)
        self.secret_new = False
        if kept:
            self.out.ok(f"Kept the lines of state/subsync.env that ./setup doesn't set: {', '.join(kept)}")
        self.own_image()
        guard(self.s['install_id'], unlabelled_own(self.prev))
        write_compose(self.s)
        self.s.update(installed=self.prev.get('installed') or time.strftime('%Y-%m-%d'))
        save_settings(self.s)
        self.write_keys()
        self.start('AIOSubtitles', 'AIOSubtitles is running (it restarts by itself after a reboot)')
        if made_before_labels(self.prev):
            self.out.ok(TAKEN_OVER)
        self.write_setup_script()
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
        self.show_step('hostcheck', 'host')
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
        return self.closing_title(good), lines, []

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
            order = [k for k in OUTCOMES if k in results] + sorted(k for k in results if k not in OUTCOMES)
            checked = ', '.join(f'{results[k]} {OUTCOMES.get(k, k)}' for k in order) or 'none yet'
            rows.append(('ok', f"subtitle requests since start: {st.get('subtitle_requests', 0)}; waiting jobs: {st.get('queue', 0)}",
                         f"synced copies offered: {results.get('synced', 0)}; subtitles checked: {sum(results.values())} "
                         f"({checked}); read today: {st.get('bytes_today', 0) / 1e6:.1f} MB"))
            if st.get('backing_off'):
                rows.append(('warn', 'backing off after a refused read (it waits before reading again)', None))
        when = s.get('audio_when') if s.get('audio_when') in ('pick', 'open') else plan.audio_when_of(env_values())
        audio = (f"on when {'opened' if when == 'open' else 'picked'}, up to {s.get('audio_budget') or '1'} GB per file"
                 if s.get('audio') else 'off')
        details = [('AIOStreams', s.get('aios_public')), ('languages', ', '.join(s.get('languages', []))),
                   ('audio', audio), ('config', short(keys.get('VIEWER_UUID', ''))),
                   ("subsync's config", short(keys.get('SYNC_UUID', '')))] \
            + ([("subsync's Usenet config", short(keys['USENET_UUID']))] if keys.get('USENET_UUID') else []) \
            + [('logs', f'docker logs {CONTAINER}')]
        return rows, details

    def remove(self):
        """Takes subsync out of the config your apps use, deletes subsync's own configs and removes the container with
        its stored copies (./setup --uninstall, and Remove subsync on the page). A removal stopped half way is finished
        by the next one. `self.stays`: what is left."""
        s, keys = self.prev, self.keys
        if not s:
            stop('subsync is not set up here.')
        self.aios = saved_aios(s)
        self.out.say(self.out.paint('1', '  Removing subsync'))
        uuid = keys.get('VIEWER_UUID')

        def check_password(value):
            try:
                self.viewer, _ = self.aios.get_config(uuid, value)
            except AiosError as e:
                return False, e.message, value
            return True, '', value
        password = self.ask.text('AIOS_CONFIG_PASSWORD', screens.CONFIG_PASSWORD.format(config=short(uuid)),
                                 secret=True, check=check_password)
        new, removed = plan.without_subsync(self.viewer, s.get('manifest_url'))
        self.out.wrap(screens.remove_text(keys, f' ({removed} entr{"y" if removed == 1 else "ies"})'))
        added = s.get('added_opensubtitles')
        if added and any(p.get('instanceId') == added for p in new.get('presets') or []):
            if self.ask.yesno('REMOVE_OPENSUBTITLES', screens.LABELS['REMOVE_OPENSUBTITLES'], True):
                new['presets'] = [p for p in new['presets'] if p.get('instanceId') != added]
                removed += 1
        if not self.ask.yesno('REMOVE', 'Remove subsync?', False):       # its own yes, never CONFIRM's
            stop('Nothing was changed.')
        if (STATE / 'compose.yaml').exists():
            guard(s.get('install_id'), unlabelled_own(s))
        if removed:
            path = backup(self.viewer, uuid)
            self.with_sign_in(lambda: self.aios.save_config(uuid, password, new))
            self.out.ok(f'Your config no longer lists subsync (copy before: {shown(path)})')
        for kind, done in (('SYNC', 'Deleted subsync\'s own config'), ('USENET', "Deleted subsync's Usenet config")):
            if keys.get(f'{kind}_UUID'):
                try:
                    self.with_sign_in(lambda: self.aios.delete_config(keys[f'{kind}_UUID'], keys[f'{kind}_PASSWORD']))
                    self.out.ok(done)
                except AiosError as e:
                    if str(e.code) not in WRONG_PASSWORD_CODES:     # else already gone: an earlier removal deleted it
                        self.out.warn(e.message)
        if (STATE / 'compose.yaml').exists():
            self.take_down()
        if s.get('cache_before'):
            self.out.note('AIOStreams\' subtitle cache setting for subsync stays; it does nothing without subsync.')
        for name in ('settings.json', 'secrets.env', 'subsync.env', 'compose.yaml'):
            (STATE / name).unlink(missing_ok=True)
        self.out.ok('Done.')
        self.stays = [f"the subsync image (about 1 GB): docker image rm {image_handle(s.get('image'))}"]
        if s.get('public_kind') == 'path':
            self.stays.append(f"the rule in your web server for {s.get('public_base')}: remove it there and reload the web server")
        self.stays.append(f'this folder, {host_folder()}, with copies of your config in state/backups/ (./setup --restore FILE puts one back)')
        self.say_what_stays()

    def remove_host(self):
        """Stops a public instance and removes its container with everyone's records and stored copies, once the box
        saying so is ticked (in a terminal: answered yes); the sync configs it made on people's AIOStreams stay
        (opening a record needs their link)."""
        s = self.prev
        if not s:
            stop('subsync is not set up here.')
        if not self.ask.yesno('REMOVE', screens.DELETE_RECORDS, False):     # its own yes, never CONFIRM's
            if self.page:
                raise Problem('REMOVE', screens.TICK_TO_REMOVE)
            stop('Nothing was changed.')
        if (STATE / 'compose.yaml').exists():
            guard(s.get('install_id'), unlabelled_own(s))
            self.take_down()
        for name in ('settings.json', 'secrets.env', 'subsync.env', 'compose.yaml'):
            (STATE / name).unlink(missing_ok=True)
        self.out.ok('Done.')
        folder = f'this folder, {host_folder()}'
        # a public instance's state/backups/ holds the databases' copies made before updates, never a config's
        if any(real_folder(path) for path in (STATE / 'backups').glob('databases-*')):
            folder += ", with the database copies made before updates in state/backups/, which hold everyone's records"
        self.stays = [f"the subsync image (about 1 GB): docker image rm {image_handle(s.get('image'))}",
                      f"the rule in your web server for {s.get('public_base')}: remove it there and reload the web server",
                      folder]
        self.say_what_stays()

    def take_down(self):
        """The service's container removed with its stored copies; the volume kept from an install setup 1.3.0 or
        before is removed by its name after it, since Compose never removes a volume its file declares external. When
        Docker fails, setup stops before any of its files is deleted, so removing it again finishes the job."""
        code, out, err = dockerenv.compose(STATE, 'down', '-v', timeout=300)
        if code == 0 and self.prev.get('external_volume'):
            code, out, err = dockerenv.run(['docker', 'volume', 'rm', self.prev['external_volume']], timeout=120)
            if 'no such volume' in err.lower():       # gone already: a removal stopped after removing it
                code = 0
        if code != 0:
            stop(REMOVE_FAILED, last_lines(out, err))
        self.out.ok('Removed the subsync container and its stored copies')

    def say_what_stays(self):
        self.out.say()
        self.out.say('  These stay, in case you set it up again; remove them when you no longer need them:')
        for line in self.stays:
            self.out.say(f'    • {line}')

    # ------------------------------------------------------------------------------------- updates
    def update(self):
        """The page's Update, or ./setup --update: the service moves to this setup's own version."""
        if not self.prev.get('installed'):
            stop('subsync is not set up here yet.', 'Run ./setup')
        if made_before_labels(self.prev):       # its image was built from code since replaced: no switch, no way back
            raise Stop(update.EARLY_INSTALL_PAGE if self.page else update.EARLY_INSTALL)
        ready = update.compare(self.prev, PLACE.image if PLACE else None, setup_version())
        if ready.how == 'newest':
            raise Stop(update.NEWEST)
        if ready.how == 'newer-here':
            raise Stop(update.NEWER_HERE.format(running=self.prev.get('version'), setup=setup_version()))
        return self.switch(PLACE.image, PLACE.local, setup_version())

    def rollback(self, restore=False):
        """./setup --rollback, and the page's button: back to the version before the last update, with the databases
        from before that update when `restore`."""
        prev = self.prev.get('previous')
        if not prev:
            raise Stop(update.NO_ROLLBACK)
        copy = prev.get('databases') if restore else None
        if copy and not real_folder(STATE / 'backups' / copy):
            raise Stop(update.FAILED.format(reason=f'{copy} is not in state/backups', version=self.prev.get('version')))
        return self.switch(prev['image'], prev.get('local', False), prev['version'], back=True, restore=copy)

    def route_check(self, out=None):
        """The route's own check from this machine: a public instance's address and sign-up page, or the address apps
        use. Shown on `out`; silent without it."""
        out = out or Rows()
        if self.s.get('route') == 'host':
            return hosting.check(self.s['public_base'], out)
        if self.public_health():
            out.ok(f"Your apps can reach subsync at {self.s['public_base']}")
            return True
        out.warn(hosting.NO_ANSWER.format(address=self.s['public_base']))
        return False

    def switch(self, image, local, version, back=False, restore=None):
        """Copy the databases; check `image` while the old version keeps running; switch; check again; put the old
        version back (with the copy when it needs it) if the new one doesn't answer."""
        s = self.s = dict(self.prev)
        old = {'image': s.get('image'), 'local': bool(s.get('local')), 'version': s.get('version')}
        guard(s.get('install_id'), unlabelled_own(s))
        held = self.route_check()            # what held before: a check that failed already can't fail the update
        needed = {(s.get('previous') or {}).get('databases'), restore} - {None}     # never pruned: a rollback needs them
        copy = self.copy_databases(old['version'], keep=needed)
        self.out.working(update.CHECKING.format(version=version, old=old['version']))
        good, reason = dockerenv.check_image(image, plan.project(), s['install_id'])
        if not good:
            raise Stop(update.FAILED.format(reason=reason.rstrip('.'), version=old['version']))
        self.out.ok(update.CHECKED)
        self.out.working(update.SWITCHING.format(version=version))
        s.update(image=image, local=bool(local), version=version)
        write_compose(s)
        if restore:        # put back first: the version it is for may not read the databases as they are now
            answers = self.databases_back(image, STATE / 'backups' / restore)
        else:
            answers = dockerenv.compose(STATE, 'up', '-d', '--remove-orphans', timeout=300)[0] == 0 \
                and self.wait_healthy() and self.databases_ok()
        if answers and held and not self.route_check():
            answers = False
        if not answers:
            self.go_back(old, copy, version, restored=bool(restore))
        if back:
            s.pop('previous', None)
        else:
            s['previous'] = dict(old, databases=copy.name)
        save_settings(s)
        self.out.ok(update.SWITCHED.format(version=version))
        if not back:
            self.out.note(update.KEPT.format(version=old['version']))
            self.out.note(update.OLD_IMAGES.format(image=image_handle(old['image'])))
        return self.route_check(self.out)

    def go_back(self, old, copy, new_version, restored=False):
        """The new version didn't answer: the old one again, on the data as it is, else on the copy. When this switch
        has put other databases back (`restored`, a rollback's), the data as it is isn't the old version's any more: it
        goes back on the copy at once, so everything is as before the switch. Always raises."""
        write_compose(dict(self.s, **old))
        message = update.ROLLED_BACK.format(new=new_version, old=old['version'])
        if restored:
            if self.databases_back(old['image'], copy):
                raise Stop(message)
        else:
            dockerenv.compose(STATE, 'up', '-d', '--remove-orphans', timeout=300)
            if self.wait_healthy() and self.databases_ok():
                raise Stop(message)
            if self.databases_back(old['image'], copy):
                raise Stop(message + update.WITH_DATABASES)
        raise Stop(update.STILL_DOWN.format(old=old['version'], container=CONTAINER))

    def databases_back(self, image, folder):
        """The databases of a copy in `folder` put back into the service's volume by `image`, with the service
        stopped, then started again. True when it answers."""
        folder = Path(folder)
        files = {name: (folder / name).read_bytes() for name in sorted(plain_databases(folder))}
        dockerenv.compose(STATE, 'stop', timeout=120)
        good, _ = dockerenv.restore_databases(image, data_volume(self.s), files)
        dockerenv.compose(STATE, 'up', '-d', '--remove-orphans', timeout=300)
        return good and self.wait_healthy() and self.databases_ok()

    def databases_ok(self):
        """Whether the running service reads its databases (/health answers without them): its read-only status
        command, which opens both."""
        code, out, _ = dockerenv.exec_in(CONTAINER, ['python', '-m', 'subsync.hostcmd', 'status'], timeout=60)
        try:
            return code == 0 and isinstance(json.loads(out), dict)
        except ValueError:
            return False

    def copy_databases(self, old_version, keep=()):
        """A checked copy of the service's databases, out of its container, the newest three kept. Returns its folder."""
        self.out.working(update.COPYING)
        stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
        inside = f'/data/before-update-{stamp}'
        code, out, err = dockerenv.exec_in(CONTAINER, ['python', '-m', 'subsync.hostcmd', 'copy-databases', inside],
                                           timeout=600)
        backups = STATE / 'backups'
        dest = backups / f'databases-{stamp}'
        if code == 0:
            backups.mkdir(parents=True, exist_ok=True)
            backups.chmod(0o700)
            own(backups)
            code, out, err = dockerenv.run(['docker', 'cp', f'{CONTAINER}:{inside}', str(dest)], timeout=600)
            if code == 0 and not real_folder(dest):
                code, out, err = 1, '', update.COPY_NOT_A_FOLDER
            if code != 0:
                remove_entry(dest)      # what the copy that failed left: never kept as one of the copies
        dockerenv.exec_in(CONTAINER, ['python', '-c', f'import shutil; shutil.rmtree({inside!r}, ignore_errors=True)'])
        if code != 0:
            reason = ((err or out).strip().splitlines() or ['no answer'])[-1]
            raise Stop(update.FAILED.format(reason=reason.rstrip('.'), version=old_version))
        databases = plain_databases(dest)
        for path in list(dest.iterdir()):
            if path.name not in databases:
                remove_entry(path)      # a link above all: only the databases' own files are kept
        dest.chmod(0o700)
        own(dest)
        for name in databases:
            (dest / name).chmod(0o600)
            own(dest / name)
        update.keep_newest(backups, 'databases-', 3, keep=set(keep) | {dest.name})
        self.out.ok(update.SAVED_DATABASES.format(path=shown(dest)))
        return dest


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
