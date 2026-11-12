"""The address check of the one request gate (realdebrid.urlopen and the AIOStreams API client both open through it).

Off (an install of your own): requests go where they are sent, as before; your AIOStreams usually sits on a private
network next to subsync.

Public mode: what people type and what their listings contain decides where the service connects, so it connects only
to public internet addresses. A name is looked up first and refused when any of its addresses is private, loopback,
link-local (the cloud's own metadata service among them), shared, multicast or reserved; after connecting, the address
the connection really reached is checked again, so a name that changes its answer between the two is refused too.
Redirects are opened through the same connections. Only http and https are opened, and proxies from the environment are
not used.

Standard library only, so ./setup can import it.
"""
import http.client
import ipaddress
import socket
import urllib.parse
import urllib.request

_public_only = False
_resolver = socket.getaddrinfo


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
    """Whether an address is on the public internet."""
    try:
        address = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_global and not address.is_multicast


def check_host(host, port=None):
    """In public mode, refuses a host any of whose addresses is not public. Lookup errors pass through as OSError."""
    if not _public_only:
        return
    if not host:
        raise Refused("a request without a host")
    try:
        ipaddress.ip_address(str(host).split("%", 1)[0])
        literal = True
    except ValueError:
        literal = False
    if literal:   # an address written as such is checked as it is, without a lookup
        if not is_public_ip(host):
            raise Refused(f"{host} is not a public internet address")
        return
    for *_, sockaddr in _resolver(host, port, type=socket.SOCK_STREAM):
        if not is_public_ip(sockaddr[0]):
            raise Refused(f"{host} is not a public internet address")


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


def _guarded_create_connection(address, *args, **kwargs):
    host, port = address[0], address[1]
    check_host(host, port)
    sock = socket.create_connection(address, *args, **kwargs)
    if _public_only:
        try:
            peer = sock.getpeername()[0]
        except OSError:
            sock.close()
            raise
        if not is_public_ip(peer):
            sock.close()
            raise Refused(f"{host} reached an address that is not public")
    return sock


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
