"""subsync's own store: SQLite + gzipped subtitle bodies on its volume, the copies capped (default 2 GB), LRU by last
served.

results    one row per (file, language, subtitle), so one subtitle listed under two languages gets a verdict for each
: key subsync:<ALGO_VERSION>:<oshash>-<size>:<lang>:<sha256(subtitle)[:16]>
last_good  per (file, language): the synced result a viewer is offered; a new or better-ranked subtitle replaces it
           only when it passes; an original that passes as it is clears it (nothing to offer). With best fit
           the worker sets it from its ranking instead (set_offers).
offers     per (file, language): the offered results, best fit first (last_good is the first)
judged     per (file, language): when every subtitle of that language was last judged for the file, and under which
           pass rule; a language with no subtitle at all counts as judged. With many people of different languages
           sharing the store (public mode), a file read for one language is read again for another.
files      per file: served size, hash, whether it has a usable text track; per file hint: its read failures and back-off
           (read_at: the last failure, or the last request the back-off met)
aliases    file hints (AIOS videoHash, or file name + size) -> file key, so a listing finds a file read earlier
budget     counters per UTC day and kind (day_add, day_count): "read" is the bytes received (the daily budget); a
           self-hosted install also counts "usenet" (Usenet listings), "usenet_stop" (1: none for the rest of the day),
           "audio_syncs" and "audio_opened" (audio syncs started, and of those, as soon as a file was opened)
pickable   files that wait for the viewer's pick before the audio route may start (kept across a restart)
originals  per file hint: the original subtitle per language (the pick entries' fallback, kept across a restart)
over_cap   per file hint: the byte cap a job of the file stopped at; it isn't read again under that cap
           (at: when it was reached, or the last request it met)
kept_original  per (file, language) with nothing passing: the original its "<Language> (original)" entry serves,
           by candidate id (none when every subtitle of the language is in another language), and its measure

In public mode (keep_texts=False) no subtitle text is kept: a synced row stores how its copy was made from the
original (subs.timing_recipe: new timings only) under recipes/, and each person's copy is rebuilt from the original their
own list gives (addon.App.text_of), because the subtitle sites' terms don't allow serving one person's stored copy to
others.

Lifetimes: synced/in_sync rows live 180 days after last served; keep_original is retried after 30 days
(or when ALGO_VERSION changes, which changes the key), and a kept_original row goes after 30 days unless the file is
judged again; a file that couldn't be read backs off 10 min / 1 h / 6 h / 24 h, a refused one (and one neither
MKV nor MP4, or a Usenet file) 1 h / 6 h / 24 h, then it stops. Those are failures in a row, each back-off counting its
own: a read of the file that succeeds clears them. A file's names and details (aliases, files) go once nobody asked
for it for 180 days and no result of it is left, and its marks (its read failures, its byte cap) 180 days after the last
request they met, so a file still asked for is never read again.

In public mode the rows of one person alone (their waiting files, their original subtitles and the refusals of their
reads) are keyed "p:<their rows key>:<hash>" (worker.Worker._person_key), so Delete removes them (drop_rows_of,
).
"""
import contextlib
import gzip
import json
import logging
import os
import sqlite3
import threading
import time
import zlib

from . import ALGO_VERSION
from .settings import MB
from .subs import fingerprint, line_positions, timing_recipe

log = logging.getLogger("subsync.store")

# The databases in SUBSYNC_DATA, named once: the store's own and, in public mode, the people's records (users.Users,
# which __main__ opens). ./setup copies both before an update (hostcmd.DATABASES, a test keeps it equal to these).
DATABASE = "subsync.sqlite3"
USERS_DATABASE = "users.sqlite3"

DAY = 86400
KEEP_SERVED_S = 180 * DAY
RETRY_KEEP_ORIGINAL_S = 30 * DAY
READ_FAILED_BACKOFF_S = (600, 3600, 6 * 3600, 24 * 3600)   # a file that couldn't be read
# a refused file, one neither MKV nor MP4, and a Usenet file whose first read failed: an hour first, as before
REFUSED_BACKOFF_S = (3600, 6 * 3600, 24 * 3600)
KEEP_PICKABLE_S = 7 * DAY
JUDGED_FOR_S = DAY            # a file judged for every language asked within this long is not read again
# the files row that counts a file hint's failures under REFUSED_BACKOFF_S (and other waits than READ_FAILED_BACKOFF_S):
# the hint with this after it, so the two back-offs count apart
OTHER_BACKOFF_SUFFIX = "|refused"
# what reading a damaged copy file raises: cut short, not gzip (or its check fails), garbled, not UTF-8 or not JSON
DAMAGED = (EOFError, gzip.BadGzipFile, zlib.error, ValueError)

SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
  key TEXT PRIMARY KEY, file_key TEXT NOT NULL, sub_sha TEXT NOT NULL, lang TEXT NOT NULL, rank INTEGER,
  outcome TEXT NOT NULL, reason TEXT, route TEXT, detail TEXT, fmt TEXT, body_bytes INTEGER DEFAULT 0,
  candidate_id TEXT, created REAL NOT NULL, last_served REAL, retry_at REAL);
CREATE INDEX IF NOT EXISTS results_file ON results(file_key, lang);
CREATE INDEX IF NOT EXISTS results_lru ON results(last_served) WHERE body_bytes>0;
CREATE TABLE IF NOT EXISTS last_good (
  file_key TEXT NOT NULL, lang TEXT NOT NULL, key TEXT NOT NULL, rank INTEGER, updated REAL,
  PRIMARY KEY (file_key, lang));
CREATE INDEX IF NOT EXISTS last_good_key ON last_good(key);
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
CREATE INDEX IF NOT EXISTS offers_key ON offers(key);
CREATE TABLE IF NOT EXISTS judged (file_key TEXT NOT NULL, lang TEXT NOT NULL, rule TEXT NOT NULL, at REAL NOT NULL,
  PRIMARY KEY (file_key, lang));
CREATE TABLE IF NOT EXISTS usenet_misses (key TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS over_cap (alias TEXT PRIMARY KEY, cap INTEGER NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS kept_original (file_key TEXT NOT NULL, lang TEXT NOT NULL, candidate_id TEXT, measure REAL,
  at REAL NOT NULL, PRIMARY KEY (file_key, lang));
CREATE TABLE IF NOT EXISTS title_language (title TEXT PRIMARY KEY, answer TEXT NOT NULL, at REAL NOT NULL, until REAL);
"""
USENET_MISSES_KEPT_S = 2 * DAY


def result_key(file_key, lang, sub_sha):
    return f"subsync:{ALGO_VERSION}:{file_key}:{lang}:{sub_sha[:16]}"


def day_of(t=None):
    """The UTC day of `t` (default now), as the day counters here and in users.py key it."""
    return time.strftime("%Y-%m-%d", time.gmtime(t if t is not None else time.time()))


@contextlib.contextmanager
def transaction(db, lock):
    """Statements on `db` (opened with isolation_level=None) as one transaction, under `lock`: one commit, and all of
    them or none; inside another, they are part of it. The store's (Store._tx) and the people's records' (users.py)."""
    with lock:
        if db.in_transaction:
            yield
            return
        db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if db.in_transaction:   # SQLite may have rolled it back itself (a full disk)
                db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")


def _discard(path):
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


class Store:
    def __init__(self, root, cap_bytes=2 * 1024 ** 3, clock=time.time, keep_texts=True):
        self.root, self.cap, self.clock, self.keep_texts = root, cap_bytes, clock, keep_texts
        self.database_over_cap_said = False      # the database alone past the cap is logged once (enforce_cap)
        os.makedirs(os.path.join(root, "bodies"), exist_ok=True)
        self.db = sqlite3.connect(os.path.join(root, DATABASE), check_same_thread=False,
                                  isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        # copies and counters: with WAL, NORMAL commits without syncing the disk and can lose only the last commits on
        # a power cut, never corrupt the database (users.py keeps the default FULL: a sign-up must survive one)
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self._language_in_keys()
        self._drop_title_aliases()

    def _drop_title_aliases(self):
        """Links from a bare title to a file ("title:<type>/<id>"), which an earlier version made for a request that
        gave a file's hash without its size: every other release of the title, and every request that named
        no file, then got that file's copies. A title names no file: those links go; the file's results and its own
        names stay."""
        with self.lock:
            self.db.execute("DELETE FROM aliases WHERE alias LIKE 'title:%'")

    def _language_in_keys(self):
        """Results stored before a result's key named its language get it, their copies, offers and
        last_good with them, so nothing is read or judged again. The copies move first: a start that stops half-way
        finds the rows as they were and does the rest. A row an earlier image stored again under its old key replaces
        the one under its new key."""
        with self.lock:
            old = [(key, lang) for key, lang in self.db.execute("SELECT key, lang FROM results")
                   if key.count(":") == 3]
        if not old:
            return
        moves = []
        for key, lang in old:
            prefix, version, file_key, sha = key.split(":")
            new = ":".join((prefix, version, file_key, lang, sha))
            for path, moved in ((self._body_path(key), self._body_path(new)),
                                (self._recipe_path(key), self._recipe_path(new))):
                with contextlib.suppress(FileNotFoundError):
                    os.replace(path, moved)
            moves.append((new, key))
        with self._tx():
            self.db.executemany("UPDATE OR REPLACE results SET key=? WHERE key=?", moves)
            for table in ("last_good", "offers"):
                self.db.executemany(f"UPDATE {table} SET key=? WHERE key=?", moves)
        log.info("%d stored result%s now keyed by language too", len(moves), "" if len(moves) == 1 else "s")

    def close(self):
        with self.lock:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _tx(self):
        """The store's statements as one transaction, under its lock (transaction())."""
        return transaction(self.db, self.lock)

    # -- bodies ------------------------------------------------------------------------------------------------
    def _body_path(self, key):
        name = key.replace(":", "_")
        return os.path.join(self.root, "bodies", name[-2:], name + ".gz")

    def _recipe_path(self, key):
        name = key.replace(":", "_")
        return os.path.join(self.root, "recipes", name[-2:], name + ".json.gz")

    def _copy_path(self, key):
        return self._body_path(key) if self.keep_texts else self._recipe_path(key)

    def _remove_copy(self, key):
        for path in (self._body_path(key), self._recipe_path(key)):
            _discard(path)

    def _read_copy(self, path, key, parse=lambda text: text):
        """A copy file's content, or None: there is none, or it is damaged (DAMAGED), and then it goes with its row, so
        it is no longer offered and the subtitle is judged again."""
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return parse(f.read())
        except FileNotFoundError:
            return None
        except DAMAGED as e:
            log.warning("a stored copy can't be read (%s): dropped, to be judged again", type(e).__name__)
            self._drop(key)
            return None

    @contextlib.contextmanager
    def _temp_copy(self, key, content):
        """`content` (None: no copy) gzipped into a temporary file next to the key's copy, named for this thread, so two
        puts of one key never write into the same file; it is removed unless it was put in place."""
        if content is None:
            yield None
            return
        path = self._copy_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.tmp"
        try:
            with gzip.open(tmp, "wt", encoding="utf-8") as f:
                f.write(content)
            yield tmp
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp)

    def _place_copy(self, key, tmp):
        """Lock held: the copy written to `tmp` replaces the key's copy, and its size is returned; with none (tmp None),
        the key's earlier copy goes and 0 is returned."""
        if tmp is None:
            self._remove_copy(key)
            return 0
        path = self._copy_path(key)
        os.replace(tmp, path)
        return os.path.getsize(path)

    def body(self, key):
        """A synced copy's text; never in public mode (keep_texts off), where only its recipe is kept."""
        if not self.keep_texts:
            return None
        return self._read_copy(self._body_path(key), key)

    def recipe(self, key):
        """Public mode: how a synced copy is made from its original (subs.apply_recipe), or None."""
        return self._read_copy(self._recipe_path(key), key, json.loads)

    def drop_texts(self):
        """Public mode, at start: copies kept as text by an earlier version are dropped with their rows, so they are
        judged again and kept as recipes. Returns how many rows went."""
        with self._tx():
            texts = sum(len(names) for _, _, names in os.walk(os.path.join(self.root, "bodies")))
            keys = [k for (k,) in self.db.execute("SELECT key FROM results WHERE body_bytes>0")
                    if not os.path.exists(self._recipe_path(k))]
            for key in keys:
                file_key = self.db.execute("SELECT file_key FROM results WHERE key=?", (key,)).fetchone()
                self._drop(key)
                if file_key:   # judged again on the next play, so the copy comes back as a recipe
                    self.db.execute("DELETE FROM judged WHERE file_key=?", file_key)
            for root, _, names in os.walk(os.path.join(self.root, "bodies")):
                for n in names:
                    os.remove(os.path.join(root, n))
        if keys or texts:   # counted before anything was removed (at the first deploy it said 0 texts for 21)
            log.info("public mode: %d stored subtitle text%s dropped (%d row%s to judge again)", texts,
                     "" if texts == 1 else "s", len(keys), "" if len(keys) == 1 else "s")
        return len(keys)

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

    def put(self, file_key, sub_sha, lang, rank, outcome, reason=None, *, route=None, detail=None, fmt=None, body=None,
            candidate_id=None, offer=True, original=None):
        """offer=False (best fit): the result is stored but what is offered is left to set_offers. Public mode keeps
        the copy's recipe from `original` (the original's text) instead of `body`; with no recipe it isn't offered.
        What follows the reason is named at each call, so no two of its values can trade places unseen."""
        key = result_key(file_key, lang, sub_sha)
        now = self.clock()
        content = None
        if outcome == "synced" and body is not None:
            content = body
            if not self.keep_texts:
                recipe = timing_recipe(original, body) if original is not None else None
                if recipe is not None:   # the original is recognised by its words and timings if its bytes change,
                    # and only while its lines are where they were (subs.recognised)
                    recipe["fp"], recipe["positions"] = fingerprint(original), line_positions(original)
                content = json.dumps(recipe) if recipe is not None else None
                if content is None:
                    log.info("public mode: a copy that isn't a re-timing of its original is not kept")
        retry = None
        if outcome == "keep_original":
            retry = now + RETRY_KEEP_ORIGINAL_S
        # written before the lock, put in place with its row: a verdict with no copy removes the copy an earlier
        # verdict of the key made, so no copy file outlives its row or goes uncounted
        with self._temp_copy(key, content) as tmp, self._tx():
            body_bytes = self._place_copy(key, tmp)
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
            elif outcome == "keep_original":
                # judged again and no longer passing (another pass rule, the language check): no longer offered
                self.db.execute("DELETE FROM last_good WHERE key=?", (key,))
                self.db.execute("DELETE FROM offers WHERE key=?", (key,))
        if body_bytes:   # the cap is checked when a copy was added
            self.enforce_cap()
        return key

    def results_of(self, file_key, lang):
        """Every stored result of a file and language: candidate_id, outcome, reason, detail."""
        with self.lock:
            rows = self.db.execute("SELECT candidate_id, outcome, reason, detail FROM results WHERE file_key=? AND lang=?",
                                   (file_key, lang)).fetchall()
        return [{"candidate_id": c, "outcome": o, "reason": r, "detail": json.loads(d or "{}")} for c, o, r, d in rows]

    def last_good(self, file_key, lang):
        with self.lock:
            row = self.db.execute("SELECT key, rank, updated FROM last_good WHERE file_key=? AND lang=?",
                                  (file_key, lang)).fetchone()
        return {"key": row[0], "rank": row[1], "updated": row[2]} if row else None

    def set_offers(self, file_key, lang, keys):
        """Best fit: what is offered for a file and language, best first; the first becomes last_good."""
        now = self.clock()
        with self._tx():
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
                if r and r["outcome"] == "synced" and os.path.exists(self._copy_path(r["key"])):
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

    def file_detail_update(self, file_key, **fields):
        """Adds `fields` to what the read of a stored file found (file_read's detail), keeping the rest."""
        with self.lock:
            row = self.db.execute("SELECT detail FROM files WHERE file_key=?", (file_key,)).fetchone()
            if row is not None:
                self.db.execute("UPDATE files SET detail=? WHERE file_key=?",
                                (json.dumps(dict(json.loads(row[0] or "{}"), **fields)), file_key))

    def file_state(self, file_key):
        """(when the file was read, what its first read found as file_detail gives it), or (None, None)."""
        with self.lock:
            row = self.db.execute("SELECT read_at, detail FROM files WHERE file_key=?", (file_key,)).fetchone()
        return (row[0], json.loads(row[1] or "{}")) if row else (None, None)

    def file_detail(self, file_key):
        """What the first read of a file found (`file_read`'s detail: its tracks, bytes, whether it has a track a
        subtitle can be judged against, its audio byte rate, and the languages first passes left to the audio route,
        "audio_only"), or None."""
        return self.file_state(file_key)[1]

    def file_failed(self, alias, backoff=READ_FAILED_BACKOFF_S):
        """A read failure for a file (by alias, since a file that can't be read has no hash): the waits of `backoff`
        in turn (10 min / 1 h / 6 h / 24 h; REFUSED_BACKOFF_S 1 h / 6 h / 24 h), then never again. They count failures
        in a row (file_ok clears them), each back-off its own. Returns the retry time, or None when it stops."""
        key = alias if backoff == READ_FAILED_BACKOFF_S else alias + OTHER_BACKOFF_SUFFIX
        now = self.clock()
        with self.lock:
            row = self.db.execute("SELECT fails FROM files WHERE file_key=?", (key,)).fetchone()
            fails = (row[0] if row else 0) + 1
            retry = now + backoff[fails - 1] if fails <= len(backoff) else None
            self.db.execute("INSERT OR REPLACE INTO files (file_key, fails, retry_at, read_at) VALUES (?,?,?,?)",
                            (key, fails, retry if retry is not None else float("inf"), now))
        return retry

    def file_ok(self, *aliases):
        """A read of the file succeeded: the failures counted under these aliases no longer count."""
        keys = [key for alias in aliases for key in (alias, alias + OTHER_BACKOFF_SUFFIX)]
        with self.lock:
            self.db.execute(f"DELETE FROM files WHERE fails>0 AND file_key IN ({','.join('?' * len(keys))})", keys)

    def file_retry(self, alias):
        """When a file whose reads failed may be read again (inf: never), or None."""
        with self.lock:
            row = self.db.execute("SELECT MAX(retry_at) FROM files WHERE file_key IN (?,?) AND fails>0",
                                  (alias, alias + OTHER_BACKOFF_SUFFIX)).fetchone()
        return row[0]

    def file_blocked(self, alias):
        retry = self.file_retry(alias)
        return retry is not None and retry > self.clock()

    def marks_asked(self, *aliases):
        """A request for a file met its marks (its read failures, a refusal's, its byte cap; by alias, None left out):
        each is kept 180 days from now (expire), so a file still asked for isn't read again."""
        aliases = [a for a in aliases if a]
        keys = [key for alias in aliases for key in (alias, alias + OTHER_BACKOFF_SUFFIX)]
        now = self.clock()
        with self._tx():
            self.db.execute(f"UPDATE files SET read_at=? WHERE fails>0 AND file_key IN ({','.join('?' * len(keys))})",
                            (now, *keys))
            self.db.execute(f"UPDATE over_cap SET at=? WHERE alias IN ({','.join('?' * len(aliases))})",
                            (now, *aliases))

    # -- a file over the job's byte cap -------------------------------------------------------------
    def cap_reached(self, alias, cap):
        """A job of the file (by alias, since its index wasn't read) stopped at this byte cap. A new table, so an older
        image still opens the store."""
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO over_cap VALUES (?,?,?)", (alias, int(cap), self.clock()))

    def over_cap(self, alias):
        """The byte cap a job of the file stopped at, or None."""
        with self.lock:
            row = self.db.execute("SELECT cap FROM over_cap WHERE alias=?", (alias,)).fetchone()
        return row[0] if row else None

    # -- the original kept for a file and language with nothing passing ------------------------------------
    def kept_original_put(self, file_key, lang, candidate_id, measure=None):
        """The original a file's "<Language> (original)" entry serves: a candidate id (None: every subtitle of the
        language is in another language) and its measure (its share of lines within 200 ms as it is; None for the
        likeliest by name). A new table, so an older image still opens the store."""
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kept_original VALUES (?,?,?,?,?)",
                            (file_key, lang, candidate_id, measure, self.clock()))

    def kept_original(self, file_key, lang):
        """{"candidate_id", "measure", "at"} or None."""
        with self.lock:
            row = self.db.execute("SELECT candidate_id, measure, at FROM kept_original WHERE file_key=? AND lang=?",
                                  (file_key, lang)).fetchone()
        return {"candidate_id": row[0], "measure": row[1], "at": row[2]} if row else None

    # -- Usenet: a file not found there ---------------------------------------------------------------------------
    def usenet_missed(self, key):
        """A file not found in a Usenet listing, by a key that names neither the person nor the file (worker.UsenetGate),
        so it lasts through restarts and stopped workers. Entries older than two days go."""
        now = self.clock()
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO usenet_misses VALUES (?,?)", (key, now))
            self.db.execute("DELETE FROM usenet_misses WHERE at < ?", (now - USENET_MISSES_KEPT_S,))

    def usenet_recently_missed(self, key, within):
        with self.lock:
            row = self.db.execute("SELECT at FROM usenet_misses WHERE key=?", (key,)).fetchone()
        return bool(row and self.clock() - row[0] < within)

    # -- which languages were judged for a file -------------------------------------------------------------------
    def judged_put(self, file_key, langs, rule):
        now = self.clock()
        with self._tx():
            for lang in langs:
                self.db.execute("INSERT OR REPLACE INTO judged VALUES (?,?,?,?)", (file_key, lang, rule, now))

    def judged_all(self, file_key, langs, rule, since):
        """Whether every one of langs was judged for the file under this rule at `since` or later. `rule`: the rule, or a
        test of a recorded rule (origlang.current). None: the file has no judged rows at all (stored before this memory
        existed; the file's read time decides, as before)."""
        with self.lock:
            rows = dict((lang, (r, at)) for lang, r, at in self.db.execute(
                "SELECT lang, rule, at FROM judged WHERE file_key=?", (file_key,)))
        if not rows:
            return None
        ok = rule if callable(rule) else (lambda r: r == rule)
        return all(lang in rows and ok(rows[lang][0]) and rows[lang][1] >= since for lang in langs)

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

    def audio_judged(self, file_key, langs):
        """Whether every one of langs has a verdict of the audio route for the file (a copy, or the original kept)."""
        with self.lock:
            have = {row[0] for row in self.db.execute(
                "SELECT DISTINCT lang FROM results WHERE file_key=? AND route='audio'", (file_key,))}
        return all(lang in have for lang in langs)

    def drop_rows_of(self, rows_key):
        """Public mode, when a person's data is deleted: their own rows (keys "p:<rows_key>:", users.rows_key), the
        files they wait to pick, their original subtitles and the refusals of their reads, go at once.
        Returns how many went."""
        mine = f"p:{rows_key}:%"       # a hex key: nothing in it that LIKE reads as a pattern
        with self._tx():
            return sum(self.db.execute(f"DELETE FROM {table} WHERE {column} LIKE ?", (mine,)).rowcount
                       for table, column in (("pickable", "alias"), ("originals", "alias"), ("files", "file_key")))

    def drop_shared_waiting(self):
        """Public mode: waiting files and their original subtitles are each person's own (keys starting "p:");
        rows an earlier version kept by the file alone, shared by everyone, are deleted. Returns how many went."""
        with self.lock:
            gone = sum(self.db.execute(f"DELETE FROM {table} WHERE alias NOT LIKE 'p:%'").rowcount
                       for table in ("pickable", "originals"))
        return gone

    def originals_put(self, alias, langs):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO originals VALUES (?,?,?)", (alias, json.dumps(langs), self.clock()))

    def originals_get(self, alias):
        with self.lock:
            row = self.db.execute("SELECT langs FROM originals WHERE alias=? AND updated>?",
                                  (alias, self.clock() - KEEP_PICKABLE_S)).fetchone()
        return json.loads(row[0]) if row else None

    # -- counters per UTC day (the budget table; its kinds are in the module's docstring) --------------------------
    def day_add(self, kind, n):
        with self.lock:
            self.db.execute("INSERT INTO budget VALUES (?,?,?) ON CONFLICT(day, kind) DO UPDATE SET bytes=bytes+?",
                            (day_of(self.clock()), kind, n, n))

    def day_count(self, kind):
        with self.lock:
            row = self.db.execute("SELECT bytes FROM budget WHERE day=? AND kind=?", (day_of(self.clock()), kind)).fetchone()
        return row[0] if row else 0

    # -- housekeeping ------------------------------------------------------------------------------------------
    def copy_bytes(self):
        """The synced copies' bytes: what the cap (SUBSYNC_STORE_CAP_MB) counts."""
        with self.lock:
            return self.db.execute("SELECT COALESCE(SUM(body_bytes),0) FROM results").fetchone()[0]

    def database_bytes(self):
        """The database's files, its write-ahead log included. Not counted against the cap: deleting copies
        never shrinks them; what nobody used for 180 days is deleted instead (expire)."""
        return sum(os.path.getsize(os.path.join(self.root, f)) for f in os.listdir(self.root)
                   if f.startswith(DATABASE) and os.path.isfile(os.path.join(self.root, f)))

    def _drop(self, key):
        with self._tx():
            self._remove_copy(key)
            self.db.execute("DELETE FROM results WHERE key=?", (key,))
            self.db.execute("DELETE FROM last_good WHERE key=?", (key,))
            self.db.execute("DELETE FROM offers WHERE key=?", (key,))

    def enforce_cap(self):
        """Least recently served synced copies go first until the copies are under the cap: at once, as many as cover
        what they are over by. The database isn't counted: deleting copies can't shrink it, so if it alone
        grows past the cap, that is logged once and no copy goes for it."""
        with self.lock:
            if not self.database_over_cap_said and self.database_bytes() > self.cap:
                self.database_over_cap_said = True
                log.warning("the store's database alone is larger than SUBSYNC_STORE_CAP_MB (%s MB); copies aren't "
                            "deleted for it", f"{self.cap / MB:.10g}")
            while (over := self.copy_bytes() - self.cap) > 0:
                keys, freed = [], 0
                with contextlib.closing(self.db.execute(
                        "SELECT key, body_bytes FROM results WHERE body_bytes>0 ORDER BY last_served")) as rows:
                    for key, size in rows:
                        keys.append(key)
                        freed += size
                        if freed >= over:
                            break
                if not keys:
                    break
                with self._tx():
                    for key in keys:
                        self._drop(key)

    def expire(self):
        now = self.clock()
        with self._tx():
            old = [r[0] for r in self.db.execute(
                "SELECT key FROM results WHERE outcome IN ('synced','in_sync') AND last_served < ?", (now - KEEP_SERVED_S,))]
            for key in old:
                self._drop(key)
            self.db.execute("DELETE FROM results WHERE outcome='keep_original' AND retry_at < ?", (now,))
            self.db.execute("DELETE FROM budget WHERE day < ?", (day_of(now - 40 * DAY),))
            self.db.execute("DELETE FROM pickable WHERE created < ?", (now - KEEP_PICKABLE_S,))
            self.db.execute("DELETE FROM originals WHERE updated < ?", (now - KEEP_PICKABLE_S,))
            self.db.execute("DELETE FROM judged WHERE at < ?", (now - 2 * JUDGED_FOR_S,))
            self.db.execute("DELETE FROM kept_original WHERE at < ?", (now - RETRY_KEEP_ORIGINAL_S,))
            self.db.execute("DELETE FROM title_language WHERE until IS NOT NULL AND until < ?", (now,))
            # a file nobody asked for in 180 days and with no result left: its names and details; and the marks no
            # request met in as long
            cutoff = now - KEEP_SERVED_S
            self.db.execute("DELETE FROM aliases WHERE file_key IN (SELECT file_key FROM aliases GROUP BY file_key "
                            "HAVING MAX(updated) < ? EXCEPT SELECT file_key FROM results)", (cutoff,))
            self.db.execute("DELETE FROM files WHERE read_at < ? AND file_key NOT IN (SELECT file_key FROM aliases) "
                            "AND file_key NOT IN (SELECT file_key FROM results)", (cutoff,))
            self.db.execute("DELETE FROM over_cap WHERE at < ?", (cutoff,))
        return len(old)

    # -- titles' original languages (origlang.py): codes only, shared by everyone ------------------------------
    def title_language(self, title, now):
        """A title's kept answer while it is current (a found one with no expiry always), else None."""
        with self.lock:
            row = self.db.execute("SELECT answer FROM title_language WHERE title=? AND (until IS NULL OR until>?)",
                                  (title, now)).fetchone()
        try:
            return json.loads(row[0]) if row else None
        except ValueError:
            return None

    def title_language_put(self, title, answer, at, until):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO title_language VALUES (?,?,?,?)",
                            (title, json.dumps(answer, sort_keys=True), at, until))

    def title_languages_expiring(self, keep_s, expires):
        """Gives each answer kept with no expiry that `expires(answer)` picks an expiry `keep_s` after it was kept:
        answers kept before their source had a time limit (origlang.TMDB_KEEP_S). One that has expired is then gone."""
        with self._tx():
            rows = self.db.execute("SELECT title, answer, at FROM title_language WHERE until IS NULL").fetchall()
            for title, answer, at in rows:
                try:
                    picked = expires(json.loads(answer))
                except ValueError:
                    continue
                if picked:
                    self.db.execute("UPDATE title_language SET until=? WHERE title=?", (at + keep_s, title))

    def title_languages_clear(self):
        """Deletes every kept answer (the lookup switched off)."""
        with self.lock:
            self.db.execute("DELETE FROM title_language")

    def stats(self):
        with self.lock:
            rows = self.db.execute("SELECT outcome, COUNT(*) FROM results GROUP BY outcome").fetchall()
        return {"results": dict(rows), "bytes": self.copy_bytes(), "database_bytes": self.database_bytes(),
                "cap": self.cap}
