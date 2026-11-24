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
import json
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

# an addon link's query and value separators, and the dots between the parts of a token's settings (header.payload.
# signature: each part is base64 of its own)
_PARTS = re.compile(r"[?&|,;=.]")
_BASE64_SEGMENT = re.compile(r"[A-Za-z0-9+_-]+")   # base64 text between two "/" (standard base64 writes "/" too)
# A run of base64 segments is decoded from each segment to each later one while it is at most this long (an
# addon's settings and the plain path parts around them are far shorter), else whole only, so no link costs more than
# about two thousand decodes of a few KB
_SUB_RUN_SEGMENTS = 64
_SUB_RUN_CHARS = 8 * 1024
_WORDS = re.compile(r"[a-z0-9]+")
# the words of a setting's name, split at capitals too ("rdToken": rd, token; "realDebridKey": real, debrid, key)
_NAME_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_RD_CODE = "rd"   # StremThru's store code, e.g. {"c":"rd"}
_MIN_ENCODED = 16


class Blocked(OSError):
    """A request that would reach Real-Debrid. An OSError, so every caller's network-error path stops the job."""


# ------------------------------------------------------------------------------------------- 3. every request

def _lookup_name(host):
    """`host` as the resolver looks it up: lower case, IDNA-encoded (which drops a soft hyphen and reads a full-width
    letter as its plain one), with no trailing dot; as written when it can't be encoded (its lookup fails then)."""
    host = (host or "").lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return host.rstrip(".")


def is_real_debrid_host(host):
    host = _lookup_name(host)
    return bool(host) and any(host == d or host.endswith("." + d) for d in DOMAINS)


def check(url):
    """Raises Blocked (and logs the host, never the link) when `url` is on a Real-Debrid domain, however its host is
    spelled: the host urllib connects to has its percent escapes undone (Request does that), then is looked up as
    _lookup_name gives it."""
    host = _lookup_name(urllib.parse.unquote(urllib.parse.urlsplit(url).hostname or ""))
    if is_real_debrid_host(host):
        log.warning("blocked a request to Real-Debrid (%s)", host)
        raise Blocked(f"a request to Real-Debrid ({host})")


class GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects like urllib does, except to a Real-Debrid address, without the request's credentials (a
    source's key, a password) when the redirect leaves its origin (urllib would send every header on to wherever it
    points), and without reading the redirect's own body."""
    CREDENTIALS = ("authorization", "x-api-key")

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            check(newurl)
            net.check_url(newurl)
        except OSError:
            fp.close()
            raise
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            if not net.credentials_follow(req.full_url, new.full_url):
                for name in [k for k in new.headers if k.lower() in self.CREDENTIALS]:
                    del new.headers[name]
            # urllib reads a redirect's whole body before it follows it, with no bound on its size or its time
            #: closed here, the body is never read, and its connection goes with it
            fp.close()
        return new


_OPENER = net.build_opener()


def urlopen(request, timeout, opener=None):
    """urllib.request.urlopen with the checks on the address and on every redirect (and, in public mode, net.py's
    public-address check on every connection). `request`: a urllib Request, or an address, made into one first so the
    checks read the address urllib connects to (a Request unwraps "<URL:…>", which urlsplit reads as a path).
    `opener`: another of net.build_opener's (with a cookie jar, or net.NoRedirects for a request that carries a key or
    a password)."""
    if not isinstance(request, urllib.request.Request):
        request = urllib.request.Request(request)
    check(request.full_url)
    net.check_scheme(request.full_url)
    return (opener or _OPENER).open(request, timeout=timeout)


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

def _encoded_pieces(link):
    """The parts of an addon link that may be base64 text: each piece between its separators and its "/", and each run
    of base64 text with the "/" in it kept (standard base64 writes "/" too, and decoded in pieces, settings
    that hold accented or non-Latin text are read wrong from the first "/" on), from each of its segments to each later
    one (a plain path part before or after the settings, such as "c", "stremio/store" or "configure", would
    else be decoded with them, and nothing of them read)."""
    for part in _PARTS.split(link):
        segments = part.split("/")
        yield from segments
        run = []
        for segment in segments + [""]:     # the empty one ends the last run
            if _BASE64_SEGMENT.fullmatch(segment):
                run.append(segment)
                continue
            if len(run) > 1:
                yield from _sub_runs(run)
            run = []


def _sub_runs(run):
    """A run of two or more base64 segments joined with their "/": from each segment to each later one, or whole only
    when it is longer than an addon's settings with plain parts around them (_SUB_RUN_SEGMENTS, _SUB_RUN_CHARS)."""
    if len(run) > _SUB_RUN_SEGMENTS or sum(map(len, run)) > _SUB_RUN_CHARS:
        return ["/".join(run)]
    return ["/".join(run[start:end]) for start in range(len(run) - 1) for end in range(start + 2, len(run) + 1)]


def _decoded_pieces(link):
    """The parts of an addon link that are base64 text (many addons keep their settings, keys included, that way)."""
    for piece in dict.fromkeys(_encoded_pieces(link)):
        if len(piece) < _MIN_ENCODED:
            continue
        raw = piece.replace("+", "-").replace("/", "_")
        try:
            yield base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
        except (binascii.Error, ValueError):
            continue


def _setting_names(text):
    """The names of the settings in decoded text that is JSON (each object's keys, at any depth); none otherwise."""
    try:
        todo = [json.loads(text)]
    except (ValueError, RecursionError):
        return []
    names = []
    while todo:
        value = todo.pop()
        if isinstance(value, dict):
            names += value.keys()
            todo += value.values()
        elif isinstance(value, list):
            todo += value
    return names


def services_named(link):
    """The debrid services an addon link names ("torbox", "real-debrid", "put.io"), as written or inside its base64
    settings: each as whole words, and Real-Debrid also in any word that holds "realdebrid" and in a setting's name
    split at capitals ("rdToken", "realDebridKey"). Encrypted text isn't split, so it can't name Real-Debrid by
    chance, and an encrypted link names none."""
    plain = urllib.parse.unquote(link or "")
    settings = list(_decoded_pieces(plain))
    words = [w for text in [plain] + settings for w in _WORDS.findall(text.lower())]
    name_words = [w.lower() for text in settings for name in _setting_names(text) for w in _NAME_WORDS.findall(name)]
    names = set(words) | {a + b for a, b in zip(words, words[1:])}   # two-word spellings: real-debrid, put.io
    named = names & DEBRID_SERVICES.keys()
    if _RD_CODE in words or _RD_CODE in name_words or any(SERVICE in w for w in words):
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
