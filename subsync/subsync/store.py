"""subsync's own store: SQLite + gzipped subtitle bodies on its volume, capped (default 2 GB), LRU by last served.

results    one row per (file, subtitle): key subsync:v1:<oshash>-<size>:<sha256(subtitle)[:16]>
last_good  per (file, language): the synced result a viewer is offered; a new or better-ranked subtitle replaces it
           only when it passes; an original that passes as it is clears it (nothing to offer). With best fit
           the worker sets it from its ranking instead (set_offers).
offers     per (file, language): the offered results, best fit first (last_good is the first)
judged     per (file, language): when every subtitle of that language was last judged for the file, and under which
           pass rule; a language with no subtitle at all counts as judged. With many people of different languages
           sharing the store (public mode), a file read for one language is read again for another.
files      per file: served size, hash, whether it has a usable text track, read failures and back-off
aliases    file hints (AIOS videoHash, or file name + size) -> file key, so a listing finds a file read earlier
budget     bytes received per UTC day
pickable   files that wait for the viewer's pick before the audio route may start (kept across a restart)
originals  per file hint: the original subtitle per language (the pick entries' fallback, kept across a restart)

Lifetimes: synced/in_sync rows live 180 days after last served; keep_original is retried after 30 days
(or when ALGO_VERSION changes, which changes the key); read_failed backs off 1 h / 6 h / 24 h, then stops.
"""
import gzip
import json
import os
import sqlite3
import threading
import time

from . import ALGO_VERSION

DAY = 86400
KEEP_SERVED_S = 180 * DAY
RETRY_KEEP_ORIGINAL_S = 30 * DAY
READ_FAILED_BACKOFF_S = (3600, 6 * 3600, 24 * 3600)
KEEP_PICKABLE_S = 7 * DAY
JUDGED_FOR_S = DAY            # a file judged for every language asked within this long is not read again

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
  key TEXT PRIMARY KEY, file_key TEXT NOT NULL, sub_sha TEXT NOT NULL, lang TEXT NOT NULL, rank INTEGER,
  outcome TEXT NOT NULL, reason TEXT, route TEXT, detail TEXT, fmt TEXT, body_bytes INTEGER DEFAULT 0,
  candidate_id TEXT, created REAL NOT NULL, last_served REAL, retry_at REAL);
CREATE INDEX IF NOT EXISTS results_file ON results(file_key, lang);
CREATE TABLE IF NOT EXISTS last_good (
  file_key TEXT NOT NULL, lang TEXT NOT NULL, key TEXT NOT NULL, rank INTEGER, updated REAL,
  PRIMARY KEY (file_key, lang));
CREATE TABLE IF NOT EXISTS files (
  file_key TEXT PRIMARY KEY, size INTEGER, oshash TEXT, text_track INTEGER, detail TEXT, read_at REAL,
  fails INTEGER DEFAULT 0, retry_at REAL);
CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, file_key TEXT NOT NULL, updated REAL);
CREATE TABLE IF NOT EXISTS budget (day TEXT NOT NULL, kind TEXT NOT NULL, bytes INTEGER NOT NULL,
  PRIMARY KEY (day, kind));
CREATE TABLE IF NOT EXISTS pickable (alias TEXT PRIMARY KEY, type TEXT NOT NULL, video_id TEXT NOT NULL,
  hints TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS originals (alias TEXT PRIMARY KEY, langs TEXT NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS offers (file_key TEXT NOT NULL, lang TEXT NOT NULL, pos INTEGER NOT NULL, key TEXT NOT NULL,
  updated REAL NOT NULL, PRIMARY KEY (file_key, lang, pos));
CREATE TABLE IF NOT EXISTS judged (file_key TEXT NOT NULL, lang TEXT NOT NULL, rule TEXT NOT NULL, at REAL NOT NULL,
  PRIMARY KEY (file_key, lang));
"""


def result_key(file_key, sub_sha):
    return f"subsync:{ALGO_VERSION}:{file_key}:{sub_sha[:16]}"


def day_of(t=None):
    return time.strftime("%Y-%m-%d", time.gmtime(t if t is not None else time.time()))


class Store:
    def __init__(self, root, cap_bytes=2 * 1024 ** 3, clock=time.time):
        self.root, self.cap, self.clock = root, cap_bytes, clock
        os.makedirs(os.path.join(root, "bodies"), exist_ok=True)
        self.db = sqlite3.connect(os.path.join(root, "subsync.sqlite3"), check_same_thread=False,
                                  isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    # -- bodies ------------------------------------------------------------------------------------------------
    def _body_path(self, key):
        name = key.replace(":", "_")
        return os.path.join(self.root, "bodies", name[-2:], name + ".gz")

    def body(self, key):
        try:
            with gzip.open(self._body_path(key), "rt", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    # -- results -----------------------------------------------------------------------------------------------
    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT key, file_key, sub_sha, lang, rank, outcome, reason, route, detail, fmt, "
                                  "body_bytes, candidate_id, created, last_served, retry_at FROM results WHERE key=?",
                                  (key,)).fetchone()
        if not row:
            return None
        cols = ("key", "file_key", "sub_sha", "lang", "rank", "outcome", "reason", "route", "detail", "fmt",
                "body_bytes", "candidate_id", "created", "last_served", "retry_at")
        out = dict(zip(cols, row))
        out["detail"] = json.loads(out["detail"] or "{}")
        return out

    def fresh(self, key):
        """A result that needs no new attempt now."""
        r = self.get(key)
        if not r:
            return None
        if r["outcome"] in ("synced", "in_sync"):
            return r
        if r["retry_at"] is not None and r["retry_at"] > self.clock():
            return r
        return None

    def put(self, file_key, sub_sha, lang, rank, outcome, reason=None, route=None, detail=None, fmt=None, body=None,
            candidate_id=None, offer=True):
        """offer=False (best fit): the result is stored but what is offered is left to set_offers."""
        key = result_key(file_key, sub_sha)
        now = self.clock()
        body_bytes = 0
        if outcome == "synced" and body is not None:
            path = self._body_path(key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                f.write(body)
            os.replace(tmp, path)
            body_bytes = os.path.getsize(path)
        retry = None
        if outcome == "keep_original":
            retry = now + RETRY_KEEP_ORIGINAL_S
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (key, file_key, sub_sha, lang, rank, outcome, reason, route,
                             json.dumps(detail or {}), fmt, body_bytes, candidate_id, now, now, retry))
            if outcome == "synced" and offer:
                lg = self.last_good(file_key, lang)
                if lg is None or rank is None or lg["rank"] is None or rank <= lg["rank"]:
                    self.db.execute("INSERT OR REPLACE INTO last_good VALUES (?,?,?,?,?)", (file_key, lang, key, rank, now))
            elif outcome == "in_sync" and rank == 0 and offer:
                # the subtitle a viewer sees first is fine as it is: no synced copy to offer
                self.db.execute("DELETE FROM last_good WHERE file_key=? AND lang=?", (file_key, lang))
        self.enforce_cap()
        return key

    def last_good(self, file_key, lang):
        with self.lock:
            row = self.db.execute("SELECT key, rank, updated FROM last_good WHERE file_key=? AND lang=?",
                                  (file_key, lang)).fetchone()
        return {"key": row[0], "rank": row[1], "updated": row[2]} if row else None

    def set_offers(self, file_key, lang, keys):
        """Best fit: what is offered for a file and language, best first; the first becomes last_good."""
        now = self.clock()
        with self.lock:
            self.db.execute("DELETE FROM offers WHERE file_key=? AND lang=?", (file_key, lang))
            for pos, key in enumerate(keys):
                self.db.execute("INSERT INTO offers VALUES (?,?,?,?,?)", (file_key, lang, pos, key, now))
            if keys:
                self.db.execute("INSERT OR REPLACE INTO last_good VALUES (?,?,?,?,?)", (file_key, lang, keys[0], None, now))
            else:
                self.db.execute("DELETE FROM last_good WHERE file_key=? AND lang=?", (file_key, lang))

    def offers(self, file_key, langs, counts=None):
        """The synced results to list for a file: per language its offers in order (up to counts[lang], default 1),
        else its last_good."""
        out = []
        for lang in langs:
            n = (counts or {}).get(lang, 1)
            with self.lock:
                keys = [k for (k,) in self.db.execute("SELECT key FROM offers WHERE file_key=? AND lang=? ORDER BY pos",
                                                       (file_key, lang))]
            if not keys:
                lg = self.last_good(file_key, lang)
                keys = [lg["key"]] if lg else []
            for key in keys[:n]:
                r = self.get(key)
                if r and r["outcome"] == "synced" and os.path.exists(self._body_path(r["key"])):
                    out.append(r)
        return out

    def served(self, key):
        with self.lock:
            self.db.execute("UPDATE results SET last_served=? WHERE key=?", (self.clock(), key))

    # -- files and aliases -------------------------------------------------------------------------------------
    def alias(self, alias, file_key):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?,?)", (alias, file_key, self.clock()))

    def resolve(self, *aliases):
        with self.lock:
            for a in aliases:
                if not a:
                    continue
                row = self.db.execute("SELECT file_key FROM aliases WHERE alias=?", (a,)).fetchone()
                if row:
                    return row[0]
        return None

    def file_read(self, file_key, size, oshash, text_track, detail):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,0,NULL)",
                            (file_key, size, oshash, 1 if text_track else 0, json.dumps(detail), self.clock()))

    def file_failed(self, alias):
        """A read failure for a file (by alias, since a file that can't be read has no hash): 1 h / 6 h / 24 h,
        then never again. Returns the retry time, or None when it stops."""
        with self.lock:
            row = self.db.execute("SELECT fails FROM files WHERE file_key=?", (alias,)).fetchone()
            fails = (row[0] if row else 0) + 1
            retry = self.clock() + READ_FAILED_BACKOFF_S[fails - 1] if fails <= len(READ_FAILED_BACKOFF_S) else None
            self.db.execute("INSERT OR REPLACE INTO files (file_key, fails, retry_at, read_at) VALUES (?,?,?,?)",
                            (alias, fails, retry if retry is not None else float("inf"), self.clock()))
        return retry

    def file_blocked(self, alias):
        with self.lock:
            row = self.db.execute("SELECT retry_at FROM files WHERE file_key=? AND fails>0", (alias,)).fetchone()
        return bool(row and row[0] is not None and row[0] > self.clock())

    # -- which languages were judged for a file -------------------------------------------------------------------
    def judged_put(self, file_key, langs, rule):
        now = self.clock()
        with self.lock:
            for lang in langs:
                self.db.execute("INSERT OR REPLACE INTO judged VALUES (?,?,?,?)", (file_key, lang, rule, now))

    def judged_all(self, file_key, langs, rule, since):
        """Whether every one of langs was judged for the file under this rule at `since` or later. None: the file has
        no judged rows at all (stored before this memory existed; the file's read time decides, as before)."""
        with self.lock:
            rows = dict((lang, (r, at)) for lang, r, at in self.db.execute(
                "SELECT lang, rule, at FROM judged WHERE file_key=?", (file_key,)))
        if not rows:
            return None
        return all(lang in rows and rows[lang][0] == rule and rows[lang][1] >= since for lang in langs)

    # -- files waiting for a pick, and their original subtitles ---------------------------------------------------
    def pickable_put(self, alias, type_, video_id, hints):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO pickable VALUES (?,?,?,?,?)",
                            (alias, type_, video_id, json.dumps(hints), self.clock()))

    def pickable_get(self, alias):
        with self.lock:
            row = self.db.execute("SELECT type, video_id, hints FROM pickable WHERE alias=? AND created>?",
                                  (alias, self.clock() - KEEP_PICKABLE_S)).fetchone()
        return {"type": row[0], "video_id": row[1], "hints": json.loads(row[2])} if row else None

    def pickable_drop(self, alias):
        with self.lock:
            self.db.execute("DELETE FROM pickable WHERE alias=?", (alias,))

    def originals_put(self, alias, langs):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO originals VALUES (?,?,?)", (alias, json.dumps(langs), self.clock()))

    def originals_get(self, alias):
        with self.lock:
            row = self.db.execute("SELECT langs FROM originals WHERE alias=? AND updated>?",
                                  (alias, self.clock() - KEEP_PICKABLE_S)).fetchone()
        return json.loads(row[0]) if row else None

    # -- budget ------------------------------------------------------------------------------------------------
    def add_bytes(self, kind, n):
        with self.lock:
            self.db.execute("INSERT INTO budget VALUES (?,?,?) ON CONFLICT(day, kind) DO UPDATE SET bytes=bytes+?",
                            (day_of(self.clock()), kind, n, n))

    def bytes_today(self, kind):
        with self.lock:
            row = self.db.execute("SELECT bytes FROM budget WHERE day=? AND kind=?", (day_of(self.clock()), kind)).fetchone()
        return row[0] if row else 0

    # -- housekeeping ------------------------------------------------------------------------------------------
    def total_bytes(self):
        with self.lock:
            bodies = self.db.execute("SELECT COALESCE(SUM(body_bytes),0) FROM results").fetchone()[0]
        db = sum(os.path.getsize(os.path.join(self.root, f)) for f in os.listdir(self.root)
                 if f.startswith("subsync.sqlite3") and os.path.isfile(os.path.join(self.root, f)))
        return bodies + db

    def _drop(self, key):
        try:
            os.remove(self._body_path(key))
        except FileNotFoundError:
            pass
        self.db.execute("DELETE FROM results WHERE key=?", (key,))
        self.db.execute("DELETE FROM last_good WHERE key=?", (key,))
        self.db.execute("DELETE FROM offers WHERE key=?", (key,))

    def enforce_cap(self):
        """Least recently served synced bodies go first until the store is under its cap."""
        with self.lock:
            while self.total_bytes() > self.cap:
                row = self.db.execute("SELECT key FROM results WHERE body_bytes>0 ORDER BY last_served LIMIT 1").fetchone()
                if not row:
                    break
                self._drop(row[0])

    def expire(self):
        now = self.clock()
        with self.lock:
            old = [r[0] for r in self.db.execute(
                "SELECT key FROM results WHERE outcome IN ('synced','in_sync') AND last_served < ?", (now - KEEP_SERVED_S,))]
            for key in old:
                self._drop(key)
            self.db.execute("DELETE FROM results WHERE outcome='keep_original' AND retry_at < ?", (now,))
            self.db.execute("DELETE FROM budget WHERE day < ?", (day_of(now - 40 * DAY),))
            self.db.execute("DELETE FROM pickable WHERE created < ?", (now - KEEP_PICKABLE_S,))
            self.db.execute("DELETE FROM originals WHERE updated < ?", (now - KEEP_PICKABLE_S,))
            self.db.execute("DELETE FROM judged WHERE at < ?", (now - 2 * JUDGED_FOR_S,))
        return len(old)

    def stats(self):
        with self.lock:
            rows = self.db.execute("SELECT outcome, COUNT(*) FROM results GROUP BY outcome").fetchall()
        return {"results": dict(rows), "bytes": self.total_bytes(), "cap": self.cap}
