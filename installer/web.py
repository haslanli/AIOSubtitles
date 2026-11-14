"""The setup page: a temporary web page on this machine, served by ./setup itself.

It listens on 127.0.0.1 only, at a free port, and answers only under a one-time random key in the address and only to
a Host of 127.0.0.1 or localhost at that port (a page elsewhere in the same browser can't guess it, and DNS rebinding
gets nothing). Every page has the public site's security header and nothing inline. Passwords and keys typed in stay
in this process's memory for the run and are never sent back in a page. The work of each step is installer/engine.py;
long work (installing, checking, updating) runs in the background once, and its page loads itself again every 2
seconds until it is done. The page stops when setup is done, after 60 minutes without a request, or with Ctrl-C.
"""
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from installer import dockerenv, engine, limits, plan, screens, update
from installer.aios import AiosError
from installer.ui import Stop
from subsync import ui

# the public site's pages' header (subsync/subsync/public.py PAGE_HEADERS; a test keeps them equal)
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
                               "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store"}
STATIC_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
MAX_BODY = 16 * 1024
IDLE_S = 3600
DONE_S = 60          # the page stays up this long after setup is done, so the browser has the last page whole
FONT = re.compile(r'^/static/fonts/([A-Za-z0-9-]+\.(?:ttf|txt))$')

STOPS = ('Setup is done; the page has stopped.',)


def terminal_lines(port, url):
    """What ./setup prints when the page starts."""
    return ['Setup runs in your browser. Open this address on this machine:', f'  {url}',
            'On another computer, run this there first, then open the same address:',
            f'  ssh -L {port}:127.0.0.1:{port} you@this-server   (your usual ssh login to this machine)',
            'The page stops when you finish, after 60 minutes without use, or with Ctrl-C.']


class Session:
    """One run through a route's steps: its engine, what each step found, and its background work."""

    def __init__(self, route, answers):
        self.route = route
        self.e = engine.Engine(dict(answers), page=True)
        self.e.route = route
        self.done, self.rows, self.problems, self.stops, self.asks = set(), {}, {}, {}, {}
        self.saved = self.test_read = False
        self.jobs = {}
        self.lock = threading.Lock()

    @property
    def ids(self):
        return screens.HOSTING_IDS if self.route == 'host' else screens.PERSONAL_IDS

    def run(self, step, work):
        """Runs one step's work; keeps its rows and how it ended. True when it went through."""
        out, start = self.e.out, len(self.e.out.rows)
        ok = False
        for kept in (self.problems, self.stops, self.asks):
            kept.pop(step, None)
        try:
            work()
            ok = True
        except engine.Problem as p:
            (self.asks.__setitem__(step, p.field) if p.missing else self.problems.__setitem__(step, (p.field, p.message)))
        except Stop as s:
            self.stops[step] = (s.message, s.page_fix)
        except AiosError as a:
            self.stops[step] = (a.message, a.fix)
        if not ok:      # work that stopped: a line it was on isn't turning any more
            for row in out.rows[start:]:
                if row[0] == 'now':
                    row[0] = 'next'
        self.rows[step] = [tuple(r) for r in out.rows[start:]]
        if ok:
            self.done.add(step)
            if step in self.ids:      # a step done again: the ones after it are to be done again too
                self.done -= set(self.ids[self.ids.index(step) + 1:])
        return ok

    def start_job(self, step, work):
        """Starts a step's background work once; a second start while it runs, or after it ran, does nothing."""
        with self.lock:
            if step in self.jobs:
                return
            job = {'start': len(self.e.out.rows), 'running': True}
            self.jobs[step] = job

        def go():
            try:
                self.run(step, work)
            finally:
                job['running'] = False
        threading.Thread(target=go, daemon=True).start()

    def again(self, step):
        with self.lock:
            if step in self.jobs and not self.jobs[step]['running']:
                del self.jobs[step]

    def running(self, step):
        return step in self.jobs and self.jobs[step]['running']

    def live(self, step):
        """The rows of a step, while its work runs too."""
        if self.running(step):
            return [tuple(r) for r in self.e.out.rows[self.jobs[step]['start']:]]
        return self.rows.get(step, [])


class App:
    def __init__(self, port=0, idle_s=IDLE_S, fonts_dir=None, resume=None, no_browser=False):
        self.key = secrets.token_urlsafe(24)
        self.no_browser = no_browser
        self.port, self.idle_s, self.fonts_dir = port, idle_s, fonts_dir
        self.last = time.monotonic()
        self.finished = threading.Event()
        self.finishing = None
        self.session = None
        self.after = None            # the page after setup's own engine and work (updating, removing)
        self.changing = False
        self.ready = None            # what update.look found when the page after setup opened
        self.resume = resume

    # ------------------------------------------------------------------------------------- HTTP
    def handle(self, method, path, headers, body):
        self.last = time.monotonic()
        host = headers.get('Host') or headers.get('host') or ''
        if host not in (f'127.0.0.1:{self.port}', f'localhost:{self.port}'):
            return 404, dict(PAGE_HEADERS, **{'Content-Type': 'text/plain'}), b''
        parsed = urllib.parse.urlsplit(path)
        if parsed.path.startswith('/static/'):
            return self.static(parsed.path)
        prefix = f'/{self.key}/'
        if not parsed.path.startswith(prefix):
            return self.page(screens.ended(), 404)
        if method == 'POST' and len(body) > MAX_BODY:
            return 413, dict(PAGE_HEADERS, **{'Content-Type': 'text/plain'}), b'Too large'
        form = urllib.parse.parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True) if method == 'POST' else {}
        name = parsed.path[len(prefix):].strip('/')
        try:
            return self.route(method, name, form)
        except Stop as s:                           # the engine stopped outside a step (state missing, ...)
            return self.page(screens.working('Set up AIOSubtitles', [], False, stop=(s.message, s.page_fix)))

    def page(self, html, status=200):
        return status, dict(PAGE_HEADERS, **{'Content-Type': 'text/html; charset=utf-8'}), html.encode()

    def go(self, name):
        return 303, dict(PAGE_HEADERS, **{'Location': f'/{self.key}/{name}', 'Content-Length': '0'}), b''

    def static(self, path):
        if path == '/static/app.css':
            return 200, dict(STATIC_HEADERS, **{'Content-Type': 'text/css; charset=utf-8'}), ui.APP_CSS.encode()
        if path == '/static/app.js':
            return 200, dict(STATIC_HEADERS, **{'Content-Type': 'text/javascript; charset=utf-8'}), ui.APP_JS.encode()
        m = FONT.match(path)
        for folder in (self.fonts_dir, os.path.join(ui.STATIC, 'fonts')):
            if m and folder and os.path.isfile(os.path.join(folder, m.group(1))):
                with open(os.path.join(folder, m.group(1)), 'rb') as f:
                    kind = 'font/ttf' if m.group(1).endswith('.ttf') else 'text/plain; charset=utf-8'
                    return 200, dict(STATIC_HEADERS, **{'Content-Type': kind}), f.read()
        return 404, dict(STATIC_HEADERS, **{'Content-Type': 'text/plain'}), b''

    # ------------------------------------------------------------------------------------- the routes
    def route(self, method, name, form):
        one = {k: v[-1] for k, v in form.items()}
        installed = bool(engine.load_settings().get('installed'))
        if name == '':
            if installed and not self.changing and (not self.session or self.finishing is not None):
                return self.after_setup() if method == 'GET' else self.go('')
            if method == 'POST':
                route = one.get('ROUTE') if one.get('ROUTE') in ('yourself', 'host') else 'yourself'
                answers = engine.Engine({}, page=True).prefill() if installed else {}
                self.session = Session(route, {k: v for k, v in answers.items() if k != 'installed'})
                self.session.e.answers['ROUTE'] = route
                return self.go('machine')
            prev = engine.load_settings().get('route') or 'yourself'
            return self.page(screens.start(self.session.route if self.session else prev))
        if name in ('update', 'updating', 'remove', 'answers'):
            return getattr(self, f'after_{name}')(method, one)
        s = self.session
        if not s or name not in s.ids + ('done',):
            return self.page(screens.ended(), 404)
        # a step before its turn goes back to the first one not done
        for earlier in s.ids[:s.ids.index(name) if name in s.ids else len(s.ids)]:
            if earlier not in s.done:
                return self.go(earlier)
        return getattr(self, f'step_{name}')(s, method, form, one)

    # ------------------------------------------------------------------------------------- both routes
    def step_machine(self, s, method, form, one):
        if 'machine' not in s.done and 'machine' not in s.stops:
            s.run('machine', s.e.check_machine)
        if method == 'POST' and 'machine' in s.done:
            return self.go(s.ids[1])
        return self.page(screens.machine(s.route, s.rows.get('machine', []), s.stops.get('machine')))

    # ------------------------------------------------------------------------------------- install it for yourself
    def step_aios(self, s, method, form, one):
        e = s.e
        if method == 'POST':
            if one.get('go') == 'next' and 'aios' in s.done and not any(k in one for k in ('AIOS_URL', 'AIOS_CONTAINER')):
                return self.go('config')
            for key in ('AIOS_URL', 'AIOS_CONTAINER'):
                if one.get(key):
                    e.answers[key] = one[key]
            s.run('aios', e.find_aios)
        elif 'aios' not in s.done and 'aios' not in s.asks and 'aios' not in s.problems:
            s.run('aios', e.find_aios)
        problem = s.problems.get('aios')
        ask = (problem or (None,))[0] or s.asks.get('aios')
        containers = [(c['name'], f"{c['name']}  ({c['image']})") for c in getattr(e, 'found', [])]
        value = e.answers.get(ask or '', '') or e.prefill().get(ask or '', '')
        if ask == 'AIOS_URL' and not value:
            value = 'http://127.0.0.1:3000'
        return self.page(screens.aios(s.rows.get('aios', []), ask=ask, value=value, problem=problem,
                                      containers=containers, stop=s.stops.get('aios')))

    def step_config(self, s, method, form, one):
        e = s.e
        if method == 'POST':
            password = one.get('AIOS_CONFIG_PASSWORD', '')
            if password:
                e.answers['AIOS_CONFIG_ID'] = one.get('AIOS_CONFIG_ID', '')
                e.answers['AIOS_CONFIG_PASSWORD'] = password
                s.run('config', e.read_viewer)
            elif one.get('go') == 'next' and 'config' in s.done:
                return self.go('choices')
            else:
                s.problems['config'] = ('AIOS_CONFIG_PASSWORD', '"Its password" needs an answer.')
        config_id = e.answers.get('AIOS_CONFIG_ID') or e.prefill().get('AIOS_CONFIG_ID', '')
        if 'config' in s.done:
            config_id = getattr(e, 'uuid', config_id)
        return self.page(screens.config(s.rows.get('config', []), config_id, s.problems.get('config'),
                                        s.stops.get('config')))

    def step_choices(self, s, method, form, one):
        e = s.e
        offer = not plan.subtitle_sources(e.viewer)
        if method == 'POST':
            chosen = [code for code in form.get('LANGUAGES', []) if code]
            e.answers['LANGUAGES'] = ', '.join(chosen)
            e.answers['AUDIO'] = 'yes' if one.get('AUDIO') else 'no'
            if getattr(e, 'usenet_services', None):
                e.answers['USENET'] = 'yes' if one.get('USENET') else 'no'
            if offer:
                e.answers['ADD_OPENSUBTITLES'] = 'yes' if one.get('ADD_OPENSUBTITLES') else 'no'
            for key in ('SUBDL_KEY', 'WYZIE_KEY'):
                if one.get(key):
                    e.answers[key] = one[key]
            e.answers['PUBLIC_ADDRESS'] = one.get('PUBLIC_ADDRESS', '')
            if s.run('choices', e.choices):
                return self.go('limits')
        filled = e.prefill()
        text = e.answers.get('LANGUAGES') or filled.get('LANGUAGES') or 'English'
        languages = [three for _, _, three in (plan.parse_languages(text)[0] or [])] or ['eng']
        audio = str(e.answers.get('AUDIO') or filled.get('AUDIO')) == 'yes'
        usenet = str(e.answers.get('USENET') or filled.get('USENET')) == 'yes' \
            if getattr(e, 'usenet_services', None) else None
        address = e.answers.get('PUBLIC_ADDRESS') or filled.get('PUBLIC_ADDRESS') or \
            plan.public_default(e.s['aios_public'], dockerenv.lan_address())[1]
        return self.page(screens.choices(languages, audio, address, s.problems.get('choices'), offer_opensubtitles=offer,
                                         add_opensubtitles=e.answers.get('ADD_OPENSUBTITLES', 'yes') == 'yes',
                                         usenet=usenet, stop=s.stops.get('choices')))

    def step_limits(self, s, method, form, one):
        e = s.e
        if method == 'POST':
            e.answers[limits.DAILY.setting] = one.get(limits.DAILY.setting, '')
            if one.get(limits.AUDIO.setting):
                e.answers['AUDIO_BUDGET'] = one[limits.AUDIO.setting]
            if one.get(limits.USENET.setting):
                e.answers[limits.USENET.setting] = one[limits.USENET.setting]
            if s.run('limits', e.limits):
                return self.go('review')
        values = {limits.DAILY.setting: e.answers.get(limits.DAILY.setting) or e.prefill()[limits.DAILY.setting],
                  limits.AUDIO.setting: e.answers.get('AUDIO_BUDGET') or e.known_audio_budget() or '1',
                  limits.USENET.setting: e.answers.get(limits.USENET.setting)
                  or limits.current(limits.USENET, engine.env_values())}
        return self.page(screens.limits('yourself', values, s.problems.get('limits'), audio=e.audio,
                                        usenet=getattr(e, 'usenet', False)))

    def step_review(self, s, method, form, one):
        e = s.e
        if method == 'POST':
            if s.saved:
                return self.go('install')
            e.answers['CACHE'] = 'yes' if one.get('CACHE') else 'no'
            for key in ('AIOS_USERNAME', 'AIOS_PASSWORD'):
                if one.get(key):
                    e.answers[key] = one[key]
            e.answers['CONFIRM'] = 'yes'
            if s.run('review', e.review_and_save):
                s.saved = True
                s.start_job('install', e.install)
                return self.go('install')
        sections = e.plan_changes()
        problem = s.problems.get('review')
        sign_in = s.asks.get('review') in ('AIOS_USERNAME', 'AIOS_PASSWORD') or (problem or ('',))[0] in (
            'AIOS_USERNAME', 'AIOS_PASSWORD')
        body = screens.review(sections, cache=e.answers.get('CACHE') == 'yes', sign_in=sign_in, problem=problem,
                              username=e.answers.get('AIOS_USERNAME', ''))
        if s.stops.get('review'):
            body = screens.review(sections, problem=('CONFIRM', s.stops['review'][0]))
        return self.page(body)

    def step_install(self, s, method, form, one):
        return self.installing(s, 'install', method, 'check')

    def installing(self, s, step, method, next_step):
        if method == 'POST' and step in s.done:
            return self.go(next_step)
        running = s.running(step)
        rule = getattr(s.e, 'web_server_rule', None)
        return self.page(screens.installing(s.route, s.live(step), running, s.stops.get(step), rule=rule,
                                            rule_intro=getattr(s.e, 'rule_intro', None)))

    def step_check(self, s, method, form, one):
        e = s.e
        if method == 'POST' and not s.running('check') and not s.running('check-read'):
            if one.get('TEST_READ') and not s.test_read and getattr(e, 'ready_rows', None):
                s.test_read = True
                s.start_job('check-read', lambda: setattr(e, 'read_good', e.test_read(e.ready_rows)))
                return self.go('check')
            if 'check' in s.done or ('check' in s.jobs and not s.running('check')):   # done, or it couldn't run
                s.done.add('check')
                return self.go('done')
        s.start_job('check', e.self_test_checks)
        running = s.running('check') or s.running('check-read')
        rows = s.live('check') + s.live('check-read')
        offer = bool(getattr(e, 'ready_rows', None)) and not s.test_read
        return self.page(screens.check(s.route, rows, running, offer_test_read=offer and not running,
                                       stop=s.stops.get('check') or s.stops.get('check-read')))

    def step_done(self, s, method, form, one):
        e = s.e
        if s.route == 'host':
            title, lines, after = e.host_summary_parts(getattr(e, 'good', False))
        else:
            title, lines, after = e.summary_parts(getattr(e, 'good', False) and getattr(e, 'read_good', True))
        self.finish_in(DONE_S)
        return self.page(screens.done(s.route, title, lines, after))

    # ------------------------------------------------------------------------------------- host a public instance
    def step_address(self, s, method, form, one):
        e = s.e
        if method == 'POST':
            e.answers['HOST_ADDRESS'] = one.get('HOST_ADDRESS', '')
            e.answers['SUBSYNC_TRUSTED_PROXY'] = one.get('SUBSYNC_TRUSTED_PROXY', '')
            if s.run('address', e.host_address):
                return self.go('signups')
        prev = e.prev if e.prev.get('route') == 'host' else {}
        return self.page(screens.host_address(e.answers.get('HOST_ADDRESS') or prev.get('public_base', ''),
                                              e.answers.get('SUBSYNC_TRUSTED_PROXY') or
                                              engine.env_values().get('SUBSYNC_TRUSTED_PROXY') or 'gateway',
                                              s.problems.get('address'), s.stops.get('address')))

    def step_signups(self, s, method, form, one):
        e = s.e
        if method == 'POST':
            e.answers['SUBSYNC_PUBLIC_INVITE'] = one.get('SUBSYNC_PUBLIC_INVITE', '')
            if s.run('signups', e.host_signups):
                return self.go('hostchoices')
        if not hasattr(e, 'invite'):
            e.host_signups()
        return self.page(screens.host_signups(e.invite, s.problems.get('signups')))

    def step_hostchoices(self, s, method, form, one):
        e = s.e
        if method == 'POST':
            for key in ('SUBDL_KEY', 'SUBSYNC_SUBSOURCE_KEY'):
                if one.get(key):
                    e.answers[key] = one[key]
            e.answers['SUBSYNC_USENET'] = 'yes' if one.get('SUBSYNC_USENET') else 'no'
            if s.run('hostchoices', e.host_choices):
                return self.go('hostlimits')
        usenet = e.answers.get('SUBSYNC_USENET', '') == 'yes' if 'SUBSYNC_USENET' in e.answers else \
            engine.env_values().get('SUBSYNC_USENET', 'off').strip().lower() in ('on', 'yes', 'true', '1')
        return self.page(screens.host_choices(s.problems.get('hostchoices'), usenet=usenet))

    def step_hostlimits(self, s, method, form, one):
        e = s.e
        if method == 'POST':
            for lim in limits.hosting(getattr(e, 'host_usenet', True)):
                if one.get(lim.setting):
                    e.answers[lim.setting] = one[lim.setting]
            if s.run('hostlimits', e.host_limits):
                s.start_job('hostinstall', e.host_install)
                return self.go('hostinstall')
        env = engine.env_values()
        usenet = getattr(e, 'host_usenet', True)
        values = {lim.setting: e.answers.get(lim.setting) or limits.current(lim, env) for lim in limits.hosting(usenet)}
        return self.page(screens.limits('host', values, s.problems.get('hostlimits'), usenet=usenet))

    def step_hostinstall(self, s, method, form, one):
        return self.installing(s, 'hostinstall', method, 'hostcheck')

    def step_hostcheck(self, s, method, form, one):
        if method == 'POST' and not s.running('hostcheck'):
            if one.get('go') == 'again':
                s.again('hostcheck')
            elif 'hostcheck' in s.jobs:
                s.done.add('hostcheck')
                return self.go('done')
        s.start_job('hostcheck', s.e.host_check)
        running = s.running('hostcheck')
        if not running and 'hostcheck' in s.jobs:
            s.done.add('hostcheck')
        return self.page(screens.check('host', s.live('hostcheck'), running, stop=s.stops.get('hostcheck')))

    # ------------------------------------------------------------------------------------- after setup
    def after_setup(self, problem=None):
        e = engine.Engine({}, page=True)
        rows, details = e.status_parts()
        if self.ready is None:
            self.ready = update.look(engine.REPO)        # only when this page opens
        prev = e.prev
        offer = bool(prev.get('added_opensubtitles'))
        return self.page(screens.after_setup(prev.get('route') or 'yourself', rows, details,
                                             prev.get('version') or engine.current_version(), self.ready, problem,
                                             offer_opensubtitles=offer,
                                             config=engine.short(e.keys.get('VIEWER_UUID', ''))))

    def after_answers(self, method, one):
        if method == 'POST':
            self.changing, self.session = True, None
        return self.go('')

    def after_remove(self, method, one):
        if method != 'POST':
            return self.go('')
        e = engine.Engine({k: v for k, v in one.items() if k in ('AIOS_CONFIG_PASSWORD', 'REMOVE_OPENSUBTITLES',
                                                                    'CONFIRM')}, page=True)
        host = e.prev.get('route') == 'host'
        if not host and 'REMOVE_OPENSUBTITLES' not in one:
            e.answers['REMOVE_OPENSUBTITLES'] = 'no'
        try:
            e.remove_host() if host else e.remove()
        except engine.Problem as p:
            return self.after_setup(problem=(p.field, p.message or '"Its password" needs an answer.'))
        except (Stop, AiosError) as s:
            return self.after_setup(problem=('AIOS_CONFIG_PASSWORD', s.message))
        self.finish_in(DONE_S)
        return self.page(screens.removed('Removing subsync', [tuple(r) for r in e.out.rows], getattr(e, 'stays', [])))

    def after_update(self, method, one):
        if method == 'POST' and (not self.after or not self.after.running('update')):   # again after a failure
            self.after = Session(engine.load_settings().get('route') or 'yourself', {})
            self.after.start_job('update', lambda: self.after.e.update(restart=self.restart))
        return self.go('updating')

    def after_updating(self, method, one):
        if not self.after:
            return self.go('')
        running = self.after.running('update')
        return self.page(screens.working('Updates', self.after.live('update'), running, self.after.stops.get('update')))

    def start_resumed(self):
        """On the new code after an update's restart: the second half, shown on the same page."""
        self.after = Session(engine.load_settings().get('route') or 'yourself', {})
        out = self.after.e.out
        out.rows += [['done', update.FETCHING.format(version=engine.current_version()), None],
                     ['done', update.RESTARTING, None]]
        self.after.start_job('update', lambda: self.after.e.finish_update(self.resume))

    def restart(self, old):
        """The update's restart: this process becomes setup on the new code, same port and key."""
        os.environ.update(AIOSUBTITLES_SETUP_PORT=str(self.port), AIOSUBTITLES_SETUP_KEY=self.key,
                          AIOSUBTITLES_SETUP_RESUME=old)
        if getattr(self, 'server', None):
            self.server.socket.close()
        os.execv(sys.executable, [sys.executable, '-I', str(engine.REPO / 'installer' / 'wizard.py')]
                 + (['--no-browser'] if self.no_browser else []))

    # ------------------------------------------------------------------------------------- stopping
    def finish_in(self, seconds):
        if self.finishing is None:
            self.finishing = time.monotonic() + seconds

    def should_stop(self):
        now = time.monotonic()
        return (self.finishing is not None and now >= self.finishing) or now - self.last > self.idle_s


class Handler(BaseHTTPRequestHandler):
    server_version = 'AIOSubtitles-setup'
    sys_version = ''

    def log_message(self, *args):       # nothing is logged: the address carries the key
        pass

    def _answer(self, method):
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            status, headers, body = 413, dict(PAGE_HEADERS, **{'Content-Type': 'text/plain'}), b'Too large'
        else:
            body_in = self.rfile.read(length) if length else b''
            status, headers, body = self.server.app.handle(method, self.path, dict(self.headers.items()), body_in)
        self.send_response(status)
        for name, value in headers.items():
            if name != 'Content-Length':
                self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._answer('GET')

    def do_POST(self):
        self._answer('POST')


def start(app):
    """Starts the page's server in the background; it stops by itself when the app says so."""
    server = ThreadingHTTPServer(('127.0.0.1', app.port), Handler)
    server.daemon_threads = True
    server.app, app.server = app, server
    app.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True).start()

    def watch():
        while not app.should_stop():
            time.sleep(0.1)
        server.shutdown()
        app.finished.set()
    threading.Thread(target=watch, daemon=True).start()
    return server


def serve(app, say=print, opener=None):
    """./setup's page: prints where it is, opens the browser on a desktop, and waits until it stops."""
    server = start(app)
    url = f'http://127.0.0.1:{app.port}/{app.key}/'
    for line in terminal_lines(app.port, url):
        say(line)
    if opener and (sys.platform == 'darwin' or os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        try:
            opener(url)
        except Exception:      # no browser here: the address above still works
            pass
    try:
        app.finished.wait()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        server.server_close()
    if stop_line(app):
        say(stop_line(app))
    return 0


def stop_line(app):
    """The last line, only when setup (or removing it) finished, not after the idle stop or Ctrl-C."""
    return STOPS[0] if app.finishing is not None else None
