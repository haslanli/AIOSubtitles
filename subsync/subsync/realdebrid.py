"""Real-Debrid is never used: no listing that makes AIOStreams search or check Real-Debrid, no read of a Real-Debrid file,
no request to a Real-Debrid server, whatever else the user's AIOStreams setup holds.

Why: REASON below (also the reason ./setup gives, and part of the rule's explanation in explain.py).

Three checks, each enough on its own to stop a call:
  1. the config subsync lists through: ./setup builds it without Real-Debrid (`without_real_debrid`), and subsync checks
     it again before every request through it (sources.py), since AIOStreams searches and checks the cache of every
     service in a config while it answers a listing;
  2. each listed row: a row of Real-Debrid, or of no named service, is never read (`row_problem`);
  3. every web request and each redirect: an address on a Real-Debrid domain is refused and logged (`urlopen`).

Standard library only, so ./setup imports this module too.
"""
import base64
import binascii
import copy
import logging
import re
import urllib.parse
import urllib.request

from . import net

log = logging.getLogger("subsync.realdebrid")

SERVICE = "realdebrid"
DOMAINS = ("real-debrid.com",)   # its site, API and download servers
# AIOStreams' debrid service ids (v2.37) and their names
DEBRID_SERVICES = {"realdebrid": "Real-Debrid", "alldebrid": "AllDebrid", "premiumize": "Premiumize",
                   "debridlink": "Debrid-Link", "torbox": "TorBox", "offcloud": "Offcloud", "putio": "put.io",
                   "easydebrid": "EasyDebrid", "debrider": "Debrider", "pikpak": "PikPak", "seedr": "Seedr",
                   "torrin": "Torrin"}
REASON = ("it reads parts of each file you watch from the machine it runs on, and Real-Debrid's terms forbid using an "
          "account from a server, VPS or cloud service unless you pay extra for \"Remote Traffic\"; Real-Debrid records "
          "every connection and suspends accounts that break its terms")
REAL_DEBRID_ONLY = (f"AIOSubtitles can't work with Real-Debrid only, because {REASON}. "
                    "Add another supported service, such as TorBox, to use it.")

_PIECES = re.compile(r"[/?&|,;=]")
_WORDS = re.compile(r"[a-z0-9]+")
_RD_CODE = "rd"   # StremThru's store code, e.g. {"c":"rd"}
_MIN_ENCODED = 16


class Blocked(OSError):
    """A request that would reach Real-Debrid. An OSError, so every caller's network-error path stops the job."""


# ------------------------------------------------------------------------------------------- 3. every request

def is_real_debrid_host(host):
    host = (host or "").lower().rstrip(".")
    return bool(host) and any(host == d or host.endswith("." + d) for d in DOMAINS)


def check(url):
    """Raises Blocked (and logs the host, never the link) when `url` is on a Real-Debrid domain."""
    host = urllib.parse.urlsplit(url).hostname
    if is_real_debrid_host(host):
        log.warning("blocked a request to Real-Debrid (%s)", host)
        raise Blocked(f"a request to Real-Debrid ({host})")


class GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects like urllib does, except to a Real-Debrid address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check(newurl)
            net.check_url(newurl)
        except OSError:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = net.build_opener()


def urlopen(request, timeout):
    """urllib.request.urlopen with the checks on the address and on every redirect (and, in public mode, net.py's
    public-address check on every connection)."""
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    check(url)
    net.check_scheme(url)
    return _OPENER.open(request, timeout=timeout)


# ------------------------------------------------------------------------------------------- 2. each listed row

def row_service(stream):
    return ((stream.get("streamData") or {}).get("service") or {}).get("id")


def row_problem(stream):
    """Why subsync may not read this debrid row because of its service (None = it may)."""
    service = row_service(stream)
    if service == SERVICE:
        return "a Real-Debrid file"
    if not service:
        return "no named debrid service"
    return None


# ------------------------------------------------------------------------------------------- 1. the config

def _decoded_pieces(link):
    """The parts of an addon link that are base64 text (many addons keep their settings, keys included, that way)."""
    for piece in _PIECES.split(link):
        if len(piece) < _MIN_ENCODED:
            continue
        raw = piece.replace("+", "-").replace("/", "_")
        try:
            yield base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        except (binascii.Error, ValueError):
            continue


def services_named(link):
    """The debrid services an addon link names as whole words ("torbox", "real-debrid", "put.io"), as written or inside
    its base64 settings. An encrypted link names none."""
    plain = urllib.parse.unquote(link or "")
    words = []
    for text in [plain] + list(_decoded_pieces(plain)):
        words += _WORDS.findall(text.lower())
    names = set(words) | {a + b for a, b in zip(words, words[1:])}   # two-word spellings: real-debrid, put.io
    named = names & DEBRID_SERVICES.keys()
    if _RD_CODE in words:
        named.add(SERVICE)
    return named


def _link_of(preset):
    """The link of an addon the user added by link (a custom addon, another AIOStreams): its own settings live in it."""
    link = (preset.get("options") or {}).get("manifestUrl")
    return link if isinstance(link, str) and link.strip() else None


def _may_list_streams(preset):
    resources = (preset.get("options") or {}).get("resources")
    return not resources or "stream" in resources


def _name(preset):
    return str((preset.get("options") or {}).get("name") or preset.get("type"))


def debrid_in_use(config):
    """The debrid services a config uses: the ones switched on in its service list, and the ones named in the links of
    its addons added by link."""
    used = {s.get("id") for s in config.get("services") or [] if s.get("enabled", True) and s.get("id") in DEBRID_SERVICES}
    for preset in config.get("presets") or []:
        link = _link_of(preset)
        if link and preset.get("enabled", True) and _may_list_streams(preset):
            used |= services_named(link)
    return used


def real_debrid_only(config):
    """Real-Debrid is used and nothing is left once every way to it is taken out (`without_real_debrid`)."""
    return SERVICE in debrid_in_use(config) and not debrid_in_use(without_real_debrid(config)[0])


def without_real_debrid(config):
    """The config with every way to Real-Debrid taken out, and a plain line for each change (none: it had none).

    Real-Debrid comes off the service list and out of each addon's own service choice; an addon set to Real-Debrid only
    is left out; an addon added by link is left out when its link names Real-Debrid or doesn't show which debrid service
    it uses (its settings may hold a Real-Debrid key). Addons added by link that give only subtitles stay."""
    new = copy.deepcopy(config)
    changes = []
    services = new.get("services") or []
    if any(s.get("id") == SERVICE for s in services):
        new["services"] = [s for s in services if s.get("id") != SERVICE]
        changes.append("Real-Debrid taken off the service list")
    kept = []
    for preset in new.get("presets") or []:
        link = _link_of(preset)
        if link and _may_list_streams(preset):
            named = services_named(link)
            if SERVICE in named:
                changes.append(f'"{_name(preset)}" left out: its link names Real-Debrid')
                continue
            if not named:
                changes.append(f'"{_name(preset)}" left out: its link doesn\'t show which debrid service it uses, '
                               'so it could use Real-Debrid')
                continue
        chosen = (preset.get("options") or {}).get("services")
        if isinstance(chosen, list) and SERVICE in chosen:
            rest = [s for s in chosen if s != SERVICE]
            if not rest:
                changes.append(f'"{_name(preset)}" left out: it is set to use Real-Debrid only')
                continue
            preset["options"]["services"] = rest
            changes.append(f'"{_name(preset)}" no longer uses Real-Debrid')
        kept.append(preset)
    if "presets" in new:
        new["presets"] = kept
    return new, changes
