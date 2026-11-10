"""The Stremio subtitles addon that AIOStreams calls as a custom addon, and the synced files players fetch.

GET  /manifest.json
GET  /subtitles/<type>/<id>[/<extras>].json   extras = videoHash, videoSize, filename (AIOStreams passes them on)
     -> one "<Language> (synced)" entry per language that has a synced copy for this file: ISO `lang`, `label`, and
        `variantOf` = the id of the original subtitle it was made from.
        Unknown file: a job is queued; with SUBSYNC_PENDING_ENTRY on, a pending entry is listed whose file waits up to
        SUBSYNC_PENDING_WAIT_MS for the job, then serves the original.
GET  /file/<id>.<srt|ass>                      a synced copy
GET  /pending/<token>/<lang>.srt               a pending entry
GET  /health, /status

With SUBSYNC_ADDON_KEY set, the manifest, the subtitles route and /status answer only under /<key>/ (the manifest URL
AIOStreams is given carries it); /file, /pending and /health answer with or without it. SUBSYNC_BASE_PATH (e.g.
/subsync) is removed from a request path that starts with it, for a reverse proxy that forwards the path as is.
"""
import base64
import hmac
import json
import logging
import re
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .langs import name as lang_name
from .sources import parse_extras
from .subs import is_ass, parse, to_srt
from .worker import Job

log = logging.getLogger("subsync.addon")

MANIFEST = {
    "id": "org.aiosubtitles.subsync",
    "version": "1.0.0",
    "name": "subsync",
    "description": "Synced copies of the subtitles a viewer sees, checked against the release's own subtitle track "
                   "or its speech; below the bar the original is kept.",
    "resources": ["subtitles"],
    "types": ["movie", "series"],
    "catalogs": [],
    "behaviorHints": {"configurable": False},
}
SUB_ROUTE = re.compile(r"^/subtitles/(movie|series)/([^/]+?)(?:/([^/]*))?\.json$")
FILE_ROUTE = re.compile(r"^/file/(v\d+_[0-9a-f]{16}-\d+_[0-9a-f]{16})\.(srt|ass)$")
PENDING_ROUTE = re.compile(r"^/pending/([A-Za-z0-9_-]+)/([a-z]{3})\.srt$")
REPROCESS_S = 24 * 3600


def file_id(key):
    return key.split(":", 1)[1].replace(":", "_")


def key_of(fid):
    return "subsync:" + fid.replace("_", ":")


def token(alias):
    return base64.urlsafe_b64encode(alias.encode()).decode().rstrip("=")


def alias_of(tok):
    return base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode()


class App:
    def __init__(self, settings, store, worker, safety):
        self.s, self.store, self.worker, self.safety = settings, store, worker, safety
        self.subtitle_requests, self.last_subtitle_request = 0, None   # the installer's self-test reads these

    def route(self, path):
        """(path without the base path and key, keyed) for a request path."""
        base = self.s.base_path
        if base and (path == base or path.startswith(base + "/")):
            path = path[len(base):] or "/"
        key = self.s.addon_key
        if key:
            first, sep, rest = path[1:].partition("/")
            if sep and hmac.compare_digest(first.encode(), key.encode()):
                return "/" + rest, True
        return path, not key

    def entry(self, r):
        fid = file_id(r["key"])
        return {"id": f"subsync-{fid}", "url": f"{self.s.public_base}/file/{fid}.{r['fmt'] or 'srt'}",
                "lang": r["lang"], "label": f"{lang_name(r['lang'])} (synced)",
                "variantOf": r.get("candidate_id") or "original"}

    def offers(self, aliases):
        file_key = self.store.resolve(*aliases)
        out = []
        if file_key:
            for r in self.store.offers(file_key, self.s.languages):
                out.append(self.entry(r))
                self.store.served(r["key"])
        return file_key, out

    def subtitles(self, type_, video_id, extras):
        hints = parse_extras(extras)
        job = Job(type_, video_id, hints)
        aliases = [job.alias]
        if hints.get("filename") and hints.get("videoSize"):
            aliases.append(f"name:{hints['filename']}|{hints['videoSize']}")
        file_key, out = self.offers(aliases)
        if len(out) == len(self.s.languages) or not (hints.get("filename") or hints.get("videoHash")):
            return out
        recent = False
        if file_key:
            row = self.store.db.execute("SELECT read_at FROM files WHERE file_key=?", (file_key,)).fetchone()
            recent = bool(row and row[0] and time.time() - row[0] < REPROCESS_S)
        if not recent and not self.worker.recently_unlisted(job.alias):
            job = self.worker.submit(job) or job
        if not self.worker.busy_with(job.alias):
            return out
        # A file being synced now: the Cues route takes a fraction of a second, so wait up to SUBSYNC_PENDING_WAIT_MS
        # for it and list what is ready (players and AIOStreams may keep this answer a while).
        originals = self.worker.wait_for_file(job.alias, self.s.pending_wait)
        file_key, out = self.offers(aliases)
        if not (self.s.pending_entry and originals and self.worker.busy_with(job.alias)):
            return out
        have = {e["lang"] for e in out}
        tok = token(job.alias)
        for lang in self.s.languages:
            original = originals.get(lang)
            if lang not in have and original:   # only languages that have a subtitle to sync
                out.append({"id": f"subsync-pending-{lang}", "url": f"{self.s.public_base}/pending/{tok}/{lang}.srt",
                            "lang": lang, "label": f"{lang_name(lang)} (synced)", "variantOf": original["id"]})
        return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    app = None

    def log_message(self, *a):  # no access log: URLs may carry file names
        pass

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200, cache=None):
        self._send(code, json.dumps(obj), headers={"Cache-Control": cache} if cache else None)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        app = self.app
        path, keyed = app.route(urllib.parse.urlsplit(self.path).path)
        try:
            if path == "/health":
                return self._send(200, "ok", "text/plain")
            if not keyed and (path in ("/manifest.json", "/status") or path.startswith("/subtitles/")):
                return self._send(404, b"")
            if path == "/manifest.json":
                return self._json(MANIFEST, cache="max-age=3600")
            if path == "/status":
                return self._json({"store": app.store.stats(), "queue": len(app.worker.jobs),
                                   "subtitle_requests": app.subtitle_requests,
                                   "last_subtitle_request": app.last_subtitle_request,
                                   "busy": app.worker.current is not None, "backing_off": app.safety.backing_off(),
                                   "bytes_today": app.store.bytes_today("read")})
            m = SUB_ROUTE.match(path)
            if m:
                type_, vid, extras = m.group(1), urllib.parse.unquote(m.group(2)), urllib.parse.unquote(m.group(3) or "")
                app.subtitle_requests += 1
                app.last_subtitle_request = int(time.time())
                return self._json({"subtitles": app.subtitles(type_, vid, extras)}, cache="max-age=60")
            m = FILE_ROUTE.match(path)
            if m:
                key = key_of(m.group(1))
                body = app.store.body(key)
                if body is None:
                    return self._send(404, b"")
                app.store.served(key)
                if m.group(2) == "srt" and is_ass(body):
                    body = to_srt(parse(body))
                ctype = "text/x-ssa; charset=utf-8" if m.group(2) == "ass" else "application/x-subrip; charset=utf-8"
                return self._send(200, body, ctype, {"Cache-Control": "max-age=3600"})
            m = PENDING_ROUTE.match(path)
            if m:
                try:
                    alias = alias_of(m.group(1))
                except ValueError:
                    return self._send(404, b"")
                key, original = app.worker.wait_for(alias, m.group(2), app.s.pending_wait)
                body = app.store.body(key) if key else None
                if body is not None:
                    app.store.served(key)
                    return self._send(200, to_srt(parse(body)) if is_ass(body) else body,
                                      "application/x-subrip; charset=utf-8", {"Cache-Control": "no-store"})
                if original:
                    return self._send(302, b"", "text/plain", {"Location": original, "Cache-Control": "no-store"})
                return self._send(503, b"", "text/plain", {"Retry-After": "1", "Cache-Control": "no-store"})
            return self._send(404, b"")
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:
            log.warning("request failed: %s", type(e).__name__)
            return self._send(500, b"")

def serve(app, port):
    Handler.app = app
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    return server
