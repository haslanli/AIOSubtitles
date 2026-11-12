"""The one gate every outbound request passes: in public mode it refuses anything but public internet addresses, checked
on the name's lookup and again on the address the connection really reached, redirects included. No test here looks a
name up or connects anywhere but this machine."""
import os
import socket
import sys
import unittest
import urllib.error
import urllib.request
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subsync import net, realdebrid  # noqa: E402
from helpers import local_network_only  # noqa: E402


def resolver_answering(ip):
    def resolve(host, port, *a, **k):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 80))]
    return resolve


class Server:
    """A local server that records every request and answers 200, or redirects when told to."""

    def __init__(self, redirect_to=None):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                if redirect_to:
                    self.send_response(302)
                    self.send_header("Location", redirect_to)
                else:
                    self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def refused(test, call):
    """The call is refused by the gate: net.Refused itself, or urllib's wrapper around it."""
    try:
        call()
    except net.Refused:
        return
    except urllib.error.URLError as e:
        test.assertIsInstance(e.reason, net.Refused, e)
        return
    test.fail("not refused")


class RangesTest(unittest.TestCase):
    def test_ranges(self):
        for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
                   "224.0.0.1", "240.0.0.1", "::1", "fe80::1", "fe80::1%eth0", "fc00::1", "::ffff:127.0.0.1",
                   "::ffff:10.0.0.1", "not an address"):
            with self.subTest(ip=ip):
                self.assertFalse(net.is_public_ip(ip))
        for ip in ("8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"):
            with self.subTest(ip=ip):
                self.assertTrue(net.is_public_ip(ip))


class GateTest(unittest.TestCase):
    def tearDown(self):
        net.configure(public_only=False)

    def test_a_name_resolving_to_a_private_address_is_refused_before_connecting(self):
        net.configure(public_only=True, resolver=resolver_answering("10.0.0.5"))
        with local_network_only():
            refused(self, lambda: realdebrid.urlopen("http://evil.test/x", timeout=5))

    def test_the_metadata_service_is_refused(self):
        net.configure(public_only=True, resolver=resolver_answering("169.254.169.254"))
        with local_network_only():
            refused(self, lambda: realdebrid.urlopen("http://169.254.169.254/opc/v2/instance/", timeout=5))

    def test_a_connection_that_lands_on_a_private_address_is_refused(self):
        server = Server()
        try:
            net.configure(public_only=True, resolver=resolver_answering("93.184.216.34"))
            real = socket.create_connection

            def lands_here(address, *a, **k):   # the lookup said public; the connection reaches this machine
                return real(("127.0.0.1", server.port), *a, **k)
            with unittest.mock.patch("socket.create_connection", lands_here):
                refused(self, lambda: realdebrid.urlopen(f"http://rebind.test:{server.port}/x", timeout=5))
            self.assertEqual(server.requests, [])
        finally:
            server.close()

    def test_a_redirect_to_a_private_address_is_refused(self):
        net.configure(public_only=True, resolver=resolver_answering("10.9.9.9"))
        with local_network_only():
            refused(self, lambda: realdebrid.GuardedRedirects().redirect_request(
                urllib.request.Request("http://public.test/"), _Closable(), 302, "Found", {}, "http://inside.test/x"))

    def test_other_schemes_are_refused(self):
        net.configure(public_only=True, resolver=resolver_answering("93.184.216.34"))
        for url in ("file:///etc/passwd", "ftp://ftp.test/x", "data:text/plain,hello"):
            with self.subTest(url=url):
                refused(self, lambda: realdebrid.urlopen(url, timeout=5))

    def test_policy_off_reaches_local_servers(self):
        server = Server()
        try:
            with realdebrid.urlopen(f"http://127.0.0.1:{server.port}/x", timeout=5) as r:
                self.assertEqual(r.read(), b"ok")
        finally:
            server.close()

    def test_real_debrid_is_refused_under_both_policies(self):
        for public in (False, True):
            net.configure(public_only=public, resolver=resolver_answering("93.184.216.34"))
            with self.subTest(public=public), local_network_only():
                with self.assertRaises(realdebrid.Blocked):
                    realdebrid.urlopen("https://api.real-debrid.com/rest/1.0/user", timeout=5)

    def test_proxies_from_the_environment_are_not_used_in_public_mode(self):
        server = Server()
        try:
            net.configure(public_only=True, resolver=resolver_answering("10.0.0.8"))
            real = net.is_public_ip
            # the proxy counts as public here: used, it would get the request for the private address
            with unittest.mock.patch.dict(os.environ, {"http_proxy": f"http://127.0.0.1:{server.port}", "no_proxy": ""}), \
                    unittest.mock.patch.object(net, "is_public_ip", lambda ip: ip == "127.0.0.1" or real(ip)), \
                    local_network_only():
                with unittest.mock.patch.object(realdebrid, "_OPENER", net.build_opener()):   # proxies read now
                    refused(self, lambda: realdebrid.urlopen("http://inside.test/x", timeout=5))
            self.assertEqual(server.requests, [])
        finally:
            server.close()


class _Closable:
    def close(self):
        pass


if __name__ == "__main__":
    unittest.main()
