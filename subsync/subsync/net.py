"""The address check of the one request gate (realdebrid.urlopen and the AIOStreams API client both open through it).

Off (an install of your own): requests go where they are sent, as before; your AIOStreams usually sits on a private
network next to subsync.

Public mode: what people type and what their listings contain decides where the service connects, so it connects only
to public internet addresses. A name is looked up once and refused when any of its addresses is private, loopback,
link-local (the cloud's own metadata service among them), shared, multicast or reserved; the connection goes to the
addresses checked, and the address it really reached is checked again. An IPv6 address that stands for an IPv4 one
(IPv4-mapped, or a translator's prefix such as NAT64's) is judged as that IPv4 address. Redirects are opened through the
same connections. Only http and https are opened, and proxies from the environment are not used.

Standard library only, so ./setup can import it.
"""
import http.client
import ipaddress
import math
import socket
import urllib.error
import urllib.parse
import urllib.request

_public_only = False
_resolver = socket.getaddrinfo
# IPv6 prefixes whose last 32 bits are the IPv4 address a connection reaches: IPv4-mapped, IPv4-translated (SIIT), the
# NAT64 well-known prefix (RFC 6052) and the old IPv4-compatible form; Python counts the last three as global whatever
# IPv4 address they carry
_CARRY_IPV4 = tuple(ipaddress.ip_network(p) for p in ("::ffff:0:0/96", "::ffff:0:0:0/96", "64:ff9b::/96", "::/96"))


class Refused(OSError):
    """A request the gate refuses. Its text names the host, never the link."""


def configure(public_only, resolver=None):
    """Turns the public-address check on or off for every request from now on (tests pass a resolver of their own)."""
    global _public_only, _resolver
    _public_only = bool(public_only)
    _resolver = resolver or socket.getaddrinfo


def public_only():
    return _public_only


def is_public_ip(ip):
    """Whether an address is on the public internet (an IPv6 address carrying an IPv4 one: whether that one is)."""
    try:
        address = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return False
    if address.version == 6 and any(address in prefix for prefix in _CARRY_IPV4):
        address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address.is_global and not address.is_multicast


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
    """In public mode, refuses anything but http and https."""
    if not _public_only:
        return
    scheme = urllib.parse.urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise Refused(f"a {scheme or 'schemeless'} address")


def check_url(url):
    """In public mode, refuses anything but http and https to public hosts, before any connection (a redirect target is
    checked this way first; the connection checks it again)."""
    if not _public_only:
        return
    check_scheme(url)
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
    """The seconds a "too many requests" answer's Retry-After asks to wait, as a number; None without one or for one
    that isn't a number (an HTTP-date, "nan"): each caller then takes its own default, and applies its own bounds."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(seconds) else seconds


def _guarded_create_connection(address, *args, **kwargs):
    """socket.create_connection through the check: in public mode the name is looked up once, and the connection goes
    to the addresses checked, in the lookup's order, as create_connection's own would (the last error when none
    answers); the address it really reached is checked again."""
    if not _public_only:
        return socket.create_connection(address, *args, **kwargs)
    host, port = address[0], address[1]
    error = OSError("getaddrinfo returns an empty list")
    for ip in _public_addresses(host, port):
        try:
            sock = socket.create_connection((ip, port), *args, **kwargs)
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
    """Ahead of urllib's own handlers: in public mode, file, ftp and data addresses are refused."""
    handler_order = 100

    def _refuse(self, req):
        if _public_only:
            raise Refused(f"a {req.type} address")
        return None   # off: urllib's own handler opens it, as before

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
    return urllib.request.build_opener(_NoEnvironmentProxies(), _HTTPHandler(), _HTTPSHandler(), _OtherSchemes(),
                                       GuardedRedirects(), *handlers)
