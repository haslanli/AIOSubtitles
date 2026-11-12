"""Test helpers: a local HTTP range server (stands in for AIOS's proxy), with switchable faults."""
import os
import shutil
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class RangeServer:
    """Serves files under root with Range support. fault: None | '403' | 'ignore-range' | 'wrong-size'."""

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
                if rng and outer.fault != "ignore-range":
                    a, _, b = rng[6:].partition("-")
                    start, end = int(a), min(int(b) if b else size - 1, size - 1)
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
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                outer.log.append((start, end - start + 1))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, name):
        return f"http://127.0.0.1:{self.port}/{name}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TempDir:
    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="subsync-test-")
        return self.path

    def __exit__(self, *a):
        shutil.rmtree(self.path, ignore_errors=True)


def have(tool):
    return shutil.which(tool) is not None


LOCAL = ("127.0.0.1", "localhost", "::1")


def local_network_only():
    """A patch that fails the test, before any lookup or packet, when code opens a connection to anything but this
    machine: tests about addresses that must never be reached can't reach them, even while the code is wrong."""
    real = socket.create_connection

    def guarded(address, *args, **kwargs):
        if address[0] not in LOCAL:
            raise AssertionError(f"a connection to {address[0]} was opened")
        return real(address, *args, **kwargs)
    return mock.patch("socket.create_connection", guarded)


class FakeAiostreams:
    """A stand-in for a person's AIOStreams: its status, its config API (read, save, create, delete, with basic auth)
    and each config's manifest. Records every request (method, path). Faults: 'wrong_password', 'refuse_create',
    'refuse_save', 'hang' (answers after `hang_s`), 'html' (answers a web page), 'bad_config' (userData not an object),
    'no_manifest' (configs' manifests answer 404), 'rate_limited' (new configs answer 429, retry in an hour)."""

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
        self.encrypted = {}            # config id -> the encrypted password its link carries (default "ENC" + id[:8])
        self.auth_ids = []             # the config ids each password-checked request named
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
                        if outer.fault == "refuse_create":
                            return self.answer(400, {"success": False, "error": {"code": "USER_INVALID_CONFIG",
                                                                                 "message": "bad config"}})
                        b = self.body()
                        new = str(uuidlib.uuid4())
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
                        stored["config"] = self.body()["config"]
                        return self.answer(200, {"success": True})
                    if self.command == "DELETE":
                        del outer.configs[uuid]
                        return self.answer(200, {"success": True})
                parts = path.split("/")
                if len(parts) == 5 and parts[1] == "stremio" and parts[4] == "manifest.json" and parts[2] in outer.configs \
                        and outer.fault != "no_manifest":
                    return self.answer(200, {"id": "com.aiostreams", "name": "AIOStreams", "resources": ["stream"]})
                return self.answer(404, {"success": False})

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = handle_any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def enc(self, uuid):
        return self.encrypted.get(uuid, "ENC" + uuid[:8])

    def add(self, config, password):
        import uuid as uuidlib
        uuid = str(uuidlib.uuid4())
        self.configs[uuid] = {"password": password, "config": config}
        return uuid

    def writes(self):
        return [r for r in self.requests if r[0] in ("POST", "PUT", "DELETE")]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
