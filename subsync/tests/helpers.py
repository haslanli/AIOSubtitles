"""Test helpers: a local HTTP range server (stands in for AIOS's proxy), with switchable faults, and the stand-ins and
data the test modules share.

Every test module imports helpers before anything from subsync: this is the one place that puts the package on the
import path (the source next to these tests, ahead of any other copy), so all modules test the same copy and each one
also runs on its own (python3 -m unittest test_x from this folder). The image build is the exception: it tests the
image's own copy (see IMAGE_BUILD). It is also the one place that keeps the whole run on this machine (local_lookup)."""
import logging
import os
import random
import shutil
import socket
import sys
import tempfile
import threading
import time
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
    """`resource` (a Store), closed when the test ends, so no test leaves its database open."""
    test.addCleanup(resource.close)
    return resource


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
    'no_manifest' (configs' manifests answer 404), 'rate_limited' (new configs answer 429, retry in an hour).
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
    """What each public-mode harness starts from (test_public_http, test_recheck, test_signup): a data folder, public
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
    added to the index's audio_bytes; with `ends` it carries its speech-run ends ("offsets"), as every window does
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
            mask, onsets, offsets = np.zeros(n, dtype=bool), [], []
            for a, b in speech[self.track or 0] if isinstance(speech, dict) else speech:
                if b <= start or a >= start + length:
                    continue
                mask[max(0, int((a - start) * 100)):min(n, int((b - start) * 100))] = True
                if a >= start:
                    onsets.append(a - self.lead)
                if b <= start + length:
                    offsets.append(b)
            window = {"start": start, "len": length, "mask": mask, "onsets": np.array(onsets)}
            if ends:
                window["offsets"] = np.array(offsets)
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
    """A person's limits (public.UserLimits' methods), with today's data left and today's audio syncs."""

    def __init__(self, who="a" * 16, queue=3, left=10 ** 12, per_day=20, opened_per_day=5, room=True):
        self.who, self.queue, self.left, self.room = who, queue, left, room
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
