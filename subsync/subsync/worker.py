"""The queue and the worker: one file at a time per worker, plus one audio sync beside it when the audio route is on
(two lanes: the main lane reads indexes and text and image tracks, the audio lane runs audio syncs, and a file is never
in both at once). In public mode every person's worker shares one Turn with SUBSYNC_SYNCS_AT_ONCE places, an audio sync
also takes one of the audio Turn's SUBSYNC_AUDIO_AT_ONCE places, and never two workers work on the same file at once.

A job is a file a viewer opened or played. A subtitle request that names the file (its name, size or hash, as Stremio
and AIOStreams send them) queues it; the file is then found in subsync's own AIOStreams listing (cached debrid only),
and, with Usenet on and only when that listing doesn't have it, in the Usenet config's listing (UsenetGate: a few a
day; a file not found there isn't looked up again for a day, a found one's row is reused for an hour; none after an
error).
Reads count toward a daily budget (SUBSYNC_DAILY_BUDGET_GB, bytes received). Any HTTP 403/429 stops every read for a
back-off (SUBSYNC_403_BACKOFF_S, doubling up to BACKOFF_MAX_S), since the refusal is most likely per account. Every
connection is closed after its read (reader.py).
"""
import contextlib
import hashlib
import http.client
import logging
import threading
import time
import urllib.error
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout

from . import audiofit, choose, langcheck, pipeline, realdebrid, srclimits
from . import origlang
from .langs import DEFAULT_LANGUAGE
from .langs import name as lang_name
from .mkv import NotMatroska
from .mp4 import open_index
from .reader import MAX_RE_RESOLVE, CapReached, IndexTooLarge, RangeReader, ReadFailed, Refused, Stopped
from .sources import bt_alias, expected_size, fetch_subtitle, plausible_size, same_hash
from .store import DAY, JUDGED_FOR_S, READ_FAILED_BACKOFF_S, REFUSED_BACKOFF_S, result_key
from .subs import NO_DIALOGUE, decode, fingerprint, is_ass, parse, recognised

log = logging.getLogger("subsync.worker")
UNLISTED_S = 3600   # a file the listing didn't have (uncached, or another release) isn't looked for again this long
USENET_ROW_KEPT_S = 3600   # a file found on Usenet: a retry or its audio job reuses its row and address this long
PICKED_EARLY_S = 3600   # an entry downloaded while the file's first pass ran still counts as its pick this long
LIST_TTL_S = 600    # a text pass's subtitle list and downloads, reused by the audio job that follows a pick
EXPIRE_EVERY_S = 3600   # the store's old rows are removed this often (Store.expire; in public mode by __main__)
IDLE_POLL_S = 5.0   # an idle lane looks at the queue again this often (a job deferred by not_before comes due)
BACKOFF_MAX_S = 6 * 3600   # the refusal back-off doubles up to this (Safety)
READ_RETRY_S = 10   # a first read that got no answer is tried once more this much later
FETCHED_CAP = 32 * 1024 * 1024   # downloads kept for the audio job that follows a pick (worker._fetch_one), in bytes
# In-memory maps of a worker (in public mode, of a person): entries kept, the oldest dropped first (put_bounded)
SEEN_ENTRIES = 2000         # files asked about (seen)
PICKS_ENTRIES = 2000        # files waiting for a pick, and picks that came before their file was known to wait
RESULTS_ENTRIES = 500       # files' results, kept in step: done, judged and originals
GUESSES_ENTRIES = 500       # a waiting file's anchored guesses (provisional) and its originals' measures
UNLISTED_ENTRIES = 5000     # files the listing didn't have
USENET_ROWS_ENTRIES = 200   # files found on Usenet
LISTS_ENTRIES = 200         # files' subtitle lists
FETCH_AT_ONCE = 4   # a language's subtitle downloads at once: one round (a picked language's alass after it)
CONSENSUS_TRIES = 12   # a file with no track: the main language's downloads beside its guess, for a consensus
# Originals downloaded (and subtitle lists fetched again) on a request's own time (Worker.original_for) at once, in one
# pool for the worker's life: more than one person's player asks for at a time; one that outlives its request ends
# there and is kept (fetched_subs, lists)
ORIGINAL_DOWNLOADS_AT_ONCE = 8
# Usenet: fixed rules, explained in explain.RULES
USENET_UNLISTED_S = 24 * 3600     # a file not found on Usenet isn't looked up there again for a day
USENET_FIRST_READ_S = 120         # its first read may wait this long (the Usenet service opens the NZB first)
USENET_MAX_RE_RESOLVE = 1         # its link is asked at most twice per job (a fresh look-up could fetch the NZB again)
# The audio route's room check: a file's audio sync read up to 2.44 times its planned windows in a real test
# (each subtitle tried places its own windows), so a pick needs room for this many times them, at most the
# per-file audio limit
AUDIO_NEED_MARGIN = 2.5
# Why every read stops for a while (Stopped): the refusal back-off (Safety), or the day's budget spent
BACKING_OFF = "backing off after a refusal"
BUDGET_SPENT = "daily budget spent"


def put_bounded(d, key, value, limit):
    """d[key] = value as the newest entry of a map kept to `limit` entries, the oldest dropped first: a key set again
    moves to the end (a dict keeps a key set again in its first place, so the file just asked about would go first)."""
    d.pop(key, None)
    d[key] = value
    trim_oldest(d, limit)


def trim_oldest(d, limit):
    """Drops a map's oldest entries (a dict keeps insertion order) past `limit`."""
    while len(d) > limit:
        d.pop(next(iter(d)))


def plain_address(url):
    """An http(s) address with a host, in ASCII with no space or control character (as http.client sends one): it
    can go out as it is in a redirect's Location header, where a line break would start a header of its own."""
    if not url.isascii() or any(c <= " " or c == "\x7f" for c in url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:   # e.g. an unclosed "[" in its host
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def usenet_size_ok(listed):
    """The rule a Usenet file's served size must meet: AIOStreams lists a release by its whole NZB (the video and its
    par2 and other files), so the file it serves is a few per cent smaller (3.79 GB served for 4.10 GB
    listed); at least half the listed size and not more than it, so a small error video (AIOStreams answers a release
    it can't open with a 19,856-byte download_failed.mp4) or another file is still refused. With no size listed, any
    size a video file can have (sources.plausible_size), so the error video is refused then too."""
    if not plausible_size(listed):
        return plausible_size
    return lambda total: plausible_size(total) and listed * 0.5 <= total <= listed


def audio_facts(index):
    """What an audio sync of a file could read depends on: its average byte rate and how many audio tracks
    the fit may try; None when the index doesn't tell."""
    try:
        duration = index.duration()
        rate = (getattr(index, "size", 0) or 0) / duration if duration else 0
    except Exception:   # an index without a duration: the whole audio limit is assumed
        return None
    if not rate:
        return None
    try:
        tracks = audiofit.most_tracks(index, getattr(index, "original_language", None))
    except Exception:
        tracks = 2
    return {"rate": round(rate), "tracks": tracks}


class StoreUsenetCounts:
    """The Usenet listings and the day's stop of a service for one viewer (./setup's install), in the store's day
    counters. Public mode counts per person in the users' records instead (public.UserLimits)."""

    def __init__(self, store):
        self.store = store

    def listings(self):
        return self.store.day_count("usenet")

    def add_listing(self):
        self.store.day_add("usenet", 1)

    def stopped(self):
        return self.store.day_count("usenet_stop") > 0

    def stop(self):
        if not self.stopped():
            self.store.day_add("usenet_stop", 1)


class StoreAudioCounts:
    """The audio syncs a service for one viewer (./setup's install) started today, in the store's day counters: at most
    SUBSYNC_USER_AUDIO_PER_DAY, and of those, started as soon as a file was opened, SUBSYNC_USER_OPENED_PER_DAY. Public
    mode counts per person in the users' day table instead (public.UserLimits)."""

    def __init__(self, store, settings):
        self.store, self.s = store, settings

    def may_start_audio(self, opened=False):
        if self.store.day_count("audio_syncs") >= self.s.user_audio_per_day:
            return False
        return not opened or self.store.day_count("audio_opened") < self.s.user_opened_per_day

    def audio_started(self, opened=False):
        """A picked sync counts one audio sync; an opened one counts one of each."""
        self.store.day_add("audio_syncs", 1)
        if opened:
            self.store.day_add("audio_opened", 1)


class UsenetGate:
    """Whether a Usenet listing may be made now: at most `per_day` a UTC day (0: none), none for the
    rest of the day once a Usenet listing or read failed (an indexer's error or limit, or one AIOSubtitles can't tell
    from it), none for a file not found there in the last 24 h. `counts`: listings(), add_listing(), stopped(), stop();
    `who`: an opaque key of the person (public mode), so one person's misses don't stop another's look-ups."""

    def __init__(self, store, per_day, counts, who=""):
        self.store, self.per_day, self.counts, self.who = store, per_day, counts, who or ""
        self._lock = threading.Lock()   # both lanes of a worker list through its one gate

    @staticmethod
    def key_of(who, alias):
        return hashlib.sha256(f"usenet|{who}|{alias}".encode()).hexdigest()[:32]

    def _shut(self, alias):
        """No listing for this file now (lock held)."""
        return self.per_day <= 0 or self.counts.stopped() or self.counts.listings() >= self.per_day \
            or self.store.usenet_recently_missed(self.key_of(self.who, alias), USENET_UNLISTED_S)

    def may_take(self, alias):
        """Whether a listing for this file could be taken now, nothing counted: asked before the work a listing needs
        first (the config's check), which take then repeats."""
        with self._lock:
            return not self._shut(alias)

    def take(self, alias):
        """A listing for this file, if one may be made now: checked and counted in one step, so two lanes can't both
        take the day's last one."""
        with self._lock:
            if self._shut(alias):
                return False
            self.counts.add_listing()
            return True

    def missed(self, alias):
        self.store.usenet_missed(self.key_of(self.who, alias))

    def stop(self, why):
        self.counts.stop()
        log.info("Usenet stopped until 00:00 UTC: %s", why)


class UsenetFinder:
    """A file's row in the Usenet config's listing, for a file the sync config's listing doesn't have:
    listed only when the gate allows one. A file found is kept USENET_ROW_KEPT_S with the address its first read
    resolved, so a retry or its audio job lists nothing again and reads where its first pass did (the NZB isn't
    fetched again). `config`: the Usenet config's sources.Aios; `gate`: its UsenetGate. Both lanes of a worker find and
    keep rows through its one finder, under the finder's lock."""

    def __init__(self, config, gate):
        self.config, self.gate = config, gate
        self.rows = {}   # alias -> (when, row, address) of a file found on Usenet
        self.lock = threading.Lock()

    def _kept(self, alias):
        """The file's (when, row, address), found within USENET_ROW_KEPT_S; else None."""
        with self.lock:
            kept = self.rows.get(alias)
        return kept if kept and time.time() - kept[0] < USENET_ROW_KEPT_S else None

    def audio_row(self, job):
        """An audio job's Usenet row and the address its first pass resolved, kept with the job for its life (a
        job stopped and queued again keeps them), taken from the first pass's within USENET_ROW_KEPT_S; None
        otherwise."""
        if job.kept is None:
            kept = self._kept(job.alias)
            if kept is None:
                return None
            job.kept = kept[1:]
        return job.kept[0]

    def keep_address(self, alias, row, address):
        """The address a Usenet file's link pointed to, kept with its row for the audio job that follows."""
        with self.lock:
            kept = self.rows.get(alias)
            if address and kept and kept[1] is row:
                self.rows[alias] = (kept[0], row, address)   # the same entry: its time and place stay

    def find(self, job, hints):
        """The played file's row in one Usenet listing, when the gate allows one: the listing is counted once the
        config is checked; an error row, an HTTP error or no answer stops Usenet for the day and nothing is read; a
        file not listed isn't looked up there again for a day."""
        kept = self._kept(job.alias)
        if kept is not None:     # a retry, or the same file asked for again
            return kept[1]
        if not self.gate.may_take(job.alias):
            return None
        try:
            # the config first: one that can't be checked lists nothing, so no listing of the day is spent on it
            self.config.check_config()
        except realdebrid.Blocked:
            return None
        if not self.gate.take(job.alias):
            return None
        try:
            listed = self.config.streams(job.type, job.video_id)
            error_row = any((s.get("streamData") or {}).get("type") == "error" for s in listed)
        except realdebrid.Blocked:       # the Usenet config couldn't be checked: nothing was listed
            return None
        except urllib.error.HTTPError as e:
            e.close()
            self.gate.stop(f"HTTP {e.code} from the Usenet listing")
            return None
        # no answer, a cut one (http.client.IncompleteRead is no OSError) or one that isn't a listing (not a JSON
        # object of rows)
        except (OSError, ValueError, http.client.HTTPException, AttributeError, TypeError) as e:
            self.gate.stop(f"the Usenet listing failed ({type(e).__name__})")
            return None
        if error_row:
            self.gate.stop("an error in the Usenet listing")
            return None
        found = self.config.match(listed, hints, usenet=True)
        if found is None:
            self.gate.missed(job.alias)
        else:
            with self.lock:
                put_bounded(self.rows, job.alias, (time.time(), found, None), USENET_ROWS_ENTRIES)
        return found

    def missed(self, alias):
        """The file isn't on Usenet (its listed row is another file): not looked up there again for a day."""
        self.gate.missed(alias)

    def stop(self, why):
        """No Usenet listing until 00:00 UTC (a Usenet file's first read failed or was refused)."""
        self.gate.stop(why)


class Safety:
    """The refusal back-off every read checks first."""

    def __init__(self, backoff_s=900, backoff_max_s=BACKOFF_MAX_S, clock=time.time):
        self.backoff_s, self.backoff_max_s, self.clock = backoff_s, backoff_max_s, clock
        self.refused_until = 0.0
        self.refusals = 0
        self.lock = threading.Lock()

    def refused(self):
        with self.lock:
            self.refusals += 1
            wait = min(self.backoff_s * 2 ** (self.refusals - 1), self.backoff_max_s)
            self.refused_until = self.clock() + wait
            return wait

    def read_ok(self):
        with self.lock:
            self.refusals = 0

    def backing_off(self):
        return self.clock() < self.refused_until


class OverLimit(Exception):
    """A person's own limit is reached (public mode): the job is dropped, not tried again later."""


class BudgetSpent(Stopped):
    """The day's budget can't cover a read (JobGuard): spent for the job until 00:00 UTC, when the day's reads count
    from nothing again; a retry before then only checks that (README: the daily limit spent is a pause)."""


class Turn:
    """Up to `places` holders at once, let in in the order they asked: in public mode every person's worker shares one,
    so the service works on that many files at once for everyone (SUBSYNC_SYNCS_AT_ONCE). `claim(key)` keeps one file
    to one worker at a time, so the same file is never worked on twice at once. A place is never held while waiting
    for a claim: a worker takes its file's claim before its place, and one it needs once it holds a place (the file's
    torrent file, known from its listing) is waited for with its places given back (Worker._claim)."""

    def __init__(self, places=1):
        self.places = max(1, int(places or 1))
        self._cv = threading.Condition()
        self._next = 0          # the next ticket handed out
        self._let_in = 0        # tickets let in so far (the next one to let in)
        self._inside = 0
        self._claims = set()

    def __enter__(self):
        with self._cv:
            ticket = self._next
            self._next += 1
            while ticket != self._let_in or self._inside >= self.places:
                self._cv.wait()
            self._let_in += 1
            self._inside += 1
            self._cv.notify_all()
        return self

    def __exit__(self, *exc):
        with self._cv:
            self._inside -= 1
            self._cv.notify_all()
        return False

    def take(self, key, wait=True):
        """Claims `key`, waiting while another holder has it; with wait=False, only if it is free now. Returns whether
        it is claimed (give it back with release)."""
        with self._cv:
            while key in self._claims:
                if not wait:
                    return False
                self._cv.wait()
            self._claims.add(key)
            return True

    def release(self, key):
        with self._cv:
            self._claims.discard(key)
            self._cv.notify_all()

    @contextlib.contextmanager
    def claim(self, key):
        self.take(key)
        try:
            yield
        finally:
            self.release(key)


def audio_places(places, at_once="auto"):
    """Audio syncs at once in public mode (SUBSYNC_AUDIO_AT_ONCE). auto: half the turn's places, at least
    one, so with two places or more an audio sync, which keeps a processor busy for minutes, never holds every place
    (with 2: one beside the other files); with one place it holds it, and other files wait for it. A number: as given,
    within the places (Settings.conflicts refuses one above them)."""
    places = max(1, int(places or 1))
    if at_once in (None, "auto"):
        return max(1, places // 2)
    return max(1, min(int(at_once), places))


class NoTurn:
    """Self-hosted: one worker and nothing shared, so nothing to wait for."""
    places = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def claim(self, key):
        return contextlib.nullcontext()

    def take(self, key, wait=True):
        return True

    def release(self, key):
        pass


class JobGuard:
    """Checked before every read of a job: the back-off, the day's budget, the job's own byte cap and (public mode) the
    person's own limits. A job reads in parallel (audio windows, line reads), so each read's length stays reserved
    from its before_read to its after_read: a read is checked against the bytes received plus the reads still running,
    and when only those could take it past a limit, it waits for them to end (a read may get less than it asked: a
    window's stream stops once the window is decoded). So the job stops where reading one at a time would have stopped,
    and reads checked at once never pass a limit together."""

    def __init__(self, safety, store, daily_budget, file_cap=None, limits=None):
        self.safety, self.store = safety, store
        self.daily_budget, self.file_cap, self.limits = daily_budget, file_cap, limits
        self.bytes = 0      # received by the job's reads that ended
        self.reserved = 0   # asked by its reads still running
        self._cv = threading.Condition()

    def before_read(self, length):
        with self._cv:
            while True:
                if self.safety.backing_off():
                    raise Stopped(BACKING_OFF)
                try:
                    self._check(self.reserved + length)
                    break
                except (Stopped, OverLimit):   # CapReached is a Stopped
                    if not self.reserved:
                        raise
                self._cv.wait()   # a running read ends: its after_read comes also when it failed (reader.py)
            self.reserved += length

    def _check(self, length):
        """Raises when reading `length` more bytes would pass a limit (cv held)."""
        if self.store.day_count("read") + length > self.daily_budget:
            raise BudgetSpent(BUDGET_SPENT)
        if self.file_cap and self.bytes + length > self.file_cap:
            raise CapReached(self.file_cap)
        if self.limits is not None:
            self.limits.before_read(length)

    def after_read(self, n, asked=None):
        """A read ended with `n` bytes received (when it failed, what arrived before it did:); `asked`: the
        length its before_read reserved (by default n, a read that got what it asked for)."""
        if n:
            self.store.day_add("read", n)
            if self.limits is not None:
                self.limits.after_read(n)
            self.safety.read_ok()
        with self._cv:
            self.bytes += n
            self.reserved = max(0, self.reserved - (n if asked is None else asked))
            self._cv.notify_all()


class Job:
    def __init__(self, type_, video_id, hints=None):
        self.type, self.video_id = type_, video_id
        self.hints = {k: v for k, v in (hints or {}).items() if v}
        self.fetched = {}         # lang -> [(candidate, text)] downloaded for a file that waits for the audio route
        self.not_before = 0.0
        self.audio = False        # may this job use the audio route (set when the viewer picked subsync's entry)
        self.opened = False       # an audio job started as soon as the file was opened, not by a pick
        self.counted = False      # its audio sync is counted in today's (once, also when it is queued again)
        self.listed = ()          # the languages with a subtitle to fit, from the file's subtitle list
        self.rule = None          # the rule its verdicts are recorded under (origlang.rule), set once its file is read
        self.usenet = False       # found through the Usenet config (its audio job too)
        self.kept = None          # an audio job's (row, address) of its file's first pass on Usenet, for its life
        self.bt = None            # the listed row's torrent file ("bt:<hash>:<index>"), once found (cap mark)
        # Languages whose entry was downloaded while this job was queued or running -> when (Worker.prioritize),
        # for alass sooner; they no longer hold the other languages' downloads back
        self.picked = {}
        self.stopped = False      # stopped by the back-off or the day's budget, queued again: its retry checks them
        self.budget_until = 0.0   # stopped by the day's budget: spent for it until then (00:00 UTC, BudgetSpent)
        self.file_counted = False  # counted in the person's new files today (public mode): once, also when queued again
        self.refused_wait = None  # the back-off its refused read started (Worker._refused): once per job
        # The languages its first pass left to the audio route (the file's only track too small for their
        # subtitles), marked in the file's stored detail ("audio_only") for everyone else's first pass
        self.to_audio = set()
        self.places = ()          # the places it works in while it holds them (Worker._places), given back for a wait

    def again(self):
        """A new job of the same file (its type, id and hints), with none of this one's state."""
        return Job(self.type, self.video_id, self.hints)

    @property
    def alias(self):
        h = self.hints
        if h.get("videoHash") and h.get("videoSize"):
            return f"hash:{h['videoHash']}|{h['videoSize']}"
        if h.get("filename") and h.get("videoSize"):
            return f"name:{h['filename']}|{h['videoSize']}"
        if h.get("filename"):
            # a file name without a size still names one file; the title alone would let another release of the
            # same title match this file's result
            return f"name:{h['filename']}|None"
        if h.get("videoHash"):   # so does a hash without a size
            return f"hash:{h['videoHash']}|None"
        # a request that names no file: it queues nothing, so no file is ever linked to a bare title
        return f"title:{self.type}/{self.video_id}"

    @property
    def aliases(self):
        """The aliases the store may know the file by: `alias`, and with a file name its name and size (a file read for
        a request that named it is found again by one that also gives its hash)."""
        h = self.hints
        named = f"name:{h['filename']}|{h.get('videoSize')}" if h.get("filename") else None
        return tuple(dict.fromkeys(a for a in (self.alias, named) if a))

    @property
    def label(self):
        return f"{self.type}/{self.video_id}"


def rewrite(url, rules):
    """Public stream URLs to internal ones (SUBSYNC_URL_REWRITE 'https://public=http://internal,...')."""
    for rule in (rules or "").split(","):
        src, _, dst = rule.partition("=")
        src, dst = src.strip(), dst.strip()
        if src and dst and url.startswith(src):   # a side left empty (or only spaces) is no rule
            return dst + url[len(src):]
    return url


class Worker(threading.Thread):
    def __init__(self, settings, store, aios, safety, vad_factory=None, extra=None, turn=None, limits=None, usenet=None,
                 usenet_gate=None, audio_turn=None, lookup=None):
        super().__init__(daemon=True, name="subsync-worker")
        self.s, self.store, self.aios, self.safety = settings, store, aios, safety
        # Usenet: the Usenet config's sources.Aios and its UsenetGate, both or neither
        self.usenet_finder = UsenetFinder(usenet, usenet_gate) if usenet is not None and usenet_gate is not None \
            else None
        # public mode: the turn every person's worker shares, and this person's limits (public.UserLimits, or
        # public.StoreOnly: who, rows_key, may_queue, may_start_file, file_started, before_read, after_read,
        # has_room, may_start_audio, audio_started); None on a self-hosted install
        self.turn = turn if turn is not None else NoTurn()
        # public mode: the audio syncs' own places, shared by everyone (audio_places), taken before a place of the turn
        self.audio_turn = audio_turn if audio_turn is not None else NoTurn()
        self.limits = limits
        # The audio syncs this viewer may start a day: public mode's person's own limits, a self-hosted
        # install's in the store's day counters
        self.audio_counts = StoreAudioCounts(store, settings) if limits is None else limits
        self.extra = extra          # extra.ExtraSources: sources subsync searches itself
        # origlang.Lookup: titles' original languages (one per process, shared in public mode); None: never looked up
        self.lookup = lookup
        # public mode: an opaque key of this person for the sources' fair shares (sourcestate.Ledger), never logged
        self.who = limits.who if limits is not None else None
        # public mode: the key of this person's own rows in the shared store (users.rows_key, _person_key); and once
        # their data is deleted (stop(forget=True)), nothing more of theirs is written there
        self.rows_key = limits.rows_key if limits is not None else None
        self.forgotten = False
        self.vad_factory = vad_factory
        self._vad = None
        self.queue, self.jobs = [], {}   # jobs in arrival order; alias -> queued job
        self.cv = threading.Condition()
        self.current = None       # the job the main worker runs (index reads, text and image routes)
        self.current_audio = None  # the job the audio worker runs, so an audio route never holds another file
        self.audio_worker = None   # the audio lane's thread, once start() opened one (start)
        self.done = {}            # alias -> {lang: result key | None}: the best pass so far, for pending entries and picks
        self.judged = {}          # alias -> languages whose every candidate is judged (their `done` key is final)
        self.originals = {}       # alias -> {lang: {"url", "id"} of the original subtitle}, the pending entries' fallback
        self.measures = {}        # alias -> {lang: {candidate id: share within 200 ms as it is}}
        self.provisional = {}     # alias -> {lang: body}: a waiting file's other languages, anchored to the main guess
        self.lists = {}           # alias -> (time, candidates): a text pass's subtitle list, for the audio job that follows
        self.by_id = {}           # (candidate id, language) -> candidate, from this person's lists (public mode's recipes)
        self.fetched_subs = {}    # subtitle url -> (time, bytes): downloads shared by a file's text pass and audio job
        self.fetching = {}        # subtitle url -> Future of its download running now (_fetch_one): waited for, once
        self.unlisted = {}        # alias -> when a file was last not found in the listing
        self.pickable = {}        # alias -> job of a file that needs the audio route and waits for the viewer's pick
        self.picked_early = {}    # alias -> when its entry was downloaded before the file was known to need audio
        self.seen = {}            # alias -> (type, video_id, hints) of its last job, so a pick can ask for the list again
        self.stop_flag = False
        self.last_expire = 0.0
        self.vad_lock = threading.Lock()
        self.audio_lock = threading.Lock()     # one audio fit at a time (shared reader)
        # whether a file waits for a pick or its audio job starts (_needs_pick) is decided one at a time: a job's
        # languages, judged in parallel, may each come to it
        self.pick_lock = threading.Lock()
        # the originals' downloads and subtitle lists on a request's own time (original_for), timed out there; threads
        # start when needed
        self.original_pool = ThreadPoolExecutor(max_workers=ORIGINAL_DOWNLOADS_AT_ONCE, thread_name_prefix="subsync-original")

    @property
    def usenet(self):
        """The Usenet config's sources.Aios, or None when Usenet is off for this worker."""
        return self.usenet_finder.config if self.usenet_finder is not None else None

    # -- queue -------------------------------------------------------------------------------------------------
    def remember(self, job):
        """A file this person asked about: its subtitle list can be fetched again for them (original_text, a pick).
        Also when the answer came from stored copies alone and no job runs."""
        with self.cv:
            put_bounded(self.seen, job.alias, (job.type, job.video_id, dict(job.hints)), SEEN_ENTRIES)

    def submit(self, job):
        """Queues a file's job: the job that gives its results (this one, or the file's job queued or running already),
        or None when this person's queue is full or the worker has stopped."""
        with self.cv:
            return self._submit(job)

    def _submit(self, job):
        """submit (cv held), so a caller can queue a job and change its own state in the same step."""
        self.remember(job)
        old = self.jobs.get(job.alias)
        if old is not None:
            old.audio = old.audio or job.audio
            return old
        running = self._running(job.alias)
        if running is not None and (running.audio or not job.audio):
            return running
        if self.stop_flag or not self._may_queue():
            return None   # this person's queue is full (the answer comes from the store only), or the worker stopped
        self.jobs[job.alias] = job
        self.queue.append(job)
        # a wait ends on this job's results, not an earlier job's: a pick waits for its final ones, and a pending wait
        # for its first pass (an earlier job's placeholders are dropped; a key it published is served meanwhile)
        self.judged.pop(job.alias, None)
        passes = {lang: key for lang, key in self.done.pop(job.alias, {}).items() if key is not None}
        if passes:
            self.done[job.alias] = passes
        self.cv.notify_all()
        return job

    def audio_allowed(self, job):
        """May this job read the file's audio: an audio job (picked, or started as soon as the file was opened)
        of a viewer whose choice isn't off, a Usenet file's too; every first pass reads the index only."""
        return self.s.audio_choice != "off" and bool(job.audio)

    def _cap(self, job):
        """The most a job reads of its file (JobGuard's byte cap): the audio route's per-file cap, else the index cap."""
        return self.s.file_cap if self.audio_allowed(job) else self.s.index_cap

    def capped(self, job, bt=None):
        """A job of this file stopped at a byte cap at least as large as the one it would read under now:
        the file isn't read again and keeps its original subtitles until the cap is raised. `bt`: the listed row's
        torrent file, marked too, so the same file asked under another name is known by it."""
        cap = self._cap(job)
        over = [c for c in (self.store.over_cap(a) for a in (job.alias, bt) if a) if c is not None]
        return bool(cap) and any(cap <= c for c in over)

    def _lister(self):
        """The config subtitle lists come through: the sync config, else (a setup with none) the Usenet one."""
        if getattr(self.aios, "base", True) or self.usenet is None:
            return self.aios
        return self.usenet

    def _person_key(self, alias):
        """In public mode, the store's key of a file for this person alone, under the key of their own rows
        (users.rows_key: only their personal link leads to it), so Delete removes them all; None on a
        self-hosted install and for a public worker without a person's key."""
        if not (self.s.public and self.rows_key):
            return None
        return f"p:{self.rows_key}:" + hashlib.sha256(f"{self.rows_key}|{alias}".encode()).hexdigest()[:32]

    def _row_key(self, alias):
        """The store's key for a file this person waits to pick and its original subtitles: the file itself on a
        self-hosted install; in public mode the person's own (_person_key), so nobody's waiting files or subtitle links
        reach anyone else, and None (memory only) for a public worker without a person's key, or once the person's
        data is deleted (stop(forget=True)): a job still running then writes nothing more of theirs."""
        if not self.s.public:
            return alias
        return None if self.forgotten else self._person_key(alias)

    def _restore(self, alias):
        """After a restart: a file that waited for a pick, and its original subtitles, from the store (cv held)."""
        key = self._row_key(alias)
        if key is None:
            return
        if alias not in self.pickable:
            row = self.store.pickable_get(key)
            if row:
                self.pickable[alias] = Job(row["type"], row["video_id"], row["hints"])
        if alias not in self.originals:
            langs = self.store.originals_get(key)
            if langs is not None:
                self.originals[alias] = langs

    def waits_for_pick(self, alias):
        with self.cv:
            self._restore(alias)
            return alias in self.pickable

    def ask_list_again(self, alias):
        """A pick for a file whose subtitle list is not known (it came back empty): queue its job again, which
        fetches the list first; not for a file the listing didn't have within UNLISTED_S (it isn't looked for again,
        as for a subtitle request). Returns True if a job is queued or running for it."""
        with self.cv:
            seen = self.seen.get(alias)
        if seen is None or self.recently_unlisted(alias):
            return self.busy_with(alias)
        return self.submit(Job(*seen)) is not None

    def known_originals(self, alias):
        """The original subtitle per language for a file subsync listed, or None if its list is not known."""
        with self.cv:
            self._restore(alias)
            return self.originals.get(alias)

    def pick(self, alias):
        """The viewer downloaded subsync's entry for this file: start its audio route, once. Returns True if started.
        A pick that can't start now (the person's queue is full, there is no room in today's data, or today's audio
        syncs are used up) leaves the file waiting, so a later pick can start it. A viewer whose choice
        is off starts nothing (an entry from before they turned it off)."""
        if self.s.audio_choice == "off":
            return False
        room = self._audio_room(alias)   # the store's and users' lookups, unlocked
        may = self._may_start_audio(opened=False)
        with self.cv:
            self._restore(alias)
            waiting = self.pickable.get(alias)
            if waiting is None:
                put_bounded(self.picked_early, alias, time.time(), PICKS_ENTRIES)
                return False
            job = waiting.again()
            job.audio = True
            # queued in the same step as the file stops waiting, so a pick never drops a file it didn't queue
            if not (room and may) or self._submit(job) is None:
                log.info("picked %s: %s, it waits for another pick", waiting.label,
                         "today's audio syncs are used up" if not may else
                         "no room for the audio route now" if not room else "no place in the queue")
                return False
            self.pickable.pop(alias)
            key = self._row_key(alias)
            if key is not None:
                self.store.pickable_drop(key)
        log.info("picked %s: the audio route starts", job.label)
        return True

    def _needs_pick(self, job):
        """A file with no usable subtitle track of its own, read index-only: it waits for the viewer's pick. True when
        its audio job is queued now: the pick came already, or the viewer chose "as soon as a file is opened"
        and _may_open holds; either starts exactly as a pick does, with room in the day's data and today's audio syncs
        left. Otherwise, or when it can't start now, the file waits for a pick. Decided one at a time (pick_lock): the
        job's languages, judged in parallel, each come here, and one must find the audio job another queued, never put
        the file waiting beside it."""
        with self.pick_lock:
            if self._audio_queued(job.alias):
                return True
            early = False
            while True:
                with self.cv:   # read and dropped in one step: pick() writes it, under the lock, from a request thread
                    at = self.picked_early.pop(job.alias, None)
                # its entry was downloaded while this read ran
                early = early or (at is not None and time.time() - at <= PICKED_EARLY_S)
                opened = not early and self._may_open(job)
                start = (early or opened) and self._audio_room(job.alias) and self._may_start_audio(opened)
                with self.cv:
                    if not start and job.alias in self.picked_early:
                        continue   # a pick came while this was decided (the file wasn't waiting yet): as one before
                    self.picked_early.pop(job.alias, None)
                    again = job.again()
                    again.audio, again.opened = True, opened
                    if not start or self._submit(again) is None:   # queued, or waiting, in one step
                        self._put_waiting(job)
                        return False
                break
        log.info("%s %s: the audio route starts", "opened" if opened else "picked", job.label)
        return True

    def _may_open(self, job):
        """A file with no usable track (the caller's first point) starts its audio sync as soon as
        it is opened, for a viewer who chose that, only when at least one of their languages that has a subtitle to fit
        (job.listed) has no synced copy from anyone and no audio verdict yet (a "keep original" one counts as tried).
        The room and today's counts are checked by the caller. A Real-Debrid file is never read (every read refuses it),
    so it isn't checked here; a Usenet file counts like any other."""
        if self.s.audio_choice != "open" or not job.listed:
            return False
        file_key = self.store.resolve(job.alias)
        if not file_key:
            return False
        return any(not self.store.offers(file_key, [lang]) and not self.store.audio_judged(file_key, [lang])
                   for lang in job.listed)

    def _may_start_audio(self, opened):
        """Today's audio syncs of this viewer leave room for one more; with opened, an opened one too."""
        return self.audio_counts.may_start_audio(opened)

    def _audio_started(self, job):
        """An audio job starts reading: counted once in today's audio syncs."""
        if not job.counted:
            job.counted = True
            self.audio_counts.audio_started(job.opened)

    def _put_waiting(self, job):
        """The file waits for the person's pick, in memory and in the store (kept across a restart) (cv held)."""
        put_bounded(self.pickable, job.alias, job, PICKS_ENTRIES)
        key = self._row_key(job.alias)
        if key is not None:
            self.store.pickable_put(key, job.type, job.video_id, job.hints)
            self._keep_originals(job.alias)

    def _keep_originals(self, alias):
        """A file's original subtitles (self.originals) kept in the store too, so a pick after a restart finds them (cv
        held, as when stop(forget=True) is set: a deletion never comes between the key and the write)."""
        key = self._row_key(alias)
        if key is not None and self.originals.get(alias) is not None:
            self.store.originals_put(key, self.originals[alias])

    def _may_queue(self):
        """This person may queue one more job (cv held)."""
        return self.limits is None or self.limits.may_queue(len(self.jobs))

    def _audio_room(self, alias):
        """Whether what is left of the person's day's data and the service's covers the most an audio job of the
        file could read. Asked when the pick comes and again when the job starts, since several picks can be
        queued against the same data left."""
        need = self._audio_need(alias)
        if self.store.day_count("read") + need > self.s.daily_budget:
            return False
        return self.limits is None or self.limits.has_room(need)

    def _audio_need(self, alias):
        """What an audio job of the file may read: its index again and its audio windows (audiofit.most_read) with
        AUDIO_NEED_MARGIN, from what its first read found, at most the smaller of the per-file audio limit and the
        per-file cap (the job stops at either); that bound and the index cap when they aren't known."""
        file_key = self.store.resolve(alias)
        detail = (self.store.file_detail(file_key) if file_key else None) or {}
        audio = detail.get("audio") or {}
        bound = min((int(x) for x in (self.s.audio_budget, self.s.file_cap) if x), default=None)
        if audio.get("rate"):
            most = int(AUDIO_NEED_MARGIN * audiofit.most_read(audio["rate"], self.s.audio_budget, self.s.audio_extra,
                                                              audio.get("tracks") or 1))
            if bound is not None:
                most = min(most, bound)
        else:
            most = bound or 0
        return most + int(detail.get("bytes") or self.s.index_cap)

    @staticmethod
    def fit_rank(row):
        """Best fit: how well a passing result fits, smaller first. An original that fits as it is (a subtitle
        made for this release), or one moved as a whole by one shift that passes the main rule, before any re-timed copy;
        copies passing the main rule before those passing as clearly better; then the share of lines within 200 ms (a
        stacked subtitle, a second layer such as transcriptions or another language on screen with the dialogue, counts
        pipeline.STACKED_PENALTY less) and the median error, measured before the per-line snap (the snapped copy reads high
        by construction)."""
        reason = row.get("reason") or ""
        d = row.get("detail") or {}
        passed = Worker.passed_as(d, reason)
        if row.get("outcome") == "in_sync" or d.get("unchanged") or d.get("magnet") or "as it is" in reason:
            cls, g = 0, d.get("gate_original") or {}
        elif d.get("shift_fit") and passed == "main":   # moved as a whole: its author's own timing
            cls, g = 0, d.get("gate_synced") or {}
        elif passed == "clearly better":
            cls, g = 2, d.get("gate_synced") or {}
        else:
            cls, g = 1, d.get("gate_synced") or d.get("gate_snapped") or {}
        med = g.get("median_ms")
        stacked = (d.get("stacked") or 0.0) >= pipeline.STACKED_SHARE
        within = (g.get("within200") or 0.0) - (pipeline.STACKED_PENALTY if stacked else 0.0)
        return (cls, -within, med if med is not None else 1e9)

    @staticmethod
    def passed_as(facts, reason):
        """How a re-timed copy passed (pipeline.judge_copy): "clearly better" than the original, "aligned" to another
        language's track, else "main" (the main rule against its own language). Told by the gates its judging recorded
        (`facts`: a result, or a stored row's detail); by the reason's words only for a row stored without them."""
        def gate(key, said):
            got = facts.get(key)
            return bool(got.get("pass")) if isinstance(got, dict) else said
        if gate("better", "clearly better" in reason):
            return "clearly better"
        if gate("aligned", reason.startswith("aligned")):
            return "aligned"
        return "main"

    def start_lookup(self, video_id):
        """Starts the title's original-language lookup in the background (nothing if it is kept or running); with this
        person's own TMDB key in public mode, else the service's."""
        if self.lookup is not None:
            try:
                self.lookup.start(video_id, key=self.s.tmdb_key)
            except Exception as e:   # a lookup never stops a request
                log.warning("original-language lookup not started: %s", type(e).__name__)

    def rule_for(self, video_id):
        """The rule a file of this title is judged under now (origlang.rule): the title's answer as kept, or none."""
        return origlang.rule(self.s.rule_id, self.lookup.peek(video_id) if self.lookup is not None else None)

    def fresh(self, key, rule=None):
        """A stored result that needs no new attempt now: as the store says, except that one judged under another pass
        rule (code version, rule settings or the title's original language: `rule`, the job's) is judged again: a "keep
        original" always, a passing one when it was judged against the file's own text or image track (only the index
        is read again); an audio-route copy that passed stays offered (judging it again reads audio)."""
        got = self.store.fresh(key)
        if got and not origlang.current(rule or self.s.rule_id)((got.get("detail") or {}).get("rule")) \
                and (got["outcome"] == "keep_original" or got.get("route") != "audio"):
            return None
        return got

    def recently_unlisted(self, alias, within=UNLISTED_S):
        """A file the listing didn't have (Usenet, uncached) is not looked for again for an hour."""
        with self.cv:
            t = self.unlisted.get(alias)
        return t is not None and time.time() - t < within

    def _note_unlisted(self, alias):
        """The listing doesn't have this file now (recently_unlisted)."""
        with self.cv:
            put_bounded(self.unlisted, alias, time.time(), UNLISTED_ENTRIES)

    def file_of(self, aliases, video_hash=None):
        """The stored file the first known of `aliases` names, or None; with `video_hash` (the hash the player sent)
        only a file of that hash: a file named by its torrent file or its name may be another one, so the
        player's own file is read instead. A stored file's key is "<its hash>-<its size>"."""
        file_key = self.store.resolve(*aliases)
        if file_key and video_hash and not same_hash(video_hash, file_key.split("-")[0]):
            return None
        return file_key

    def busy_with(self, alias):
        with self.cv:
            return self._busy(alias)

    def _busy(self, alias):
        """A job of the file is queued or running (cv held)."""
        return alias in self.jobs or self._running(alias) is not None

    def _working(self, alias):
        """The job of the file a wait is for (cv held): queued and due, else running; None when there is none. A queued
        job put off (not_before: stopped by a back-off or the day's budget, tried again SUBSYNC_RETRY_STOPPED_S later)
        gives nothing before its time, so no wait holds for it; it still counts as the file's (busy_with)."""
        job = self.jobs.get(alias)
        return job if job is not None and job.not_before <= time.time() else self._running(alias)

    def busy_reading(self, alias):
        """A job works on the file now that gives its result in seconds (not the audio route, which takes minutes)."""
        with self.cv:
            job = self._working(alias)
            return job is not None and not job.audio

    def wait_for(self, alias, lang, timeout, final=False):
        """Wait up to timeout for a language's result on a queued file: (result key or None, original URL or None). The
        first published pass ends the wait; with `final`, only the language's final result (every candidate judged) does,
        and on timeout the best pass published so far is returned. The waits here time out on the monotonic clock
        (Condition.wait_for): a step of the wall clock never stretches them; none waits for a job put off (_working)."""
        def published():
            return lang in self.done.get(alias, {}) and (not final or lang in self.judged.get(alias, ()))
        with self.cv:
            self.cv.wait_for(lambda: published() or self._working(alias) is None, timeout)
            return self.done.get(alias, {}).get(lang), (self.originals.get(alias, {}).get(lang) or {}).get("url")

    def wait_for_file(self, alias, timeout):
        """Wait up to timeout for a queued file's languages; returns {lang: {"url", "id"} of the original} once its
        subtitle list is known (None while it isn't)."""
        with self.cv:
            self.cv.wait_for(lambda: self._working(alias) is None
                             or all(lang in self.done.get(alias, {}) for lang in self.s.languages), timeout)
            return self.originals.get(alias)

    def wait_for_originals(self, alias, timeout):
        """The file's original subtitles per language once its subtitle list is known, waiting up to timeout while its
        job is queued or running (None if still unknown)."""
        with self.cv:
            self.cv.wait_for(lambda: alias in self.originals or self._working(alias) is None, timeout)
            return self.originals.get(alias)

    def _running(self, alias):
        """The job running for a file on either worker, or None (cv held)."""
        for job in (self.current, self.current_audio):
            if job is not None and job.alias == alias:
                return job
        return None

    def prioritize(self, alias, lang=None):
        """The viewer downloaded subsync's entry for this file: its queued job goes first. And the
        entry's language `lang` is recorded on the file's text pass, queued or running, for alass sooner
        (_sync_language); the other languages' downloads go on as before. An audio job (a file with no track
        of its own) is left as it is."""
        with self.cv:
            job, running = self.jobs.get(alias), self._running(alias)
            for j in (job, running if running is not job else None):
                if j is not None and lang in self.s.languages and not j.audio and lang not in j.picked:
                    j.picked[lang] = time.time()
            if job is not None and job in self.queue and self.queue[0] is not job:
                self.queue.remove(job)
                self.queue.insert(0, job)
            self.cv.notify_all()

    def provisional_for(self, alias, lang):
        """The anchored guess a download of a waiting file's entry serves for a language, or None."""
        with self.cv:
            return (self.provisional.get(alias) or {}).get(lang)

    def _set_provisional(self, job):
        """A file that waits for the audio route has nothing checked yet; each language but the first gets
        its subtitle that lines up best with the first language's best guess (pipeline.anchored_guess), so its first
        download is not just the best-named one."""
        main = self.s.languages[0] if self.s.languages else None
        guess = ((self.originals.get(job.alias) or {}).get(main) or {}).get("id")
        main_text = next((t for c, t in job.fetched.get(main, []) if c["id"] == guess), None)
        if main_text is not None and langcheck.refuses(main_text, main):   # never an anchor in another language
            main_text = None
        if main_text is None:
            if job.fetched:
                log.info("no anchored guesses %s: the %s guess was not among its downloads", job.label, main)
            return
        out = {}
        for lang, fetched in job.fetched.items():
            if lang == main:
                continue
            fetched = [(c, t) for c, t in fetched if not langcheck.refuses(t, lang)]
            got = pipeline.anchored_guess(main_text, [t for _, t in fetched])
            if got:
                out[lang] = got["body"]
                # named by its place among the downloads and its source, never its id: a subtitle a config lists without
                # one has its address as its id, and the log never holds a config's address (README)
                log.info("anchored %s %s: candidate %d of %d (%s) lines up with the %s guess (%.0f %%)", job.label,
                         lang, got["index"] + 1, len(fetched), fetched[got["index"]][0].get("source") or "config", main,
                         100 * got["share"])
        with self.cv:   # a language the audio route has judged keeps its verdict, not a guess
            judged = self.judged.get(job.alias) or set()
            put_bounded(self.provisional, job.alias, {lang: body for lang, body in out.items() if lang not in judged},
                        GUESSES_ENTRIES)

    def _note_measure(self, alias, lang, cand_id, within):
        """A downloaded original measured as it is against the file's own track (check_original's share of
        lines within 200 ms), noted for the pick's answer when nothing passes (measured_original); memory only."""
        if within is None:
            return
        with self.cv:
            notes = self.measures.get(alias) or {}
            notes.setdefault(lang, {})[cand_id] = within
            put_bounded(self.measures, alias, notes, GUESSES_ENTRIES)

    def measured_original(self, alias, lang):
        """The candidate id a pick serves when nothing passed by its deadline: the downloaded original that
        measured best against the file's own track, when it beats the name-ranked guess's measure or the guess wasn't
        measured; None when nothing was measured or the guess measured as well (it is served as before)."""
        with self.cv:
            notes = dict((self.measures.get(alias) or {}).get(lang) or {})
            guess = ((self.originals.get(alias) or {}).get(lang) or {}).get("id")
        if not notes:
            return None
        best = max(notes, key=notes.get)
        return None if guess in notes and notes[guess] >= notes[best] else best

    def published(self, alias, lang):
        """The result key a running job published for a language (best fit: its first pass), or None."""
        with self.cv:
            return (self.done.get(alias) or {}).get(lang)

    def _next(self, lane="main"):
        """The next job for a worker: the audio worker takes audio-route jobs, the main worker every other; a file is
        never worked on by both at once."""
        audio_lane = lane == "audio"
        with self.cv:
            while not self.stop_flag:
                now = time.time()
                for i, job in enumerate(self.queue):
                    running = self._running(job.alias)
                    if bool(job.audio) == audio_lane and job.not_before <= now and running is None:
                        del self.queue[i]
                        del self.jobs[job.alias]
                        if audio_lane:
                            self.current_audio = job
                        else:
                            self.current = job
                        return job
                self.cv.wait(IDLE_POLL_S)
        return None

    # -- the loop ----------------------------------------------------------------------------------------------
    def start(self):
        super().start()
        if self.s.public and self.s.audio_choice == "off":   # no audio lane for a person who can't start an audio sync
            return
        self.audio_worker = threading.Thread(target=self._loop, args=("audio",), daemon=True, name="subsync-audio")
        self.audio_worker.start()

    def stop(self, forget=False):
        """Ends the loop once the job it runs (if any) is done. The jobs still queued are not run; the file of an audio
        job among them (a pick not started yet) waits for another pick, as when an audio job ends without its sync.
        `forget` (public mode: the person's data is being deleted): from now on nothing of theirs is written to the
        store, those waiting files included, so Delete leaves none of their rows."""
        with self.cv:
            self.stop_flag = True
            self.forgotten = self.forgotten or forget
            for job in self.queue:
                if job.audio:
                    self._put_waiting(job.again())
            self.cv.notify_all()

    def run(self):
        self._loop("main")

    def _loop(self, lane):
        """A lane's jobs, one at a time, until stop(). Nothing a job or the store raises ends it: a store write that
        fails while a job's end is handled (a full disk, a locked database) is logged, and the next job runs."""
        while not self.stop_flag:
            job = self._next(lane)
            if job is None:
                break
            try:
                self._run(job, lane)
            except Exception as e:   # raised while its end was handled: the job is over all the same
                log.warning("failed %s: %s", job.label, type(e).__name__)
            # the store's old rows: in public mode the shared store's own housekeeping removes them (__main__), not
            # every person's worker
            if not self.s.public and time.time() - self.last_expire > EXPIRE_EVERY_S:
                self.last_expire = time.time()
                try:
                    self.store.expire()
                except Exception as e:   # never let housekeeping stop the lane
                    log.warning("store expiry failed: %s", type(e).__name__)

    def _run(self, job, lane):
        """A job of the lane, from its file's claim and its places to the handling of how it ended."""
        # an audio job that ends without its sync (stopped before it read, the day's data or its cap ran out, a read
        # failed or was refused, anything else went wrong): its file waits for another pick, as the pick promised
        unfinished = False
        try:
            # the file's claim: never two workers on the same file under the same name at once (one under another name
            # is known by its listed row: the torrent file's claim, process)
            with self.turn.claim(job.alias), self._places(job, lane):
                if self.stop_flag:   # stopped while it waited for its turn: nothing is read
                    unfinished = True
                else:
                    self.process(job)
        except OverLimit as e:
            log.info("over the limit %s: %s", job.label, e)
            unfinished = True   # the person's day ran out (in an audio sync, the file waits again)
        except CapReached as e:
            # The file needs more than the job's own byte cap: not queued again, and not read again
            # while the cap is not raised (capped). An audio job stopped at the audio route's cap says nothing
            # about the index, so the file isn't marked: it waits for another pick, as when the day runs out. An index
            # larger than the parsers ever read is marked under the job's cap too, so it isn't opened again
            # until the cap is raised
            if job.audio and not isinstance(e, IndexTooLarge):
                log.info("stopped %s at the audio cap (%.0f MB): it waits for another pick", job.label,
                         e.cap / 2 ** 20)
                unfinished = True
            else:
                cap = max(e.cap, self._cap(job) or 0)
                for alias in {job.alias, job.bt} - {None}:   # its torrent file too, as _process_row records aliases
                    self.store.cap_reached(alias, cap)
                log.info("index larger than the cap %s (%.0f MB): originals kept", job.label, e.cap / 2 ** 20)
        except Stopped as e:
            log.info("stopped %s: %s", job.label, e)
            # its retry checks the pause first, so nothing is listed, asked or counted again while it lasts
            job.stopped = True
            if isinstance(e, BudgetSpent):
                job.budget_until = (time.time() // DAY + 1) * DAY
            job.not_before = time.time() + self.s.retry_stopped_s
            self._requeue(job)
        except Refused as e:
            # most likely a per-account limit: every read waits (from when the refusal was seen, _refused); this file
            # backs off 1 h / 6 h / 24 h for this viewer, but a Usenet file in its audio sync doesn't (only a
            # first read backs a Usenet file off)
            unfinished = True
            wait = self._refused(job)
            if not (job.audio and job.usenet):
                with self.cv:       # nothing more of a person's once their data is deleted (stop(forget=True))
                    if not self.forgotten:
                        self.store.file_failed(self._refusal_key(job.alias), REFUSED_BACKOFF_S)
            log.warning("refused %s (%s): no reads for %d s", job.label, e, wait)
        except ReadFailed as e:
            if job.audio:
                # A read that fails during the audio sync ends the job (a Usenet file's doesn't stop Usenet:
                # only a first read does), and the file waits for another pick
                log.info("read failed %s during the audio sync (%s): it waits for another pick", job.label, e)
                unfinished = True
            else:
                log.warning("failed %s: %s", job.label, type(e).__name__)
        except Exception as e:  # never let one file stop the worker
            log.warning("failed %s: %s", job.label, type(e).__name__)
            unfinished = True
        finally:
            with self.cv:   # one step: never neither busy nor waiting
                if lane == "audio":
                    self.current_audio = None
                else:
                    self.current = None
                self.done.setdefault(job.alias, {})
                for lang in self.s.languages:
                    self.done[job.alias].setdefault(lang, None)
                self.cv.notify_all()
                for results in (self.done, self.judged, self.originals):
                    if job.alias in results:   # this file's are the newest now
                        results[job.alias] = results.pop(job.alias)
                    trim_oldest(results, RESULTS_ENTRIES)
                if unfinished and job.audio:   # last: a store write that fails here leaves the rest done
                    self._put_waiting(job.again())

    @contextlib.contextmanager
    def _places(self, job, lane):
        """The places a job works in, taken in order: an audio job's audio place before a place of the turn, so a held
        place never waits for another. Kept on the job while held, so a wait that isn't work gives them back
        (_given_back)."""
        places = (self.audio_turn, self.turn) if lane == "audio" else (self.turn,)
        with contextlib.ExitStack() as held:
            for place in places:
                held.enter_context(place)
            job.places = places
            try:
                yield
            finally:
                job.places = ()

    @contextlib.contextmanager
    def _given_back(self, job):
        """The job's places given back for a wait that isn't work (another worker's claim, the pause before a read is
        tried again), so nobody else's file waits for a place held idle; taken again, in the same order, after."""
        places, job.places = job.places, ()
        for place in reversed(places):
            place.__exit__(None, None, None)
        try:
            yield
        finally:
            for place in places:
                place.__enter__()
            job.places = places

    @contextlib.contextmanager
    def _claim(self, job, key):
        """Turn.claim(key) for a job that holds its places already (its file's torrent file, known from its listing):
        when another worker has it, the places are given back while it waits (Turn's rule)."""
        if not self.turn.take(key, wait=False):
            with self._given_back(job):
                self.turn.take(key)
        try:
            yield
        finally:
            self.turn.release(key)

    def _refused(self, job):
        """A read of the job was refused (HTTP 403/429): every read waits (Safety's back-off), started once per job as
        soon as a refusal is seen, so the job's other languages stop reading too. Returns the wait in seconds."""
        if job.refused_wait is None:
            job.refused_wait = self.safety.refused()
        return job.refused_wait

    def _refusal_key(self, alias):
        """The store's mark of a file whose read was refused (1 h / 6 h / 24 h): in public mode the person's own
        (_person_key: a refusal is most likely their account's limit, so it never backs the file off for anyone else),
        else the file's (a self-hosted install, or a public worker without a person's key)."""
        return self._person_key(alias) or alias

    def _file_blocked(self, alias):
        """The file backs off: after its read failures (anyone's: they are about the file) or a refusal of this
        viewer's reads."""
        return any(self.store.file_blocked(key) for key in dict.fromkeys((alias, self._refusal_key(alias))))

    def _backing_off(self, job):
        """Whether the job's file backs off (_file_blocked): then nothing of it is read, its marks stay (asked for,
), and an audio job's file waits for another pick."""
        if not self._file_blocked(job.alias):
            return False
        log.info("file backing off after read failures %s", job.label)
        self.store.marks_asked(job.alias, self._refusal_key(job.alias))
        if job.audio:
            self._wait_again(job)
        return True

    def _wait_again(self, job):
        """An audio job that ended without its sync: the file waits for the viewer's pick again."""
        with self.cv:
            self._put_waiting(job.again())

    def _requeue(self, job):
        with self.cv:
            if job.alias not in self.jobs:
                self.jobs[job.alias] = job
                self.queue.append(job)

    def vad(self):
        with self.vad_lock:
            if self._vad is None and self.vad_factory:
                self._vad = self.vad_factory()
            return self._vad

    def _publish(self, alias, lang, key, final=False):
        """A language's best pass so far (a pick waiting for it is served this one), or with `final` its result once every
        candidate is judged."""
        with self.cv:
            self.done.setdefault(alias, {})[lang] = key
            if final:
                self.judged.setdefault(alias, set()).add(lang)
            self.cv.notify_all()

    def _wake(self, _future=None):
        """Wakes every wait on the worker's condition (an original's download ended: original_for)."""
        with self.cv:
            self.cv.notify_all()

    def process(self, job):
        if job.audio and not (self._audio_room(job.alias) and (job.counted or self._may_start_audio(job.opened))):
            # the data left, or today's audio syncs (a job counted already is queued again), were used meanwhile:
            # nothing is read
            log.info("%s %s: no room for the audio route any more, it waits for another pick",
                     "opened" if job.opened else "picked", job.label)
            self._wait_again(job)
            return
        if job.stopped:
            # a job stopped by the back-off or the day's budget, due again while that pause lasts: stopped again before
            # anything is listed, asked or counted (README: a retry is only a check of the pause)
            if self.safety.backing_off():
                raise Stopped(BACKING_OFF)
            if time.time() < job.budget_until:
                raise BudgetSpent(BUDGET_SPENT)
        hints = dict(job.hints)
        finder = self.usenet_finder
        job.usenet = False
        row = finder.audio_row(job) if job.audio and finder is not None else None
        if row is not None:     # the audio job of a file its first pass found on Usenet: nothing listed again
            job.usenet = True
        else:
            row = self.aios.find_row(job.type, job.video_id, hints)   # none without a sync config (Usenet only)
        if row is None and finder is not None:
            # A file that backs off spends no Usenet listing on a first pass (indexers limit them a day); a
            # debrid file's listing above still leads to its known-file and capped outcomes (_process_row)
            if not job.audio and self._backing_off(job):
                return
            row = finder.find(job, hints)
            job.usenet = row is not None
        if row is None:
            log.info("file not in the listing %s", job.label)
            if job.audio:      # e.g. a pick after its Usenet row's hour, with Usenet stopped: it keeps waiting
                self._wait_again(job)
            self._note_unlisted(job.alias)
            return
        # a Usenet file is claimed by its name only (_run): its row names no torrent file, and its address is each
        # person's own
        bt = None if job.usenet else bt_alias(row)
        if not bt:
            return self._process_row(job, hints, row, None)
        # the listed torrent: TheSubtitleDB finds subtitles made for its very video by its hash
        _, info_hash, file_idx = bt.split(":")
        hints.update(infoHash=info_hash, fileIdx=int(file_idx))
        # another person playing the same torrent file under another name waits, then reads nothing
        with self._claim(job, bt):
            return self._process_row(job, hints, row, bt)

    def _process_row(self, job, hints, row, bt):
        """The file's work once its listed row is known (`bt`: the row's torrent file, or None)."""
        job.bt = bt
        # the title's original language: looked up in the background, never waited for (origlang.py)
        self.start_lookup(job.video_id)
        # a file known by its torrent file, or a Usenet file by its name
        key = bt or (job.alias if job.usenet else None)
        known = self.file_of((key,), hints.get("videoHash")) if key else None
        if known and self.store.judged_all(known, self.s.languages, origlang.current(self.rule_for(job.video_id)),
                                           time.time() - JUDGED_FOR_S) and not self._audio_unjudged(job, known):
            # the same file of the same torrent, judged for every language here lately (read through someone else's
            # listing, in public mode): nothing to read. Not while the audio route still has to judge it
            # (_audio_unjudged): a first pass marks judged a file whose only track is too small for its subtitles, and
            # its audio job (a pick, or "open"), or another person's first pass of their own, still reads it
            self.store.alias(job.alias, known)
            log.info("known file %s: nothing to read", job.label)
            if not self.store.keep_texts:   # this person's copies are made from their own list's originals
                self._candidates(job, hints)
            return
        if self.capped(job, bt):
            # Nothing of the file is read and no file is counted; its subtitle list and originals are still set,
            # so its entries serve the originals. Known by its torrent file only (asked under another name): this name
            # is marked too, so its listing has no pick entry either
            if not self.capped(job):
                self.store.cap_reached(job.alias, self.store.over_cap(bt))
            self.store.marks_asked(job.alias, bt)    # asked for: the mark stays
            candidates = self._candidates(job, hints)
            if candidates:
                self._set_originals(job.alias, candidates, hints.get("filename"), hints.get("videoHash"))
            log.info("index larger than the cap %s (%.0f MB): originals kept", job.label, self._cap(job) / 2 ** 20)
            return
        # an audio job is the same file as its first pass: it isn't counted again; nor is a job queued again
        # after a stop, counted already (README: new files are counted)
        counts = self.limits is not None and not job.audio and not job.file_counted
        if counts and not self.limits.may_start_file():
            raise OverLimit("files per day")
        url = row["url"]
        if self._backing_off(job):
            return
        # the subtitle list first (AIOStreams answers it from its cache), so a pending entry can fall back to the original
        candidates = self._candidates(job, hints)
        if not candidates:
            # An empty list is most likely a subtitle addon too slow for AIOStreams' answer, not "no subtitles":
            # nothing is remembered, so the next request or pick asks for the list again
            log.info("empty subtitle list %s: asked again on the next request", job.label)
            return
        self._set_originals(job.alias, candidates, hints.get("filename"), hints.get("videoHash"))
        if not any(c["lang"] in self.s.languages for c in candidates):
            log.info("no subtitles to sync %s", job.label)   # nothing is read
            return
        guard = JobGuard(self.safety, self.store, self.s.daily_budget, self._cap(job), limits=self.limits)
        reader = RangeReader(rewrite(url, self.s.url_rewrite), job.label, guard, timeout=self.s.read_timeout,
                             size=None if job.usenet else expected_size(hints, row), size_slack=self.s.size_slack,
                             max_re_resolve=USENET_MAX_RE_RESOLVE if job.usenet else MAX_RE_RESOLVE,
                             first_timeout=USENET_FIRST_READ_S if job.usenet else None,
                             size_ok=usenet_size_ok(expected_size(hints, row)) if job.usenet else None)
        if job.usenet and job.kept is not None and job.kept[1]:
            # The audio job reads at the address its first pass resolved, so the Usenet service doesn't fetch
            # the NZB again; when it stops answering, the link is asked again as any reader does (at most twice)
            reader.resolved, reader.redirects = job.kept[1], True
        if counts:
            job.file_counted = True
            self.limits.file_started()
        if job.audio:
            self._audio_started(job)
        for tries_left in (1, 0):
            try:
                index = open_index(reader)
                oshash = index.read_hash()
                break
            except NotMatroska as e:
                log.info("file not readable as Matroska or MP4 %s (%s): original kept", job.label, e)
                self.store.file_failed(job.alias, REFUSED_BACKOFF_S)
                return
            except Refused:
                if job.usenet and not job.audio:
                    self.usenet_finder.stop("the first read of a Usenet file was refused")
                raise
            except ReadFailed as e:
                if job.usenet and job.audio:
                    # Not the file's first read (its first pass read it): Usenet goes on, nothing backs off,
                    # and the file waits for another pick
                    log.info("read failed %s (%s): it waits for another pick", job.label, e)
                    self._wait_again(job)
                    return
                if tries_left and e.connection and not reader.finished and not job.usenet:
                    # The first read got no answer or broke off (a passing fault, as on Breaking Bad;
                    # what it got is counted): tried once more, with the same reader, so it is one of the
                    # link's look-ups; the job keeps its claims, but its places are free for others meanwhile
                    log.info("read failed %s (%s), tried again in %d s", job.label, e, READ_RETRY_S)
                    with self._given_back(job):
                        time.sleep(READ_RETRY_S)
                    continue
                if job.usenet:      # an NZB fetch that failed can't be told apart from other faults here
                    self.usenet_finder.stop("the first read of a Usenet file failed")
                retry = self.store.file_failed(job.alias, REFUSED_BACKOFF_S if job.usenet else READ_FAILED_BACKOFF_S)
                log.info("read failed %s (%s), retry %s", job.label, e, "never" if retry is None else time.strftime("%H:%M", time.gmtime(retry)))
                if job.audio:   # a debrid file's audio job: it backs off as any read, and waits for another pick
                    self._wait_again(job)
                return
        # the file could be read: a back-off is for failures in a row, so the earlier ones no longer count
        self.store.file_ok(job.alias, self._refusal_key(job.alias))
        if job.usenet:
            self.usenet_finder.keep_address(job.alias, row, reader.resolved)
        if hints.get("videoHash") and not same_hash(hints["videoHash"], oshash):
            # 18a: the listed row is another file than the one played (same name, another release): nothing of it is
            # this file's, and the listing is not asked for it again for an hour
            log.info("the listed row is not the played file %s: not synced", job.label)
            self._note_unlisted(job.alias)
            if job.usenet:
                self.usenet_finder.missed(job.alias)
            return
        if not hints.get("videoHash"):   # the file's own hash is known now: a subtitle made for it is the better guess
            self._set_originals(job.alias, candidates, hints.get("filename"), oshash)
        file_key = f"{oshash}-{index.size}"
        for a in {*job.aliases, f"hash:{oshash}|{index.size}", bt} - {None}:
            self.store.alias(a, file_key)
        tracks = index.text_tracks()
        images = index.image_tracks()
        # the title's original language, if its answer is there by now: every subtitle is then timed to the file's
        # track in it (pipeline.choose_reference) and the audio route fits its sound first; else the rules as before
        answer = self.lookup.peek(job.video_id) if self.lookup is not None else None
        original, _ = origlang.choose(answer, pipeline.text_references(index))
        if original:
            index.original_language = original
        job.rule = origlang.rule(self.s.rule_id, answer)
        reference = self._has_reference(index)
        read = {"tracks": [{k: t[k] for k in ("number", "language", "codec", "forced")} | {"cues": len(t["cues"])}
                           for t in tracks + images], "bytes": reader.bytes,
                "image_check": pipeline.image_check(pipeline.text_references(index), images), "rule": job.rule,
                # Whether a subtitle can be judged against the file's own tracks, and what an audio
                # sync of it could read (the room check of a pick)
                "reference": reference, "audio": audio_facts(index)}
        if answer:
            read["original_language"] = origlang.describe(answer, pipeline.text_references(index))
        # The languages earlier first passes left to the audio route stay marked; a first pass marks its own
        # languages anew once it has judged them (below)
        audio_only = set((self.store.file_detail(file_key) or {}).get("audio_only") or ())
        if not job.audio:
            audio_only -= set(self.s.languages)
        if audio_only:
            read["audio_only"] = sorted(audio_only)
        self.store.file_read(file_key, index.size, oshash, any(not t["forced"] and t["cues"] for t in tracks), read)
        per_lang = {lang: [c for c in candidates if c["lang"] == lang] for lang in self.s.languages}
        # The languages with a subtitle to fit (the "as soon as a file is opened" check)
        job.listed = tuple(lang for lang, cands in per_lang.items()
                           if any(not (self.s.skip_ai and choose.unwanted(c)) for c in cands))
        if not reference and not job.audio and self.s.audio_choice != "off":
            self._await_pick(job, per_lang, hints.get("filename"), oshash)
            log.info("done %s file=%s bytes=%d requests=%d", job.label, file_key, reader.bytes, reader.requests)
            return
        self._sync_all(job, per_lang, lambda lang, cands: self._sync_language(job, index, file_key, lang, cands, oshash,
                                                                              hints.get("filename")))
        if job.to_audio:
            self.store.file_detail_update(file_key, audio_only=sorted(audio_only | job.to_audio))
        if reference or job.audio or not self.s.audio_route:
            self.store.judged_put(file_key, self.s.languages, job.rule)
        # else: the first pass of a person whose choice is off, on a file with no track: nothing was judged, so
        # nothing is marked; everyone else who can pick it, or open it, still gets their own first pass
        log.info("done %s file=%s bytes=%d requests=%d", job.label, file_key, reader.bytes, reader.requests)

    def _candidates(self, job, hints):
        """The file's subtitle list (AIOStreams and the extra sources). An audio job reuses the list its text pass got
        within LIST_TTL_S (asking again took 0.7 to 1.8 s of a pick's 14 s)."""
        now = time.time()
        with self.cv:
            got = self.lists.get(job.alias)
        if job.audio and got and now - got[0] < LIST_TTL_S:
            return [dict(c) for c in got[1]]
        lister = self._lister()
        candidates = lister.subtitles(job.type, job.video_id, hints)
        for c in candidates:   # the config's subtitles, tagged with the source they come from when it is a known one
            c["source"] = srclimits.source_of_url(c.get("url")) or "config"
            c["from_config"] = True
        if self.extra is not None:
            # A source the config already has is asked once, through the config; a subtitle the config lists
            # (same address, or the same source's same id) isn't added twice
            skip = set(getattr(lister, "config_sources", None) or ()) if self.s.skip_config_sources else set()
            seen = {c["url"] for c in candidates} | {(c["source"], c["id"]) for c in candidates}
            try:
                found = self.extra.search(job.type, job.video_id, langs=self.s.languages, hints=hints, skip=skip)
                candidates += [c for c in found if c["url"] not in seen and (c["source"], c["id"]) not in seen]
            except Exception as e:  # never let the extra sources stop the file
                log.info("extra sources failed %s: %s", job.label, type(e).__name__)
        with self.cv:
            for c in candidates:   # a stored recipe's original is found again by its id and language
                put_bounded(self.by_id, (c["id"], c["lang"]), dict(c), srclimits.BY_ID_ENTRIES)
            if candidates:
                put_bounded(self.lists, job.alias, (now, [dict(c) for c in candidates]), LISTS_ENTRIES)
        return candidates

    def left_to_audio(self, detail):
        """This viewer's languages that first passes of a file left to the audio route (its only track too
        small for their subtitles: its stored detail's "audio_only"), when their choice isn't off. Until the audio route
        has judged them, the file is to them one with no track of its own: their own first pass and pick."""
        if self.s.audio_choice == "off":
            return ()
        marked = set((detail or {}).get("audio_only") or ())
        return tuple(lang for lang in self.s.languages if lang in marked)

    def _audio_unjudged(self, job, file_key):
        """Whether the audio route still has to judge a stored file for this job: an audio job's every language (its
        pick, or "open", queued it), or a first pass's languages left to it (left_to_audio)."""
        langs = self.s.languages if job.audio else self.left_to_audio(self.store.file_detail(file_key))
        return bool(langs) and not self.store.audio_judged(file_key, langs)

    def _audio_queued(self, alias):
        """Is an audio job queued or running for this file (its pick came while this text pass ran)?"""
        with self.cv:
            queued = self.jobs.get(alias)
            running = self.current_audio
            return bool(queued and queued.audio) or (running is not None and running.alias == alias)

    def _has_reference(self, index):
        """Has the file any text or image track a subtitle could be judged against (the most lenient test: any
        non-forced dialogue track, any plausible image track)?"""
        if not hasattr(index, "text_tracks"):
            return False
        return pipeline.choose_reference(index, 0, self.s.languages[0] if self.s.languages else DEFAULT_LANGUAGE,
                                         self.s.image_ref, self.s.prefer_plain, self.s.fallback_reference) is not None

    def _await_pick(self, job, per_lang, filename, oshash):
        """A file with no subtitle track of its own: nothing can be judged until the viewer picks subsync's entry and
        the audio route starts. Its text pass does only what a pick serves meanwhile, then hands over at once
        (12 Angry Men's audio route started 15 s after the pick, once ~40 downloads had been judged for nothing): the
        best guess, replaced by what a large group of the main language's first downloads agree on when it sits seconds
        away from them (whatever the number of languages), and with SUBSYNC_ANCHORED_GUESS the other
        languages lined up with it, from their first SUBSYNC_GATE_TRIES downloads. When the pick has come already, the
        languages are left open, so it keeps waiting (up to the pick wait) for the audio route's first pass instead of
        being served the original as this pass ends."""
        main = self.s.languages[0] if self.s.languages else None
        anchored = self.s.anchored_guess and len(per_lang) > 1
        guess = ((self.originals.get(job.alias) or {}).get(main) or {}).get("id")
        want = {}
        for lang, cands in per_lang.items():
            if lang != main and not anchored:
                continue
            first = self.viewer_first(cands)
            order = self._within_limits(self._try_order(cands, filename, oshash, viewer_first=first), filename, oshash,
                                        None, first)
            if lang == main:
                # The guess and the first CONSENSUS_TRIES others in try order, for a consensus that may replace a
                # guess timed for another release (_consensus_guess)
                want[lang] = [(r, c) for r, c in enumerate(cands) if c["id"] == guess][:1] \
                    + [(r, c) for r, c in order if c["id"] != guess][:CONSENSUS_TRIES]
            else:
                want[lang] = order[: self.s.gate_tries]
        for lang, tries in want.items():
            got = self._fetch_all(job, lang, tries)
            job.fetched[lang] = [(cand, decode(raw, lang)) for _, cand, raw in got]
            self._check_fetched(job, lang, per_lang[lang], {c["id"] for _, c in tries}, filename, oshash,
                                more=langcheck.MORE_DOWNLOADS if lang == main else 0)
        self._consensus_guess(job, main)
        if anchored:
            self._set_provisional(job)
        if not self._needs_pick(job):   # said once it waits: a sync that starts at once logs its own line
            log.info("no text or image subtitle track %s: the audio route waits for the viewer to pick the Subsync "
                     "entry", job.label)
        if self._audio_queued(job.alias):
            return   # picked already: the audio job publishes; a waiting pick gets its first pass
        for lang in per_lang:
            self._publish(job.alias, lang, None, final=True)

    def _consensus_guess(self, job, lang):
        """A file with no track: the language's guess by name is replaced by the first of a large group
        of its downloads that agree with each other when the guess sits seconds away from them (pipeline.consensus_guess),
        here and in the store, before the other languages are lined up with it."""
        with self.cv:
            current = (self.originals.get(job.alias) or {}).get(lang)
        fetched = job.fetched.get(lang) or []
        guess_text = next((t for c, t in fetched if current and c["id"] == current["id"]), None)
        others = [(c, t) for c, t in fetched if not current or c["id"] != current["id"]]
        got = pipeline.consensus_guess(guess_text, [t for _, t in others]) if guess_text is not None else None
        if got is None:
            return
        cand = others[got["index"]][0]
        with self.cv:
            langs = dict(self.originals.get(job.alias) or {})
            langs[lang] = {"url": cand["url"], "id": cand["id"]}
            self.originals[job.alias] = langs
            self._keep_originals(job.alias)
            self.cv.notify_all()
        log.info("guess %s %s: %d subtitles agree and the guess by name sits %.1f s from them: another served",
                 job.label, lang, got["group"], got["offset_s"])

    def _check_fetched(self, job, lang, candidates, tried, filename, file_hash, more=0):
        """A waiting file's downloads of a language: those whose text the language check refuses are left out (no
        anchored guess is made from them) and its original is replaced if it is one of them; with `more`, up to that many
        further downloads in name order until the original's text is checked (the main language's, the anchor)."""
        texts, refused = {}, set()
        for cand, text in job.fetched.get(lang, []):
            if langcheck.refuses(text, lang):
                refused.add(cand["id"])
            else:
                texts.setdefault(cand["id"], text)
        failed = set(tried) - set(texts) - refused if more else set()   # the anchor's: tried, and the download failed
        for _ in range(more + 1):
            self._recheck_original(job.alias, lang, candidates, texts, refused, filename, file_hash, failed)
            with self.cv:
                current = (self.originals.get(job.alias) or {}).get(lang)
            if not current or current["id"] in texts or current["id"] in failed or more <= 0:
                break
            more -= 1
            cand = next((c for c in candidates if c["id"] == current["id"]), None)
            got = self._fetch_one(job, lang, 0, cand) if cand else None
            if got is None:
                failed.add(current["id"])
                continue
            text = decode(got[2], lang)
            if langcheck.refuses(text, lang):
                refused.add(cand["id"])
            else:
                texts[cand["id"]] = text
                job.fetched.setdefault(lang, []).append((cand, text))
        job.fetched[lang] = [(c, t) for c, t in job.fetched.get(lang, []) if c["id"] not in refused]

    def _sync_all(self, job, per_lang, sync):
        """Judge every language (sync(lang, candidates) -> result key) and publish each as final. A language that waits
        for the audio route is published once the anchored guesses are set, also when another language raised (the
        first error is raised after, a refusal before any other). The languages go in their order, a picked one too
."""
        deferred, errors = {}, []
        try:
            if self.s.parallel_languages and len(per_lang) > 1:
                with ThreadPoolExecutor(max_workers=len(per_lang), thread_name_prefix="subsync-lang") as pool:
                    futures = {pool.submit(sync, lang, cands): lang for lang, cands in per_lang.items()}
                    for fut in as_completed(futures):
                        try:
                            self._finish(job, futures[fut], fut.result(), deferred)
                        except Refused as e:   # every read waits from now: the other language's next read stops too
                            self._refused(job)
                            errors.append(e)
                        except Exception as e:   # the other language still finishes
                            errors.append(e)
            else:
                for lang, cands in per_lang.items():
                    self._finish(job, lang, sync(lang, cands), deferred)
        finally:
            if deferred:
                try:
                    self._set_provisional(job)
                except Exception as e:
                    log.warning("anchored guesses failed %s: %s", job.label, type(e).__name__)
                if not self._audio_queued(job.alias):   # picked already: a waiting pick waits for the audio route
                    for lang, key in deferred.items():
                        self._publish(job.alias, lang, key, final=True)
        if errors:   # a refusal first: the job ends as refused (_run), whatever else another language raised
            raise next((e for e in errors if isinstance(e, Refused)), errors[0])

    def _finish(self, job, lang, key, deferred):
        """A language's judging ended: published as final, or kept in `deferred` while it waits for the audio route.
        The audio route's own verdict replaces the language's anchored guess (served or kept as the original)."""
        if lang in job.fetched and not job.audio:
            deferred[lang] = key
            return
        with self.cv:   # one step, so a pick in between finds the verdict or the anchored guess
            if job.audio:
                (self.provisional.get(job.alias) or {}).pop(lang, None)
            self._publish(job.alias, lang, key, final=True)

    def _set_originals(self, alias, candidates, filename, file_hash):
        """Per language, the subtitle a download of subsync's entry serves while there is no copy (kept in the store, so
        a pick after a restart still finds it): the first in name order (_name_order), so with
        SUBSYNC_RANK_BY_RELEASE the likeliest to fit the file as it is (18g: Detectorists' first download was
        the viewer's first, a DVD rip 41.7 s off); none when SUBSYNC_SKIP_AI leaves none."""
        fallbacks = {}
        for lang in self.s.languages:
            ordered = self._name_order([c for c in candidates if c["lang"] == lang], filename, file_hash)
            fallbacks[lang] = {"url": ordered[0]["url"], "id": ordered[0]["id"]} if ordered else None
        with self.cv:
            self.originals[alias] = fallbacks
            self._keep_originals(alias)
            self.cv.notify_all()

    def _try_order(self, candidates, filename, file_hash, ref_cues=None, viewer_first=True):
        """A language's subtitles as [(viewer rank, candidate)] in the one order every choice here takes them:
        with SUBSYNC_RANK_BY_RELEASE the likeliest to fit the file as it is first (choose.order), else the list's; with
        SUBSYNC_SKIP_AI never an AI-made, machine-translated or forced one."""
        if self.s.rank_by_release:
            return choose.order(candidates, filename, file_hash, skip=self.s.skip_ai, ref_cues=ref_cues,
                                viewer_first=viewer_first)
        return [(r, c) for r, c in enumerate(candidates) if not (self.s.skip_ai and choose.unwanted(c))]

    def _name_order(self, candidates, filename, file_hash, first=()):
        """A language's subtitles in the order an original is chosen by name (_set_originals): the try order
        (_try_order) with the viewer's first given no place of its own; the ids in `first` before the rest."""
        ordered = [c for _, c in self._try_order(candidates, filename, file_hash, viewer_first=False)]
        front = [i for i in first if i]
        return sorted(ordered, key=lambda c: front.index(c["id"]) if c["id"] in front else len(front))

    def _name_choice(self, candidates, texts, refused, filename, file_hash):
        """The original to serve by name once some texts are downloaded: the first in name order whose downloaded
        text the language check didn't refuse, else the first not downloaded yet (checked when it is served), else None."""
        ordered = [c for c in self._name_order(candidates, filename, file_hash) if c["id"] not in refused]
        return next((c for c in ordered if c["id"] in texts), None) or next(iter(ordered), None)

    def _recheck_original(self, alias, lang, candidates, texts, refused, filename, file_hash, failed=()):
        """A language's original chosen by name before any download (_set_originals) whose text the language
        check refused is replaced (_name_choice), here and in the store; one whose download failed (`failed`) only when
        there is another to serve."""
        with self.cv:
            current = (self.originals.get(alias) or {}).get(lang)
        if not current or current["id"] not in set(refused) | set(failed):
            return
        pick = self._name_choice(candidates, texts, set(refused) | set(failed), filename, file_hash)
        if pick is None and current["id"] not in refused:   # a failed download alone: kept, tried again when served
            return
        with self.cv:
            langs = dict(self.originals.get(alias) or {})
            langs[lang] = {"url": pick["url"], "id": pick["id"]} if pick else None
            self.originals[alias] = langs
            self._keep_originals(alias)
            self.cv.notify_all()
        why = f"not {lang_name(lang)} text" if current["id"] in refused else "its download failed"
        log.info("original %s: %s, %s served instead", lang, why, "another" if pick else "none")

    def _keep_original(self, file_key, lang, candidates, texts, refused, filename, file_hash, rule=None):
        """Nothing of a language passed: the original its "<Language> (original)" entry serves, kept per file and
        language: the one the file's judging under this rule measured best as it is (the highest share of lines within
        200 ms, gate_original; a "shift under 100 ms" is such a one), else the likeliest by name (_name_choice); never one
        whose text the language check refused."""
        best = None
        for r in self.store.results_of(file_key, lang):
            d = r["detail"]
            if not origlang.current(rule or self.s.rule_id)(d.get("rule")) or not r["candidate_id"] \
                    or r["candidate_id"] in refused \
                    or (d.get("language") or {}).get("verdict") == langcheck.FAIL:
                continue
            g = d.get("gate_original") or {}
            within = g.get("within200")
            if within is not None and (best is None or within > best[1]):
                best = (r["candidate_id"], within)
        if best is None:
            pick = self._name_choice(candidates, texts, refused, filename, file_hash)
            best = (pick["id"] if pick else None, None)
        self.store.kept_original_put(file_key, lang, *best)

    def original_for(self, alias, lang, deadline, first=None, until_copy=False):
        """The text of an original subtitle to serve unchanged for a file and language (a pick or pending entry
        while there is no copy, or the "(original)" entry), from this person's own subtitle list only:
        `first` (a candidate id), else the file's original (_set_originals), downloaded if it wasn't; if the language
        check refuses it, the next in name order among the subtitles downloaded already (kept 10 minutes), else up to
        langcheck.MORE_DOWNLOADS more downloads. Nothing of the video is read and no job is queued. Returns (text, None,
        None), or (None, why, address): "late" when `deadline` (a time) came first, with the address in this person's
        list of the original being downloaded then when a player can use it as it is (player_address; else None), "none"
        when none passes, "unknown" when this person has no subtitle of the language listed for the file. With
        `until_copy` (a pick or pending entry), a download also stops waiting, as "late", once the file's job
        publishes a synced copy of the language: the entry serves that copy."""
        with self.cv:
            self._restore(alias)
            listed = self.lists.get(alias)
            seen = self.seen.get(alias)
            original = (self.originals.get(alias) or {}).get(lang)
        hints = dict(seen[2]) if seen else {}
        if listed is None and seen is not None:   # not listed lately: this person's list again, as original_text does
            # on the request's own time, as the downloads: a list that comes later is kept (lists) for the player's
            # next try
            fetch = self.original_pool.submit(self._candidates, Job(*seen), dict(hints))
            try:
                fetch.result(timeout=max(0.0, deadline - time.time()))
            except FutureTimeout:
                return None, "late", None
            except Exception as e:
                log.info("subtitle list failed for an original: %s", type(e).__name__)
            with self.cv:
                listed = self.lists.get(alias)
        cands = [dict(c) for c in (listed[1] if listed else []) if c["lang"] == lang]
        if original and not any(c["id"] == original["id"] for c in cands):
            cands.insert(0, {"id": original["id"], "url": original["url"], "lang": lang})
        if not cands:
            return None, "unknown", None
        ordered = self._name_order(cands, hints.get("filename"), hints.get("videoHash"),
                                   (first, (original or {}).get("id")))
        if not ordered:   # every one AI-made or forced, with SUBSYNC_SKIP_AI
            return None, "none", None
        job = Job(*seen) if seen else Job("original", lang)
        tried = set()

        def downloaded(c):
            with self.cv:
                got = self.fetched_subs.get(c["url"])
            return decode(got[1], lang) if got and time.time() - got[0] < LIST_TTL_S else None

        def download(c):
            # timed out here, not by fetch_subtitle's own timeout: that one bounds each socket wait only (not a site's
            # turn or its 429 retries), and a download it stopped would be lost to the player's next try
            tried.add(c["url"])
            left = deadline - time.time()
            if left <= 0:
                return False
            future = self.original_pool.submit(self._fetch_one, job, lang, 0, c)
            future.add_done_callback(self._wake)
            with self.cv:
                self.cv.wait_for(lambda: future.done() or (until_copy and self.published(alias, lang) is not None),
                                 left)
            if not future.done():   # it goes on and is kept (fetched_subs): the player's next try gets it at once
                return False
            got = future.result()
            return decode(got[2], lang) if got else None

        def late(c):
            return None, "late", self.player_address(c)

        def passes(c, text):
            if text is None:
                return False
            if langcheck.refuses(text, lang):
                log.info("original %s %s: not %s text, not served", job.label, lang, lang_name(lang))
                return False
            return True

        text = downloaded(ordered[0])
        if text is None:
            text = download(ordered[0])
            if text is False:
                return late(ordered[0])
        tried.add(ordered[0]["url"])
        if passes(ordered[0], text):
            return text, None, None
        for c in ordered[1:]:          # the next that passes among those downloaded already
            text = downloaded(c)
            if text is not None:
                tried.add(c["url"])
                if passes(c, text):
                    return text, None, None
        more = [c for c in ordered if c["url"] not in tried][: langcheck.MORE_DOWNLOADS]
        for c in more:
            text = download(c)
            if text is False:
                return late(c)
            if passes(c, text):
                return text, None, None
        return None, "none", None

    def player_address(self, cand):
        """A subtitle's address in this person's list when a player can download it as it is (the redirect 1.4.0 sent
        for a pick): a plain_address, not one subsync found itself that needs a key (extra.headers_for) or is a zip it
        unpacks (srclimits.ZIPPED, a file's pick after "#"); None otherwise."""
        url = cand.get("url") or ""
        source = cand.get("source") or srclimits.source_of_url(url)
        if not plain_address(url) or "#" in url or source in srclimits.ZIPPED:
            return None
        if self.extra is not None and self.extra.headers_for(dict(cand, source=source)):
            return None
        return url

    def original_needs_download(self, alias, lang):
        """Whether an original of the language may still have to be downloaded to answer a pick or pending entry: the
        file's subtitle list isn't known yet, or its original (_set_originals) isn't among the downloads kept
        (fetched_subs). False when it is, or the language has none."""
        with self.cv:
            self._restore(alias)
            originals = self.originals.get(alias)
            if originals is None:
                return True
            url = (originals.get(lang) or {}).get("url")
            got = self.fetched_subs.get(url) if url else None
        return bool(url) and not (got and time.time() - got[0] < LIST_TTL_S)

    def _sync_language(self, job, index, file_key, lang, candidates, oshash=None, filename=None):
        """The viewer's first subtitle, then subtitles made for this release, then the rest, until one
        passes or `max_tries` were tried. Returns the last_good key to offer, if any. `rank` stays the viewer's order.
        With SUBSYNC_LIST_IN_SYNC on, an original further down the list that is in sync as it is gets listed as an
        unchanged "(synced)" copy, so the viewer can tell which one fits this file; the first one needs no copy.
        With SUBSYNC_FAST_PATH on, every try is downloaded at once and its original checked first (a gate, no
        alass), so an in-sync original is listed within seconds; re-timing runs only when none is in sync. With the audio
        route off, a file with no text or image subtitle track stores nothing, so turning the route on later syncs it.
        Each step is a method of LanguageSync."""
        return LanguageSync(self, job, index, file_key, lang, candidates, oshash, filename).run()

    @staticmethod
    def viewer_first(candidates):
        """Is the first candidate the viewer's own first subtitle (from their config)? With no subtitle addon in the
        config the list starts with a built-in source's, which has no place of its own."""
        return bool(candidates) and candidates[0].get("from_config", True)

    def original_text(self, result, recipe=None):
        """Public mode: the original a stored result was made from, downloaded again from the source this
        person's own list gives for it, or None when their list has no such subtitle or it changed since (its bytes
        differ, and the copy's recipe doesn't recognise it: subs.recognised)."""
        key = (result.get("candidate_id"), result.get("lang"))
        with self.cv:
            cand = self.by_id.get(key)
            seen = list(self.seen.items())
        if cand is None:   # not listed lately: the list of a file this person opened that has this result
            for alias, (type_, video_id, hints) in reversed(seen):
                if self.store.resolve(alias) == result.get("file_key"):
                    self._candidates(Job(type_, video_id, hints), dict(hints))
                    with self.cv:
                        cand = self.by_id.get(key)
                    break
        if cand is None:
            return None
        got = self._fetch_one(Job("original", str(result.get("file_key"))), result["lang"], 0, cand)
        if got is None:
            return None
        raw = got[2]
        text = decode(raw, result["lang"])
        if hashlib.sha256(raw).hexdigest() != result.get("sub_sha") and not recognised(text, recipe):
            log.info("the original of a stored copy changed at its source: not served")
            return None
        return text

    def _within_limits(self, tries, filename, oshash, ref_cues, viewer_first=None):
        """A site with a tight limit (srclimits.PER_FILE_DOWNLOADS) gives a file and language only its first
        few subtitles in the try order; near its limit (sourcestate.Ledger.tight; in public mode for a person who had
        their share) only the likeliest (srclimits.TOP_TIER); at its limit none."""
        ledger = getattr(self.extra, "ledger", None) if self.extra is not None else None
        if viewer_first is None:
            viewer_first = any(rank == 0 and cand.get("from_config", True) for rank, cand in tries)
        taken, out = {}, []
        for rank, cand in tries:
            source = cand.get("source")
            cap = srclimits.PER_FILE_DOWNLOADS.get(source)
            kind = srclimits.DOWNLOAD_KIND.get(source, "download")
            if ledger is not None and source and ledger.limits(source, kind):
                if ledger.exhausted(source, kind):
                    continue
                if ledger.tight(source, kind, who=self.who) and \
                        choose.tier_of(rank, cand, filename, oshash, ref_cues, viewer_first) > srclimits.TOP_TIER:
                    continue
            if cap is not None:
                if taken.get(source, 0) >= cap:
                    continue
                taken[source] = taken.get(source, 0) + 1
            out.append((rank, cand))
        return out

    def _fetch_one(self, job, lang, rank, cand):
        """A subtitle's download, (rank, cand, bytes), or None when it failed; kept LIST_TTL_S (fetched_subs). One of
        the same address running already (a player's retry while its first try's download goes on, or the text pass's)
        is waited for, not started again, so a site is asked, and its limit counted, once."""
        url, now = cand["url"], time.time()
        with self.cv:
            got = self.fetched_subs.get(url)
            if got and now - got[0] < LIST_TTL_S:
                return rank, cand, got[1]
            running = self.fetching.get(url)
            if running is None:
                mine = self.fetching[url] = Future()
        if running is not None:
            raw = running.result()
            return None if raw is None else (rank, cand, raw)
        raw = None
        try:
            source = cand.get("source")
            ledger = getattr(self.extra, "ledger", None) if self.extra is not None else None
            if ledger is not None and source:
                ledger.count(source, srclimits.DOWNLOAD_KIND.get(source, "download"), who=self.who)
            headers = self.extra.headers_for(cand) if self.extra is not None else None
            raw = fetch_subtitle(url, headers=headers) if headers else fetch_subtitle(url)
            with self.cv:
                self._keep_download(url, now, raw)
            return rank, cand, raw
        except Exception as e:
            log.info("subtitle fetch failed %s %s rank %d: %s", job.label, lang, rank, type(e).__name__)
            return None
        finally:   # the callers waiting for it get its bytes, or None when it failed
            with self.cv:
                del self.fetching[url]
            mine.set_result(raw)

    def _keep_download(self, url, at, raw):
        """A download (begun `at`) kept in fetched_subs as the newest entry, served for LIST_TTL_S: the entries older
        than that are dropped (nothing serves them again), then the oldest while the rest pass FETCHED_CAP, down to
        one (cv held)."""
        subs = self.fetched_subs
        subs.pop(url, None)
        subs[url] = (at, raw)
        now = time.time()
        for stale in [u for u, (t, _) in subs.items() if now - t >= LIST_TTL_S]:
            del subs[stale]
        kept = sum(len(r) for _, r in subs.values())
        while len(subs) > 1 and kept > FETCHED_CAP:
            kept -= len(subs.pop(next(iter(subs)))[1])

    def _fetch_stream(self, job, lang, tries):
        """Best fit's downloads: up to FETCH_AT_ONCE at once, each yielded as it ends; failed ones are left out. Every
        language's go at once, a picked one's too."""
        pool = ThreadPoolExecutor(max_workers=FETCH_AT_ONCE, thread_name_prefix="subsync-fetch")
        try:
            for fut in as_completed([pool.submit(self._fetch_one, job, lang, rank, cand) for rank, cand in tries]):
                got = fut.result()
                if got is not None:
                    yield got
        finally:   # closed early (judging raised): downloads not started are dropped, running ones end on their own
            pool.shutdown(wait=False, cancel_futures=True)

    def _fetch_all(self, job, lang, tries):
        """The fast path's downloads: up to FETCH_AT_ONCE at once, results in try order; failed ones are left out."""
        if len(tries) <= 1:
            got = [self._fetch_one(job, lang, rank, cand) for rank, cand in tries]
        else:
            with ThreadPoolExecutor(max_workers=FETCH_AT_ONCE, thread_name_prefix="subsync-fetch") as pool:
                got = list(pool.map(lambda rc: self._fetch_one(job, lang, *rc), tries))
        return [g for g in got if g is not None]


class LanguageSync:
    """One language of a file judged (Worker._sync_language), each step a method. The state the steps share is the
    language's: its tries, the downloads' texts and those the language check refused, the passes so far and the fits
    made. Everything is read, stored, downloaded and published through the worker, as when these steps were its own."""

    # a judged result's facts kept in its stored detail (_record)
    RECORDED = ("route", "reference", "gate_original", "gate_synced", "gate_snapped", "refined", "unchanged", "magnet",
                "shift", "model", "windows", "audio_s", "lines", "dialogue", "alass_error", "fast", "better",
                "audio_track", "shift_fit", "cross", "aligned", "recentred_ms", "text", "text_original", "sections",
                "section_checks", "close_call")

    def __init__(self, worker, job, index, file_key, lang, candidates, oshash=None, filename=None):
        self.w, self.s, self.store = worker, worker.s, worker.store
        self.job, self.index, self.file_key, self.lang = job, index, file_key, lang
        self.candidates, self.oshash, self.filename = candidates, oshash, filename
        self.best_fit = self.s.best_fit
        self.streamed = self.best_fit and self.s.fast_path   # each download judged as it ends (_stream)
        self.tries = self._tries()
        self.audio = worker.audio_allowed(job)
        self.not_language = f"not {lang_name(lang)} text"
        # the downloads' texts by candidate id, and the ids of those the language check refused
        self.texts, self.refused = {}, set()
        self.fingerprints = set()   # the subtitles judged so far, by fingerprint and sha256 (_duplicate)
        self.passed = []            # best fit: the keys of passing results
        self.best_rank = None       # best fit: the fit rank of the best pass published so far (_offer)
        # D1: a subtitle of this language passed the main rule against its own language
        self.settled = False
        self.fitted, self.shas = 0, set()   # best fit: alass runs so far, and the subtitles fitted (each once)

    def run(self):
        """The language's result key to offer, if any (Worker._sync_language); then its original is checked again and,
        when nothing passed, the one its "(original)" entry serves is kept."""
        if self.streamed:
            entries, done = self._stream()
        else:
            entries = self._downloads()
            done = self.s.fast_path and not self.best_fit and self._originals_first(entries)
        if not done:
            self._fit_all(entries)
        key = self._result()
        w = self.w
        w._recheck_original(self.job.alias, self.lang, self.candidates, self.texts, self.refused, self.filename,
                            self.oshash)
        if key is None and self.candidates:
            w._keep_original(self.file_key, self.lang, self.candidates, self.texts, self.refused, self.filename,
                             self.oshash, rule=self.job.rule)
        return key

    def _tries(self):
        """The (rank, candidate) tried, in try order within the sources' limits: with best fit and the fast path the
        originals of up to SUBSYNC_GATE_TRIES are checked (cheap) and alass runs on at most SUBSYNC_MAX_TRIES of them;
        else up to SUBSYNC_MAX_TRIES."""
        w, s = self.w, self.s
        # the release's own track in the subtitle's language, whose line count says a subtitle was made from this
        # release (choose.order); its timing is still judged against the title's original language
        ref = pipeline.choose_reference(self.index, 0, self.lang, False, s.prefer_plain, s.fallback_reference,
                                        by_original=False) if hasattr(self.index, "text_tracks") else None
        ref_cues = len(ref["cues"]) if ref else None
        first = w.viewer_first(self.candidates)
        tries = w._within_limits(w._try_order(self.candidates, self.filename, self.oshash, ref_cues, first),
                                 self.filename, self.oshash, ref_cues, first)
        return tries[: max(s.max_tries, s.gate_tries) if self.streamed else s.max_tries]

    # -- the downloads ---------------------------------------------------------------------------------------------
    def _entry(self, rank, cand, raw):
        """A download as the steps pass it on: its rank, candidate and bytes, their sha256, and the stored result of
        those bytes that needs no new attempt ("got", Worker.fresh), if any."""
        e = {"rank": rank, "cand": cand, "raw": raw, "sha": hashlib.sha256(raw).hexdigest()}
        e["got"] = self._fresh(e)
        return e

    def _fresh(self, e):
        """The download's stored result that needs no new attempt (Worker.fresh), if any."""
        return self.w.fresh(result_key(self.file_key, self.lang, e["sha"]), self.job.rule)

    def _downloads(self):
        """The downloads when they aren't judged as each ends (_stream), those in another language left out
        (_wrong_language): with the fast path every try at once (Worker._fetch_all), so the originals can be checked
        first; else one at a time, all of them for best fit, and otherwise (as in v3) only as they are judged, so the
        downloads stop at the first that passes (a generator)."""
        job, lang = self.job, self.lang
        if self.s.fast_path:
            fetched = self.w._fetch_all(job, lang, self.tries)
        else:
            fetched = (f for f in (self.w._fetch_one(job, lang, rank, cand) for rank, cand in self.tries) if f)
        entries = (e for e in (self._entry(*f) for f in fetched) if not self._wrong_language(e))
        return list(entries) if self.s.fast_path or self.best_fit else entries

    def _wrong_language(self, e):
        """A download whose text isn't in this language is recorded as kept, and never judged, offered or
        anchored on."""
        lang = self.lang
        e.setdefault("text", decode(e["raw"], lang))
        m = langcheck.measure(e["text"], lang)
        if langcheck.verdict(m, lang) != langcheck.FAIL:
            self.texts[e["cand"]["id"]] = e["text"]
            return False
        self.refused.add(e["cand"]["id"])
        got = e["got"]
        if not (got and got["outcome"] == "keep_original" and got.get("reason") == self.not_language):
            detail = {"rule": self.job.rule or self.s.rule_id, "release": (e["cand"].get("release") or "")[:120],
                      "language": {"verdict": langcheck.FAIL, "words": m["words"], "script": m["script"],
                                   "common": {c: v for c, v in m["common"].items() if v}}}
            self.store.put(self.file_key, e["sha"], lang, e["rank"], "keep_original", self.not_language, None, detail,
                           "ass" if is_ass(e["text"]) else "srt", None, e["cand"]["id"], offer=not self.best_fit)
        log.info("sync %s %s rank %d: keep_original (%s)", self.job.label, lang, e["rank"], self.not_language)
        return True

    def _duplicate(self, e):
        """The same subtitle (same words and timing; another encoding, line ending or credit line doesn't
        count) already came from another source for this file and language: judged and listed once, as the first."""
        if e["sha"] in self.fingerprints:
            return True
        try:
            e["text"] = decode(e["raw"], self.lang)   # kept: the judging below reads it again
            fp = fingerprint(e["text"])
        except Exception:   # unreadable: judged on its own, as before
            return False
        if fp in self.fingerprints:   # R3-8: every text with no dialogue line has one fingerprint (subs.NO_DIALOGUE)
            why = "no dialogue line" if fp == NO_DIALOGUE else "the same subtitle as one already judged"
            log.info("sync %s %s rank %d: %s, left out", self.job.label, self.lang, e["rank"], why)
            return True
        self.fingerprints.update((fp, e["sha"]))
        return False

    # -- the passes ------------------------------------------------------------------------------------------------
    def _stream(self):
        """Best fit with the fast path: each download judged as it ends, its original and then its one shift, so a
        waiting pick is served a pass within seconds (Kingdom of Heaven's first passes came 18 s after the request when
        all 20 downloads per language were waited for); alass after (_fit_all), in try order, and for a picked language
        on one of them as soon as FETCH_AT_ONCE are checked (_fit_early). Returns (the downloads in try order, whether
        one passed)."""
        position = {rank: i for i, (rank, _) in enumerate(self.tries)}
        entries, checked, done, early = [], [], False, False
        for f in self.w._fetch_stream(self.job, self.lang, self.tries):
            e = self._entry(*f)
            if self._duplicate(e) or self._wrong_language(e):
                continue
            entries.append(e)
            if e["got"]:
                done = self._listed_cached(e) or done
            elif self._check_one(e):
                done = True
            elif e["checked"] and self.s.shift_path:
                done = self._shift_one(e) or done
            if e.get("checked"):
                checked.append(e)
            if not (done or early) and self.lang in self.job.picked and len(checked) >= FETCH_AT_ONCE:
                early = True
                self._fit_early(checked, position)
        entries.sort(key=lambda x: position[x["rank"]])
        return entries, done

    def _originals_first(self, entries):
        """The fast path in the viewer's order (not best fit): the originals first, stopping at the first that passes
        (a stored pass counts), then each checked one's single shift. Whether one passed."""
        for e in entries:
            if e["got"]:
                if self._listed_cached(e):
                    return True
                continue
            if self._check_one(e):
                return True
        if not self.s.shift_path:
            return False
        return any(not e["got"] and e.get("checked") and self._shift_one(e) for e in entries)

    def _fit_all(self, entries):
        """alass (or the audio route) on the downloads in try order, stopping at the first that passes, unless best fit
        judges them all; a file with no text or image subtitle track stops at the first: only the audio route can judge
        it (_to_audio)."""
        for e in entries:
            if not self.streamed and self._duplicate(e):
                continue
            if e["got"]:
                if not self.s.fast_path and self._listed_cached(e) and not self.best_fit:
                    return
                continue
            if self.best_fit and not self._fitting(e):
                continue
            r, t0 = self._fit(e)
            if not self.audio and r.get("waits_audio"):   # no text or image track: only the audio route can judge it
                self._to_audio(e, entries)
                return   # nothing stored
            if r["outcome"] == "in_sync":
                r = self._in_sync_result(e, r)
            if self._record(e, r, t0) and not self.best_fit:
                return

    def _fitting(self, e):
        """Best fit: one of the SUBSYNC_MAX_TRIES alass runs taken for the download; False when the same subtitle from
        another source was fitted already, or none is left."""
        if e["sha"] in self.shas or self.fitted >= self.s.max_tries:
            return False
        self.shas.add(e["sha"])
        self.fitted += 1
        return True

    def _fit_early(self, checked, position):
        """A picked language none of whose first checked downloads fits as it is or by
        one shift: alass at once on the first of them in try order (an "as it is" measure can't see a subtitle
        that is only offset), published if it passes. It counts toward SUBSYNC_MAX_TRIES and isn't fitted again."""
        free = [x for x in checked if not x["got"] and x["sha"] not in self.shas]
        e = min(free, key=lambda x: position[x["rank"]]) if free else None
        if e is None or not self._fitting(e):
            return
        log.info("sync %s %s rank %d: picked, none of %d checked fits: alass now", self.job.label, self.lang, e["rank"],
                 len(checked))
        r, t0 = self._fit(e)
        self._record(e, self._in_sync_result(e, r) if r["outcome"] == "in_sync" else r, t0)

    def _to_audio(self, e, entries):
        """A file with no text or image subtitle track: the language is left to the audio route (job.to_audio) and,
        unless the audio route is off, the file waits for the viewer's pick (Worker._needs_pick), with the downloads
        made so far kept for the anchored guesses when every try was downloaded before the fits."""
        job, lang = self.job, self.lang
        job.to_audio.add(lang)
        if self.s.audio_choice != "off":   # a Usenet file too
            # every try downloaded before the fits (not one at a time): kept for an anchored guess (_set_provisional)
            if (self.best_fit or self.s.fast_path) and self.s.anchored_guess:
                job.fetched[lang] = [(x["cand"], x.get("text") or decode(x["raw"], lang)) for x in entries]
            if not self.w._needs_pick(job):   # a sync that starts at once logs its own line
                log.info("sync %s %s rank %d: no text or image subtitle track; the audio route waits for "
                         "the viewer to pick the Subsync entry", job.label, lang, e["rank"])
        else:   # the instance's audio route is off, or this person's choice is
            log.info("sync %s %s rank %d: no text or image subtitle track, and the audio route is off",
                     job.label, lang, e["rank"])

    # -- one download judged ---------------------------------------------------------------------------------------
    def _listed_cached(self, e):
        """A stored outcome (with an in-sync original further down listed now if it wasn't): passes?"""
        got, lang = e["got"], self.lang
        outcome = got["outcome"]
        if outcome == "in_sync" and (e["rank"] > 0 or self.best_fit) and self.s.list_in_sync:
            text = decode(e["raw"], lang)   # stored before the switch was on: list it now, unchanged
            self.store.put(self.file_key, e["sha"], lang, e["rank"], "synced",
                           f"in sync as it is (viewer's #{e['rank'] + 1}): listed unchanged", got.get("route"),
                           dict(got.get("detail") or {}, unchanged=True,
                                stacked=round(pipeline.stacked_share(parse(text)), 3)),
                           "ass" if is_ass(text) else "srt", text, e["cand"]["id"], offer=not self.best_fit,
                           original=text)
            outcome = "synced"
        ok = outcome in ("synced", "in_sync")
        if ok and pipeline.settles(dict(got, reference=(got.get("detail") or {}).get("reference")), lang):
            self.settled = True
        if ok and self.best_fit and outcome == "synced":
            self._offer(result_key(self.file_key, lang, e["sha"]))
        return ok

    def _check_one(self, e):
        """The fast path for one download: its original against the release track (a gate, no alass). In sync?"""
        s, lang = self.s, self.lang
        t0 = time.time()
        e.setdefault("text", decode(e["raw"], lang))
        e["ext"] = "ass" if is_ass(e["text"]) else "srt"
        chk = pipeline.check_original(self.index, e["text"], lang, splits=s.gate_splits, image_ref=s.image_ref,
                                      prefer_plain=s.prefer_plain, snap_within=s.snap_within if s.snap else None,
                                      better=s.better, fallback=s.fallback_reference, text_pairing=s.text_pairing)
        e["checked"] = chk is not None
        if chk is not None:   # for a pick's answer when nothing passes
            self.w._note_measure(self.job.alias, lang, e["cand"]["id"],
                                 (chk.get("gate_original") or {}).get("within200"))
        if chk and chk["in_sync"]:
            self._record(e, self._in_sync_result(e, dict(chk, fast=True)), t0)
            e["got"] = self._fresh(e)
            return True
        return False

    def _shift_one(self, e):
        """The shift path for one download whose original was judged and not in sync: one steady offset, in
        milliseconds. A pass by the main rule against its own language (no alass needed)?"""
        s = self.s
        t0 = time.time()
        r = pipeline.shift_sync(self.index, e["text"], self.lang, snap=s.snap, snap_within=s.snap_within,
                                splits=s.gate_splits, image_ref=s.image_ref, better=s.better,
                                prefer_plain=s.prefer_plain, fallback=s.fallback_reference, text_pairing=s.text_pairing)
        if not (r and r["outcome"] == "synced"):
            return False
        self._record(e, dict(r, fast=True), t0)
        e["got"] = self._fresh(e)
        return self.w.passed_as(r, r["reason"]) == "main"

    def _fit(self, e):
        """alass (or the audio route) on one download: (result, start time)."""
        s, w = self.s, self.w
        t0 = time.time()
        e.setdefault("text", decode(e["raw"], self.lang))
        e.setdefault("ext", "ass" if is_ass(e["text"]) else "srt")
        return pipeline.sync_subtitle(self.index, e["text"], self.lang, alass=s.alass, audio=self.audio,
                                      audio_lead=s.audio_lead, vad=w.vad() if self.audio else None,
                                      ffmpeg=s.ffmpeg, snap=s.snap, snap_within=s.snap_within,
                                      splits=s.gate_splits, image_ref=s.image_ref,
                                      audio_lock=w.audio_lock, better=s.better,
                                      audio_better=s.audio_better, audio_budget=s.audio_budget,
                                      prefer_plain=s.prefer_plain, fallback=s.fallback_reference,
                                      text_pairing=s.text_pairing, unsettled=not self.settled,
                                      sectioned=s.sections, audio_extra=s.audio_extra), t0

    def _in_sync_result(self, e, base):
        """An original in sync as it is: listed (when listing is on) as its magnet copy, else unchanged, with the
        reason of how it was judged (the audio route's own)."""
        r = dict(base, outcome="in_sync", reason=base["reason"] if base.get("route") == "audio"
                 else "original passes against the release track")
        if not ((e["rank"] > 0 or self.best_fit) and self.s.list_in_sync):
            return {k: v for k, v in r.items() if k not in ("body", "magnet")}
        listed = f"in sync as it is (viewer's #{e['rank'] + 1})"
        if r.get("cross"):
            listed += ", aligned to another language's track"
        if r.get("body") is not None:
            return dict(r, outcome="synced", reason=f"{listed}: lines snapped to the release's own")
        return dict(r, outcome="synced", reason=f"{listed}: listed unchanged", body=e["text"], unchanged=True)

    def _record(self, e, r, t0):
        """A judged download's result stored (offered at once unless best fit offers it) and logged. Passes?"""
        lang = self.lang
        detail = {k: r[k] for k in self.RECORDED if k in r}
        detail["seconds"] = round(time.time() - t0, 2)
        detail["rule"] = self.job.rule or self.s.rule_id
        detail["release"] = (e["cand"].get("release") or "")[:120]
        detail["stacked"] = round(pipeline.stacked_share(parse(e.get("text") or decode(e["raw"], lang))), 3)
        key = self.store.put(self.file_key, e["sha"], lang, e["rank"], r["outcome"], r.get("reason"), r.get("route"),
                             detail, e["ext"], r.get("body"), e["cand"]["id"], offer=not self.best_fit,
                             original=e.get("text") or decode(e["raw"], lang))
        log.info("sync %s %s rank %d: %s (%s)", self.job.label, lang, e["rank"], r["outcome"], r.get("reason"))
        if pipeline.settles(r, lang):
            self.settled = True
        if self.best_fit and r["outcome"] == "synced":
            self._offer(key)
        return r["outcome"] in ("synced", "in_sync")

    def _offer(self, key):
        """Best fit: a passing result; a pick waiting for this language gets the best one so far."""
        self.passed.append(key)
        rank = self.w.fit_rank(self.store.get(key) or {})
        if self.best_rank is None or rank < self.best_rank:
            self.best_rank = rank
            self.w._publish(self.job.alias, self.lang, key)

    def _result(self):
        """The key to offer: with best fit, every candidate judged, the best fit first, up to SUBSYNC_MAIN_ALTERNATIVES
        offered; else the last good result stored."""
        if self.best_fit:
            ranked = sorted(dict.fromkeys(self.passed), key=lambda k: self.w.fit_rank(self.store.get(k) or {}))
            self.store.set_offers(self.file_key, self.lang, ranked[: self.s.alternatives])
            return ranked[0] if ranked else None
        lg = self.store.last_good(self.file_key, self.lang)
        return lg["key"] if lg else None
