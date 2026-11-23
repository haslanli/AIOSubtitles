"""The address check of the one request gate (realdebrid.urlopen and the AIOStreams API client both open through it).

Off (an install of your own): requests go where they are sent, as before; your AIOStreams usually sits on a private
network next to subsync.

Public mode: what people type and what their listings contain decides where the service connects, so it connects only
to public internet addresses. A name is looked up once and refused when any of its addresses is private, loopback,
link-local (the cloud's own metadata service among them), site-local, shared, multicast or reserved; the connection
goes to the addresses checked (the two families taking turns, within twice the connect's wait in all), and the address
it really reached is checked again. An IPv6 address that stands for an IPv4 one (IPv4-mapped, or a translator's prefix
such as NAT64's) is judged as that IPv4 address. Redirects are opened through the same connections, and proxies from
the environment are not used.

Both modes: only http and https are opened (urllib would read a file: address from this machine's own disk), and a
request held within a Deadline has one bound for the whole of it, its connections and every read.

Standard library only, so ./setup can import it.
"""
import contextlib
import http.client
import ipaddress
import itertools
import math
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import digits

_public_only = False
_resolver = socket.getaddrinfo
_local = threading.local()   # .deadline: this thread's request bound (Deadline), while one is held
# IPv6 prefixes whose last 32 bits are the IPv4 address a connection reaches: IPv4-mapped, IPv4-translated (SIIT), the
# NAT64 well-known prefix (RFC 6052) and the old IPv4-compatible form; Python counts the last three as global whatever
# IPv4 address they carry
_CARRY_IPV4 = tuple(ipaddress.ip_network(p) for p in ("::ffff:0:0/96", "::ffff:0:0:0/96", "64:ff9b::/96", "::/96"))
# public mode: a name's addresses share this many connect waits in all. The first address gets the whole wait
# and the next (the other family's first) what is left, so a host whose IPv6 address never answers is still reached
# over IPv4, and a name answering dozens of addresses that never answer holds a request for two waits, not dozens.
CONNECT_WAITS = 2
# one TLS context for every opener, made as urllib's HTTPSHandler makes its own (certificates and names checked, ALPN
# http/1.1): each new one loads the system's CA store again
_TLS = http.client._create_https_context(http.client.HTTPSConnection._http_vsn)


class Refused(OSError):
    """A request the gate refuses. Its text names the host, never the link."""


def configure(public_only, resolver=None):
    """Turns the public-address check on or off for every request from now on (tests pass a resolver of their own)."""
    global _public_only, _resolver
    _public_only = bool(public_only)
    _resolver = resolver or socket.getaddrinfo


def is_public_ip(ip):
    """Whether an address is on the public internet (an IPv6 address carrying an IPv4 one: whether that one is)."""
    try:
        address = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return False
    if address.version == 6 and any(address in prefix for prefix in _CARRY_IPV4):
        address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    # Python counts IPv6's old site-local block and its unassigned reserved blocks as global
    return address.is_global and not (address.is_multicast or address.is_reserved
                                      or (address.version == 6 and address.is_site_local))


def _public_addresses(host, port):
    """The host's addresses, every one public, else Refused: an address written as such as it is, a name looked up
    once. Lookup errors pass through as OSError."""
    if not host:
        raise Refused("a request without a host")
    try:
        ipaddress.ip_address(str(host).split("%", 1)[0])
        found = [host]   # an address written as such is checked as it is, without a lookup
    except ValueError:
        found = [sockaddr[0] for *_, sockaddr in _resolver(host, port, type=socket.SOCK_STREAM)]
    for ip in found:
        if not is_public_ip(ip):
            raise Refused(f"{host} is not a public internet address")
    return found


def check_host(host, port=None):
    """In public mode, refuses a host any of whose addresses is not public. Lookup errors pass through as OSError."""
    if _public_only:
        _public_addresses(host, port)


def check_scheme(url):
    """Refuses anything but http and https, in both modes: nothing here opens another kind, and urllib would
    read a file: address, a subtitle a third party lists among them, from this machine's own disk."""
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise Refused(f"a {scheme or 'schemeless'} address")


def check_url(url):
    """Refuses anything but http and https and, in public mode, a host that isn't public, before any connection (a
    redirect target is checked this way first; the connection checks it again)."""
    check_scheme(url)
    if _public_only:
        parts = urllib.parse.urlsplit(url)
        check_host(parts.hostname, parts.port)


_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin(url):
    """An address's origin: (scheme, host, port), the scheme's port when none is written; None without a host or with a
    port that isn't one."""
    parts = urllib.parse.urlsplit(str(url or ""))
    scheme = parts.scheme.lower()
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme)
    except ValueError:
        return None
    return (scheme, parts.hostname, port) if parts.hostname else None


def same_origin(url, other):
    """Whether two addresses have the same origin: where a request's credentials (a key, a password) may go."""
    mine = origin(url)
    return mine is not None and mine == origin(other)


def credentials_follow(url, new_url):
    """Whether a redirect from `url` to `new_url` may carry the request's credentials: to the same origin, or from http
    to https on the same host and the schemes' own ports (as browsers do)."""
    if same_origin(url, new_url):
        return True
    old, new = origin(url), origin(new_url)
    return old is not None and new is not None and old == ("http", new[1], 80) and new == ("https", old[1], 443)


def retry_after(value):
    """The seconds a "too many requests" answer's Retry-After asks to wait, as a number in the ASCII digits (digits.py);
    None without one or for one that isn't a number (an HTTP-date, "nan"): each caller then takes its own default, and
    applies its own bounds."""
    seconds = digits.decimal(value)
    return None if seconds is None or math.isnan(seconds) else seconds


class Deadline:
    """A request's whole bound, held around it: every connection the gate opens on this thread meanwhile (a
    redirect's too) connects within the time left and is shut down once `seconds` have passed since the start, so a
    server that sends its status line, its headers or its body a byte now and then, each within a read's own wait,
    holds the request no longer. The read in progress then ends, with an error or cut short; `passed` says the bound
    ended it. A duplicate of each connection's socket is held until the end: while it is, the connection's number
    can't go to another one."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.passed = False
        self._lock = threading.Lock()
        self._held = []
        self._end_at = self._timer = self._outer = None

    def __enter__(self):
        self.start()
        self._outer, _local.deadline = getattr(_local, "deadline", None), self
        return self

    def __exit__(self, *exc):
        _local.deadline = self._outer
        self.end()

    def start(self):
        """Starts the bound's time; `with` also holds it, for the whole block. Started alone, it is held only while its
        request opens (opening()) and ended by end(): for an answer read across a generator's yields, where the
        thread's work between them is not the request's (reader.RangeReader.stream)."""
        self._end_at = time.monotonic() + self.seconds
        self._timer = threading.Timer(self.seconds, self._end)
        self._timer.daemon = True
        self._timer.start()
        return self

    @contextlib.contextmanager
    def opening(self):
        """Holds a started bound for the block: the connections this thread opens in it are held to it."""
        outer, _local.deadline = getattr(_local, "deadline", None), self
        try:
            yield self
        finally:
            _local.deadline = outer

    def end(self):
        """Ends the bound: its timer stops, and the connections' sockets it held are let go."""
        self._timer.cancel()
        with self._lock:
            for sock in self._held:
                sock.close()
            self._held = []

    def wait(self, timeout):
        """A connect's wait: `timeout` (seconds, or None for none), never past the end; TimeoutError once it has
        come."""
        left = self._end_at - time.monotonic()
        if self.passed or left <= 0:
            raise TimeoutError("timed out")
        return left if timeout is None else min(timeout, left)

    def hold(self, sock):
        """A new connection's socket, shut down at the end (at once if it has come)."""
        held = sock.dup()
        with self._lock:
            self._held.append(held)
            if self.passed:
                _shut(held)

    def _end(self):
        with self._lock:
            self.passed = True
            for sock in self._held:
                _shut(sock)


def _shut(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass   # its other end has closed it already


def _connect(address, timeout, *args, **kwargs):
    """socket.create_connection, within this thread's request bound (Deadline) when one is held."""
    bound = getattr(_local, "deadline", None)
    if bound is None:
        return socket.create_connection(address, timeout, *args, **kwargs)
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        timeout = socket.getdefaulttimeout()
    sock = socket.create_connection(address, bound.wait(timeout), *args, **kwargs)
    try:
        bound.hold(sock)
    except OSError:
        sock.close()
        raise
    return sock


def _families_in_turn(ips):
    """The addresses in RFC 8305's order: the two families take turns, the first address's family first, and each
    family keeps the lookup's order."""
    if not ips:
        return ips
    first = [ip for ip in ips if (":" in ip) == (":" in ips[0])]
    other = [ip for ip in ips if (":" in ip) != (":" in ips[0])]
    return [ip for pair in itertools.zip_longest(first, other) for ip in pair if ip is not None]


def _guarded_create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *args, **kwargs):
    """socket.create_connection through the check: in public mode the name is looked up once, and the connection goes
    to the addresses checked, the two families taking turns, within CONNECT_WAITS connect waits in all (the last error
    when none answers); the address it really reached is checked again. Within a request's bound (Deadline) when one
    is held."""
    if not _public_only:
        return _connect(address, timeout, *args, **kwargs)
    host, port = address[0], address[1]
    if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:
        timeout = socket.getdefaulttimeout()
    end = None if timeout is None else time.monotonic() + CONNECT_WAITS * timeout
    error = OSError("getaddrinfo returns an empty list")
    for n, ip in enumerate(_families_in_turn(_public_addresses(host, port))):
        wait = timeout
        if n and end is not None:
            wait = min(timeout, end - time.monotonic())
            if wait <= 0:
                break   # the connect's time is up: the last address's error
        try:
            sock = _connect((ip, port), wait, *args, **kwargs)
        except OSError as e:
            error = e
            continue
        try:
            peer = sock.getpeername()[0]
        except OSError:
            sock.close()
            raise
        if not is_public_ip(peer):
            sock.close()
            raise Refused(f"{host} reached an address that is not public")
        return sock
    raise error


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _guarded_create_connection


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_HTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_HTTPSConnection, req, context=self._context)


class _OtherSchemes(urllib.request.BaseHandler):
    """Ahead of urllib's own handlers: file, ftp and data addresses are refused, in both modes (check_scheme)."""
    handler_order = 100

    def _refuse(self, req):
        raise Refused(f"a {req.type} address")

    file_open = ftp_open = data_open = _refuse


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """For a request that carries a key or a password: ahead of the gate's own redirect handler, a redirect comes back
    as an HTTPError and is never followed (urllib would send every header on to wherever it points)."""
    handler_order = 400

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


class _NoEnvironmentProxies(urllib.request.ProxyHandler):
    """Proxies from the environment only when public mode is off."""

    def proxy_open(self, req, proxy, type):
        if _public_only:
            return None
        return super().proxy_open(req, proxy, type)


def build_opener(*handlers):
    """An opener whose every connection passes the check above, with the Real-Debrid redirect guard."""
    from .realdebrid import GuardedRedirects   # realdebrid imports this module
    return urllib.request.build_opener(_NoEnvironmentProxies(), _HTTPHandler(), _HTTPSHandler(context=_TLS),
                                       _OtherSchemes(), GuardedRedirects(), *handlers)
