"""What AIOSubtitles remembers about the subtitle sources it asks itself.

Health: a source that fails PAUSE_FAILURES times within PAUSE_WINDOW_S (time-outs, server errors) is skipped for
PAUSE_S, then tried with one request: a success opens it again, a failure pauses it again. A site's own "too many
requests" (429, 503 with Retry-After) pauses it for as long as it asks. "Not found" is an answer, not a failure.

Ledger: requests and downloads per source and clock window (minute, hour, UTC day), against the sites' published limits
(srclimits.LIMITS), kept in SQLite so a restart doesn't forget them. In public mode it also counts per person, so that
near a limit the people who already had their share get the site only for the best candidates.
"""
import logging
import os
import sqlite3
import threading
import time

from . import srclimits

log = logging.getLogger("subsync.sources")


class Health:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.failures = {}       # source -> [times]
        self.paused_until = {}   # source -> time
        self.probing = set()     # sources whose one trial request is out

    def allowed(self, source):
        """May the source be asked now? After a pause, one request is let through (the trial)."""
        now = self.clock()
        with self.lock:
            until = self.paused_until.get(source)
            if until is None:
                return True
            if now < until or source in self.probing:
                return False
            self.probing.add(source)
            return True

    def ok(self, source):
        with self.lock:
            was = source in self.paused_until
            self.failures.pop(source, None)
            self.paused_until.pop(source, None)
            self.probing.discard(source)
        if was:
            log.info("source %s answers again", source)

    def failed(self, source, why=""):
        now = self.clock()
        with self.lock:
            trial = source in self.probing
            self.probing.discard(source)
            times = [t for t in self.failures.get(source, []) if now - t < srclimits.PAUSE_WINDOW_S] + [now]
            self.failures[source] = times
            if trial or len(times) >= srclimits.PAUSE_FAILURES:
                self.paused_until[source] = now + srclimits.PAUSE_S
                self.failures[source] = []
                pause = True
            else:
                pause = False
        if pause:
            log.info("source %s paused for %d min (%s)", source, srclimits.PAUSE_S // 60, why or "failures")

    def limited(self, source, retry_after=None):
        """The site said "too many requests": paused for its Retry-After (at least a minute, at most an hour)."""
        try:
            wait = float(retry_after) if retry_after is not None else srclimits.PAUSE_S
        except (TypeError, ValueError):
            wait = srclimits.PAUSE_S
        wait = min(max(wait, 60.0), srclimits.RETRY_AFTER_MAX_S)
        with self.lock:
            self.probing.discard(source)
            self.paused_until[source] = max(self.paused_until.get(source, 0), self.clock() + wait)
        log.info("source %s asked to slow down: paused for %d s", source, wait)

    def status(self):
        """{source: time the pause ends} for the paused sources, for a status line."""
        now = self.clock()
        with self.lock:
            return {s: t for s, t in self.paused_until.items() if t > now}


class Ledger:
    """Counts per source, kind ("request" or "download") and window; `who` (public mode) is an opaque per-person key,
    never logged."""

    def __init__(self, path=None, clock=time.time, keyed=None):
        self.clock = clock
        self.keyed = dict(keyed or {})       # source -> has a key (picks the limits that apply)
        self.lock = threading.Lock()
        self.swept = 0.0
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path or ":memory:", check_same_thread=False, isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS counts (source TEXT NOT NULL, kind TEXT NOT NULL, span INTEGER NOT NULL,"
                        " start INTEGER NOT NULL, who TEXT NOT NULL DEFAULT '', n INTEGER NOT NULL,"
                        " PRIMARY KEY (source, kind, span, start, who))")

    def limits(self, source, kind):
        return srclimits.LIMITS.get((source, kind, bool(self.keyed.get(source))), [])

    def _windows(self, source, kind):
        now = int(self.clock())
        return [(span, limit, now - now % span) for span, limit in self.limits(source, kind)]

    def count(self, source, kind, n=1, who=None):
        wins = self._windows(source, kind)
        if not wins:
            return
        with self.lock:
            for span, _, start in wins:
                for w in ("", who or "") if who else ("",):
                    self.db.execute("INSERT INTO counts VALUES (?,?,?,?,?,?) ON CONFLICT(source, kind, span, start, who) "
                                    "DO UPDATE SET n = n + excluded.n", (source, kind, span, start, w, n))
            if self.clock() - self.swept > 3600:   # once an hour: windows that ended more than a day ago are dropped
                self.swept = self.clock()
                self.db.execute("DELETE FROM counts WHERE start < ?", (int(self.clock()) - 2 * 86400,))

    def used(self, source, kind, span, who=""):
        start = int(self.clock()) - int(self.clock()) % span
        with self.lock:
            row = self.db.execute("SELECT n FROM counts WHERE source=? AND kind=? AND span=? AND start=? AND who=?",
                                  (source, kind, span, start, who or "")).fetchone()
        return row[0] if row else 0

    def exhausted(self, source, kind):
        """No room left in some window: the source isn't used for this kind until the window ends."""
        return any(self.used(source, kind, span) >= limit for span, limit, _ in self._windows(source, kind))

    def tight(self, source, kind, who=None):
        """Near a limit (srclimits.FAIR_SHARE_AT): True when this person (public mode) already used more than their
        share, or, without a person, as soon as the limit is near."""
        for span, limit, start in self._windows(source, kind):
            used = self.used(source, kind, span)
            if used < srclimits.FAIR_SHARE_AT * limit:
                continue
            if not who:
                return True
            with self.lock:
                people = self.db.execute("SELECT COUNT(*) FROM counts WHERE source=? AND kind=? AND span=? AND start=? "
                                         "AND who != ''", (source, kind, span, start)).fetchone()[0]
            share = max(limit / max(people, 1), srclimits.MIN_SHARE * limit)
            if self.used(source, kind, span, who) >= share:
                return True
        return False
