"""AIOStreams through subsync's own config (the viewer's config without Usenet sources and without subsync): stream
listings to find the file a viewer opened, and the subtitle list a viewer would see (the same subtitle sources
and order as the viewer's config, without subsync itself).

That config has no preload or precache and failover off: a read of a listed link then opens that row only.
Background rows: cached debrid only; never Usenet, uncached, library-matched rows or rows of the addons named in
SUBSYNC_SKIP_ADDONS (empty by default).
"""
import json
import logging
import urllib.parse
import urllib.request

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
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(16 * 1024 * 1024))


def row_hints(stream):
    d = stream.get("streamData") or {}
    bh = stream.get("behaviorHints") or {}
    return {"filename": d.get("filename") or bh.get("filename"), "videoSize": d.get("size") or bh.get("videoSize"),
            "videoHash": bh.get("videoHash")}


def unreadable(stream, skip_addons=()):
    """Why subsync may not read a listed row (None = it may): it reads cached debrid files only. `skip_addons`: addon
    names (lower case) whose rows are never read."""
    d = stream.get("streamData") or {}
    svc = d.get("service") or {}
    addon = d.get("addon")
    addon_name = addon.get("name") if isinstance(addon, dict) else addon
    if not stream.get("url"):
        return "no url"
    if d.get("type") != "debrid":
        return f"type {d.get('type')}"
    if svc.get("cached") is not True:
        return "not cached"
    if d.get("library"):
        return "library row"
    if skip_addons and (addon_name or "").strip().lower() in skip_addons:
        return f"addon {addon_name.strip()}"
    return None


class Aios:
    def __init__(self, manifest_url, own_bases=(), timeout=120, skip_addons=()):
        self.base = manifest_base(manifest_url)
        self.skip = tuple(a.strip().lower() for a in skip_addons if a and a.strip())
        self.own = tuple(b.rstrip("/") for b in own_bases if b)
        self.timeout = timeout

    def streams(self, type_, video_id):
        if not self.base:
            return []
        url = f"{self.base}/stream/{urllib.parse.quote(type_)}/{urllib.parse.quote(video_id)}.json"
        return _get_json(url, self.timeout).get("streams") or []

    def find_row(self, type_, video_id, hints):
        """The listed cached debrid row for a file the viewer opened (matched by file name and size)."""
        name, size = hints.get("filename"), hints.get("videoSize")
        for s in self.streams(type_, video_id):
            h = row_hints(s)
            if unreadable(s, self.skip) is None and name and h.get("filename") == name and (not size or h.get("videoSize") == size):
                return s
        return None

    def subtitles(self, type_, video_id, hints):
        """The subtitle list in the order a viewer would see it, without subsync's own entries."""
        if not self.base:
            return []
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


def fetch_subtitle(url, timeout=30):
    req = urllib.request.Request(url, headers={"user-agent": USER_AGENT, "Connection": "close"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(MAX_SUBTITLE_BYTES + 1)
    if len(raw) > MAX_SUBTITLE_BYTES:
        raise ValueError("subtitle too large")
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    return raw
