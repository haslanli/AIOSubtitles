"""Test helpers: a local HTTP range server (stands in for AIOS's proxy), with switchable faults, and the stand-ins,
harnesses and data the test modules share.

Every test module imports helpers before anything from subsync: this is the one place that puts the package on the
import path (the source next to these tests, ahead of any other copy), so all modules test the same copy and each one
also runs on its own (python3 -m unittest test_x from this folder). The image build is the exception: it tests the
image's own copy (see IMAGE_BUILD). It is also the one place that keeps the whole run on this machine (local_lookup).

What more than one module uses lives here, never in a test module: no test module imports another, so none
runs another's top level or collects its TestCase classes. A harness here is a plain class, never a TestCase: a TestCase
takes it as its first base and its tearDown closes it; ServerHarness can also be held as a plain object."""
import dataclasses
import html.parser
import io
import json
import logging
import os
import random
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

TESTS = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.dirname(TESTS)          # subsync/, the folder that holds the package
# The Dockerfile runs these tests from a read-only view of the source with `python -m unittest` in the image's own
# folder (which -m puts on the path) and AIOSUBTITLES_IMAGE_BUILD set: there they test the image's copy of the package,
# the one that holds what the build adds to it, the OCR models and the fonts
IMAGE_BUILD = bool(os.environ.get("AIOSUBTITLES_IMAGE_BUILD"))
if not IMAGE_BUILD:
    sys.path.insert(0, SOURCE)
FIXTURES = os.path.join(TESTS, "fixtures")
LOCAL = ("127.0.0.1", "localhost", "::1")
_getaddrinfo = socket.getaddrinfo


def local_lookup(host, *args, **kwargs):
    """socket.getaddrinfo for the whole run: a name of this machine only, else the test fails before any
    lookup. Every connection looks its host up through it (socket.create_connection, so urllib too, and net's gate,
    whose resolver is taken from the socket module when the package is imported, just below), so no test reaches
    another machine, even one that leaves a setting at its default (the extra sources, the original-language lookup)."""
    name = host.decode() if isinstance(host, bytes) else host
    if name is not None and name not in LOCAL:
        raise AssertionError(f"a lookup of {name}: tests reach nothing but this machine")
    return _getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = local_lookup

from subsync import realdebrid, sources  # noqa: E402  (the package from the path set just above)
from subsync.settings import Settings  # noqa: E402
from subsync.subs import to_srt, ts  # noqa: E402

# the "clearly better than the original" rules as the service ships them (the settings' defaults): a test of the shipped
# rule follows it when its numbers change; one meant for another rule says so with its own numbers
BETTER = Settings({}).better
AUDIO_BETTER = Settings({}).audio_better

# the features a pipeline test turns on itself (image references, word pairing, the sectioned fit, both "clearly better"
# rules, the audio budget and its extra windows): off, as the pipeline's functions had them before they took one Rules
RULES_OFF = {"image_ref": False, "text_pairing": False, "sections": False, "better": None, "audio_better": None,
             "audio_budget": None, "audio_extra": 0}


def rules(**kw):
    """The rules a pipeline test judges by (settings.Rules): the settings' defaults, with RULES_OFF and then `kw`."""
    return dataclasses.replace(Settings({}).rules, **{**RULES_OFF, **kw})

# public mode keeps each person's record encrypted: its tests need the cryptography package, which the image has
try:
    import cryptography  # noqa: F401
    HAVE_CRYPTOGRAPHY = True
except ImportError:
    HAVE_CRYPTOGRAPHY = False
NEEDS = "cryptography is not installed (the image has it; tests run it in a venv)"

POLL_S = 0.05    # how often a test's server looks for stop_server
BACKLOG = 64     # connections a test's server holds until it takes them


def start_server(server):
    """Serves `server` (a socketserver server) from a daemon thread and returns it. It looks for a stop every POLL_S
    seconds, not serve_forever's default half second, which every stop_server waited out. A stand-in with
    socketserver's listen queue of 5 gets room for BACKLOG: a test that opens more connections at once (six sources
    asked together) had one of them reset now and then, more often once the run got faster."""
    if server.request_queue_size < BACKLOG:
        server.request_queue_size = BACKLOG
        server.socket.listen(BACKLOG)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": POLL_S}, daemon=True).start()
    return server


def stop_server(server):
    """Stops a server start_server serves and closes its socket."""
    server.shutdown()
    server.server_close()


class RangeServer:
    """Serves files under root with Range support. fault: None | '403' | 'ignore-range' | 'wrong-size' | 'aligned'
    (a proxy that answers whole 4096-byte blocks: from the start of the block that holds the first byte asked).
    `log`: (start, length) of each range of a file it answers, recorded before any of the answer is sent, so a client
    that has had its bytes always finds its read there."""

    def __init__(self, root):
        self.root, self.fault, self.log, self.headers = root, None, [], []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.headers.append(dict(self.headers))
                path = os.path.join(outer.root, self.path.split("?")[0].lstrip("/"))
                if outer.fault == "403":
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not os.path.isfile(path):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                size = os.path.getsize(path)
                start, end = 0, size - 1
                rng = self.headers.get("Range")
                ranged = bool(rng) and outer.fault != "ignore-range"
                if ranged:
                    a, _, b = rng[6:].partition("-")
                    start, end = int(a), min(int(b) if b else size - 1, size - 1)
                    if outer.fault == "aligned":
                        start -= start % 4096
                outer.log.append((start, end - start + 1))
                if ranged:
                    self.send_response(206)
                    shown = size + 1000 if outer.fault == "wrong-size" else size
                    self.send_header("Content-Range", f"bytes {start}-{end}/{shown}")
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    try:
                        while left > 0:
                            chunk = f.read(min(65536, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):    # the client went: so does the connection
                        self.close_connection = True

        self.server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.port = self.server.server_address[1]

    def url(self, name):
        return f"http://127.0.0.1:{self.port}/{name}"

    def close(self):
        stop_server(self.server)


class TempDir:
    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="subsync-test-")
        return self.path

    def __exit__(self, *a):
        shutil.rmtree(self.path, ignore_errors=True)


def have(tool):
    return shutil.which(tool) is not None


def closed_after(test, resource):
    """`resource` (a Store, the people's records, a database connection), closed when the test ends, so no test leaves
    its database open."""
    test.addCleanup(resource.close)
    return resource


def files_bytes(folder, prefix=""):
    """The bytes of every file in `folder` whose name starts with `prefix`, one after the other: what the scans for a
    secret on disk look through, each file closed once read."""
    data = []
    for name in os.listdir(folder):
        if name.startswith(prefix):
            with open(os.path.join(folder, name), "rb") as f:
                data.append(f.read())
    return b"".join(data)


LINK_MARKS = ("127.0.0.1", "/stremio/")   # every link the tests' servers give out is http://127.0.0.1:<port>/...


def watch_the_log(test):
    """README's fixed rule, for one test: subsync's log never holds a link. Every record a subsync logger
    makes while the test runs, DEBUG and up, is kept (the test's own assertLogs blocks included), and when the test
    ends it fails if one of them holds a local address or an AIOStreams path."""
    made, factory = [], logging.getLogRecordFactory()
    top = logging.getLogger("subsync")
    level = top.level

    def record(*args, **kwargs):
        r = factory(*args, **kwargs)
        if r.name == "subsync" or r.name.startswith("subsync."):
            made.append(r)
        return r

    def check():
        logging.setLogRecordFactory(factory)
        top.setLevel(level)
        lines = [logging.Formatter("%(name)s %(levelname)s %(message)s").format(r) for r in made]
        test.assertEqual([line for line in lines if any(mark in line for mark in LINK_MARKS)], [],
                         "subsync logged a link")
    logging.setLogRecordFactory(record)
    top.setLevel(logging.DEBUG)
    test.addCleanup(check)


def local_network_only():
    """A patch that fails the test, before any lookup or packet, when code opens a connection to anything but this
    machine: tests about addresses that must never be reached can't reach them, even while the code is wrong."""
    real = socket.create_connection

    def guarded(address, *args, **kwargs):
        if address[0] not in LOCAL:
            raise AssertionError(f"a connection to {address[0]} was opened")
        return real(address, *args, **kwargs)
    return mock.patch("socket.create_connection", guarded)


def exchange(port, raw, timeout=5):
    """Sends `raw` bytes on one connection to this machine's `port` and returns all the server sends until it closes
    the connection; a server that keeps it open fails the test (socket timeout)."""
    data = b""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(raw)
        while chunk := sock.recv(65536):
            data += chunk
    return data


def answers(data):
    """The status codes of the HTTP/1.1 answers in `data`, one after the other (each body by its Content-Length);
    bytes that are no such answer (an HTTP/0.9 one) count as one more, None."""
    codes = []
    while data:
        head, sep, rest = data.partition(b"\r\n\r\n")
        if not sep or not head.startswith(b"HTTP/1.1 "):
            codes.append(None)
            break
        lines = head.decode("latin-1").split("\r\n")
        length = next((int(line.split(":", 1)[1]) for line in lines[1:] if line.lower().startswith("content-length:")),
                      0)
        codes.append(int(lines[0].split()[1]))
        data = rest[length:]
    return codes


class FakeAiostreams:
    """A stand-in for a person's AIOStreams: its status, its config API (read, save, create, delete, with basic auth)
    and each config's manifest. Records every request (method, path). Faults: 'wrong_password', 'refuse_create',
    'refuse_save', 'hang' (answers after `hang_s`), 'html' (answers a web page), 'bad_config' (userData not an object),
    'no_manifest' (configs' manifests answer 404), 'rate_limited' (new configs answer 429, retry in an hour),
    'garbled' (a config's read answers JSON cut short, as a proxy may).
    `creates_left`: after that many new configs, the next ones are refused. Each config's stream and subtitle routes
    answer `listings[uuid]` and `subtitle_lists[uuid]` and log every listing in `listed`.
    Alias links, as stock AIOStreams (routes/stremio/alias.ts): `aliases` maps an alias (as written, case kept) to a
    config id; `/stremio/u/<alias>/<rest>` answers a 302 to `/stremio/<id>/<encrypted>/<rest>` (query kept), an unknown
    alias 400 USER_INVALID_DETAILS. `alias_answer` makes it answer otherwise: 'ok_200' (a 200 page), 'other_host' (a
    redirect to another host), 'no_id' (a redirect to a page with no config id), 'same_host' (an absolute redirect to
    this host). `alias_auth` records whether each alias request carried an Authorization header."""

    STATUS = {"success": True, "data": {"version": "2.37.0", "settings": {"presets": [
        {"ID": "custom", "OPTIONS": [{"id": "timeout", "default": 7000}]},
        {"ID": "opensubtitles", "OPTIONS": [{"id": "timeout", "default": 7000}]}]}}}

    def __init__(self):
        import base64
        import json
        import time
        import uuid as uuidlib
        self.configs, self.requests, self.fault, self.hang_s = {}, [], None, 3.0
        self.refuse_save_for = set()   # config ids whose saves are refused
        self.creates_left = None       # new configs allowed before the next ones are refused (None: no limit)
        self.listings, self.subtitle_lists, self.listed = {}, {}, []
        self.encrypted = {}            # config id -> the encrypted password its link carries (default "ENC" + id[:8])
        self.auth_ids = []             # the config ids each password-checked request named
        self.aliases, self.alias_answer, self.alias_auth = {}, None, []
        self.inflight = self.max_inflight = 0
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def answer(self, code, obj=None, ctype="application/json"):
                body = (json.dumps(obj) if ctype == "application/json" else obj or "").encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def auth(self):
                header = self.headers.get("Authorization", "")
                if not header.startswith("Basic "):
                    return None, None
                uuid, _, password = base64.b64decode(header[6:]).decode().partition(":")
                return uuid, password

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}")

            def handle_any(self):
                with outer.lock:
                    outer.inflight += 1
                    outer.max_inflight = max(outer.max_inflight, outer.inflight)
                try:
                    self.handle_one()
                finally:
                    with outer.lock:
                        outer.inflight -= 1

            def handle_one(self):
                import urllib.parse
                path, _, query = self.path.partition("?")
                outer.requests.append((self.command, path))
                if outer.fault == "hang":
                    time.sleep(outer.hang_s)
                if self.command == "HEAD" and path == "/api/v1/user":   # stock: does a config exist (no password)
                    asked = (urllib.parse.parse_qs(query).get("uuid") or [""])[0]
                    return self.answer(200 if asked in outer.configs else 400, {"success": asked in outer.configs})
                if outer.fault == "html":
                    return self.answer(200, "<html><body>Welcome</body></html>", "text/html")
                if path == "/api/v1/status":
                    return self.answer(200, outer.STATUS)
                if path.startswith("/stremio/u/") and self.command in ("GET", "HEAD"):
                    return self.alias(path, query)
                if path == "/api/v1/user":
                    uuid, password = self.auth()
                    if uuid:
                        outer.auth_ids.append(uuid)
                    if self.command == "POST":
                        if outer.fault == "rate_limited":
                            self.send_response(429)
                            body = json.dumps({"success": False, "error": {"code": "RATE_LIMIT_EXCEEDED",
                                                                           "message": "Too many requests"}}).encode()
                            self.send_header("Retry-After", "3600")
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            return
                        if outer.fault == "refuse_create" or outer.creates_left == 0:
                            return self.answer(400, {"success": False, "error": {"code": "USER_INVALID_CONFIG",
                                                                                 "message": "bad config"}})
                        b = self.body()
                        refused = outer.refusal(b["config"])
                        if refused:
                            return self.answer(400, {"success": False, "error": {"code": "USER_INVALID_CONFIG",
                                                                                 "message": refused}})
                        new = str(uuidlib.uuid4())
                        if outer.creates_left:
                            outer.creates_left -= 1
                        outer.configs[new] = {"password": b["password"], "config": b["config"]}
                        return self.answer(201, {"success": True, "data": {"uuid": new,
                                                                           "encryptedPassword": outer.enc(new)}})
                    stored = outer.configs.get(uuid)
                    if stored is None or stored["password"] != password or (
                            outer.fault == "wrong_password" and self.command == "GET"):
                        return self.answer(401, {"success": False, "error": {"code": "USER_INVALID_PASSWORD",
                                                                             "message": "Invalid password"}})
                    if self.command == "GET" and outer.fault == "garbled":   # a whole answer of cut-short JSON
                        return self.answer(200, '{"success": true, "data": {"userDa', "text/plain")
                    if self.command == "GET":
                        data = "not a config" if outer.fault == "bad_config" else stored["config"]
                        return self.answer(200, {"success": True, "data": {"userData": data,
                                                                           "encryptedPassword": outer.enc(uuid)}})
                    if self.command == "PUT":
                        if outer.fault == "refuse_save" or uuid in outer.refuse_save_for:
                            return self.answer(500, {"success": False, "error": {"code": "INTERNAL", "message": "boom"}})
                        config = self.body()["config"]
                        refused = outer.refusal(config)
                        if refused:
                            return self.answer(400, {"success": False, "error": {"code": "USER_INVALID_CONFIG",
                                                                                 "message": refused}})
                        stored["config"] = config
                        return self.answer(200, {"success": True})
                    if self.command == "DELETE":
                        del outer.configs[uuid]
                        return self.answer(200, {"success": True})
                parts = path.split("/")
                if len(parts) == 5 and parts[1] == "stremio" and parts[4] == "manifest.json" and parts[2] in outer.configs \
                        and outer.fault != "no_manifest":
                    return self.answer(200, {"id": "com.aiostreams", "name": "AIOStreams", "resources": ["stream"]})
                if len(parts) >= 6 and parts[1] == "stremio" and parts[2] in outer.configs and parts[4] in ("stream",
                                                                                                        "subtitles"):
                    outer.listed.append((parts[2], parts[4]))
                    if parts[4] == "stream":
                        return self.answer(200, {"streams": outer.listings.get(parts[2], [])})
                    return self.answer(200, {"subtitles": outer.subtitle_lists.get(parts[2], [])})
                return self.answer(404, {"success": False})

            def alias(self, path, query):
                import urllib.parse
                parts = path.split("/")
                outer.alias_auth.append(bool(self.headers.get("Authorization")))
                if outer.alias_answer == "ok_200":
                    return self.answer(200, "<html><body>A page</body></html>", "text/html")
                found = outer.aliases.get(urllib.parse.unquote(parts[3])) if len(parts) > 4 and parts[3] else None
                if not found:
                    return self.answer(400, {"success": False, "error": {"code": "USER_INVALID_DETAILS",
                                                                         "message": "Invalid UUID or password"}})
                full = f"/stremio/{found}/{outer.enc(found)}/" + "/".join(parts[4:]) + (f"?{query}" if query else "")
                location = {"other_host": "https://elsewhere.test" + full, "no_id": "/configure",
                            "same_host": outer.url + full}.get(outer.alias_answer, full)
                body = f"Found. Redirecting to {location}".encode()
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = handle_any

        self.server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def enc(self, uuid):
        return self.encrypted.get(uuid, "ENC" + uuid[:8])

    SERVICE_NAMES = {"aiostreams": "AIOStreams", "nzbdav": "NzbDAV", "altmount": "AltMount", "torbox": "TorBox",
                     "stremio_nntp": "Stremio NNTP", "stremthru_newz": "StremThru Newz", "easynews": "Easynews",
                     "realdebrid": "Real-Debrid"}
    PRESET_NAMES = {"prowlarr": "Prowlarr", "stremthruStore": "StremThru Store", "mediafusion": "MediaFusion",
                    "library": "Library", "comet": "Comet", "newznab": "Newznab", "nzbhydra": "NZBHydra",
                    "easynews-search": "Easynews Search"}
    # the services these addons can use (AIOStreams v2.35.8's presets): without one, the config is refused
    _DEBRID = {"realdebrid", "alldebrid", "premiumize", "debridlink", "torbox", "offcloud", "easydebrid", "debrider",
               "pikpak", "torrin"}
    _NEWZ = {"torbox", "nzbdav", "altmount", "stremio_nntp", "stremthru_newz", "aiostreams"}
    CAN_USE = {"newznab": _NEWZ, "nzbhydra": _NEWZ, "prowlarr": _DEBRID | _NEWZ, "easynews-search": {"easynews"}}

    @classmethod
    def refusal(cls, config):
        """Why AIOStreams would refuse this config when it is created or saved (None: it wouldn't), as far as its
        check of each addon's own services goes (Preset.getUsableServices, v2.35.8): every service an enabled addon
        names must be on the config's service list, enabled, with credentials; an empty choice leaves it none."""
        services = {s.get("id"): s for s in (config or {}).get("services") or [] if isinstance(s, dict)}
        for preset in (config or {}).get("presets") or []:
            chosen = (preset.get("options") or {}).get("services")
            if not preset.get("enabled") or not isinstance(chosen, list):
                continue
            name = cls.PRESET_NAMES.get(preset.get("type"), preset.get("type"))
            for service in chosen:
                found = services.get(service)
                if not found or not found.get("enabled") or found.get("credentials") is None:
                    return (f"You have specified {cls.SERVICE_NAMES.get(service, service)} in your configuration for "
                            f"{name}, but it is not enabled or has missing credentials")
            if not chosen:
                return f"{name} requires at least one usable service, but none were found."
        for preset in (config or {}).get("presets") or []:     # no usable service at all (generateAddons)
            can = cls.CAN_USE.get(preset.get("type"))
            if not preset.get("enabled") or can is None:
                continue
            chosen = (preset.get("options") or {}).get("services")
            usable = {s for s, found in services.items() if found.get("enabled") and found.get("credentials") is not None}
            if not ((set(chosen) if isinstance(chosen, list) else usable) & usable & can):
                return (f"{cls.PRESET_NAMES.get(preset.get('type'), preset.get('type'))} requires at least one usable "
                        "service, but none were found.")
        return None

    def add(self, config, password):
        import uuid as uuidlib
        uuid = str(uuidlib.uuid4())
        self.configs[uuid] = {"password": password, "config": config}
        return uuid

    def writes(self):
        return [r for r in self.requests if r[0] in ("POST", "PUT", "DELETE")]

    def close(self):
        stop_server(self.server)


# -- public mode: its made-up values, the log lines a test reads, and the parts every public harness starts from --------
KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"   # SUBSYNC_SECRET_KEY
INSTANCE = "https://aios.test"                        # a person's AIOStreams: the stand-in answers (PublicParts.api)
PASSWORD = "the-viewers-own-Password-1"               # their config's password


class Logs(logging.Handler):
    """The log lines made while it is on, by any logger, DEBUG and up, each as a formatter writes it, so a logged
    exception's traceback is part of its line. on hangs it on the root logger and sets the root to DEBUG;
    off takes it off and sets the root's level back (it was left at DEBUG for the rest of the run)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines, self.level_before = [], None

    def emit(self, record):
        self.lines.append(self.format(record))

    def on(self):
        root = logging.getLogger()
        self.level_before = root.level
        root.addHandler(self)
        root.setLevel(logging.DEBUG)
        return self

    def off(self):
        root = logging.getLogger()
        root.removeHandler(self)
        root.setLevel(self.level_before)


class PublicParts:
    """What each public-mode harness starts from (ServerHarness, RecheckHarness, SignupHarness): a data folder, public
    mode's settings (`env` added to them), the people's records, a stand-in AIOStreams that answers for INSTANCE through
    api(), and the log lines (Logs). start_public() makes them; stop_public() closes them and puts the logging back."""

    def start_public(self, clock=time.time, **env):
        from subsync.users import Users
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.env = dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                         "SUBSYNC_PUBLIC_BASE": "https://subs.test"}, **env)
        self.s = Settings(self.env)
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.s.secret_bytes(), clock=clock)
        self.fake = FakeAiostreams()
        self.logs = Logs().on()

    def api(self, base, timeout):
        """The service's AIOStreams client for `base` (an api_factory), INSTANCE answered by the stand-in."""
        from subsync.aiosconfig import AiosApi
        return AiosApi(base.replace(INSTANCE, self.fake.url), timeout=timeout, max_timeout=timeout, retry_wait_cap=5)

    def stop_public(self):
        self.logs.off()
        self.fake.close()
        self.users.close()
        self.tmp.__exit__(None, None, None)


class SignupHarness(PublicParts):
    """The sign-up's harness: public mode's parts, `public_env` added to their settings, and the signup module
    (`signup`). As a TestCase's first base: setUp makes them, tearDown closes them."""

    public_env = {}

    def setUp(self):
        from subsync import signup
        self.signup = signup
        self.start_public(**self.public_env)

    def tearDown(self):
        self.stop_public()


# -- the daily check: its harness, and the parts of a person's config it reads -----------------------------------------
OTHER = "https://other-aios.test"     # another AIOStreams, answered by the same stand-in (RecheckHarness.api)
TORBOX = {"id": "torbox", "enabled": True, "credentials": {"apiKey": "tb-key"}}
REALDEBRID = {"id": "realdebrid", "enabled": True, "credentials": {"apiKey": "rd-key"}}
TORRENTIO = {"type": "torrentio", "instanceId": "tio", "enabled": True, "options": {"name": "Torrentio"}}
HOUR = 3600


class RecheckHarness(PublicParts):
    """The daily check's harness: public mode's parts on a clock the test moves (`clock`), the signup module, each
    change the check reports (`changed`), and checker(); with `dead` set, every AIOStreams answers nothing (a closed
    port). As a TestCase's first base: setUp makes it, tearDown closes it."""

    def setUp(self):
        from subsync import signup
        self.signup = signup
        self.clock = Clock()
        self.start_public(clock=self.clock)
        self.dead = False      # True: every AIOStreams answers nothing (a closed port)
        self.changed = []

    def tearDown(self):
        self.stop_public()

    def api(self, base, timeout):
        from subsync.aiosconfig import AiosApi
        url = "http://127.0.0.1:9" if self.dead else base.replace(INSTANCE, self.fake.url).replace(OTHER, self.fake.url)
        return AiosApi(url, timeout=timeout, max_timeout=timeout, retry_wait_cap=5)

    def checker(self, **env):
        from subsync.recheck import Checker
        s = Settings(dict(self.env, **env))
        return Checker(s, self.users, on_change=self.changed.append, api_factory=self.api, clock=self.clock, timeout=5)

    def sign_up(self, keep=False, presets=(TORRENTIO,)):
        viewer = {"services": [dict(TORBOX)], "presets": [dict(p) for p in presets]}
        uuid = self.fake.add(viewer, PASSWORD)
        fields = {"link": f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", "password": PASSWORD,
                  "languages": "English", "add_addon": "on"}
        if keep:
            fields["keep_password"] = "on"
        made = self.signup.signup(self.s, self.users, fields, api_factory=self.api, timeout=5)
        return uuid, made.token

    def record(self, token):
        return self.users.open(token)


# -- public mode over HTTP: a person's config, one request, and the service on this machine -----------------------------
VIEWER = {"services": [{"id": "torbox", "enabled": True, "credentials": {"apiKey": "tb-key"}}],
          "presets": [{"type": "torrentio", "instanceId": "tio", "enabled": True, "options": {"name": "Torrentio"}},
                      {"type": "opensubtitles-v3-plus", "instanceId": "osp", "enabled": True,
                       "options": {"name": "OpenSubtitles V3+", "resources": ["subtitles"]}}]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is the answer (its status and Location), never followed."""

    def redirect_request(self, *a, **k):
        return None


def http(url, data=None, headers=None, method=None):
    """One request: (status, body, headers), an error answer's too."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        with e:                                  # an error answer is closed once read, as any other
            return e.code, e.read(), dict(e.headers)


def form(**fields):
    """`fields` as a form posts them."""
    return urllib.parse.urlencode(fields).encode()


class ServerHarness(PublicParts):
    """A public service on this machine, with a stand-in AIOStreams; workers are not started (jobs stay queued). start()
    makes it, `env` added to its settings; close() closes everything start() made, the store too. As a TestCase's first
    base, the test starts it (in setUp or in the test itself) and tearDown closes it; a TestCase of another kind, or a
    test that needs a second one, holds it as a plain object and closes it itself."""

    def start(self, **env):
        from subsync.public import Pool, PublicService, serve_public
        from subsync.store import Store
        self.start_public(**dict({"SUBSYNC_EXTRA_SOURCES": "", "SUBSYNC_ORIGINAL_LANGUAGE": "off",
                                  "SUBSYNC_RECHECK_HOURS": "0"}, **env))
        self.store = Store(self.dir)
        self.pool = Pool(self.s, self.env, self.store, self.users, start_workers=False)
        self.service = PublicService(self.s, self.store, self.users, self.pool, api_factory=self.api)
        self.server = start_server(serve_public(self.service, 0, host="127.0.0.1"))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        # everything start made is closed here, the store too, not by cleanups: a plain object has none
        stop_server(self.server)
        self.pool.close()
        self.store.close()
        self.stop_public()

    def tearDown(self):
        self.close()

    def sign_up(self, **extra):
        """A sign-up with VIEWER's config and `extra` fields: (its config id, the answer's status, page and headers, the
        person's token or None)."""
        uuid = self.fake.add(json.loads(json.dumps(VIEWER)), PASSWORD)
        fields = dict(link=f"{INSTANCE}/stremio/{uuid}/ENCRYPTED/manifest.json", password=PASSWORD,
                      languages="English", add_addon="on")
        fields.update(extra)
        code, body, headers = http(self.base + "/signup", form(**fields))
        token = None
        text = body.decode()
        if code == 200:
            start = text.index("https://subs.test/u/") + len("https://subs.test/u/")
            token = text[start:start + 43]
        return uuid, code, text, headers, token


# -- public mode's pages: an instance's settings, a person's link, record and day's use, a sign-up's result, and the
#    elements and text of a page ----------------------------------------------------------------------------------------
BASE = "https://subs.test"                # the instance's address (SUBSYNC_PUBLIC_BASE)
TOKEN = "T" * 43                          # a person's token
LINK = f"{BASE}/u/{TOKEN}/manifest.json"  # their addon's link
RECORD = {"languages": ["eng", "spa"], "synced_at": 0}
USAGE = {"files": 3, "bytes": 351 * 1024}


def settings(**env):
    return Settings(dict({"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY, "SUBSYNC_PUBLIC_BASE": BASE}, **env))


class Tree(html.parser.HTMLParser):
    """Every element with its attributes, the text inside each id'd element, and the page's text."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.elements, self.text_of, self.open_ids, self.words = [], {}, [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.elements.append((tag, a))
        if "id" in a and tag not in ("input", "br", "img", "meta", "link"):
            self.open_ids.append((tag, a["id"]))
            self.text_of[a["id"]] = ""

    def handle_endtag(self, tag):
        for i in range(len(self.open_ids) - 1, -1, -1):
            if self.open_ids[i][0] == tag:
                del self.open_ids[i]
                break

    def handle_data(self, data):
        self.words.append(data)
        for _, i in self.open_ids:
            self.text_of[i] += data

    def all(self, tag, **match):
        return [a for t, a in self.elements if t == tag and all(a.get(k) == v for k, v in match.items())]

    def text(self):
        return re.sub(r"\s+", " ", " ".join(self.words))


def change_lines():
    """The lines aiosconfig.viewer_change writes for a sign-up, as the signed-up page gets them: AIOSubtitles added with
    the subtitle addons it now searches itself removed, then its address updated, then moved before another subtitle
    source (a shortened stand-in kept the real lines out of the approved-words test)."""
    from subsync.aiosconfig import viewer_change
    ours = {"type": "custom", "instanceId": "a", "enabled": True,
            "options": {"name": "AIOSubtitles", "manifestUrl": LINK, "resources": ["subtitles"]}}
    twins = [{"type": "subdl", "instanceId": "d", "enabled": True, "options": {"name": "SubDL"}},
             {"type": "opensubtitles", "instanceId": "o", "enabled": True, "options": {"name": "OpenSubtitles"}}]
    old = dict(ours, options=dict(ours["options"], manifestUrl=f"{BASE}/u/{'O' * 43}/manifest.json"))
    other = {"type": "subhero", "instanceId": "h", "enabled": True, "options": {"name": "SubHero"}}
    lines = []
    for presets, clean_up in ((twins, ("subdl", "opensubtitles")), ([old], ()), ([other, ours], ())):
        lines += viewer_change({"presets": presets}, LINK, name="AIOSubtitles", clean_up=clean_up)[1]
    return lines


def result(**kw):
    # the fields of signup.SignupResult, without importing signup (it needs cryptography), so the page tests run anywhere
    base = dict(token=TOKEN, manifest_url=LINK, changes=change_lines()[:2], warning=None,
                added=True, manual=False, languages=["eng", "spa"], store_only=False, to_store_only=False,
                usenet=False, usenet_why=None, sync_made=True)
    base.update(kw)
    return types.SimpleNamespace(**base)


# Ten lines of everyday dialogue per language, written for these tests (the language check)
DIALOGUE = {
    "eng": ["I don't know what you want from me.", "We have to go back to the house before it gets dark.",
            "Is that your car out there?", "She said she would call him when they got home.",
            "Come on, just tell me the truth.", "Why are you looking at me like that?",
            "There is nothing left for us here.", "Okay, let's get out of here now.", "He was right about all of it.",
            "I think we should wait for them."],
    "spa": ["No sé qué quieres de mí.", "Tenemos que volver a la casa antes de que oscurezca.",
            "¿Es tu coche el que está ahí fuera?", "Ella dijo que lo llamaría cuando llegaran a casa.",
            "Vamos, dime la verdad.", "¿Por qué me miras así?", "Ya no hay nada para nosotros aquí.",
            "Bueno, vámonos de aquí ahora.", "Él tenía razón en todo eso.", "Yo creo que debemos esperar a los demás."],
    "vie": ["Tôi không biết anh muốn gì ở tôi.", "Chúng ta phải về nhà trước khi trời tối.",
            "Đó có phải là xe của anh ở ngoài kia không?", "Cô ấy nói sẽ gọi cho anh ta khi họ về đến nhà.",
            "Thôi nào, cứ nói thật với tôi đi.", "Sao anh lại nhìn tôi như vậy?",
            "Ở đây không còn gì cho chúng ta nữa.", "Được rồi, chúng ta đi khỏi đây thôi.",
            "Ông ấy đã đúng về tất cả mọi chuyện.", "Tôi nghĩ chúng ta nên đợi họ."],
    "rus": ["Я не знаю, чего ты от меня хочешь.", "Нам нужно вернуться домой, пока не стемнело.",
            "Это твоя машина там снаружи?", "Она сказала, что позвонит ему, когда они вернутся домой.",
            "Ну же, просто скажи мне правду.", "Почему ты так на меня смотришь?",
            "Здесь для нас больше ничего не осталось.", "Ладно, давай уйдём отсюда.", "Он был прав во всём.",
            "Я думаю, нам стоит их подождать."],
}


def dialogue_srt(lang, n=120, start=30.0, step=3.0):
    """An SRT of `n` lines of DIALOGUE[lang] in turn (at least 300 words from n=60 on), one every `step` seconds."""
    out = []
    for i in range(n):
        s = start + i * step
        out.append(f"{i + 1}\n{ts(s)} --> {ts(s + 2.0)}\n{DIALOGUE[lang][i % len(DIALOGUE[lang])]}\n")
    return "\n".join(out)


class FakeDownloads:
    """subsync.worker.fetch_subtitle for tests: the bytes of each known address, a failure for any other; every
    address asked is recorded in `asked`. `hold` (a threading.Event), when set on an address, makes its download wait
    for it."""

    def __init__(self, files=None):
        self.files = {url: (text.encode("utf-8") if isinstance(text, str) else text) for url, text in (files or {}).items()}
        self.asked, self.hold = [], {}

    def __call__(self, url, timeout=30, headers=None):
        self.asked.append(url)
        if url in self.hold:
            self.hold[url].wait(10)
        if url not in self.files:
            raise OSError("no such subtitle")
        return self.files[url]

    def patch(self):
        return mock.patch("subsync.worker.fetch_subtitle", self)


class Clock:
    """A clock the test moves by hand (t, seconds as time.time gives them)."""

    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def wait_until(check, timeout=5.0):
    """check() once it is true, or its last answer after `timeout` seconds."""
    t0 = time.time()
    while not check() and time.time() - t0 < timeout:
        time.sleep(0.02)
    return check()


# -- synthetic timings: a release's lines as (start, duration), and subtitles made from them ----------------------------
def rhythm(n, seed, start=20.0):
    """n release lines: durations 1-4 s, gaps 0.3-3 s."""
    r = random.Random(seed)
    t, out = start, []
    for _ in range(n):
        d = r.uniform(1.0, 4.0)
        out.append((t, d))
        t += d + r.uniform(0.3, 3.0)
    return out


def realistic(n, seed, start=30.0):
    """n lines with real subtitle gaps (2-frame gaps, short, mid and long pauses)."""
    r = random.Random(seed)
    t, out = start, []
    for _ in range(n):
        d = r.uniform(1.0, 4.5)
        out.append((t, d))
        x = r.random()
        t += d + (0.083 if x < 0.45 else r.uniform(0.25, 1) if x < 0.7 else r.uniform(1, 8) if x < 0.9 else r.uniform(8, 40))
    return out


def back_to_back(n, seed, start=30.0):
    """Lines as a busy film has them: a fifth follow the previous one with no gap, so their PGS clears are left out and
    the image reference keeps only its sure starts (about half here; 73 % on Kingdom of Heaven's director's cut)."""
    r = random.Random(seed)
    t, out = start, []
    for _ in range(n):
        d = r.uniform(1.0, 4.5)
        out.append((t, d))
        x = r.random()
        t += d + (0.0 if x < 0.22 else 0.083 if x < 0.39 else r.uniform(0.25, 1) if x < 0.75 else
                  r.uniform(1, 8) if x < 0.93 else r.uniform(8, 40))
    return out


def pgs_track(lines, number=7, lang="eng"):
    """An image track showing and clearing each line."""
    ev = []
    for s, d in lines:
        ev += [(s, 0.0), (s + d, 0.0)]
    return {"number": number, "language": lang, "codec": "S_HDMV/PGS", "name": "", "forced": False, "cues": ev}


def srt_of(lines, shift=0.0, jitter=0.0, seed=1):
    """An SRT of the lines, shifted, each start `jitter` (sigma, at most 120 ms) off."""
    r = random.Random(seed)
    out = []
    for s, d in lines:
        e = max(-0.12, min(0.12, r.gauss(0, jitter))) if jitter else 0.0
        out.append({"s": s + shift + e, "e": s + d + shift + e, "text": "line", "kind": "dialogue"})
    return to_srt(out)


def translated(lines, seed, shift, cut=None, sigma=0.12, resplit=0.2, merge=0.25):
    """Another language's subtitle of the same film: a quarter of the line pairs merged into one line, each start in its
    author's timing style, a fifth split elsewhere (0.3 to 0.8 s off), all shifted; with `cut` (a, b) the lines of that
    stretch left out (a subtitle for another cut)."""
    r = random.Random(seed)
    out, k = [], 0
    while k < len(lines):
        s, d = lines[k]
        if k + 1 < len(lines) and r.random() < merge:
            d = lines[k + 1][0] + lines[k + 1][1] - s
            k += 1
        k += 1
        if cut and cut[0] <= s <= cut[1]:
            continue
        e = r.gauss(0, sigma)
        if r.random() < resplit:
            e += r.choice((-1, 1)) * r.uniform(0.3, 0.8)
        out.append({"s": s + shift + e, "e": s + d + shift + e, "text": "строка", "kind": "dialogue"})
    return to_srt(out)


class FakeIndex:
    """A file's index with the given text and image tracks: no reads."""

    def __init__(self, text=(), image=()):
        self._t, self._i, self.size = list(text), list(image), 1

    def text_tracks(self):
        return self._t

    def image_tracks(self):
        return self._i


# -- the audio route on a synthetic timeline ---------------------------------------------------------------------------
def timeline_fit(speech, rate=0, ends=False, base=None):
    """An AudioFit class (`base`, AudioFit by default) whose windows come straight from a timeline of speech: no audio,
    no Silero. `speech`: [(start, end)], or {audio track: [(start, end)]}. A window read costs `rate` bytes a second,
    added to the index's audio_bytes; with `ends` it carries its speech-run ends ("ends"), as every window does
."""
    import numpy as np
    from subsync.audiofit import AudioFit

    class TimelineFit(base or AudioFit):
        def _read_window(self, start, length):
            if rate:
                self.index.audio_bytes += int(length * rate)
            self.windows_read += 1
            self.audio_s += length
            n = int(round(length * 100))
            mask, onsets, ends_at = np.zeros(n, dtype=bool), [], []
            for a, b in speech[self.track or 0] if isinstance(speech, dict) else speech:
                if b <= start or a >= start + length:
                    continue
                mask[max(0, int((a - start) * 100)):min(n, int((b - start) * 100))] = True
                if a >= start:
                    onsets.append(a - self.lead)
                if b <= start + length:
                    ends_at.append(b)
            window = {"start": start, "len": length, "mask": mask, "onsets": np.array(onsets)}
            if ends:
                window["ends"] = np.array(ends_at)
            return window
    return TimelineFit


def truth_fit(truth, starts, durations=None, extra=0, budget=None, rate=0, end_pad=-0.6, fit_class=None, **kw):
    """The audio fit of lines at `starts` on the release timeline `truth`: speech starts ~80 ms off each release line's
    start and ends `end_pad` from its end (None: windows without speech-run ends, as before). Returns (fit object,
    result)."""
    r = random.Random(5)
    speech = [(st + r.gauss(0, 0.08), st + d + (end_pad or 0.0)) for st, d in truth]
    end = truth[-1][0] + truth[-1][1] + 30

    class TheIndex:
        size = int(rate * end)
        audio_bytes = 0

        def duration(self):
            return end

    durations = durations or [d for _, d in truth]
    lines = [{"s": st, "e": st + d, "text": "x", "kind": "dialogue"} for st, d in zip(starts, durations)]
    fit_class = timeline_fit(speech, rate=rate, ends=end_pad is not None, base=fit_class)
    fit = fit_class(TheIndex(), lines, vad=object(), better=kw.pop("better", AUDIO_BETTER), extra=extra, budget=budget,
                    **kw)
    return fit, fit.run()


# -- a person in public mode: their sync config and their limits ---------------------------------------------------------
class Listing:
    """A sync config with one row and a fixed subtitle list."""

    def __init__(self, row=None, subtitles=()):
        self.row, self.subs, self.base, self.config_sources = row, list(subtitles), "https://aios.test/stremio/u/e", set()

    def find_row(self, *a):
        return self.row

    def subtitles(self, *a):
        return [dict(c) for c in self.subs]


class Limits:
    """A person's limits (public.UserLimits' methods), with today's data left and today's audio syncs. Their own rows
    in the store are keyed by `who` too (UserLimits.rows_key)."""

    def __init__(self, who="a" * 16, queue=3, left=10 ** 12, per_day=20, opened_per_day=5, room=True):
        self.who, self.queue, self.left, self.room = who, queue, left, room
        self.rows_key = who
        self.per_day, self.opened_per_day = per_day, opened_per_day
        self.started, self.read, self.syncs, self.opened = 0, 0, 0, 0

    def may_queue(self, queued):
        return queued < self.queue

    def may_start_file(self):
        return True

    def file_started(self):
        self.started += 1

    def has_room(self, n):
        return self.room and n <= self.left

    def before_read(self, length):
        from subsync.worker import OverLimit
        if length > self.left:
            raise OverLimit("bytes per day")

    def after_read(self, n):
        self.read += n
        self.left -= n

    def may_start_audio(self, opened=False):
        return self.syncs < self.per_day and (not opened or self.opened < self.opened_per_day)

    def audio_started(self, opened=False):
        self.syncs += 1
        self.opened += bool(opened)


# -- a person's AIOStreams configs for Usenet files ----------------------------------------------------------------------
def row(kind="usenet", service="aiostreams", cached=True, library=False, url="http://aios/playback/1", name="a.mkv",
        size=2_000_000_000):
    """A stream listing's row."""
    return {"url": url, "streamData": {"type": kind, "filename": name, "size": size, "library": library,
                                        "service": {"id": service, "cached": cached}}}


class FakeDebrid:
    """The sync config: never has the file (it is on Usenet only)."""

    def __init__(self, subs=()):
        self.base, self.subs, self.config_sources, self.calls = "https://aios/stremio/d/e", list(subs), set(), []

    def find_row(self, *a):
        self.calls.append("find_row")
        return None

    def subtitles(self, *a):
        self.calls.append("subtitles")
        return [dict(c) for c in self.subs]


class FakeUsenet(FakeDebrid):
    """The Usenet config: lists `listed` (or raises `fail`), counting its listings and config checks."""

    def __init__(self, listed=(), fail=None, subs=()):
        super().__init__(subs)
        self.base = "https://aios/stremio/u/e"
        self.listed, self.fail, self.listings = list(listed), fail, 0
        self.blocked, self.config_checks = False, 0   # with blocked, the config can't be checked (sources.Aios)

    def check_config(self):
        self.config_checks += 1
        if self.blocked:
            raise realdebrid.Blocked("subsync's config can't be checked for Real-Debrid")

    def streams(self, *a):
        self.check_config()                                  # as sources.Aios does before every request through it
        self.listings += 1
        if self.fail:
            raise self.fail
        return [dict(r) for r in self.listed]

    def match(self, listed, hints, usenet=False):
        return sources.Aios("").match(listed, hints, usenet=usenet)


class Counts:
    """A day's Usenet listings (the gate's counts), in memory."""

    def __init__(self):
        self.n, self.stopped_ = 0, False

    def listings(self):
        return self.n

    def add_listing(self):
        self.n += 1

    def stopped(self):
        return self.stopped_

    def stop(self):
        self.stopped_ = True


# -- files from memory: a fixture's bytes, a reader and urlopen over bytes, a made-up Matroska file --------------------
def fixture(name):
    """The bytes of fixtures/<name>."""
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


class Memory:
    """A RangeReader stand-in over bytes in memory: records each read (start, length)."""

    def __init__(self, data):
        self.b, self.size, self.reads = bytes(data), len(data), []

    def get(self, start, length):
        self.reads.append((start, length))
        return self.b[start:start + length]


def el(eid, payload):
    """An EBML element: its id (hex), an 8-byte size, its payload."""
    return bytes.fromhex(eid) + b"\x01" + len(payload).to_bytes(7, "big") + payload


def text_track(number=1):
    return el("AE", el("D7", bytes([number])) + el("83", b"\x11") + el("86", b"S_TEXT/UTF8"))


def matroska(info=None, tracks=None, pad_to=None, seek_tracks=False):
    """EBML header, a Segment of unknown size with an optional SeekHead (to Tracks), Info, a Void that puts Tracks at
    byte `pad_to`, Tracks, one Cluster."""
    ebml = el("1A45DFA3", el("4282", b"matroska"))
    seg = bytes.fromhex("18538067") + bytes.fromhex("01FFFFFFFFFFFFFF")
    seg_data = len(ebml) + len(seg)
    info = el("1549A966", el("2AD7B1", (1_000_000).to_bytes(4, "big")) + el("4489", struct.pack(">d", 90_000.0))) \
        if info is None else info
    tracks = el("1654AE6B", text_track()) if tracks is None else tracks

    def seekhead(at):
        return el("114D9B74", el("4DBB", el("53AB", bytes.fromhex("1654AE6B")) + el("53AC", at.to_bytes(8, "big"))))
    seek = seekhead(0) if seek_tracks else b""                     # the same length wherever it points
    before = seg_data + len(seek) + len(info)
    void = b"" if pad_to is None else el("EC", bytes(pad_to - before - 9))
    if seek_tracks:
        seek = seekhead(before + len(void) - seg_data)
    return ebml + seg + seek + info + void + tracks + el("1F43B675", el("E7", b"\x00"))


class Response:
    """A 206 answer from memory; with `reset_after`, the body breaks off with a reset after that many bytes."""
    status = 206

    def __init__(self, body, start, end, size, reset_after=None):
        self.body, self.pos, self.reset_after = body, 0, reset_after
        self.headers = {"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(len(body))}

    def read(self, n=-1):
        if self.reset_after is not None and self.pos >= self.reset_after:
            raise ConnectionResetError(54, "Connection reset by peer")
        n = len(self.body) - self.pos if n is None or n < 0 else n
        if self.reset_after is not None:
            n = min(n, self.reset_after - self.pos)
        chunk = self.body[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Opener:
    """realdebrid.urlopen's stand-in: serves a file's bytes from memory; `reset_after` arms a mid-body reset."""

    def __init__(self, data):
        self.data, self.reset_after = data, None

    def __call__(self, request, timeout):
        a, _, b = request.get_header("Range")[6:].partition("-")
        start, end = int(a), min(int(b), len(self.data) - 1)
        return Response(self.data[start:end + 1], start, end, len(self.data), self.reset_after)


# -- a file's link through a debrid service: a playback address that redirects -----------------------------------------
class DebridLink:
    """/link/tiny.mkv redirects (302) to /file/<token>/tiny.mkv, which serves ranges of tiny.mkv (`data`) while its
    token is valid."""

    def __init__(self):
        self.data = fixture("tiny.mkv")
        self.link_hits, self.file_hits, self.token, self.expired = 0, 0, 1, set()
        self.link_status = None      # e.g. 429: the link itself refuses
        self.file_status = None      # e.g. 429: the download address slows us down
        self.link_delay = 0.0        # seconds the link takes to answer
        self.file_dead = False       # the download address drops every connection unanswered (a network fault)
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def empty(self, code, location=None):
                self.send_response(code)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                parts = self.path.split("/")
                if parts[1] == "link":
                    outer.link_hits += 1
                    if outer.link_delay:
                        time.sleep(outer.link_delay)
                    if outer.link_status:
                        return self.empty(outer.link_status)
                    return self.empty(302, f"/file/{outer.token}/{parts[2]}")
                if parts[1] == "file":
                    outer.file_hits += 1
                    if outer.file_dead:
                        self.close_connection = True
                        return
                    if outer.file_status:
                        return self.empty(outer.file_status)
                    if int(parts[2]) in outer.expired:
                        return self.empty(403)
                    a, _, b = self.headers["Range"][6:].partition("-")
                    start, end = int(a), min(int(b), len(outer.data) - 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(outer.data)}")
                    self.send_header("Content-Length", str(end - start + 1))
                    self.end_headers()
                    try:
                        self.wfile.write(outer.data[start:end + 1])
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                self.empty(404)

        self.server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.link = f"http://127.0.0.1:{self.server.server_address[1]}/link/tiny.mkv"

    def expire(self):
        """The download address handed out so far stops answering; the link hands out a new one."""
        self.expired.add(self.token)
        self.token += 1

    def close(self):
        stop_server(self.server)


# -- the original-language lookup's services ---------------------------------------------------------------------------
class LookupServices:
    """The original-language lookup's services: TMDB at /tmdb, Wikidata's query service at /sparql, Kitsu at
    /kitsu, AniList at /anilist."""

    def __init__(self):
        self.calls, self.lock = [], threading.Lock()
        self.find, self.details, self.wikidata, self.kitsu, self.anilist = {}, {}, {}, {}, {}
        self.status = {}            # service -> (code, headers) to answer with instead
        self.delay = {}             # service -> seconds
        self.active, self.most_active = {}, {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def answer(self, code, obj=None, headers=()):
                body = json.dumps(obj if obj is not None else {}).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_any(self, method):
                url = urllib.parse.urlsplit(self.path)
                n = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(n).decode() if n else ""
                service = url.path.split("/")[1]
                with outer.lock:
                    outer.calls.append({"service": service, "method": method, "path": url.path, "query": url.query,
                                        "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                    outer.active[service] = outer.active.get(service, 0) + 1
                    outer.most_active[service] = max(outer.most_active.get(service, 0), outer.active[service])
                try:
                    if outer.delay.get(service):
                        time.sleep(outer.delay[service])
                    if service in outer.status:
                        code, headers = outer.status[service]
                        return self.answer(code, {"status_message": "made up"}, headers)
                    q = urllib.parse.parse_qs(url.query)
                    if service == "tmdb":
                        parts = url.path.split("/")
                        if parts[2] == "find":
                            return self.answer(200, outer.find.get(parts[3], {"movie_results": [], "tv_results": []}))
                        got = outer.details.get((parts[2], parts[3]))
                        return self.answer(200, got) if got else self.answer(404, {"status_code": 34})
                    if service == "sparql":
                        tt = re.search(r'wdt:P345 "(tt\d+)"', q["query"][0]).group(1)
                        return self.answer(200, {"head": {"vars": ["lang"]},
                                                 "results": {"bindings": outer.wikidata.get(tt, [])}})
                    if service == "kitsu":
                        n = url.path.split("/")[3]
                        if n not in outer.kitsu:
                            return self.answer(404, {"errors": [{"title": "Record not found"}]})
                        mal = outer.kitsu[n]
                        inc = [{"type": "mappings", "id": "1", "attributes": {"externalSite": "anidb",
                                                                               "externalId": "1"}}]
                        if mal:
                            inc.append({"type": "mappings", "id": "2", "attributes": {
                                "externalSite": "myanimelist/anime", "externalId": str(mal)}})
                        return self.answer(200, {"data": {"id": n, "type": "anime"}, "included": inc})
                    if service == "anilist":
                        mal = json.loads(body)["variables"]["id"]
                        if mal not in outer.anilist:
                            return self.answer(404, {"data": {"Media": None}, "errors": [{"message": "Not Found."}]})
                        return self.answer(200, {"data": {"Media": {"countryOfOrigin": outer.anilist[mal]}}})
                    return self.answer(404)
                finally:
                    with outer.lock:
                        outer.active[service] -= 1

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

        self.srv = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def of(self, service):
        with self.lock:
            return [c for c in self.calls if c["service"] == service]

    def close(self):
        stop_server(self.srv)


# -- the built-in subtitle sources: a stand-in that answers like them, and their harness -------------------------------
# each subtitle the stand-in serves (in a zip, once for each name, its first line's text naming it)
SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n2\n00:00:03,000 --> 00:00:04,000\nGeneral Kenobi\n"
HASH = "a" * 40      # a torrent whose subtitles name S01E02 (two) and S01E03 (one)
PACK = "b" * 40      # a season pack's torrent: its subtitles name their episodes, one names none
SINGLE = "c" * 40    # one episode's torrent, whose subtitles name no episode


def zipped(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, SRT.replace("Hello", n))
    return buf.getvalue()


def tsdb_item(i, lang, release, cues=900):
    return {"id": i, "language": lang, "format": "srt", "release_name": release, "cues": cues, "hearing_impaired": False,
            "ai": None, "ai_transcribed": False, "download_url": f"{{url}}/tsdb/get/{i}"}


class FakeSources:
    """Answers like the built-in sources. `mode` per source: None, "500", "404", "423", "429", "503" (no Retry-After),
    "503-retry" (with one), "slow"; /hop?to=<address> answers with a redirect there."""

    def __init__(self):
        self.calls, self.headers, self.mode, self.slow_s = [], [], {}, 1.5
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body, ctype="application/json", extra=None):
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def items(self, rows):
                return {"subtitles": {"total": len(rows), "limit": 100, "offset": 0,
                                      "items": [dict(r, download_url=r["download_url"].format(url=outer.url)) for r in rows]}}

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                outer.calls.append((u.path, q))
                outer.headers.append((u.path, dict(self.headers)))
                top = u.path.split("/")[1]
                mode = outer.mode.get(top)
                if mode == "slow":
                    time.sleep(outer.slow_s)
                elif mode == "500":
                    return self.send(500, {"error": "down"})
                elif mode == "429":
                    return self.send(429, {"error": "slow down"}, extra={"Retry-After": "120"})
                elif mode == "503":
                    return self.send(503, {"error": "restarting"})
                elif mode == "503-retry":
                    return self.send(503, {"error": "busy"}, extra={"Retry-After": "120"})
                p = u.path
                if p == "/hop":
                    return self.send(302, b"", "text/plain", extra={"Location": q["to"]})
                # TheSubtitleDB
                if p == "/tsdb/v1/by-imdb/tt1":
                    return self.send(200, self.items([tsdb_item(11, "en", "Movie.2010.WEB-DL"),
                                                      tsdb_item(12, "ru", "Movie.2010.BluRay", 870)]))
                if p == "/tsdb/v1/by-imdb/tt7":             # a popular title: page after page, 2 a page
                    off = int(q.get("offset", 0))
                    answer = self.items([tsdb_item(700 + k, "en", f"Movie.Release.{k}") for k in range(off, off + 2)])
                    answer["subtitles"].update(total=100, limit=2, offset=off)
                    return self.send(200, answer)
                if p == "/tsdb/v1/by-imdb/tt2/season/1/episode/2":
                    return self.send(200, self.items([tsdb_item(41, "en", "Show.S01E02.HDTV")]))
                if p == f"/tsdb/v1/by-infohash/{HASH}":
                    return self.send(200, self.items([tsdb_item(21, "en", "Show.S01E02.1080p.WEB-NTb"),
                                                      tsdb_item(22, "en", "Show.S01E03.1080p.WEB-NTb"),
                                                      tsdb_item(23, "ru", "Show.S01E02.1080p.WEB-NTb")]))
                if p == f"/tsdb/v1/by-infohash/{PACK}":
                    return self.send(200, self.items([tsdb_item(51, "en", "Show.S01E02.1080p.WEB-NTb"),
                                                      tsdb_item(52, "en", "Show.S01E03.1080p.WEB-NTb"),
                                                      tsdb_item(53, "en", "Show.S01.1080p.WEB-DL-NTb")]))
                if p == f"/tsdb/v1/by-infohash/{SINGLE}":
                    return self.send(200, self.items([tsdb_item(61, "en", "Show.1080p.WEB-DL-NTb")]))
                if p == "/tsdb/v1/by-releasename":
                    if outer.mode.get("tsdb-release") == "400":
                        return self.send(400, {"error": "bad release"})
                    # the real answer (capture): the title TheSubtitleDB matched, its closest subtitle and score,
                    # and that title's subtitles; for a name it doesn't know it matches another title
                    rel = q.get("release", "")
                    if "Inception" in rel:
                        return self.send(200, dict(self.items([tsdb_item(91, "en", "Nomadland.2020.1080p.WEB")]),
                                                   match={"sub_id": 91, "score": 0.57}, title={"imdb": "tt9770150"}))
                    return self.send(200, dict(self.items([tsdb_item(31, "en", rel), tsdb_item(32, "en", "Show.S01E02.HDTV"),
                                                           tsdb_item(21, "en", "Show.S01E02.1080p.WEB-NTb")]),
                                               match={"sub_id": 31, "score": float(outer.mode.get("score", 0.95))},
                                               title={"imdb": "tt2"}))
                if p.startswith("/tsdb/get/"):
                    return self.send(200, SRT.encode(), "text/plain")
                # Stremio's OpenSubtitles v3
                if p.startswith("/osv3/subtitles/"):
                    return self.send(200, {"subtitles": [
                        {"id": "101", "url": f"{outer.url}/osv3/file/101", "lang": "eng", "m": "h", "g": "1"},
                        {"id": "102", "url": f"{outer.url}/osv3/file/102", "lang": "rus", "m": "i", "g": "1"},
                        {"id": "103", "url": f"{outer.url}/osv3/file/103", "lang": "fre", "m": "i", "g": "1"}]})
                if p.startswith("/osv3/file/"):
                    return self.send(200, SRT.encode(), "text/plain")
                # Cinemeta + Gestdown
                if p == "/cinemeta/meta/series/tt2.json":
                    return self.send(200, {"meta": {"name": "Show", "tvdb_id": 121361}})
                if p == "/cinemeta/meta/series/tt3.json":
                    return self.send(200, {"meta": {"name": "Unknown", "tvdb_id": 999}})
                if p == "/gestdown/shows/external/tvdb/121361":
                    return self.send(200, {"shows": [{"id": "S1", "name": "Show"}]})
                if p == "/gestdown/shows/external/tvdb/999":
                    return self.send(404, {"error": "show not found"})
                if p.startswith("/gestdown/subtitles/get/S1/1/2/"):
                    if mode == "423":
                        return self.send(423, {"error": "refreshing"})
                    lang = p.rsplit("/", 1)[1]
                    if lang != "English":
                        return self.send(404, {"error": "no subtitles"})
                    return self.send(200, {"matchingSubtitles": [
                        {"subtitleId": "G1", "version": "WEB.NTb", "completed": True, "hearingImpaired": False,
                         "downloadUri": "/subtitles/download/G1"}]})
                # SubSource
                if p.startswith("/subsource/"):
                    if self.headers.get("X-API-Key") != "ss-key":
                        return self.send(401, {"error": "key"})
                    if p == "/subsource/movies/search":
                        return self.send(200, {"data": [{"id": 555, "title": "Show"}]})
                    if p == "/subsource/subtitles":
                        return self.send(200, {"data": [
                            {"subtitleId": 7001, "language": "english", "releaseInfo": ["Show.S01E02.1080p.WEB-NTb"],
                             "hearingImpaired": False},
                            {"subtitleId": 7002, "language": "english", "releaseInfo": ["Show.S01E03.1080p.WEB"]},
                            {"subtitleId": 7003, "language": "russian", "releaseInfo": "Show.S01.Complete"},
                            {"subtitleId": 7004, "language": "english", "releaseInfo": ["Show.S01E02.WEB"],
                             "productionType": "machine translated"}]})
                    if p == "/subsource/subtitles/7001/download":
                        return self.send(200, zipped(["Show.S01E01.srt", "Show.S01E02.srt"]), "application/zip")
                return self.send(404, {})

        self.server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def asked(self, prefix):
        return [p for p, _ in self.calls if p.startswith(prefix)]

    def close(self):
        stop_server(self.server)


class SourcesHarness:
    """The built-in sources against FakeSources (`fake`), each source's address in `env`; extra() makes them, more
    settings added. A connection to anything but this machine fails the test. As a TestCase's first base: setUp makes
    it, tearDown closes it."""

    def setUp(self):
        self.guard = local_network_only()
        self.guard.start()
        self.fake = FakeSources()
        u = self.fake.url
        self.env = {"SUBSYNC_LANGUAGES": "eng,rus", "SUBSYNC_TSDB_URL": f"{u}/tsdb", "SUBSYNC_CINEMETA_URL": f"{u}/cinemeta",
                    "SUBSYNC_GESTDOWN_URL": f"{u}/gestdown", "SUBSYNC_OSV3_URL": f"{u}/osv3",
                    "SUBSYNC_OSV3PLUS_URL": f"{u}/osv3plus", "SUBSYNC_SUBDL_URL": f"{u}/subdl",
                    "SUBSYNC_SUBDL_DOWNLOAD_URL": f"{u}/subdl-dl", "SUBSYNC_WYZIE_URL": f"{u}/wyzie",
                    "SUBSYNC_SUBSOURCE_URL": f"{u}/subsource"}
        self.clock = Clock()

    def tearDown(self):
        self.fake.close()
        self.guard.stop()

    def extra(self, clock=None, ledger=None, **env):
        from subsync.extra import ExtraSources
        return ExtraSources(Settings(dict(self.env, **env)), clock=clock or self.clock, ledger=ledger)
