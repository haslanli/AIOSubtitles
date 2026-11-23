"""Public mode: one subsync service for many people, each signed up with their own AIOStreams.

Each person who signed up has an encrypted record (users.py) that opens only with their personal link's token. While
their requests come in, they get their own copy of the self-hosted service: an App and a Worker with their languages,
working through the sync config the sign-up made on their AIOStreams. Everyone shares one result store, so a copy made
for one person serves everyone playing the same file, and one turn, so the service works on SUBSYNC_SYNCS_AT_ONCE files
at a time and never on the same file twice at once.

A person's personal link (/u/<token>/) opens their addon's address, their subtitle lists and their settings page; the
entries those lists name are under their read key (/r/<key>/, users.read_key), which opens nothing else.

A worker with nothing to do for a while stops, and its opened record leaves memory; the next request opens it again.
"""
import io
import logging
import re
import socket
import threading
import time
import urllib.parse

from . import digits, pages, ui
from .addon import MANIFEST_CACHE, App, Handler, Server, head_answer, is_entry, manifest
from .aiosconfig import MAX_LANGUAGES, PUBLIC_TIMEOUT, parse_languages
from .settings import AUDIO_CHOICES, Settings
from .recheck import Checker
from .signup import (SignupError, SignupGate, delete, forget_password, form_languages, public_api, refresh,
                     remove_tmdb_key, set_audio, set_languages, set_tmdb_key, signup, store_only)
from .sources import Aios
from .store import day_of
from .ui import FONT_HEADERS, PAGE_HEADERS, SCRIPT_HEADERS   # the setup page sends the same
from .users import READ_KEY, TOKEN
from .version import VERSION
from .worker import OverLimit, Safety, Turn, UsenetGate, Worker, audio_places

log = logging.getLogger("subsync.public")


def person_audio(record, settings):
    """A person's answer for files with no subtitles of their own: off, pick or open. Everyone who hasn't
    answered, the owner's account included, is on "pick" (the earlier pick-only rule); "open" is "pick" while
    the instance allows no opened syncs (SUBSYNC_USER_OPENED_PER_DAY 0)."""
    choice = record.get("audio")
    if choice not in AUDIO_CHOICES:
        return "pick"
    return "pick" if choice == "open" and settings.user_opened_per_day <= 0 else choice


def user_settings(env, record, read_key, instance=None):
    """A person's own settings: the service's, with their languages, their sync config, their Usenet config,
    the address of their subtitle entries (under their read key, users.read_key: never their personal link)
    and their own audio choice (the instance's audio route and limits, started as they chose; the instance's
    SUBSYNC_AUDIO_WHEN is not theirs). A record for stored copies only has no sync config: nothing to read
    through; one with Usenet only has only the Usenet config. `instance`: the service's own settings, made from `env`
    (the Pool passes the ones it has, so `env` is parsed once more only, for the person's own)."""
    if instance is None:
        instance = Settings(env)
    own = dict(env)
    own.update({"SUBSYNC_LANGUAGES": ",".join(record["languages"]),
                "SUBSYNC_SOURCE_MANIFEST": record.get("sync_link") or "",
                "SUBSYNC_SOURCE_PASSWORD": record.get("sync_password") or "",
                "SUBSYNC_USENET_MANIFEST": record.get("usenet_link") or "",
                "SUBSYNC_USENET_PASSWORD": record.get("usenet_password") or "",
                "SUBSYNC_PUBLIC_BASE": f"{instance.public_base}/r/{read_key}",
                "SUBSYNC_ADDON_KEY": "", "SUBSYNC_BASE_PATH": ""})
    if instance.public and instance.tmdb_personal:     # the person's own TMDB key, when the instance asks for one
        own["SUBSYNC_TMDB_KEY"] = record.get("tmdb_key") or ""
    s = Settings(own)
    s.audio_when = person_audio(record, s)
    return s


class UserLimits:
    """A person's own limits: files waiting, new files and bytes read per UTC day. The service-wide daily budget is
    checked by every read as before (worker.JobGuard)."""

    def __init__(self, settings, users, token):
        self.s, self.users, self.token = settings, users, token
        # the sources' fair shares count per person under this opaque key (not the token, never logged)
        self.who = (users.user_id(token) or "")[:16] or None
        self.rows_key = users.rows_key(token)   # their own rows in the shared store

    def may_queue(self, queued):
        return queued < self.s.user_queue

    def may_start_file(self):
        return self.users.usage(self.token)["files"] < self.s.user_files_per_day

    def file_started(self):
        self.users.add_usage(self.token, files=1)

    def before_read(self, length):
        if self.users.usage(self.token)["bytes"] + length > self.s.user_daily_bytes:
            raise OverLimit("bytes per day")

    def has_room(self, n):
        """Whether what is left of the person's data today covers n bytes: an audio sync starts only then."""
        return self.users.usage(self.token)["bytes"] + n <= self.s.user_daily_bytes

    def after_read(self, n):
        if n:
            self.users.add_usage(self.token, nbytes=n)

    def may_start_audio(self, opened=False):
        """The person's audio syncs today are below SUBSYNC_USER_AUDIO_PER_DAY and, for one started as soon as a
        file was opened, their opened ones below SUBSYNC_USER_OPENED_PER_DAY."""
        used = self.users.audio_usage(self.token)
        if used["syncs"] >= self.s.user_audio_per_day:
            return False
        return not opened or used["opened"] < self.s.user_opened_per_day

    def audio_started(self, opened=False):
        """A picked audio sync counts one; an opened one counts one of each."""
        self.users.add_audio(self.token, opened)

    def usenet_counts(self):
        """This person's Usenet listings and the day's stop (worker.UsenetGate), in their usage rows."""
        return UserUsenetCounts(self.users, self.token)


class UserUsenetCounts:
    def __init__(self, users, token):
        self.users, self.token = users, token

    def listings(self):
        return self.users.usenet_usage(self.token)["listings"]

    def add_listing(self):
        self.users.add_usenet_listing(self.token)

    def stopped(self):
        return self.users.usenet_usage(self.token)["stopped"]

    def stop(self):
        self.users.stop_usenet(self.token)


class StoreOnly:
    """The limits of a person answered from the shared store only (a record for stored copies, or no worker free
    now): nothing is queued or read."""
    who = None   # no person's key: the sources' fair shares count no one for them (Worker.who)
    rows_key = None   # nor rows of their own in the shared store (Worker._person_key)

    def may_queue(self, queued):
        return False

    def may_start_file(self):
        return False

    def file_started(self):
        pass

    def before_read(self, length):
        raise OverLimit("no worker free")

    def has_room(self, n):
        return False

    def after_read(self, n):
        pass

    def may_start_audio(self, opened=False):
        return False

    def audio_started(self, opened=False):
        pass


# What a person's App that stopped knew of the files they asked about (Worker.seen) goes to their next one for this
# long, so the entries of a list they got before still download: a player keeps a subtitle list an hour (AIOMetadata)
SEEN_KEPT_S = 3600


def _refusing(safety):
    """Whether a person's refusals in a row still count towards their next back-off (worker.Safety): some came, and the
    pause the last one started ended less than the longest back-off ago."""
    return safety.refusals > 0 and safety.clock() < safety.refused_until + safety.backoff_max_s


class Pool:
    """Everyone being served, by user id (never by token), each with an App kept between their requests: with a running
    worker, for up to SUBSYNC_ACTIVE_WORKERS people, else answering from the shared store only, as for a record for
    stored copies. Kept, an App knows the files its person asked about, which the entries of their lists need
    when they are downloaded (a copy is rebuilt from the person's own list). Their next App starts from what the
    last one had: their refusal back-off (doubling while refusals come in a row) and, for SEEN_KEPT_S, those files."""

    def __init__(self, settings, env, store, users, clock=time.time, start_workers=True):
        self.s, self.env, self.store, self.users, self.clock = settings, dict(env), store, users, clock
        self.start_workers = start_workers
        self.turn = Turn(settings.syncs_at_once or 1)
        # the audio route: its own places (SUBSYNC_AUDIO_AT_ONCE; auto is half the turn's), and
        # one speech detector for everyone
        self.audio_turn = Turn(audio_places(self.turn.places, settings.audio_at_once))
        self._vad, self._vad_lock = None, threading.Lock()
        # One set of built-in sources for everyone: answers, pauses and the sites' limits are shared
        from .extra import make_sources
        self.extra = make_sources(settings)
        # one original-language lookup for everyone: answers shared, each person's own TMDB key with their requests
        from .origlang import make_lookup
        self.lookup = make_lookup(settings, store)
        # user id -> {"app", "last", "day", "kind"}: kind "worker" (theirs runs), "stored" (a record for stored copies
        # only) or "waiting" (no worker was free: built again with one once one is)
        self.entries = {}
        self.safeties = {}         # user id -> their worker.Safety, while they have an App or refusals that count
        self.seen = {}             # user id -> (until, Worker.seen) of their App that stopped last
        self.stops = 0             # Apps stopped so far: a record opened before a stop may have changed or gone
        self.lock = threading.Lock()
        self.expired_day = None

    def _build(self, uid, token, record, limits, start):
        """A new App for the person (lock held), with their refusal back-off and the files their last one knew."""
        s = user_settings(self.env, record, self.users.read_key(token), self.s)
        aios = Aios.from_settings(s, s.source_manifest, s.source_password, self.s.public_base)
        usenet = gate = None
        if s.usenet_usable and isinstance(limits, UserLimits):
            usenet = Aios.from_settings(s, s.usenet_manifest, s.usenet_password, self.s.public_base)
            gate = UsenetGate(self.store, self.s.usenet_listings_per_day, limits.usenet_counts(), who=limits.who)
        safety = self.safeties.get(uid)
        if safety is None:
            safety = self.safeties[uid] = Safety(backoff_s=s.backoff_s)
        worker = Worker(s, self.store, aios, safety, vad_factory=self.vad, extra=self.extra, turn=self.turn,
                        limits=limits, usenet=usenet, usenet_gate=gate, audio_turn=self.audio_turn, lookup=self.lookup)
        until, seen = self.seen.pop(uid, (0.0, {}))
        if until > self.clock():
            worker.seen.update(seen)
        if start:
            worker.start()
        return App(s, self.store, worker, safety, state=record.get("state"), detail=record.get("state_detail"))

    def vad(self):
        """The one speech detector (Silero VAD) every person's audio sync uses, made on first use."""
        with self._vad_lock:
            if self._vad is None:
                from . import vad
                self._vad = vad.SileroVad()
            return self._vad

    def app(self, token):
        """The person's App, kept for their next requests, or None for a token nobody signed up with. A new one opens
        their record and gets a worker (started on first use) while fewer than SUBSYNC_ACTIVE_WORKERS people have one;
        else, as always for a record for stored copies only, it answers from the shared store only."""
        uid = self.users.user_id(token) if isinstance(token, str) else None
        while True:
            with self.lock:
                app, stops = self._kept(uid, token), self.stops
            if app is not None:
                return app
            record = self.users.open(token)
            if record is None:
                return None
            self.users.touch(token)
            with self.lock:
                # else an App was stopped meanwhile (a save, a refresh, a delete), maybe this person's: their record is
                # opened again, so one changed is read as it is now and one deleted isn't served on
                if self.stops == stops:
                    # another request of theirs may have made it meanwhile
                    return self._kept(uid, token) or self._add(uid, token, record)

    def _kept(self, uid, token):
        """The person's kept App, its use noted (their record touched once a UTC day); None when there is none, or when
        it waits for a worker and one is free now (lock held)."""
        entry = self.entries.get(uid)
        if entry is None or (entry["kind"] == "waiting" and self._workers() < self.s.active_workers):
            return None
        now = self.clock()
        entry["last"] = now
        if entry["day"] != day_of(now):
            entry["day"] = day_of(now)
            self.users.touch(token)
        return entry["app"]

    def _add(self, uid, token, record):
        """The person's new App, kept (lock held); in place of one that waited for a worker."""
        waited = self.entries.pop(uid, None)
        if waited is not None:     # its worker never started: it stops at once
            self._keep_seen(uid, waited["app"])
            waited["app"].worker.stop()
        if store_only(record):     # answered from the shared store only, never a worker or a read of theirs
            kind, limits = "stored", StoreOnly()
        elif self._workers() < self.s.active_workers:
            kind, limits = "worker", UserLimits(self.s, self.users, token)
        else:
            kind, limits = "waiting", StoreOnly()
        app = self._build(uid, token, record, limits, start=kind == "worker" and self.start_workers)
        now = self.clock()
        self.entries[uid] = {"app": app, "last": now, "day": day_of(now), "kind": kind}
        return app

    def _workers(self):
        """How many people have a worker now (lock held)."""
        return sum(entry["kind"] == "worker" for entry in self.entries.values())

    def _keep_seen(self, uid, app):
        """What an App that stops knew of the files its person asked about, for their next one (lock held)."""
        with app.worker.cv:
            seen = dict(app.worker.seen)
        if seen:
            self.seen[uid] = (self.clock() + SEEN_KEPT_S, seen)

    def active(self):
        """How many people have a worker now."""
        with self.lock:
            return self._workers()

    def live_app(self, token):
        """The person's kept App, without opening their record or starting anything; None when there is none."""
        with self.lock:
            entry = self.entries.get(self.users.user_id(token))
        return entry["app"] if entry else None

    def stop(self, token, reset=False, forget=False):
        """Stops the person's App (a settings save, a refresh, a delete, a change the daily check found); the next
        request opens their record again. Their next App keeps their refusal back-off and the files this one knew,
        except: `reset` drops the back-off (a refresh: they may have changed their debrid key), `forget` drops both (a
        delete), and its worker then writes nothing more of theirs to the store (Worker.stop)."""
        uid = self.users.user_id(token)
        with self.lock:
            self.stops += 1
            entry = self.entries.pop(uid, None)
            if entry is not None and not forget:
                self._keep_seen(uid, entry["app"])
            if reset or forget:
                self.safeties.pop(uid, None)
            if forget:
                self.seen.pop(uid, None)
        if entry is not None:
            entry["app"].worker.stop(forget=forget)

    def reap(self):
        """Stops the Apps idle longer than SUBSYNC_WORKER_IDLE_MIN with nothing waiting or running (the files they knew
        go to their person's next App), and drops what was kept of people that no next App needs any more. Returns how
        many stopped."""
        now = self.clock()
        cutoff = now - self.s.worker_idle_s
        stopped = []
        with self.lock:
            for uid, entry in list(self.entries.items()):
                if entry["last"] < cutoff and entry["app"].worker.idle():
                    del self.entries[uid]
                    self._keep_seen(uid, entry["app"])
                    stopped.append(entry["app"].worker)
            self.seen = {uid: kept for uid, kept in self.seen.items() if kept[0] > now}
            self.safeties = {uid: safety for uid, safety in self.safeties.items()
                             if uid in self.entries or _refusing(safety)}
        for worker in stopped:
            worker.stop()
        return len(stopped)

    def housekeeping(self):
        """Every minute: idle workers stop; once a UTC day, records unused for SUBSYNC_USER_KEEP_DAYS are deleted."""
        self.reap()
        today = day_of(self.clock())
        if today != self.expired_day:
            self.expired_day = today
            gone = self.users.expire(self.s.user_keep_days)
            if gone:
                log.info("deleted %d record%s unused for %d days", gone, "" if gone == 1 else "s", self.s.user_keep_days)

    def close(self):
        with self.lock:
            entries, self.entries = list(self.entries.values()), {}
        for entry in entries:
            entry["app"].worker.stop()
        if self.lookup is not None:   # lookups still waiting are dropped, so stopping isn't held up by them
            self.lookup.close()


# ------------------------------------------------------------------------------------------- the router

PERSONAL_HEADERS = dict(PAGE_HEADERS, **{"X-Robots-Tag": "noindex"})   # pages under a personal link
FONT_PATH = re.compile(r"^/static/fonts/([A-Za-z0-9-]+\.ttf)$")
MAX_FORM = 8192
MAX_FIELDS = 50          # the sign-up form (its language slots and every other field) fits with room to spare
USER_PATH = re.compile(f"^/u/({TOKEN})(/.*)?$")
# the subtitle entries a person's apps get: under their read key, which opens these only (users.read_key)
ENTRY_PATH = re.compile(f"^/r/({READ_KEY})(/.*)$")
PUBLIC_MANIFEST = {     # its description made for the instance by addon.manifest
    "id": "org.aiosubtitles.public",
    "version": VERSION,
    "name": "AIOSubtitles",
    "description": "Synced copies of the subtitles you see, checked against each file's own subtitle track{speech}; "
                   "below the bar the original is kept.",
    "resources": ["subtitles"],
    "types": ["movie", "series"],
    "catalogs": [],
    "behaviorHints": {"configurable": True},
}


class FormError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message, self.code = message, code


class PublicService:
    """What the public pages and routes share: settings, the stores, the pool, the sign-up gate."""

    def __init__(self, settings, store, users, pool, api_factory=public_api, timeout=PUBLIC_TIMEOUT, clock=time.time):
        self.s, self.store, self.users, self.pool = settings, store, users, pool
        self.api_factory, self.timeout, self.clock = api_factory, timeout, clock
        self.gate = SignupGate(settings, users.count, clock=clock)
        # each person's AIOStreams checked again about once a day while they use the service; a change restarts their
        # worker with the new record
        self.checker = Checker(settings, users, on_change=pool.stop, api_factory=api_factory, clock=clock,
                               timeout=timeout)
        # the routes everyone shares (health, and synced files from a store that keeps their texts) need no person's
        # service: an App that never works
        self.shared = App(settings, store, Worker(settings, store, Aios(""), Safety(), limits=StoreOnly()), Safety())

    def client_ip(self, request):
        """The visitor's address: the last X-Forwarded-For hop when the request comes from the trusted web server."""
        peer = request.client_address[0]
        if peer in self.s.trusted_proxy:
            forwarded = request.headers.get("X-Forwarded-For") or ""
            last = forwarded.split(",")[-1].strip()
            if last:
                return last
        return peer

    def manifest_url(self, token):
        return f"{self.s.public_base}/u/{token}/manifest.json"


# A request's line, headers and form come whole within this long of its first bytes, however slowly they trickle in:
# a sender that keeps each wait under the connection's timeout can't hold one of SUBSYNC_MAX_CONNECTIONS for hours
REQUEST_ARRIVAL_S = 10


class _ArrivalBound(socket.SocketIO):
    """A connection's reads (PublicHandler.rfile). A request's first bytes are waited for as an idle kept-open
    connection is (the handler's timeout); after them each read waits at most what is left of REQUEST_ARRIVAL_S."""

    def __init__(self, sock, timeout):
        super().__init__(sock, "rb")
        self.sock, self.timeout, self.until = sock, timeout, None

    def readinto(self, b):
        if self.until is None:          # a request's first bytes
            got = super().readinto(b)
            self.until = time.monotonic() + REQUEST_ARRIVAL_S
            return got
        left = self.until - time.monotonic()
        if left <= 0:
            raise TimeoutError("the request came too slowly")
        self.sock.settimeout(min(left, self.timeout))
        try:
            return super().readinto(b)
        finally:
            self.sock.settimeout(self.timeout)


class PublicHandler(Handler):
    service = None

    def setup(self):
        super().setup()
        # every read through _ArrivalBound: the reader setup() made is let go before it has read anything
        self.rfile.close()
        self.arrival = _ArrivalBound(self.connection, self.timeout)
        self.rfile = io.BufferedReader(self.arrival)

    def handle_one_request(self):
        self.arrival.until = None       # this request's time starts with its first bytes
        super().handle_one_request()

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _html(self, code, text, personal=False):
        self._send(code, text, "text/html; charset=utf-8", PERSONAL_HEADERS if personal else PAGE_HEADERS)

    def _asset(self, text, ctype, url):
        """The script or the stylesheet. Pages ask for it at `url`, which carries the hash of its text (ui.JS_URL,
        ui.CSS_URL): what that address gives never changes, so it is kept for good, as the typefaces are; asked for at
        any other (an old page's hash, none), for an hour."""
        return self._send(200, text, ctype, FONT_HEADERS if self.path == url else SCRIPT_HEADERS)

    def _font(self, name):
        """One of the two typefaces, as the image build put them in; 404 otherwise."""
        found = ui.font_path(name)
        if found is None:
            return self._send(404, b"")
        return self._send(200, ui.font_bytes(found), "font/ttf", FONT_HEADERS)

    def _not_allowed(self, allow):
        """405 for a method the address doesn't take, naming those it does (`allow`), as RFC 9110 asks."""
        return self._send(405, b"", headers={"Allow": allow})

    def _browser(self):
        """Whether a browser asked (it accepts a page): an error then gets a short page, where an app gets nothing."""
        return "text/html" in (self.headers.get("Accept") or "")

    def _not_found(self):
        """404: a short page for a browser, nothing for an app. The same for any unknown address, a personal link
        nobody signed up with included, so the answer tells nothing about which links exist."""
        if self._browser():
            return self._html(404, pages.not_found_page(self.service.s))
        return self._send(404, b"")

    def _failed(self, path):
        """500 after an unexpected failure: the "Something went wrong" page for a browser, a personal page
        under a personal link; nothing for an app, as before."""
        if not self._browser():
            return self._send(500, b"")
        personal = USER_PATH.match(path) is not None
        return self._html(500, pages.error_page(settings=self.service.s, personal=personal), personal=personal)

    def _see(self, done):
        """After a save: the settings page again by its address, so reloading it sends nothing a second time."""
        self.send_response(303)
        self.send_header("Location", f"configure?done={done}")
        self.send_header("Content-Length", "0")
        for k, v in PERSONAL_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def _form(self):
        # a length in the digits 0 to 9 alone, as every number in a header is read (digits.py), and as
        # parse_request reads it: a sign or an underscore makes it none
        length = digits.whole((self.headers.get("Content-Length") or "").strip())
        if length is None:
            self.close_connection = True
            raise FormError("The form arrived without its length.", 411)
        if length > MAX_FORM:
            self.close_connection = True
            raise FormError("The form is too large: send it again from this page.", 413)
        raw = self.read_body(length)
        try:
            text = raw.decode("utf-8")
            fields = urllib.parse.parse_qs(text, keep_blank_values=True, max_num_fields=MAX_FIELDS)
        except (UnicodeDecodeError, ValueError):
            raise FormError("The form couldn't be read. Send it again from the page.") from None
        form = {k: v[0] for k, v in fields.items()}
        if "lang" in fields:
            form["lang"] = fields["lang"]           # every language slot's choice, in the page's order
        return form

    def _route(self, method):
        svc = self.service
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/" and method == "GET":
                return self._html(200, pages.signup_page(svc.s))
            if path == "/signup" and method == "POST":
                return self._signup()
            if path == "/privacy" and method == "GET":
                return self._html(200, pages.privacy_page(svc.s))
            if path == "/static/app.js" and method == "GET":
                return self._asset(ui.APP_JS, "text/javascript; charset=utf-8", ui.JS_URL)
            if path == "/static/app.css" and method == "GET":
                return self._asset(ui.APP_CSS, "text/css; charset=utf-8", ui.CSS_URL)
            font = FONT_PATH.match(path)
            if font and method == "GET":
                return self._font(font.group(1))
            if path == "/health" or path.startswith("/file/"):
                if method != "GET":
                    return self._not_allowed("GET, HEAD")
                if path != "/health" and not svc.store.keep_texts:
                    # a store of recipes (public mode's): a copy is rebuilt from a person's own list, under
                    # their read key (/r/<read key>/file/, ENTRY_PATH), never here
                    return self._send(404, b"")
                return self.dispatch(svc.shared, path, False)
            m = ENTRY_PATH.match(path)
            if m:
                if method != "GET":
                    return self._not_allowed("GET, HEAD")
                token = svc.users.token_of(m.group(1))
                if token is None or not is_entry(m.group(2)):   # never the personal link's routes
                    return self._not_found()
                return self._person(token, m.group(2))
            m = USER_PATH.match(path)
            if not m:
                return self._not_found()
            token, rest = m.group(1), m.group(2) or "/"
            if rest == "/configure":
                return self._configure(token, method)
            if method != "GET":
                return self._not_allowed("GET, HEAD")
            if rest == "/manifest.json":
                if svc.users.open(token) is None:
                    return self._not_found()
                return self._json(manifest(svc.s, PUBLIC_MANIFEST), cache=MANIFEST_CACHE)
            # the personal link lists subtitles; its entries are under the read key, and the service's own
            # numbers (/status) are not for the people using it
            if not rest.startswith("/subtitles/"):
                return self._not_found()
            return self._person(token, rest)
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:
            log.warning("request failed: %s", type(e).__name__)
            return self._failed(path)

    def _person(self, token, rest):
        """A request of a person's own service (their subtitle lists, and their entries), answered by their App. A HEAD
        of a list or an entry (head_answer) is answered from their record alone: neither their service nor their daily
        check starts for it."""
        svc = self.service
        # a person's entries carry no code (user_settings: no SUBSYNC_ADDON_KEY), their read key guards them
        head = head_answer(rest) if self.command == "HEAD" else None
        if head:
            return self._not_found() if svc.users.open(token) is None else self._headers_only(*head)
        app = svc.pool.app(token)
        if app is None:
            return self._not_found()
        svc.checker.maybe_start(token)   # in the background, at most once per SUBSYNC_RECHECK_HOURS
        return self.dispatch(app, rest, True)

    def _signup(self):
        svc = self.service
        try:
            form = self._form()
        except FormError as e:
            return self._html(e.code, pages.signup_page(svc.s, error=e.message))
        try:
            form["languages"] = form_languages(form)
            svc.gate.check(svc.client_ip(self), form.get("invite"))
            result = signup(svc.s, svc.users, form, api_factory=svc.api_factory, timeout=svc.timeout)
        except SignupError as e:
            # too many attempts (429) or a full instance (503): entering the cleared boxes again only costs a try
            return self._html(e.code, pages.signup_page(svc.s, error=e.message, field=e.field,
                                                        order=_chosen(form, svc.s.max_languages),
                                                        audio=form.get("audio"), ticked=_ticked(form),
                                                        retry=e.code not in (429, 503)))
        return self._html(200, pages.result_page(result, svc.s), personal=True)

    def _configure(self, token, method):
        svc = self.service
        record = svc.users.open(token)
        if record is None:
            return self._not_found()
        manifest_url = svc.manifest_url(token)

        def page(code=200, message=None, error=None, notice=None, field=None):
            current = svc.users.open(token) or record
            live = svc.pool.live_app(token)
            refused = live is not None and live.action() == "reads_refused"
            return self._html(code, pages.settings_page(svc.s, current, svc.users.usage(token), manifest_url,
                                                        message=message, error=error, notice=notice,
                                                        now=svc.clock(), field=field,
                                                        state="reads_refused" if refused else None,
                                                        usenet_usage=svc.users.usenet_usage(token)), personal=True)
        if method == "GET":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            message, notice = pages.done_message((query.get("done") or [None])[0], record)
            return page(message=message, notice=notice)
        if method != "POST":
            return self._not_allowed("GET, HEAD, POST")
        try:
            form = self._form()
        except FormError as e:
            return page(e.code, error=e.message)
        action = form.get("action")
        try:
            if action == "languages":
                set_languages(svc.users, token, form_languages(form), svc.s.max_languages)
                svc.pool.stop(token)          # the next request opens the record with the new languages
                return self._see("languages")
            if action == "audio":             # files with no subtitles of their own: off, on pick, when opened
                set_audio(svc.users, token, form.get("audio"), svc.s)
                svc.pool.stop(token)          # the next request opens the record with the new choice
                return self._see("audio")
            if action == "refresh":
                try:
                    result = refresh(svc.s, svc.users, token,
                                     {"password": form.get("password"), "add_addon": "on", "link": form.get("link"),
                                      "keep_password": form.get("keep_password"), "usenet": form.get("usenet")},
                                     api_factory=svc.api_factory, timeout=svc.timeout)
                except SignupError as e:      # the password field is called "refresh" on this page, the link
                    # "new_link"; with that box left empty, a link error is about the stored AIOStreams: no box is
                    # marked
                    link = "new_link" if (form.get("link") or "").strip() else None
                    return page(e.code, error=e.message,
                                field={"password": "refresh", "link": link}.get(e.field, e.field))
                svc.pool.stop(token, reset=True)   # reads start again at once: they may have changed their debrid key
                svc.checker.forget(token)
                if result.store_only:          # stored copies only: moved there by this refresh, or still
                    done = ("to-stored" if result.to_store_only else "stored") + ("-manual" if result.manual else "")
                    return self._see(done)
                return self._see("refreshed-manual" if result.manual else "refreshed-rd" if result.warning
                                 else "refreshed")
            if action == "forget-password":
                forget_password(svc.users, token)
                return self._see("forgot")
            if action == "tmdb-key" and pages.personal_tmdb(svc.s):
                set_tmdb_key(svc.users, token, form.get("tmdb_key"), svc.s)   # checked with TMDB once
                svc.pool.stop(token)          # the next request opens the record with it
                return self._see("tmdb-key")
            if action == "tmdb-key-remove":   # also after the instance stopped asking for keys
                remove_tmdb_key(svc.users, token)
                svc.pool.stop(token)
                return self._see("tmdb-key-removed")
            if action == "delete-confirm":
                return self._html(200, pages.delete_confirm_page(stored=store_only(record), settings=svc.s),
                                  personal=True)
            if action == "delete":
                def forget():                 # their service stops, and writes nothing more of theirs (Pool.stop)
                    svc.pool.stop(token, forget=True)
                    svc.checker.forget(token)
                try:                          # forgotten once the delete is certain: never for a mistyped password
                    outcome = delete(svc.s, svc.users, token, {"password": form.get("password") or ""},
                                     api_factory=svc.api_factory, timeout=svc.timeout, on_confirmed=forget)
                except SignupError as e:      # back to the delete step, not the settings page
                    return self._html(e.code, pages.delete_confirm_page(
                        error=e.message, field="delete" if e.field == "password" else e.field,
                        stored=store_only(record), settings=svc.s), personal=True)
                # a request while it took (its AIOStreams calls) may have opened the record and made an App again: it
                # stops now that the record is gone (Pool.app opens a record again after a stop, and finds none)
                forget()
                # then their own rows in the shared store: their waiting files and original subtitles
                svc.store.drop_rows_of(svc.users.rows_key(token))
                return self._html(200, pages.deleted_page(outcome, svc.s), personal=True)
        except SignupError as e:
            return page(e.code, error=e.message, field=e.field)
        return page(400, error="Choose one of the buttons on this page.")


def _chosen(form, limit=MAX_LANGUAGES):
    """The languages a form chose, as codes in order, to show them again after a problem (never anything else); None
    when they can't be shown (more than the instance's limit, or a name it doesn't know)."""
    try:
        text = form_languages(form)
    except SignupError:
        return []                 # none chosen: show none ticked, as sent
    langs, problem = parse_languages(text or "", limit)
    return None if problem else [three for _, _, three in langs]


def _ticked(form):
    """The sign-up's boxes as a form left them, to show them so again after a problem: ticked is "on", as the
    sign-up reads them."""
    return {name: form.get(name) == "on" for name in pages.SIGNUP_BOXES}


class _CappedServer(Server):
    """At most SUBSYNC_MAX_CONNECTIONS connections at once; the next is answered 503 at once and closed."""

    def __init__(self, address, handler, cap):
        super().__init__(address, handler)
        self.slots = threading.BoundedSemaphore(max(1, cap))

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 5\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def serve_public(service, port, host="0.0.0.0"):
    handler = type("BoundPublicHandler", (PublicHandler,), {"service": service})
    return _CappedServer((host, port), handler, service.s.max_connections)

