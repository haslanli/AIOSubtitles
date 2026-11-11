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
