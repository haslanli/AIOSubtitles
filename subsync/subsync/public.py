"""Public mode: one subsync service for many people, each signed up with their own AIOStreams.

Each person who signed up has an encrypted record (users.py) that opens only with their personal link's token. While
their requests come in, they get their own copy of the self-hosted service: an App and a Worker with their languages,
working through the sync config the sign-up made on their AIOStreams. Everyone shares one result store, so a copy made
for one person serves everyone playing the same file, and one turn, so the service works on one file at a time.

A worker with nothing to do for a while stops, and its opened record leaves memory; the next request opens it again.
"""
import logging
import re
import threading
import time
import urllib.parse
from http.server import ThreadingHTTPServer

from . import pages
from .addon import App, Handler
from .aiosconfig import parse_languages
from .settings import Settings
from .signup import SignupError, SignupGate, delete, form_languages, public_api, refresh, set_languages, signup
from .sources import Aios
from .users import day_of
from .worker import OverLimit, Safety, Turn, Worker

log = logging.getLogger("subsync.public")


def user_settings(env, record, token):
    """A person's own settings: the service's, with their languages, their sync config and their own address."""
    base = Settings(env).public_base
    own = dict(env)
    own.update({"SUBSYNC_LANGUAGES": ",".join(record["languages"]),
                "SUBSYNC_SOURCE_MANIFEST": record["sync_link"],
                "SUBSYNC_SOURCE_PASSWORD": record["sync_password"],
                "SUBSYNC_PUBLIC_BASE": f"{base}/u/{token}",
                "SUBSYNC_AUDIO_ROUTE": "off", "SUBSYNC_ADDON_KEY": "", "SUBSYNC_BASE_PATH": ""})
    return Settings(own)


class UserLimits:
    """A person's own limits: files waiting, new files and bytes read per UTC day. The service-wide daily budget is
    checked by every read as before (worker.JobGuard)."""

    def __init__(self, settings, users, token):
        self.s, self.users, self.token = settings, users, token

    def may_queue(self, queued):
        return queued < self.s.user_queue

    def may_start_file(self):
        return self.users.usage(self.token)["files"] < self.s.user_files_per_day

    def file_started(self):
        self.users.add_usage(self.token, files=1)

    def before_read(self, length):
        if self.users.usage(self.token)["bytes"] + length > self.s.user_daily_bytes:
            raise OverLimit("bytes per day")

    def after_read(self, n):
        if n:
            self.users.add_usage(self.token, nbytes=n)


class StoreOnly:
    """The limits of a person who gets no worker now (too many people active): nothing is queued or read."""

    def may_queue(self, queued):
        return False

    def may_start_file(self):
        return False

    def file_started(self):
        pass

    def before_read(self, length):
        raise OverLimit("no worker free")

    def after_read(self, n):
        pass


class Pool:
    """The people with a running worker, by user id (never by token)."""

    def __init__(self, settings, env, store, users, clock=time.time, start_workers=True):
        self.s, self.env, self.store, self.users, self.clock = settings, dict(env), store, users, clock
        self.start_workers = start_workers
        self.turn = Turn()
        self.entries = {}          # user id -> {"app", "last", "day"}
        self.lock = threading.Lock()
        self.expired_day = None

    def _build(self, token, record, limits, start):
        s = user_settings(self.env, record, token)
        aios = Aios(s.source_manifest, own_bases=(self.s.public_base,), skip_addons=s.skip_addons,
                    size_slack=s.size_slack, password=s.source_password)
        safety = Safety(backoff_s=s.backoff_s)
        extra = None
        if s.extra_sources or s.subdl_key or s.wyzie_key:
            from .extra import ExtraSources
            extra = ExtraSources(s)
        worker = Worker(s, self.store, aios, safety, extra=extra, turn=self.turn, limits=limits)
        if start:
            worker.start()
        return App(s, self.store, worker, safety)

    def app(self, token):
        """The person's App (their worker started on first use), or None for a token nobody signed up with. Past
        SUBSYNC_ACTIVE_WORKERS, an App that answers from the shared store only."""
        uid = self.users.user_id(token) if isinstance(token, str) else None
        now = self.clock()
        with self.lock:
            entry = self.entries.get(uid)
            if entry is not None:
                entry["last"] = now
                if entry["day"] != day_of(now):
                    entry["day"] = day_of(now)
                    self.users.touch(token)
                return entry["app"]
        record = self.users.open(token)
        if record is None:
            return None
        self.users.touch(token)
        with self.lock:
            entry = self.entries.get(uid)
            if entry is not None:          # another request opened it meanwhile
                return entry["app"]
            if len(self.entries) >= self.s.active_workers:
                return self._build(token, record, StoreOnly(), start=False)
            app = self._build(token, record, UserLimits(self.s, self.users, token), start=self.start_workers)
            self.entries[uid] = {"app": app, "last": now, "day": day_of(now)}
            return app

    def active(self):
        with self.lock:
            return len(self.entries)

    def stop(self, token):
        """Stops the person's worker (refresh, delete); the next request opens their record again."""
        with self.lock:
            entry = self.entries.pop(self.users.user_id(token), None)
        if entry is not None:
            entry["app"].worker.stop()

    def reap(self):
        """Stops the workers idle longer than SUBSYNC_WORKER_IDLE_MIN with nothing waiting or running."""
        cutoff = self.clock() - self.s.worker_idle_s
        stopped = []
        with self.lock:
            for uid, entry in list(self.entries.items()):
                worker = entry["app"].worker
                if entry["last"] < cutoff and not worker.jobs and worker.current is None:
                    stopped.append(self.entries.pop(uid)["app"].worker)
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


# ------------------------------------------------------------------------------------------- the router

PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; form-action 'self'; "
                               "base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store"}
PERSONAL_HEADERS = dict(PAGE_HEADERS, **{"X-Robots-Tag": "noindex"})   # pages under a personal link
SCRIPT_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "public, max-age=3600",
                  "Referrer-Policy": "no-referrer"}
MAX_FORM = 8192
MAX_FIELDS = 50          # the sign-up form with every language chip ticked fits
USER_PATH = re.compile(r"^/u/([A-Za-z0-9_-]{43})(/.*)?$")
PUBLIC_MANIFEST = {
    "id": "org.aiosubtitles.public",
    "version": "1.0.0",
    "name": "AIOSubtitles",
    "description": "Synced copies of the subtitles you see, checked against each file's own subtitle track; below the "
                   "bar the original is kept.",
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

    def __init__(self, settings, env, store, users, pool, api_factory=public_api, timeout=30, clock=time.time):
        self.s, self.env, self.store, self.users, self.pool = settings, env, store, users, pool
        self.api_factory, self.timeout, self.clock = api_factory, timeout, clock
        self.gate = SignupGate(settings, users.count, clock=clock)
        # the routes everyone shares (synced files, health) need no person's service: an App that never works
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


class PublicHandler(Handler):
    service = None
    timeout = 30           # an idle kept-open connection gives its slot back after this long

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _html(self, code, text, personal=False):
        self._send(code, text, "text/html; charset=utf-8", PERSONAL_HEADERS if personal else PAGE_HEADERS)

    def _not_found(self):
        """404: a short page for a browser, nothing for an app. The same for any unknown address, a personal link
        nobody signed up with included, so the answer tells nothing about which links exist."""
        if "text/html" in (self.headers.get("Accept") or ""):
            return self._html(404, pages.not_found_page())
        return self._send(404, b"")

    def _see(self, done):
        """After a save: the settings page again by its address, so reloading it sends nothing a second time."""
        self.send_response(303)
        self.send_header("Location", f"configure?done={done}")
        self.send_header("Content-Length", "0")
        for k, v in PERSONAL_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    def _form(self):
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self.close_connection = True
            raise FormError("The form arrived without its length.", 411) from None
        if length < 0 or length > MAX_FORM:
            self.close_connection = True
            raise FormError("The form is too large: send it again from this page.", 413)
        raw = self.rfile.read(length)
        try:
            text = raw.decode("utf-8")
            fields = urllib.parse.parse_qs(text, keep_blank_values=True, max_num_fields=MAX_FIELDS)
        except (UnicodeDecodeError, ValueError):
            raise FormError("The form couldn't be read. Send it again from the page.") from None
        form = {k: v[0] for k, v in fields.items()}
        if "lang" in fields:
            form["lang"] = fields["lang"]           # every ticked language chip, in the page's order
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
                return self._send(200, pages.APP_JS, "text/javascript; charset=utf-8", SCRIPT_HEADERS)
            if path == "/health" or path.startswith("/file/"):
                return self.dispatch(svc.shared, path, False) if method == "GET" else self._send(405, b"")
            m = USER_PATH.match(path)
            if not m:
                return self._not_found()
            token, rest = m.group(1), m.group(2) or "/"
            if rest == "/configure":
                return self._configure(token, method)
            if method != "GET":
                return self._send(405, b"")
            if rest == "/status":        # the service's own numbers are not for the people using it
                return self._not_found()
            if rest == "/manifest.json":
                if svc.users.open(token) is None:
                    return self._not_found()
                return self._json(PUBLIC_MANIFEST, cache="max-age=3600")
            app = svc.pool.app(token)
            if app is None:
                return self._not_found()
            return self.dispatch(app, rest, True)
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:
            log.warning("request failed: %s", type(e).__name__)
            return self._send(500, b"")

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
            return self._html(e.code, pages.signup_page(svc.s, error=e.message, field=e.field,
                                                        order=_chosen(form)))
        return self._html(200, pages.result_page(result), personal=True)

    def _configure(self, token, method):
        svc = self.service
        record = svc.users.open(token)
        if record is None:
            return self._not_found()
        manifest_url = svc.manifest_url(token)

        def page(code=200, message=None, error=None, notice=None, field=None):
            current = svc.users.open(token) or record
            return self._html(code, pages.settings_page(svc.s, current, svc.users.usage(token), manifest_url,
                                                        message=message, error=error, notice=notice,
                                                        now=svc.clock(), field=field), personal=True)
        if method == "GET":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            message, notice = pages.done_message((query.get("done") or [None])[0], record)
            return page(message=message, notice=notice)
        if method != "POST":
            return self._send(405, b"")
        try:
            form = self._form()
        except FormError as e:
            return page(e.code, error=e.message)
        action = form.get("action")
        try:
            if action == "languages":
                set_languages(svc.users, token, form_languages(form))
                svc.pool.stop(token)          # the next request opens the record with the new languages
                return self._see("languages")
            if action == "refresh":
                try:
                    result = refresh(svc.s, svc.users, token, {"password": form.get("password"), "add_addon": "on"},
                                     api_factory=svc.api_factory, timeout=svc.timeout)
                except SignupError as e:      # the password field is called "refresh" on this page
                    return page(e.code, error=e.message, field="refresh" if e.field == "password" else e.field)
                svc.pool.stop(token)
                return self._see("refreshed-manual" if result.manual else "refreshed-rd" if result.warning
                                 else "refreshed")
            if action == "delete-confirm":
                return self._html(200, pages.delete_confirm_page(), personal=True)
            if action == "delete":
                svc.pool.stop(token)
                try:
                    outcome = delete(svc.s, svc.users, token, {"password": form.get("password") or ""},
                                     api_factory=svc.api_factory, timeout=svc.timeout)
                except SignupError as e:      # back to the delete step, not the settings page
                    return self._html(e.code, pages.delete_confirm_page(
                        error=e.message, field="delete" if e.field == "password" else e.field), personal=True)
                return self._html(200, pages.deleted_page(outcome), personal=True)
        except SignupError as e:
            return page(e.code, error=e.message, field=e.field)
        return page(400, error="Choose one of the buttons on this page.")


def _chosen(form):
    """The languages a form chose, as codes in order, to show them again after a problem (never anything else)."""
    try:
        text = form_languages(form)
    except SignupError:
        return []                 # none chosen: show none ticked, as sent
    langs, problem = parse_languages(text or "")
    return None if problem else [three for _, _, three in langs]


class _CappedServer(ThreadingHTTPServer):
    """At most SUBSYNC_MAX_CONNECTIONS connections at once; the next is answered 503 at once and closed."""
    daemon_threads = True

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
    handler = type("BoundPublicHandler", (PublicHandler,), {"service": service, "app": service.shared})
    return _CappedServer((host, port), handler, service.s.max_connections)

