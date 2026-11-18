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
GET  /pick/<token>/<lang>.srt                  a "<Language> (Subsync)" entry ("(AIOSubtitles)" in public mode):
     the synced copy when there is one (waiting
     up to SUBSYNC_PICK_WAIT_MS for a job working on the file), else the original's text; for a language that
     has no subtitle at all a one-line note; 503 while the file's subtitle list is still unknown. With the audio
     route on (pick mode) the download also starts the audio route for a file that needs it.
GET  /original/<token>/<lang>.srt              a "<Language> (original)" entry: listed for a language that has
     subtitles but no synced copy, on a file that neither waits for a pick nor has a job on it; one original unchanged,
     the one the file's judging measured best as it is, else the likeliest by name. It starts no job and reads nothing
     of the file.
     Every original these routes serve is downloaded from this person's own subtitle list and sent as text, only if
     the language check doesn't refuse it (else the next that passes, else the note). A pick or pending entry
     stops waiting for the job ORIGINAL_DOWNLOAD_S early while that original isn't downloaded; when SUBSYNC_PICK_WAIT_MS
     still ends first, a redirect to the original's own address in this person's list (503 when a player can't use it).
GET  /notice.srt                               public mode: what the person must do, while the daily check (recheck.py)
     found something only they can fix; listed as one "AIOSubtitles: action needed" entry after the others
GET  /health, /status

With SUBSYNC_ADDON_KEY set, the manifest, the subtitles route and /status answer only under /<key>/ (the manifest URL
AIOStreams is given carries it); /file, /health and the entries the subtitle lists name (/pick, /pending, /original)
answer with or without it, players fetching them as listed. Then an entry's <token> ends in a code made from the key
(entry_code), so an entry's address can't be made up without the key, which never appears in it; one that doesn't carry
its code is "not found" and starts nothing. SUBSYNC_BASE_PATH (e.g. /subsync) is removed from a request path that starts
with it, for a reverse proxy that forwards the path as is.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import explain, origlang, srclimits
from .langs import name as lang_name
from .sources import parse_extras
from .subs import apply_recipe, is_ass, parse, to_srt
from .worker import Job

log = logging.getLogger("subsync.addon")

# The pick entry's name in subtitle lists: "<Language> (Subsync)" self-hosted, "<Language> (AIOSubtitles)" on the public
# service.
PICK_NAME = {False: "Subsync", True: "AIOSubtitles"}

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


def manifest(settings):
    """The addon's manifest; with a TMDB key, TMDB's notice in its description (TMDB's terms ask for it wherever its
    data is used)."""
    if explain.tmdb_used(settings):
        return dict(MANIFEST, description=MANIFEST["description"] + " " + explain.TMDB_NOTICE)
    return MANIFEST


SUB_ROUTE = re.compile(r"^/subtitles/(movie|series)/([^/]+?)(?:/([^/]*))?\.json$")
FILE_ROUTE = re.compile(r"^/file/(v\d+_[0-9a-f]{16}-\d+_[0-9a-f]{16})\.(srt|ass)$")
PENDING_ROUTE = re.compile(r"^/pending/([A-Za-z0-9_-]+)/([a-z]{3})\.srt$")
PICK_ROUTE = re.compile(r"^/pick/([A-Za-z0-9_-]+)/([a-z]{3})\.srt$")
ORIGINAL_ROUTE = re.compile(r"^/original/([A-Za-z0-9_-]+)/([a-z]{3})\.srt$")
# a Stremio id is short ("tt0944947:1:1", "kitsu:46474:1"): a longer one, or one with a line break or another control
# character, names no title and is answered 404 (it would reach the log as it is); so is one with a "/" (sent as %2F: it
# would be more of the path in AIOStreams' address)
MAX_VIDEO_ID = 256
REPROCESS_S = 24 * 3600
# A pick or pending entry stops waiting for the job this long before its deadline while the original it would serve
# isn't downloaded yet, so that download has time (a subtitle file is tens of KB; a slow site's first answer takes a
# second or two); past the deadline, the redirect to the original's own address as before
ORIGINAL_DOWNLOAD_S = 3.0


def file_id(key):
    return key.split(":", 1)[1].replace(":", "_")


def key_of(fid):
    return "subsync:" + fid.replace("_", ":")


CODE_CHARS = 16   # an entry's code: the first 96 bits of an HMAC-SHA256, in base64url


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def entry_code(key, alias):
    """The code an entry's address carries under SUBSYNC_ADDON_KEY `key`: made from the key and the file's alias, so
    it can't be made up without the key, which it doesn't give away."""
    return _b64(hmac.new(key.encode(), b"entry:" + alias.encode(), hashlib.sha256).digest())[:CODE_CHARS]


def token(alias, key=""):
    """An entry's token: the file's alias in base64url, then, with SUBSYNC_ADDON_KEY `key`, its code."""
    return _b64(alias.encode()) + (entry_code(key, alias) if key else "")


def alias_of(tok, key=""):
    """The file's alias in an entry's token; with `key`, only when the token ends in the alias's code. ValueError for
    any other token (answered "not found")."""
    tok, code = (tok[:-CODE_CHARS], tok[-CODE_CHARS:]) if key else (tok, "")
    alias = base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode()
    if key and not hmac.compare_digest(code, entry_code(key, alias)):
        raise ValueError("an entry address subsync didn't list")
    return alias


def as_srt(text):
    """A subtitle's text as the ".srt" routes send it: SRT as it is, ASS and WebVTT written as SRT."""
    if is_ass(text) or text.lstrip("\ufeff").startswith("WEBVTT"):
        return to_srt(parse(text))
    return text


def no_subtitle_note(lang):
    """An error here reaches the viewer as an HTTP 502 through AIOMetadata; say it plainly instead."""
    return f"1\n00:00:01,000 --> 00:00:08,000\nNo {lang_name(lang)} subtitle was found for this file.\n"


# What a pick, pending or "(original)" answer sent, now that an original comes as text with 200 like a
# synced copy: "copy" (a synced copy), "original" (an original sent unchanged), "guess" (a waiting file's anchored
# guess) or "note" (the "No <Language> subtitle was found" note); a redirect has none
SERVED = "X-AIOSubtitles-Served"

REFUSALS_FOR_NOTICE = 3   # reads refused in a row before a person is told their debrid service refuses them


class App:
    def __init__(self, settings, store, worker, safety, state=None, detail=None):
        self.s, self.store, self.worker, self.safety = settings, store, worker, safety
        self.state, self.detail = state, detail      # public mode: what the daily check found (the person's record)
        self.subtitle_requests, self.last_subtitle_request = 0, None   # the installer's self-test reads these
        self.texts, self.texts_lock = {}, threading.Lock()   # public mode: this person's rebuilt copies, for an hour

    def text_of(self, key):
        """A synced copy's text: from the store, or in public mode rebuilt from the original this person's own
        list gives, with the stored timings; kept here an hour, for this person only. None when there is none."""
        if not key:
            return None
        if self.store.keep_texts:
            return self.store.body(key)
        now = time.time()
        with self.texts_lock:
            got = self.texts.get(key)
        if got and now - got[0] < srclimits.TEXT_KEEP_S:
            return got[1]
        recipe, result = self.store.recipe(key), self.store.get(key)
        if not recipe or not result:
            return None
        original = self.worker.original_text(result, recipe.get("fp"))
        text = apply_recipe(original, recipe) if original is not None else None
        if text is not None:
            with self.texts_lock:
                self.texts[key] = (now, text)
                while len(self.texts) > srclimits.TEXT_KEEP_ENTRIES:
                    self.texts.pop(next(iter(self.texts)))
        return text

    def action(self):
        """Public mode: the state only the person can fix now (explain.PUBLIC_STATES), or None."""
        if not self.s.public:
            return None
        if self.state in explain.PUBLIC_STATES and explain.PUBLIC_STATES[self.state].person_must_act:
            return self.state
        if getattr(self.safety, "refusals", 0) >= REFUSALS_FOR_NOTICE:
            return "reads_refused"
        return None

    def notice_text(self):
        """The "action needed" entry's subtitle: what to do, in two cues; None when nothing is needed."""
        state = self.action()
        if state is None:
            return None
        title, text = explain.PUBLIC_STATES[state].title, explain.state_text(state, self.detail)
        return (f"1\n00:00:01,000 --> 00:00:09,000\n{title}.\n{text}\n\n"
                f"2\n00:00:09,500 --> 00:00:16,000\n{explain.PUBLIC_TEXTS['notice_where']}\n")

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
        """The entries for a file, plus (audio route on, and the viewer's choice not off) one "<Language>
        (Subsync)" entry per language for a file that has no subtitle track of its own, or whose tracks are not known
        yet: downloading it is the viewer's pick. It serves the synced copy when there is one, else the original."""
        t0 = time.time()
        out = self._with_pick_entries(Job(type_, video_id, parse_extras(extras)))
        if self.action() is not None:     # public mode: one entry after the others while only the person can fix something
            out.append({"id": "aiosubtitles-notice", "url": f"{self.s.public_base}/notice.srt",
                        "lang": self.s.languages[0], "label": explain.PUBLIC_NOTICE_LABEL})
        log.info("answer %s/%s: %d entries (%d to pick) in %d ms", type_, video_id, len(out),
                 sum(str(e.get("id", "")).startswith("subsync-pick-") for e in out), (time.time() - t0) * 1000)
        return out

    def _with_pick_entries(self, job):
        out = self._subtitles(job)
        pick_mode = self.s.audio_choice != "off"   # on pick or when opened, a pick entry starts it too
        if not (job.hints.get("filename") or job.hints.get("videoHash")):
            return out
        # Also with the audio route off, a file subsync works on now gets the entry (its download serves the copy
        # once the job has it); in pick mode a file that waits for the viewer's pick gets it too. A file whose
        # index is over the cap gets none, since a pick could only serve the original
        if self.worker.capped(job) or not ((pick_mode and self.worker.waits_for_pick(job.alias))
                                           or self.worker.busy_with(job.alias)):
            return out + self._original_entries(job, out)
        if not pick_mode and self.s.pending_entry:
            return out   # the pending "(synced)" entries cover a file being worked on
        if not pick_mode and self._no_track(job):
            # a viewer whose choice is off (the audio route off, or their own answer): a file known to have no
            # track of its own gets no copy from this job, and a pick starts nothing, so its original is listed as such
            return out + self._original_entries(job, out)
        # This answer is kept by AIOStreams (5 minutes) and by players (AIOMetadata: an hour), so the entry must be in
        # the first one, when the file's subtitle list is usually not known yet: then every language gets it, and the
        # download waits for the list. Once the list is known, only languages that have a subtitle get it.
        originals = self.worker.known_originals(job.alias)
        have = {e["lang"] for e in out}
        tok = token(job.alias, self.s.addon_key)
        for lang in self.s.languages:
            original = (originals or {}).get(lang)
            if lang in have or (originals is not None and not original):
                continue
            out.append({"id": f"subsync-pick-{lang}", "url": f"{self.s.public_base}/pick/{tok}/{lang}.srt",
                        "lang": lang, "label": f"{lang_name(lang)} ({PICK_NAME[self.s.public]})",
                        "variantOf": original["id"] if original else "original"})
        return out

    def _no_track(self, job):
        """The file's first read found no subtitle track of its own to judge against (False while it isn't read)."""
        file_key = self.store.resolve(*job.aliases)
        detail = self.store.file_detail(file_key) if file_key else None
        return (detail or {}).get("reference") is False

    def kept_choice(self, alias, lang):
        """The kept original of a file and language: (True, candidate id or None) when the file's judging chose
        one (None: every subtitle of the language is in another language), (False, None) when it chose none."""
        file_key = self.store.resolve(alias)
        kept = self.store.kept_original(file_key, lang) if file_key else None
        return (True, kept["candidate_id"]) if kept else (False, None)

    def _original_entries(self, job, out):
        """One "<Language> (original)" entry for each language asked for that has subtitles and no synced copy
        here: the original the file's judging kept, else (a file not judged) this person's likeliest by name. Its
        address names only the file; the subtitle comes from this person's own list when it is downloaded."""
        have = {e["lang"] for e in out}
        file_key = self.store.resolve(*job.aliases)
        originals = None
        entries = []
        tok = token(job.alias, self.s.addon_key)
        for lang in self.s.languages:
            if lang in have:
                continue
            kept = self.store.kept_original(file_key, lang) if file_key else None
            if kept is not None:
                chosen = kept["candidate_id"]
            else:
                if originals is None:
                    originals = self.worker.known_originals(job.alias) or {}
                chosen = (originals.get(lang) or {}).get("id")
            if chosen:
                entries.append({"id": f"subsync-original-{lang}", "url": f"{self.s.public_base}/original/{tok}/{lang}.srt",
                                "lang": lang, "label": f"{lang_name(lang)} (original)", "variantOf": chosen})
        if entries:   # its download fetches this person's list again when it isn't known any more
            self.worker.remember(job)
        return entries

    def _subtitles(self, job):
        names_file = bool(job.hints.get("filename") or job.hints.get("videoHash"))
        self.worker.start_lookup(job.video_id)     # the title's original language, in the background (origlang.py)
        file_key, out = self.offers(job.aliases)
        if out and names_file:
            # A copy listed here is rebuilt from this person's own subtitle list when it is downloaded; with no
            # job for the file, the list is fetched then, from this request's file
            self.worker.remember(job)
        if len({e["lang"] for e in out}) == len(self.s.languages) or not names_file:
            return out
        recent = False
        if file_key:
            read_at, detail = self.store.file_state(file_key)
            now = self.store.clock()
            recent = bool(read_at and now - read_at < REPROCESS_S)
            detail = detail or {}
            rule, now_rule = detail.get("rule"), origlang.current(self.worker.rule_for(job.video_id))
            if recent and rule is not None and not now_rule(rule):
                # read under another pass rule, or before its title's original language was known: judge its
                # subtitles again now
                recent = False
            judged = self.store.judged_all(file_key, self.s.languages, now_rule, now - REPROCESS_S) if recent else None
            no_track = detail.get("reference") is False
            if judged is False and not (no_track and self.s.audio_route and self.s.audio_choice == "off"):
                # read lately, but not for every language asked here (another person's, in public mode); for a
                # person whose choice is off, a file with no track can't gain from another read
                recent = False
            elif recent and self.s.audio_choice != "off" and no_track and not self.worker.waits_for_pick(job.alias) \
                    and not self.store.audio_judged(file_key, self.s.languages):
                # A file with no subtitle track of its own, read lately for someone else and not judged by the
                # audio route yet: the waiting file is each person's own, so this person gets their own first pass
                # (an index read) and can pick it, or open it; never someone whose choice is off
                recent = False
        if not recent and not self.worker.recently_unlisted(job.alias):
            # a job of its own for the queue: the worker changes a queued job's state (a pick makes it an audio job),
            # and this request's job is read again by _with_pick_entries
            self.worker.submit(job.again())
        if not self.worker.busy_with(job.alias):
            return out
        if not self.s.pending_entry:
            # AIOStreams lists subtitle addons in the order they answer, and players keep the first few per language
            # (AIOMetadata: 3): an answer held back for the job arrives after the other addons' and its entries
            # are cut. The "(Subsync)" entry needs no result, so answer at once.
            return out
        # A file being synced now: the Cues route takes a fraction of a second, so wait up to SUBSYNC_PENDING_WAIT_MS
        # for it and list what is ready (players and AIOStreams may keep this answer a while).
        originals = self.worker.wait_for_file(job.alias, self.s.pending_wait)
        file_key, out = self.offers(job.aliases)
        if not (self.s.pending_entry and originals and self.worker.busy_with(job.alias)):
            return out
        have = {e["lang"] for e in out}
        tok = token(job.alias, self.s.addon_key)
        for lang in self.s.languages:
            original = originals.get(lang)
            if lang not in have and original:   # only languages that have a subtitle to sync
                out.append({"id": f"subsync-pending-{lang}", "url": f"{self.s.public_base}/pending/{tok}/{lang}.srt",
                            "lang": lang, "label": f"{lang_name(lang)} (synced)", "variantOf": original["id"]})
        return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AIOSubtitles"   # the Server header, without http.server's name and the Python version
    sys_version = ""
    timeout = 30           # an idle kept-open connection gives its thread (public mode: its slot) back after this long
    app = None
    body_unread = False    # the request declared a body no route has read (parse_request)

    def log_message(self, *a):  # no access log: URLs may carry file names
        pass

    def parse_request(self):
        """http.server's parsing, noting whether the request declares a body: one a route doesn't read would stay on a
        kept-open connection and be read as the next request (another one hidden in it, run as if a proxy sent it)."""
        self.body_unread = False
        if not super().parse_request():
            return False
        length = (self.headers.get("Content-Length") or "0").strip()
        self.body_unread = bool(self.headers.get("Transfer-Encoding")) or length != "0"
        return True

    def read_body(self, length):
        """The request's body, `length` bytes as its Content-Length says. A body that also declares a Transfer-Encoding
        (which http.server can't follow) still closes the connection after the answer."""
        raw = self.rfile.read(length)
        self.body_unread = bool(self.headers.get("Transfer-Encoding"))
        return raw

    def end_headers(self):
        if self.body_unread and not self.close_connection:
            self.send_header("Connection", "close")   # the body unread: nothing more is read from this connection
        super().end_headers()

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

    def _subtitle(self, body, served):
        """A pick, pending or "(original)" route's subtitle as SRT, saying what it is (SERVED), readable by a web player
        too."""
        return self._send(200, body, "application/x-subrip; charset=utf-8",
                          {"Cache-Control": "no-store", SERVED: served, "Access-Control-Expose-Headers": SERVED})

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
                return self._json(manifest(app.s), cache="max-age=3600")
            if path == "/status":
                return self._json({"store": app.store.stats(), "queue": len(app.worker.jobs),
                                   "subtitle_requests": app.subtitle_requests,
                                   "last_subtitle_request": app.last_subtitle_request,
                                   "busy": app.worker.current is not None or app.worker.current_audio is not None,
                                   "busy_audio": app.worker.current_audio is not None,
                                   "backing_off": app.safety.backing_off(),
                                   "bytes_today": app.store.day_count("read")})
            m = SUB_ROUTE.match(path)
            if m:
                vid = urllib.parse.unquote(m.group(2))
                if len(vid) > MAX_VIDEO_ID or not vid.isprintable() or "/" in vid:
                    return self._send(404, b"")
                app.subtitle_requests += 1
                app.last_subtitle_request = int(time.time())
                # the extras as they are in the path: parse_extras splits them before it decodes each value
                return self._json({"subtitles": app.subtitles(m.group(1), vid, m.group(3) or "")}, cache="max-age=60")
            m = FILE_ROUTE.match(path)
            if m:
                key = key_of(m.group(1))
                body = app.text_of(key)
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
                    alias = alias_of(m.group(1), app.s.addon_key)
                except ValueError:
                    return self._send(404, b"")
                deadline = time.time() + app.s.pick_wait
                key, original = self._wait_for(app, alias, m.group(2), min(time.time() + app.s.pending_wait, deadline),
                                               deadline)
                body = app.text_of(key)
                if body is not None:
                    app.store.served(key)
                    return self._subtitle(to_srt(parse(body)) if is_ass(body) else body, "copy")
                if original:   # the original's text once it is checked, never a redirect to it
                    return self._original(app, alias, m.group(2), deadline)
                return self._send(503, b"", "text/plain", {"Retry-After": "1", "Cache-Control": "no-store"})
            m = ORIGINAL_ROUTE.match(path)
            if m:
                try:
                    alias = alias_of(m.group(1), app.s.addon_key)
                except ValueError:
                    return self._send(404, b"")
                kept, first = app.kept_choice(alias, m.group(2))
                if kept and first is None:   # every subtitle of the language is in another language
                    return self._subtitle(no_subtitle_note(m.group(2)), "note")
                return self._original(app, alias, m.group(2), time.time() + app.s.pick_wait, first,
                                      unknown=None if kept else 404)
            if path == "/notice.srt" and keyed:
                text = app.notice_text()
                if text is None:   # fixed since the list that holds this entry was made (apps keep lists a while)
                    text = f"1\n00:00:01,000 --> 00:00:08,000\n{explain.PUBLIC_NOTICE_DONE}\n"
                return self._send(200, text, "application/x-subrip; charset=utf-8", {"Cache-Control": "no-store"})
            m = PICK_ROUTE.match(path)
            if m:
                try:
                    alias = alias_of(m.group(1), app.s.addon_key)
                except ValueError:
                    return self._send(404, b"")
                lang = m.group(2)
                # One deadline for the whole answer (AIOMetadata gives up on a subtitle after 15 s)
                deadline = time.time() + app.s.pick_wait
                left = lambda: max(0.0, deadline - time.time())   # noqa: E731
                started = app.worker.pick(alias)   # pick mode: the audio route, which takes minutes, starts now
                # This file's queued job goes first, and this language gets alass sooner in it
                app.worker.prioritize(alias, lang)
                if not started and app.worker.busy_reading(alias):
                    # the Cues route takes seconds, the audio route minutes: the language's final result, else the best
                    # pass published by the deadline (less the original's download time while it isn't downloaded)
                    self._wait_for(app, alias, lang, deadline, deadline, final=True)
                file_key = app.store.resolve(alias)
                good = app.store.last_good(file_key, lang) if file_key else None
                # While best fit still judges, the best slot is empty but the first pass is published
                key = good["key"] if good else app.worker.published(alias, lang)
                body = app.text_of(key)
                if body is not None:
                    app.store.served(key)
                    return self._subtitle(to_srt(parse(body)) if is_ass(body) else body, "copy")
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
                    return self._subtitle(provisional, "guess")
                if (originals.get(lang) or {}).get("url"):   # the original's text once it is checked
                    # Nothing passed by the deadline: the downloaded original that measured best against the
                    # file's own track, when it beats the name-ranked guess
                    return self._original(app, alias, lang, deadline, app.worker.measured_original(alias, lang))
                return self._subtitle(no_subtitle_note(lang), "note")
            return self._send(404, b"")
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:
            log.warning("request failed: %s", type(e).__name__)
            return self._send(500, b"")

    @staticmethod
    def _wait_for(app, alias, lang, end, deadline, final=False):
        """Worker.wait_for until `end` (a time), stopping ORIGINAL_DOWNLOAD_S before `deadline` while the original the
        answer may serve still has to be downloaded, so it gets that time; once it is downloaded, until `end`."""
        got = app.worker.wait_for(alias, lang, max(0.0, min(end, deadline - ORIGINAL_DOWNLOAD_S) - time.time()), final)
        if time.time() < end and not app.worker.original_needs_download(alias, lang):
            got = app.worker.wait_for(alias, lang, max(0.0, end - time.time()), final)
        return got

    def _original(self, app, alias, lang, deadline, first=None, unknown=None):
        """An original's text from this person's own list, checked (Worker.original_for). When the deadline came first:
        a redirect to that original's own address in this person's list (as 1.4.0 answered), or, when a player can't
        use that address, 503, so the player asks again and gets the download it started. Else the one-line note (or
        `unknown`'s code when this person has no subtitle of the language listed for the file)."""
        text, why, address = app.worker.original_for(alias, lang, deadline, first)
        if text is not None:
            return self._subtitle(as_srt(text), "original")
        if why == "late":
            if address:
                return self._send(302, b"", "text/plain", {"Location": address, "Cache-Control": "no-store"})
            return self._send(503, b"", "text/plain", {"Retry-After": "1", "Cache-Control": "no-store"})
        if why == "unknown" and unknown:
            return self._send(unknown, b"")
        return self._subtitle(no_subtitle_note(lang), "note")


class Server(ThreadingHTTPServer):
    """The HTTP server of both modes: a thread per connection, none of them keeping the process up."""
    daemon_threads = True
    # connections waiting to be taken: socketserver's 5 overflow when players and AIOStreams ask together, and the
    # system then refuses the next ones (macOS) or drops them for the client to try again 1 to 3 s later (Linux)
    request_queue_size = 128

    def handle_error(self, request, client_address):
        """A connection that broke between requests (the client reset it, or it timed out) is no failure; anything else
        is logged by its kind only, as a request's own failure is (socketserver would print a traceback)."""
        error = sys.exc_info()[1]
        if not isinstance(error, (ConnectionError, TimeoutError)):
            log.warning("connection failed: %s", type(error).__name__)


def serve(app, port, host="0.0.0.0"):
    handler = type("BoundHandler", (Handler,), {"app": app})   # this server's app, not every Handler's
    return Server((host, port), handler)
