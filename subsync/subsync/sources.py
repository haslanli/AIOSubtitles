"""AIOStreams through subsync's own config (the viewer's config without Usenet sources and without subsync): stream
listings to find the file a viewer opened, and the subtitle list a viewer would see (the same subtitle sources
and order as the viewer's config, without subsync itself).

That config has no preload or precache and failover off: a read of a listed link then opens that row only.
Background rows: cached debrid only; never Usenet, uncached, library-matched rows or rows of the addons named in
SUBSYNC_SKIP_ADDONS (empty by default).

Real-Debrid is never used (realdebrid.py): before every request through the link, the config behind it is read with
its password (SUBSYNC_SOURCE_PASSWORD) and nothing is asked when it could reach Real-Debrid or can't be read; a listed
Real-Debrid row is never read.
"""
import base64
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import realdebrid, srclimits
from .aiosconfig import USENET_READABLE
from .langs import lang3

log = logging.getLogger("subsync.sources")
MAX_SUBTITLE_BYTES = 5 * 1024 * 1024
# Stock AIOStreams adds `streamData` (file name, size, service, cache flag) to stream listings only for
# clients whose User-Agent starts with "AIO" (unless its PROVIDE_STREAM_DATA setting says otherwise). Without it no
# listed row passes `unreadable`, and no file is ever found.
USER_AGENT = "AIOSubtitles/1.0 (subsync)"


def manifest_base(url):
    url = (url or "").strip()
    if url.endswith("/manifest.json"):
        url = url[: -len("/manifest.json")]
    return url.rstrip("/")


def extras(hints):
    parts = []
    if hints.get("videoHash"):
        parts.append("videoHash=" + urllib.parse.quote(str(hints["videoHash"]), safe=""))
    if hints.get("videoSize"):
        parts.append(f"videoSize={int(hints['videoSize'])}")
    if hints.get("filename"):
        parts.append("filename=" + urllib.parse.quote(str(hints["filename"]), safe=""))
    return "&".join(parts)


def parse_extras(text):
    out = {}
    for part in (text or "").split("&"):
        k, _, v = part.partition("=")
        if k in ("videoHash", "filename"):
            out[k] = urllib.parse.unquote(v)
        elif k == "videoSize" and v.isdigit():
            out[k] = int(v)
    return out


def _get_json(url, timeout):
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": USER_AGENT,
                                               "Connection": "close"})
    with realdebrid.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(16 * 1024 * 1024))


def row_hints(stream):
    d = stream.get("streamData") or {}
    bh = stream.get("behaviorHints") or {}
    return {"filename": d.get("filename") or bh.get("filename"), "videoSize": d.get("size") or bh.get("videoSize"),
            "videoHash": bh.get("videoHash")}


_INFO_HASH = re.compile(r"^[0-9a-fA-F]{40}$")


def bt_alias(row):
    """A listed row's torrent hash and file index as a file alias ("bt:<hash>:<index>"), so a file read through one
    person's listing is known by the same torrent in another's without being read again. None without both."""
    torrent = (row.get("streamData") or {}).get("torrent") or {}
    info_hash, index = torrent.get("infoHash"), torrent.get("fileIdx")
    if not isinstance(info_hash, str) or not _INFO_HASH.match(info_hash):
        return None
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        return None
    return f"bt:{info_hash.lower()}:{index}"


MIN_VIDEO_BYTES = 10_000_000   # a listed size under this is no video file's: a listing error (Torrentio: 1,679 bytes)


def plausible_size(size):
    return isinstance(size, (int, float)) and not isinstance(size, bool) and size >= MIN_VIDEO_BYTES


def same_hash(a, b):
    """Two OpenSubtitles hashes (hex) name the same file: compared by value, so a hash sent without its leading zeros
    or in capitals still matches."""
    try:
        return int(str(a), 16) == int(str(b), 16)
    except ValueError:   # not hex: not a hash
        return False


def expected_size(hints, row):
    """The size the reader checks the played file's first reply against: the request's when a video file can have it,
    else the listing's, else none (the first reply gives it)."""
    for size in (hints.get("videoSize"), row_hints(row).get("videoSize")):
        if plausible_size(size):
            return size
    return None


_VIDEO_EXTENSION = re.compile(r"\.(mkv|mp4|m4v|avi|ts|m2ts|webm|mov|wmv)$", re.I)


def release_name(name):
    """A file or release name without its video extension, for comparing a Usenet release with the file inside it."""
    return _VIDEO_EXTENSION.sub("", str(name or "")).strip()


def usenet_match(rows, name, size):
    """A Usenet row of the same release when the exact rule found none ('s test): AIOStreams lists a release by
    its name at its whole NZB's size until its engine has opened it, then as a library row by the file inside (".mkv")
    at the file's size, a few per cent smaller. So the names are compared without a video extension, and the sizes
    by the reader's Usenet rule (the smaller at least half the larger). A release whose file inside has a random name
    is listed, once opened, by that name, with the release's name as the row's folder: that counts as its name too."""
    wanted = release_name(name)

    def names(s):
        return {release_name(row_hints(s).get("filename")), release_name((s.get("streamData") or {}).get("folderName"))}

    def fits(s):
        listed = row_hints(s).get("videoSize")
        if not (size and plausible_size(listed) and plausible_size(size)):
            return True
        return min(listed, size) >= 0.5 * max(listed, size)
    same = [s for s in rows if wanted in names(s) and fits(s)]
    return same[0] if same else None


def unreadable(stream, skip_addons=(), usenet=False):
    """Why subsync may not read a listed row (None = it may): it reads cached debrid files, and, listed through the
    Usenet config (`usenet`), ready Usenet files of the services that serve part of a file: never
    Stremio's NNTP streams, never a TorBox library item. `skip_addons`: addon names (lower case) whose rows are never
    read."""
    d = stream.get("streamData") or {}
    svc = d.get("service") or {}
    addon = d.get("addon")
    addon_name = addon.get("name") if isinstance(addon, dict) else addon
    if not stream.get("url"):
        return "no url"
    is_usenet = usenet and d.get("type") == "usenet"
    if d.get("type") != "debrid" and not is_usenet:
        return f"type {d.get('type')}"
    service_problem = realdebrid.row_problem(stream)
    if service_problem:
        return service_problem
    if is_usenet and svc.get("id") not in USENET_READABLE:
        return f"usenet service {svc.get('id')}"
    if svc.get("cached") is not True:
        return "not cached"
    # a library row is a file already in the person's account: for TorBox never read (no download list of theirs is
    # touched); for NzbDAV, AltMount and AIOStreams' own Usenet streaming it is the release being played
    if d.get("library") and not (is_usenet and svc.get("id") != "torbox"):
        return "library row"
    if skip_addons and (addon_name or "").strip().lower() in skip_addons:
        return f"addon {addon_name.strip()}"
    return None


CONFIG_LINK = re.compile(r"^(?P<instance>.+?)/stremio/(?P<uuid>[^/]+)/[^/]+$")


class Aios:
    def __init__(self, manifest_url, own_bases=(), timeout=120, skip_addons=(), size_slack=0, password=""):
        self.base = manifest_base(manifest_url)
        self.size_slack = size_slack     # bytes a listed size may be off by rounding (SUBSYNC_SIZE_SLACK_MB)
        self.skip = tuple(a.strip().lower() for a in skip_addons if a and a.strip())
        self.own = tuple(b.rstrip("/") for b in own_bases if b)
        self.timeout = timeout
        self.password = password         # of the config behind the link, to read it for the Real-Debrid check
        self.config_sources = set()      # built-in sources the config has as subtitle addons (srclimits.config_sources)

    CONFIG_RETRY_MAX_S = 5   # AIOStreams' user API answers 429 past a few calls in 5 s: one wait and a second try

    def _read_config(self):
        link = CONFIG_LINK.match(self.base)
        if not link or not self.password:
            raise ValueError("no config link and password")
        auth = base64.b64encode(f"{link['uuid']}:{self.password}".encode()).decode()
        req = urllib.request.Request(link["instance"] + "/api/v1/user", headers={
            "accept": "application/json", "user-agent": USER_AGENT, "authorization": "Basic " + auth, "Connection": "close"})
        try:
            r = realdebrid.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(min(_wait_429(e, 1), self.CONFIG_RETRY_MAX_S))
            e.close()
            r = realdebrid.urlopen(req, timeout=self.timeout)
        with r:
            body = json.loads(r.read(16 * 1024 * 1024))
        if not body.get("success"):
            raise ValueError("refused")
        return body["data"]["userData"]

    def check_config(self):
        """Before every request through the link: the config behind it must hold no way to Real-Debrid (AIOStreams
        searches and checks every service in a config while it answers a listing). Raises realdebrid.Blocked if it
        could reach Real-Debrid or can't be read."""
        try:
            config = self._read_config()
        except urllib.error.HTTPError as e:
            reason = f"HTTP {e.code}"
            e.close()
        except (OSError, ValueError, KeyError, TypeError) as e:
            reason = str(e) if isinstance(e, ValueError) else type(e).__name__
        else:
            _, ways = realdebrid.without_real_debrid(config)
            if not ways:
                # The built-in sources this config already has as subtitle addons are asked once, through it
                self.config_sources = srclimits.config_sources(config)
                return
            log.warning("Real-Debrid check: subsync's config could reach Real-Debrid (%s); nothing is listed or read "
                        "through it until ./setup is run again", "; ".join(ways))
            raise realdebrid.Blocked("subsync's config could reach Real-Debrid")
        log.warning("Real-Debrid check: can't read subsync's config (%s), so nothing is listed through it", reason)
        raise realdebrid.Blocked("subsync's config can't be checked for Real-Debrid")

    def streams(self, type_, video_id):
        if not self.base:
            return []
        self.check_config()
        url = f"{self.base}/stream/{urllib.parse.quote(type_)}/{urllib.parse.quote(video_id)}.json"
        return _get_json(url, self.timeout).get("streams") or []

    def find_row(self, type_, video_id, hints):
        """The listed cached debrid row of the file a viewer opened (checklist 18a), from one listing: `match`."""
        return self.match(self.streams(type_, video_id), hints)

    def match(self, listed, hints, usenet=False):
        """The row of the file a viewer opened among `listed` rows (with `usenet`, ready Usenet rows too): a row whose
        listing gives the file's hash; else a row of the file's name with the request's size, or the closest listed
        size within the rounding slack (Stremio sends the real size, listings round it to 0.01 GiB); else the one row
        of that name whose listed size no video file can have (Torrentio listed 1,679 bytes for a 3 GB file: the first
        reply gives the real size; of several such rows the first).
        Another size of the same name is another file. The file's own hash, read with its index, settles it."""
        name, size = hints.get("filename"), hints.get("videoSize")
        video_hash = str(hints.get("videoHash") or "").lower()
        real_debrid = sum(1 for s in listed if realdebrid.row_service(s) == realdebrid.SERVICE)
        if real_debrid:
            log.warning("Real-Debrid check: %d listed Real-Debrid file%s skipped, never read", real_debrid,
                        "" if real_debrid == 1 else "s")
        rows = [s for s in listed if unreadable(s, self.skip, usenet=usenet) is None]
        if video_hash:
            listed = next((s for s in rows if str(row_hints(s).get("videoHash") or "").lower() == video_hash), None)
            if listed is not None:
                return listed
        found = self._by_name_and_size(rows, name, size)
        if found is None and usenet and name:
            found = usenet_match(rows, name, size)
        return found

    def _by_name_and_size(self, rows, name, size):
        named = [s for s in rows if name and row_hints(s).get("filename") == name]
        if not size:
            return named[0] if named else None

        def off(s):
            return abs(row_hints(s).get("videoSize") - size)
        close = sorted((s for s in named if plausible_size(row_hints(s).get("videoSize")) and off(s) <= self.size_slack),
                       key=off)
        if close:
            return close[0]
        unsized = [s for s in named if not plausible_size(row_hints(s).get("videoSize"))]
        return unsized[0] if unsized else None

    def subtitles(self, type_, video_id, hints):
        """The subtitle list in the order a viewer would see it, without subsync's own entries."""
        if not self.base:
            return []
        self.check_config()
        ex = extras(hints)
        url = f"{self.base}/subtitles/{urllib.parse.quote(type_)}/{urllib.parse.quote(video_id)}{'/' + ex if ex else ''}.json"
        out = []
        for s in _get_json(url, self.timeout).get("subtitles") or []:
            u = s.get("url")
            if not isinstance(u, str) or not u:
                continue
            if any(u.startswith(b) for b in self.own) or str(s.get("id", "")).startswith("subsync-"):
                continue
            out.append({"id": str(s.get("id") or u), "url": u, "lang": lang3(s.get("lang")), **release_info(s)})
        return out


def release_info(sub):
    """What a subtitle entry says about the release it was made for: OpenSubtitles V3+ gives `title`,
    `moviehash` (set when it matched the file's hash) and `ai_translated`; SubMaker gives `label`
    ("<release> • <provider>"); Stremio's official OpenSubtitles v3 gives `movieReleaseName` (else
    `subtitleFileName`) and `m`, how the subtitle was matched ("i" by IMDb id, "h" by the requested file's hash).
    AIOStreams passes these fields through unchanged (its subtitle schema keeps unknown keys)."""
    title = sub.get("title") or sub.get("movieReleaseName")
    if not title and isinstance(sub.get("subtitleFileName"), str):
        title = sub["subtitleFileName"].rsplit(".", 1)[0]
    if not title and isinstance(sub.get("label"), str):
        title = sub["label"].rsplit(" \u2022 ", 1)[0]
    mh = sub.get("moviehash")
    return {"release": str(title or ""),
            "hash": str(mh).lower() if mh not in (None, "", "None", "none") else "",
            "hash_match": str(sub.get("m", "")).lower() == "h",
            "ai": str(sub.get("ai_translated", "")).lower() == "true"}


# OpenSubtitles (the V3+ addon and Stremio's own, whose files come from *.strem.io) answers HTTP 429 when many
# downloads arrive at once: at most PACE at a time per such host, and a 429 is tried again after a short wait.
PACED_HOSTS = ("opensubtitles", "strem.io")
PACE = 2
RETRIES_429 = 2
_gates, _gates_lock = {}, threading.Lock()


def _gate(host):
    if not any(p in host for p in PACED_HOSTS):
        return None
    with _gates_lock:
        return _gates.setdefault(host, threading.BoundedSemaphore(PACE))


def _wait_429(err, attempt):
    try:
        wait = float(err.headers.get("Retry-After") or 0)
    except (TypeError, ValueError):
        wait = 0.0
    return min(max(wait, 1.5 * attempt), 5.0)


def fetch_subtitle(url, timeout=30, headers=None):
    """A subtitle's bytes. Gzip is unpacked; a zip (SubDL, SubSource) gives its subtitle file, the one named for the
    episode when the link ends in #SxxEyy. `headers`: what the source needs besides (a key, never in the address)."""
    url, _, frag = url.partition("#")
    gate = _gate((urllib.parse.urlsplit(url).hostname or "").lower())
    for attempt in range(1, RETRIES_429 + 2):
        req = urllib.request.Request(url, headers={"user-agent": USER_AGENT, "Connection": "close", **(headers or {})})
        try:
            if gate:
                gate.acquire()
            try:
                with realdebrid.urlopen(req, timeout=timeout) as r:
                    raw = r.read(MAX_SUBTITLE_BYTES + 1)
            finally:
                if gate:
                    gate.release()
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt > RETRIES_429:
                raise
            time.sleep(_wait_429(e, attempt))
    if len(raw) > MAX_SUBTITLE_BYTES:
        raise ValueError("subtitle too large")
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    if raw[:4] == b"PK\x03\x04":
        raw = _from_zip(raw, frag)
    return raw


def _from_zip(raw, frag):
    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = [i for i in z.infolist() if i.filename.lower().endswith((".srt", ".ass", ".ssa", ".vtt"))
                 and i.file_size <= MAX_SUBTITLE_BYTES]
        if not names:
            raise ValueError("no subtitle in the zip")
        if frag:
            want = frag.lower()
            hits = [i for i in names if want in i.filename.lower().replace(" ", "").replace(".", "")]
            names = hits or names
        return z.read(names[0])
