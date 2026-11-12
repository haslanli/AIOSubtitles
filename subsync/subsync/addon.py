"""The Stremio subtitles addon that AIOStreams calls as a custom addon, and the synced files players fetch.

GET  /manifest.json
GET  /subtitles/<type>/<id>[/<extras>].json   extras = videoHash, videoSize, filename (AIOStreams passes them on)
     -> one "<Language> (synced)" entry per language that has a synced copy for this file: ISO `lang`, `label`, and
        `variantOf` = the id of the original subtitle it was made from.
        Unknown file: a job is queued and the answer comes at once, with one "<Language> (Subsync)" entry per language
        while the job works on the file; with SUBSYNC_PENDING_ENTRY on, instead, the answer waits up to
        SUBSYNC_PENDING_WAIT_MS for the job and a pending "(synced)" entry is listed.
GET  /file/<id>.<srt|ass>                      a synced copy
GET  /pending/<token>/<lang>.srt               a pending entry
GET  /pick/<token>/<lang>.srt                  a "<Language> (Subsync)" entry: the synced copy when there is one (waiting
     up to SUBSYNC_PICK_WAIT_MS for a job working on the file), else a redirect to the original; for a language that
     has no subtitle at all a one-line note; 503 while the file's subtitle list is still unknown. With the audio
     route on (pick mode) the download also starts the audio route for a file that needs it.
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
PICK_ROUTE = re.compile(r"^/pick/([A-Za-z0-9_-]+)/([a-z]{3})\.srt$")
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

    def entry(self, r, n=1):
        fid = file_id(r["key"])
        return {"id": f"subsync-{fid}", "url": f"{self.s.public_base}/file/{fid}.{r['fmt'] or 'srt'}",
                "lang": r["lang"], "label": f"{lang_name(r['lang'])} (synced{'' if n == 1 else f' {n}'})",
                "variantOf": r.get("candidate_id") or "original"}

    def offers(self, aliases):
        """The finished copies for a file: per language its best fit, the first language up to
        SUBSYNC_MAIN_ALTERNATIVES, labelled "(synced)", "(synced 2)", "(synced 3)"."""
        file_key = self.store.resolve(*aliases)
        out = []
        if file_key:
            counts = {self.s.languages[0]: self.s.alternatives} if self.s.best_fit and self.s.languages else None
            seen = {}
            for r in self.store.offers(file_key, self.s.languages, counts):
                seen[r["lang"]] = seen.get(r["lang"], 0) + 1
                out.append(self.entry(r, seen[r["lang"]]))
                self.store.served(r["key"])
        return file_key, out

    def subtitles(self, type_, video_id, extras):
        """The entries for a file, plus (audio route on, started by a pick) one "<Language> (Subsync)" entry per language
        for a file that has no subtitle track of its own, or whose tracks are not known yet: downloading it is the
        viewer's pick. It serves the synced copy when there is one, else the original."""
        t0 = time.time()
        out = self._with_pick_entries(type_, video_id, extras)
        log.info("answer %s/%s: %d entries (%d to pick) in %d ms", type_, video_id, len(out),
                 sum(str(e.get("id", "")).startswith("subsync-pick-") for e in out), (time.time() - t0) * 1000)
        return out

    def _with_pick_entries(self, type_, video_id, extras):
        out = self._subtitles(type_, video_id, extras)
        pick_mode = self.s.audio_route and self.s.audio_on_pick
        if not pick_mode and self.s.pending_entry:
            return out   # the pending "(synced)" entries cover a file being worked on
        job = Job(type_, video_id, parse_extras(extras))
        if not (job.hints.get("filename") or job.hints.get("videoHash")):
            return out
        # Also with the audio route off, a file subsync works on now gets the entry (its download serves the copy
        # once the job has it); in pick mode a file that waits for the viewer's pick gets it too
        if not ((pick_mode and self.worker.waits_for_pick(job.alias)) or self.worker.busy_with(job.alias)):
            return out
        # This answer is kept by AIOStreams (5 minutes) and by players (AIOMetadata: an hour), so the entry must be in
        # the first one, when the file's subtitle list is usually not known yet: then every language gets it, and the
        # download waits for the list. Once the list is known, only languages that have a subtitle get it.
        originals = self.worker.known_originals(job.alias)
        have = {e["lang"] for e in out}
        tok = token(job.alias)
        for lang in self.s.languages:
            original = (originals or {}).get(lang)
            if lang in have or (originals is not None and not original):
                continue
            out.append({"id": f"subsync-pick-{lang}", "url": f"{self.s.public_base}/pick/{tok}/{lang}.srt",
                        "lang": lang, "label": f"{lang_name(lang)} (Subsync)",
                        "variantOf": original["id"] if original else "original"})
        return out

    def _subtitles(self, type_, video_id, extras):
        hints = parse_extras(extras)
        job = Job(type_, video_id, hints)
        aliases = [job.alias]
        if hints.get("filename") and hints.get("videoSize"):
            aliases.append(f"name:{hints['filename']}|{hints['videoSize']}")
        file_key, out = self.offers(aliases)
        if len({e["lang"] for e in out}) == len(self.s.languages) or not (hints.get("filename") or hints.get("videoHash")):
            return out
        recent = False
        if file_key:
            row = self.store.db.execute("SELECT read_at, detail FROM files WHERE file_key=?", (file_key,)).fetchone()
            recent = bool(row and row[0] and time.time() - row[0] < REPROCESS_S)
            rule = json.loads(row[1] or "{}").get("rule") if row else None
            if recent and rule is not None and rule != self.s.rule_id:
                recent = False   # read under another pass rule: judge its subtitles again now
            if recent and self.store.judged_all(file_key, self.s.languages, self.s.rule_id,
                                                time.time() - REPROCESS_S) is False:
                recent = False   # read lately, but not for every language asked here (another person's, in public mode)
        if not recent and not self.worker.recently_unlisted(job.alias):
            job = self.worker.submit(job) or job
        if not self.worker.busy_with(job.alias):
            return out
        if not self.s.pending_entry:
            # AIOStreams lists subtitle addons in the order they answer, and players keep the first few per language
            # (AIOMetadata: 3): an answer held back for the job arrives after the other addons' and its entries are cut
            #. The "(Subsync)" entry needs no result, so answer at once.
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
        self.dispatch(app, path, keyed)

    def dispatch(self, app, path, keyed):
        """Answers `path` (base path and key already taken off) for `app`: the self-hosted service has one; in public
        mode the router hands each person's requests to their own."""
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
                                   "busy": app.worker.current is not None or app.worker.current_audio is not None,
                                   "busy_audio": app.worker.current_audio is not None,
                                   "backing_off": app.safety.backing_off(),
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
            m = PICK_ROUTE.match(path)
            if m:
                try:
                    alias = alias_of(m.group(1))
                except ValueError:
                    return self._send(404, b"")
                lang = m.group(2)
                # One deadline for the whole answer (AIOMetadata gives up on a subtitle after 15 s)
                deadline = time.time() + app.s.pick_wait
                left = lambda: max(0.0, deadline - time.time())   # noqa: E731
                started = app.worker.pick(alias)   # pick mode: the audio route, which takes minutes, starts now
                app.worker.prioritize(alias)       # this file's queued job goes first
                if not started and app.worker.busy_reading(alias):
                    # the Cues route takes seconds, the audio route minutes: the language's final result, else the best
                    # pass published by the deadline
                    app.worker.wait_for(alias, lang, left(), final=True)
                file_key = app.store.resolve(alias)
                good = app.store.last_good(file_key, lang) if file_key else None
                # While best fit still judges, the best slot is empty but the first pass is published
                key = good["key"] if good else app.worker.published(alias, lang)
                body = app.store.body(key) if key else None
                if body is not None:
                    app.store.served(key)
                    return self._send(200, to_srt(parse(body)) if is_ass(body) else body,
                                      "application/x-subrip; charset=utf-8", {"Cache-Control": "no-store"})
                originals = app.worker.wait_for_originals(alias, left())
                if originals is None:
                    originals = app.worker.known_originals(alias)
                if originals is None and app.worker.ask_list_again(alias):   # the list came back empty
                    originals = app.worker.wait_for_originals(alias, left())
                if originals is None:
                    if file_key or app.worker.busy_with(alias):   # ours, list not known yet: the player may retry
                        return self._send(503, b"", "text/plain", {"Retry-After": "1", "Cache-Control": "no-store"})
                    return self._send(404, b"")
                provisional = app.worker.provisional_for(alias, lang)   # a waiting file's anchored guess
                if provisional:
                    return self._send(200, provisional, "application/x-subrip; charset=utf-8", {"Cache-Control": "no-store"})
                original = (originals.get(lang) or {}).get("url")
                if original:
                    return self._send(302, b"", "text/plain", {"Location": original, "Cache-Control": "no-store"})
                # An error here reaches the viewer as an HTTP 502 through AIOMetadata; say it plainly instead
                note = f"1\n00:00:01,000 --> 00:00:08,000\nNo {lang_name(lang)} subtitle was found for this file.\n"
                return self._send(200, note, "application/x-subrip; charset=utf-8", {"Cache-Control": "no-store"})
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
