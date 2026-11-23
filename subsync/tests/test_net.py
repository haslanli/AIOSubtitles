"""The one gate every outbound request passes: in public mode it refuses anything but public internet addresses, checked
on the name's lookup and again on the address the connection really reached, redirects included. No test here looks a
name up or connects anywhere but this machine."""
import os
import pathlib
import socket
import ssl
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import local_network_only, start_server, stop_server  # noqa: E402
from subsync import net, realdebrid  # noqa: E402


def resolver_answering(*ips, asked=None):
    """A name lookup answering these addresses, in this order; the names asked are added to `asked`."""
    def resolve(host, port, *a, **k):
        if asked is not None:
            asked.append(host)
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 80))
                for ip in ips]
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

        self.server = start_server(ThreadingHTTPServer(("127.0.0.1", 0), H))
        self.port = self.server.server_address[1]

    def close(self):
        stop_server(self.server)


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

    def test_an_ipv6_address_carrying_an_ipv4_one_is_judged_by_it(self):
        # A NAT64 gateway (64:ff9b::/96), a SIIT translator (::ffff:0:0:0/96) and the old IPv4-compatible form
        # (::/96) reach the IPv4 address in the last 32 bits; Python counts these prefixes as global whatever it is
        for ip in ("64:ff9b::a9fe:a9fe", "64:ff9b::7f00:1", "64:ff9b::a00:1", "64:ff9b::e000:1", "::ffff:0:a9fe:a9fe",
                   "::ffff:0:a00:1", "::a9fe:a9fe", "::a00:1"):
            with self.subTest(ip=ip):
                self.assertFalse(net.is_public_ip(ip))
        for ip in ("64:ff9b::808:808", "::ffff:0:808:808", "::808:808"):
            with self.subTest(ip=ip):
                self.assertTrue(net.is_public_ip(ip))

    def test_ipv6_site_local_and_reserved_space_is_refused(self):
        # Python counts IPv6's old site-local block (fec0::/10) and the unassigned blocks it marks reserved
        # (4000::/3, 5f00::/16 of SRv6 among them) as global; the gate refuses them, as its docstring says
        for ip in ("fec0::1", "feff::1", "5f00::1", "4000::1", "e000::1", "200::1"):
            with self.subTest(ip=ip):
                self.assertFalse(net.is_public_ip(ip))
        for ip in ("2001:4860:4860::8888", "2a00:1450:4001::1"):     # in 2000::/3, the global unicast space: public
            with self.subTest(ip=ip):
                self.assertTrue(net.is_public_ip(ip))


class OriginTest(unittest.TestCase):
    def test_where_credentials_may_follow_a_redirect(self):
        # A redirect keeps a key or password only on the same origin, or from http to https on the same host
        self.assertEqual(net.origin("https://API.example/x?y"), ("https", "api.example", 443))
        self.assertIsNone(net.origin("https://api.example:99999/x"))
        self.assertIsNone(net.origin("/relative"))
        follow = (("https://a.example/x", "https://a.example:443/y"), ("http://a.example/x", "https://a.example/y"))
        drop = (("https://a.example/x", "https://b.example/x"), ("https://a.example/x", "http://a.example/x"),
                ("https://a.example/x", "https://a.example:8443/x"), ("http://a.example:8080/x", "https://a.example/x"),
                ("https://a.example/x", "https://a.example.evil.example/x"))
        for old, new in follow:
            with self.subTest(old=old, new=new):
                self.assertTrue(net.credentials_follow(old, new))
        for old, new in drop:
            with self.subTest(old=old, new=new):
                self.assertFalse(net.credentials_follow(old, new))


class GateTest(unittest.TestCase):
    def tearDown(self):
        net.configure(public_only=False)

    def test_a_name_resolving_to_a_private_address_is_refused_before_connecting(self):
        net.configure(public_only=True, resolver=resolver_answering("10.0.0.5"))
        with local_network_only():
            refused(self, lambda: realdebrid.urlopen("http://evil.test/x", timeout=5))

    def test_a_name_with_any_address_not_public_is_refused_before_connecting(self):
        # Every address the name answers is checked, not only the first (the order a lookup gives varies)
        for ips in (("93.184.216.34", "10.0.0.5"), ("10.0.0.5", "93.184.216.34"),
                    ("2606:4700:4700::1111", "93.184.216.34", "fd00::1")):
            net.configure(public_only=True, resolver=resolver_answering(*ips))
            with self.subTest(ips=ips), local_network_only():
                refused(self, lambda: realdebrid.urlopen("http://mixed.test/x", timeout=5))

    def test_an_address_written_in_brackets_is_judged_as_it_is(self):
        # An IPv6 address in the link is checked without a lookup: loopback, IPv4-mapped private and
        # link-local with its zone are refused, a public one passes
        asked = []
        net.configure(public_only=True, resolver=resolver_answering("93.184.216.34", asked=asked))
        for url in ("http://[::1]/x", "http://[::ffff:10.0.0.1]/x", "http://[fe80::1%25eth0]/x"):
            with self.subTest(url=url), local_network_only():
                refused(self, lambda: realdebrid.urlopen(url, timeout=5))
        net.check_url("http://[2606:4700:4700::1111]/x")
        self.assertEqual(asked, [])

    def test_the_metadata_service_is_refused(self):
        net.configure(public_only=True, resolver=resolver_answering("169.254.169.254"))
        with local_network_only():
            refused(self, lambda: realdebrid.urlopen("http://169.254.169.254/opc/v2/instance/", timeout=5))

    def test_the_metadata_service_through_nat64_is_refused(self):
        net.configure(public_only=True, resolver=resolver_answering("93.184.216.34"))
        with local_network_only():
            refused(self, lambda: realdebrid.urlopen("http://[64:ff9b::a9fe:a9fe]/latest/meta-data/", timeout=5))

    def test_a_name_is_looked_up_once_and_its_checked_address_connected_to(self):
        # The connection goes to the address the check passed, not to the name looked up a second time
        server = Server()
        try:
            lookups = []

            def resolve(host, port, *a, **k):
                lookups.append(host)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            net.configure(public_only=True, resolver=resolve)
            real = net.is_public_ip
            with unittest.mock.patch.object(net, "is_public_ip", lambda ip: ip == "127.0.0.1" or real(ip)), \
                    local_network_only():
                with realdebrid.urlopen(f"http://files.test:{server.port}/x", timeout=5) as r:
                    self.assertEqual(r.read(), b"ok")
            self.assertEqual((lookups, server.requests), (["files.test"], ["/x"]))
        finally:
            server.close()

    def test_an_address_that_refuses_gives_way_to_the_next_checked_one(self):
        # The fall-back to the lookup's next checked address, and the last error when none answers, had no
        # test
        server = Server()
        try:
            net.configure(public_only=True, resolver=resolver_answering("93.184.216.34", "93.184.216.35"))
            real, tried = socket.create_connection, []

            def second_answers(address, *a, **k):
                tried.append(address[0])
                if address[0] == "93.184.216.34":
                    raise ConnectionRefusedError("refused")
                return real(("127.0.0.1", server.port), *a, **k)
            real_public = net.is_public_ip
            with unittest.mock.patch("socket.create_connection", second_answers), \
                    unittest.mock.patch.object(net, "is_public_ip", lambda ip: ip == "127.0.0.1" or real_public(ip)):
                with realdebrid.urlopen(f"http://two.test:{server.port}/x", timeout=5) as r:
                    self.assertEqual(r.read(), b"ok")
            self.assertEqual((tried, server.requests), (["93.184.216.34", "93.184.216.35"], ["/x"]))

            def none_answers(address, *a, **k):
                raise (ConnectionRefusedError if address[0].endswith(".34") else TimeoutError)(address[0])
            with unittest.mock.patch("socket.create_connection", none_answers), self.assertRaises(TimeoutError) as e:
                net._guarded_create_connection(("two.test", 443), 5)
            self.assertEqual(str(e.exception), "93.184.216.35")          # the last address's error
        finally:
            server.close()

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

    def test_an_alias_lookup_passes_the_gate(self):   # the lookup opens through the same gate
        from subsync.aiosconfig import AiosApi, AiosError
        net.configure(public_only=True, resolver=resolver_answering("10.0.0.5"))
        with local_network_only():
            with self.assertRaises(AiosError) as e:
                AiosApi("https://aios.test", timeout=5).alias_config_id("fam")
        self.assertIn("Can't reach", e.exception.message)

    def test_other_schemes_are_refused(self):
        net.configure(public_only=True, resolver=resolver_answering("93.184.216.34"))
        for url in ("file:///etc/passwd", "ftp://ftp.test/x", "data:text/plain,hello"):
            with self.subTest(url=url):
                refused(self, lambda: realdebrid.urlopen(url, timeout=5))

    def test_other_schemes_are_refused_off_public_mode_too(self):
        # A subtitle listed as file:///proc/self/environ by a third-party addon was read from this machine's
        # disk (its secrets among it) on an install of your own; nothing here opens anything but http and https
        net.configure(public_only=False)
        with tempfile.NamedTemporaryFile("w", suffix=".srt") as local:
            local.write("SUBSYNC_SOURCE_PASSWORD=secret\n")
            local.flush()
            for url in (pathlib.Path(local.name).as_uri(), "ftp://127.0.0.1:9/x", "data:text/plain,hello"):
                with self.subTest(url=url):
                    refused(self, lambda: realdebrid.urlopen(url, timeout=5))
                    refused(self, lambda: net.build_opener().open(url, timeout=5))     # any opener of the gate
            refused(self, lambda: realdebrid.GuardedRedirects().redirect_request(          # and a redirect to one
                urllib.request.Request("http://files.test/"), _Closable(), 302, "Found", {}, "ftp://127.0.0.1:9/x"))

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


class DeadlineTest(unittest.TestCase):
    """A request's whole bound, held around it, covers every connection the gate opens on its thread."""

    def setUp(self):
        net.configure(public_only=False)

    def test_each_connect_waits_the_time_left_at_most(self):
        waits = []

        def refuses(address, timeout, *a, **k):
            waits.append(timeout)
            raise ConnectionRefusedError("refused")
        with unittest.mock.patch("socket.create_connection", refuses):
            with net.Deadline(5), self.assertRaises(ConnectionRefusedError):
                net._guarded_create_connection(("127.0.0.1", 9), 30)
            with net.Deadline(5), self.assertRaises(ConnectionRefusedError):
                net._guarded_create_connection(("127.0.0.1", 9), 2)
            with self.assertRaises(ConnectionRefusedError):         # no bound held: the wait as given
                net._guarded_create_connection(("127.0.0.1", 9), 30)
            with net.Deadline(0), self.assertRaises(TimeoutError):  # none left: no connect at all
                net._guarded_create_connection(("127.0.0.1", 9), 30)
        self.assertEqual(len(waits), 3)
        self.assertTrue(4 < waits[0] <= 5, waits)
        self.assertEqual(waits[1:], [2, 30])

    def test_its_connections_are_shut_down_at_its_end(self):
        with socket.create_server(("127.0.0.1", 0)) as listener:
            port = listener.getsockname()[1]
            t0 = time.monotonic()
            with net.Deadline(0.3) as bound:
                with net._guarded_create_connection(("127.0.0.1", port), 5) as sock:
                    sock.settimeout(5)                               # a read's own wait, past the bound
                    self.assertEqual(sock.recv(1), b"")              # nothing ever comes: ended at the bound
                self.assertTrue(bound.passed)
            self.assertLess(time.monotonic() - t0, 2)
            with net.Deadline(5) as bound:
                with net._guarded_create_connection(("127.0.0.1", port), 5) as sock:
                    sock.settimeout(0.2)
                    with self.assertRaises(TimeoutError):            # still open within its bound
                        sock.recv(1)
                self.assertFalse(bound.passed)


class ConnectSpanTest(unittest.TestCase):
    """In public mode a name's addresses share twice the connect's wait; each had the whole wait, so a name
    answering dozens of addresses that never answer held a request for dozens of waits."""

    def tearDown(self):
        net.configure(public_only=False)

    def test_many_silent_addresses_share_twice_the_wait(self):
        ips = [f"93.184.216.{n}" for n in range(1, 13)]
        net.configure(public_only=True, resolver=resolver_answering(*ips))
        waits = []

        def silent(address, timeout, *a, **k):     # an address that never answers: the whole wait, then timed out
            waits.append(timeout)
            time.sleep(timeout)
            raise TimeoutError("timed out")
        t0 = time.monotonic()
        with unittest.mock.patch("socket.create_connection", silent), self.assertRaises(TimeoutError):
            net._guarded_create_connection(("many.test", 443), 0.05)
        self.assertEqual(waits[0], 0.05)                               # the first address: the whole wait, as before
        self.assertLessEqual(sum(waits), 2 * 0.05 + 1e-6, waits)
        self.assertLess(len(waits), len(ips))
        self.assertLess(time.monotonic() - t0, 0.5)

    def test_the_families_take_turns(self):
        # RFC 8305's order: the lookup's IPv6 addresses that never answer use up the time no more than their turn, so
        # the IPv4 one is tried second; each family keeps the lookup's order, and the last error is raised
        net.configure(public_only=True, resolver=resolver_answering("2606:4700::1", "2606:4700::2", "93.184.216.34",
                                                                    "93.184.216.35"))
        tried = []

        def refuses(address, *a, **k):
            tried.append(address[0])
            raise ConnectionRefusedError(address[0])
        with unittest.mock.patch("socket.create_connection", refuses), self.assertRaises(ConnectionRefusedError) as e:
            net._guarded_create_connection(("both.test", 443), 5)
        self.assertEqual(tried, ["2606:4700::1", "93.184.216.34", "2606:4700::2", "93.184.216.35"])
        self.assertEqual(str(e.exception), "93.184.216.35")


class TlsContextTest(unittest.TestCase):
    def test_openers_share_one_tls_context(self):
        # Each opener made its own TLS context, loading the system's CA store again (about 8 ms of CPU, two
        # openers per AIOStreams client); the shared one checks certificates and names as urllib's own does
        with unittest.mock.patch.object(ssl.SSLContext, "load_default_certs") as load:
            net.build_opener()
            net.build_opener(net.NoRedirects())
        load.assert_not_called()
        self.assertEqual((net._TLS.verify_mode, net._TLS.check_hostname), (ssl.CERT_REQUIRED, True))


class RetryAfterTest(unittest.TestCase):
    def test_a_number_of_seconds_or_none(self):
        # One reading of Retry-After for the clients that pause or wait on it; "nan" is none, as every
        # comparison with it is false and no bound holds it
        for value, seconds in (("120", 120.0), (" 7 ", 7.0), ("1.5", 1.5), ("0", 0.0), ("inf", float("inf"))):
            self.assertEqual(net.retry_after(value), seconds)
        for value in (None, "", "nan", "NaN", "Wed, 21 Oct 2026 07:28:00 GMT"):
            self.assertIsNone(net.retry_after(value))

    def test_digits_of_other_scripts_are_no_number(self):
        # A header's number is read in the ASCII digits only (digits.py), as AiosApi reads this header
        for value in ("١٢٠", "１２", "٣٠٠٠", "１.５"):
            with self.subTest(value=value):
                self.assertIsNone(net.retry_after(value))


if __name__ == "__main__":
    unittest.main()
