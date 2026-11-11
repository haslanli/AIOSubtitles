"""The queue and the worker: one file at a time.

A job is a file a viewer opened or played. A subtitle request that names the file (its name, size or hash, as Stremio
and AIOStreams send them) queues it; the file is then found in subsync's own AIOStreams listing (cached debrid only).
Reads count toward a daily budget (default 50 GB, bytes received). Any HTTP 403/429 stops every read for a back-off
(15 min, doubling to 6 h), since the refusal is most likely per account. Every connection is closed after its read
(reader.py).
"""
import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import choose, pipeline
from .langs import lang3
from .mkv import MkvIndex, NotMatroska  # noqa: F401
from .mp4 import open_index
from .reader import RangeReader, ReadFailed, Refused, Stopped
from .sources import expected_size, fetch_subtitle, same_hash
from .store import result_key
from .subs import decode, is_ass, parse

log = logging.getLogger("subsync.worker")
UNLISTED_S = 3600
FETCHED_CAP = 32 * 1024 * 1024   # downloads kept for the audio job that follows a pick (worker._fetch_one), in bytes


class Safety:
    """The refusal back-off every read checks first."""

    def __init__(self, backoff_s=900, backoff_max_s=6 * 3600, clock=time.time):
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


class JobGuard:
    """Checked before every read of a job: the back-off, the day's budget and the job's own byte cap."""

    def __init__(self, safety, store, daily_budget, file_cap=None):
        self.safety, self.store = safety, store
        self.daily_budget, self.file_cap = daily_budget, file_cap
        self.bytes = 0
        self._lock = threading.Lock()   # audio windows are read in parallel

    def before_read(self, length):
        if self.safety.backing_off():
            raise Stopped("backing off after a refusal")
        if self.store.bytes_today("read") + length > self.daily_budget:
            raise Stopped("daily budget spent")
        if self.file_cap and self.bytes + length > self.file_cap:
            raise Stopped("file byte cap")

    def after_read(self, n):
        with self._lock:
            self.bytes += n
        self.store.add_bytes("read", n)
        if n:
            self.safety.read_ok()


class Job:
    def __init__(self, type_, video_id, hints=None):
        self.type, self.video_id = type_, video_id
        self.hints = {k: v for k, v in (hints or {}).items() if v}
        self.fetched = {}         # lang -> [(candidate, text)] downloaded for a file that waits for the audio route
        self.not_before = 0.0
        self.audio = False        # may this job use the audio route (set when the viewer picked subsync's entry)

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
        return f"title:{self.type}/{self.video_id}"

    @property
    def label(self):
        return f"{self.type}/{self.video_id}"


def rewrite(url, rules):
    """Public stream URLs to internal ones (SUBSYNC_URL_REWRITE 'https://public=http://internal,...')."""
    for rule in (rules or "").split(","):
        src, _, dst = rule.partition("=")
        if src and dst and url.startswith(src.strip()):
            return dst.strip() + url[len(src.strip()):]
    return url


class Worker(threading.Thread):
    def __init__(self, settings, store, aios, safety, vad_factory=None, extra=None):
        super().__init__(daemon=True, name="subsync-worker")
        self.s, self.store, self.aios, self.safety = settings, store, aios, safety
        self.extra = extra          # extra.ExtraSources: sources subsync searches itself
        self.vad_factory = vad_factory
        self._vad = None
        self.queue, self.jobs = [], {}   # jobs in arrival order; alias -> queued job
        self.cv = threading.Condition()
        self.current = None       # the job the main worker runs (index reads, text and image routes)
        self.current_audio = None  # the job the audio worker runs, so an audio route never holds another file
        self.done = {}            # alias -> {lang: result key | None}: the best pass so far, for pending entries and picks
        self.judged = {}          # alias -> languages whose every candidate is judged (their `done` key is final)
        self.originals = {}       # alias -> {lang: {"url", "id"} of the original subtitle}, the pending entries' fallback
        self.provisional = {}     # alias -> {lang: body}: a waiting file's other languages, anchored to the main guess
        self.lists = {}           # alias -> (time, candidates): a text pass's subtitle list, for the audio job that follows
        self.fetched_subs = {}    # subtitle url -> (time, bytes): downloads shared by a file's text pass and audio job
        self.unlisted = {}        # alias -> when a file was last not found in the listing
        self.pickable = {}        # alias -> job of a file that needs the audio route and waits for the viewer's pick
        self.picked_early = {}    # alias -> when its entry was downloaded before the file was known to need audio
        self.seen = {}            # alias -> (type, video_id, hints) of its last job, so a pick can ask for the list again
        self.stop_flag = False
        self.last_expire = 0.0
        self.vad_lock = threading.Lock()
        self.audio_lock = threading.Lock()     # one audio fit at a time (shared reader)

    # -- queue -------------------------------------------------------------------------------------------------
    def submit(self, job):
        with self.cv:
            self.seen[job.alias] = (job.type, job.video_id, dict(job.hints))
            while len(self.seen) > 2000:
                self.seen.pop(next(iter(self.seen)))
            old = self.jobs.get(job.alias)
            if old is not None:
                old.audio = old.audio or job.audio
                return old
            running = self._running(job.alias)
            if running is not None and (running.audio or not job.audio):
                return running
            self.jobs[job.alias] = job
            self.queue.append(job)
            self.judged.pop(job.alias, None)   # a pick waits for this job's results, not an earlier job's
            self.cv.notify_all()
            return job

    def audio_allowed(self, job):
        return self.s.audio_route and (job.audio or not self.s.audio_on_pick)

    def _restore(self, alias):
        """After a restart: a file that waited for a pick, and its original subtitles, from the store (cv held)."""
        if alias not in self.pickable:
            row = self.store.pickable_get(alias)
            if row:
                self.pickable[alias] = Job(row["type"], row["video_id"], row["hints"])
        if alias not in self.originals:
            langs = self.store.originals_get(alias)
            if langs is not None:
                self.originals[alias] = langs

    def waits_for_pick(self, alias):
        with self.cv:
            self._restore(alias)
            return alias in self.pickable

    def ask_list_again(self, alias):
        """A pick for a file whose subtitle list is not known (it came back empty): queue its job again, which
        fetches the list first. Returns True if a job is queued or running for it."""
        with self.cv:
            seen = self.seen.get(alias)
        if seen is None:
            return self.busy_with(alias)
        if not self.busy_with(alias):
            self.submit(Job(*seen))
        return True

    def known_originals(self, alias):
        """The original subtitle per language for a file subsync listed, or None if its list is not known."""
        with self.cv:
            self._restore(alias)
            return self.originals.get(alias)

    def pick(self, alias):
        """The viewer downloaded subsync's entry for this file: start its audio route, once. Returns True if started."""
        with self.cv:
            self._restore(alias)
            waiting = self.pickable.pop(alias, None)
            if waiting is not None:
                self.store.pickable_drop(alias)
            if waiting is None:
                if self.s.audio_route and self.s.audio_on_pick:
                    self.picked_early[alias] = time.time()
                    while len(self.picked_early) > 2000:
                        self.picked_early.pop(next(iter(self.picked_early)))
                return False
        job = Job(waiting.type, waiting.video_id, waiting.hints)
        job.audio = True
        log.info("picked %s: the audio route starts", job.label)
        self.submit(job)
        return True

    def _needs_pick(self, job):
        """A file with no usable subtitle track of its own, read index-only: it waits for the viewer's pick. True when
        the pick came already (its audio job is queued now)."""
        with self.cv:
            early = self.picked_early.pop(job.alias, None)
            if early is None or time.time() - early > 3600:
                self.pickable[job.alias] = job
                while len(self.pickable) > 2000:
                    self.pickable.pop(next(iter(self.pickable)))
                self.store.pickable_put(job.alias, job.type, job.video_id, job.hints)   # kept across a restart
                if self.originals.get(job.alias) is not None:
                    self.store.originals_put(job.alias, self.originals[job.alias])
                return False
        again = Job(job.type, job.video_id, job.hints)   # its entry was already downloaded while this read ran
        again.audio = True
        log.info("picked %s: the audio route starts", job.label)
        self.submit(again)
        return True

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
        if row.get("outcome") == "in_sync" or d.get("unchanged") or d.get("magnet") or "as it is" in reason:
            cls, g = 0, d.get("gate_original") or {}
        elif d.get("shift_fit") and reason.startswith("passes"):   # moved as a whole: its author's own timing
            cls, g = 0, d.get("gate_synced") or {}
        elif "clearly better" in reason:
            cls, g = 2, d.get("gate_synced") or {}
        else:
            cls, g = 1, d.get("gate_synced") or d.get("gate_snapped") or {}
        med = g.get("median_ms")
        stacked = (d.get("stacked") or 0.0) >= pipeline.STACKED_SHARE
        within = (g.get("within200") or 0.0) - (pipeline.STACKED_PENALTY if stacked else 0.0)
        return (cls, -within, med if med is not None else 1e9)

    def fresh(self, key):
        """A stored result that needs no new attempt now: as the store says, except that one judged under another pass
        rule (code version or rule settings) is judged again: a "keep original" always, a passing one when it was judged
        against the file's own text or image track (only the index is read again); an audio-route copy that passed
        stays offered (judging it again reads audio)."""
        got = self.store.fresh(key)
        if got and (got.get("detail") or {}).get("rule") != self.s.rule_id \
                and (got["outcome"] == "keep_original" or got.get("route") != "audio"):
            return None
        return got

    def recently_unlisted(self, alias, within=UNLISTED_S):
        """A file the listing didn't have (Usenet, uncached) is not looked for again for an hour."""
        t = self.unlisted.get(alias)
        return t is not None and time.time() - t < within

    def busy_with(self, alias):
        with self.cv:
            return alias in self.jobs or self._running(alias) is not None

    def busy_reading(self, alias):
        """A job works on the file now that gives its result in seconds (not the audio route, which takes minutes)."""
        with self.cv:
            job = self.jobs.get(alias) or self._running(alias)
            return job is not None and not job.audio

    def wait_for(self, alias, lang, timeout, final=False):
        """Wait up to timeout for a language's result on a queued file: (result key or None, original URL or None). The
        first published pass ends the wait; with `final`, only the language's final result (every candidate judged) does,
        and on timeout the best pass published so far is returned."""
        end = time.time() + timeout
        with self.cv:
            while True:
                got = self.done.get(alias, {})
                original = (self.originals.get(alias, {}).get(lang) or {}).get("url")
                if lang in got and (not final or lang in self.judged.get(alias, ())):
                    return got[lang], original
                if not (alias in self.jobs or self._running(alias) is not None):
                    return got.get(lang), original
                left = end - time.time()
                if left <= 0:
                    return got.get(lang), original
                self.cv.wait(left)

    def wait_for_file(self, alias, timeout):
        """Wait up to timeout for a queued file's languages; returns {lang: {"url", "id"} of the original} once its
        subtitle list is known (None while it isn't)."""
        end = time.time() + timeout
        with self.cv:
            while True:
                busy = alias in self.jobs or self._running(alias) is not None
                done = self.done.get(alias, {})
                if not busy or all(lang in done for lang in self.s.languages):
                    break
                left = end - time.time()
                if left <= 0:
                    break
                self.cv.wait(left)
            return self.originals.get(alias)

    def wait_for_originals(self, alias, timeout):
        """The file's original subtitles per language once its subtitle list is known, waiting up to timeout while its
        job is queued or running (None if still unknown)."""
        end = time.time() + timeout
        with self.cv:
            while alias not in self.originals:
                busy = alias in self.jobs or self._running(alias) is not None
                left = end - time.time()
                if not busy or left <= 0:
                    break
                self.cv.wait(left)
            return self.originals.get(alias)

    def _running(self, alias):
        """The job running for a file on either worker, or None (cv held)."""
        for job in (self.current, self.current_audio):
            if job is not None and job.alias == alias:
                return job
        return None

    def prioritize(self, alias):
        """The viewer downloaded subsync's entry for this file: its queued job goes first."""
        with self.cv:
            job = self.jobs.get(alias)
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
        if main_text is None:
            if job.fetched:
                log.info("no anchored guesses %s: the %s guess was not among its downloads", job.label, main)
            return
        out = {}
        for lang, fetched in job.fetched.items():
            if lang == main:
                continue
            got = pipeline.anchored_guess(main_text, [t for _, t in fetched])
            if got:
                out[lang] = got["body"]
                log.info("anchored %s %s: candidate %s lines up with the %s guess (%.0f %%)", job.label, lang,
                         fetched[got["index"]][0]["id"][:40], main, 100 * got["share"])
        with self.cv:   # a language the audio route has judged keeps its verdict, not a guess
            judged = self.judged.get(job.alias) or set()
            self.provisional[job.alias] = {lang: body for lang, body in out.items() if lang not in judged}
            while len(self.provisional) > 500:
                self.provisional.pop(next(iter(self.provisional)))

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
                self.cv.wait(5.0)
        return None

    # -- the loop ----------------------------------------------------------------------------------------------
    def start(self):
        super().start()
        self.audio_worker = threading.Thread(target=self._loop, args=("audio",), daemon=True, name="subsync-audio")
        self.audio_worker.start()

    def run(self):
        self._loop("main")

    def _loop(self, lane):
        while not self.stop_flag:
            job = self._next(lane)
            if job is None:
                break
            try:
                self.process(job)
            except Stopped as e:
                log.info("stopped %s: %s", job.label, e)
                job.not_before = time.time() + self.s.retry_stopped_s
                self._requeue(job)
            except Refused as e:
                # most likely a per-account limit: every read waits; this file backs off 1 h / 6 h / 24 h
                wait = self.safety.refused()
                self.store.file_failed(job.alias)
                log.warning("refused %s (%s): no reads for %d s", job.label, e, wait)
            except Exception as e:  # never let one file stop the worker
                log.warning("failed %s: %s", job.label, type(e).__name__)
            finally:
                with self.cv:
                    if lane == "audio":
                        self.current_audio = None
                    else:
                        self.current = None
                    self.done.setdefault(job.alias, {})
                    for lang in self.s.languages:
                        self.done[job.alias].setdefault(lang, None)
                    self.cv.notify_all()
                    while len(self.done) > 500:
                        self.done.pop(next(iter(self.done)))
                    while len(self.judged) > 500:
                        self.judged.pop(next(iter(self.judged)))
                    while len(self.originals) > 500:
                        self.originals.pop(next(iter(self.originals)))
            if time.time() - self.last_expire > 3600:
                self.last_expire = time.time()
                self.store.expire()

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

    def process(self, job):
        hints = dict(job.hints)
        row = self.aios.find_row(job.type, job.video_id, hints)
        if row is None:
            log.info("file not in the listing %s", job.label)
            self.unlisted[job.alias] = time.time()
            while len(self.unlisted) > 5000:
                self.unlisted.pop(next(iter(self.unlisted)))
            return
        url = row["url"]
        if self.store.file_blocked(job.alias):
            log.info("file backing off after read failures %s", job.label)
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
        guard = JobGuard(self.safety, self.store, self.s.daily_budget,
                         self.s.file_cap if self.audio_allowed(job) else self.s.index_cap)
        reader = RangeReader(rewrite(url, self.s.url_rewrite), job.label, guard, timeout=self.s.read_timeout,
                             size=expected_size(hints, row), size_tolerance=0.0005, size_slack=self.s.size_slack)
        try:
            index = open_index(reader)
            oshash = index.read_hash()
        except NotMatroska as e:
            log.info("file not readable as Matroska or MP4 %s (%s): original kept", job.label, e)
            self.store.file_failed(job.alias)
            return
        except Refused:
            raise
        except ReadFailed as e:
            retry = self.store.file_failed(job.alias)
            log.info("read failed %s (%s), retry %s", job.label, e, "never" if retry is None else time.strftime("%H:%M", time.gmtime(retry)))
            return
        if hints.get("videoHash") and not same_hash(hints["videoHash"], oshash):
            # 18a: the listed row is another file than the one played (same name, another release): nothing of it is
            # this file's, and the listing is not asked for it again for an hour
            log.info("the listed row is not the played file %s: not synced", job.label)
            self.unlisted[job.alias] = time.time()
            return
        if not hints.get("videoHash"):   # the file's own hash is known now: a subtitle made for it is the better guess
            self._set_originals(job.alias, candidates, hints.get("filename"), oshash)
        file_key = f"{oshash}-{index.size}"
        for a in {job.alias, f"hash:{oshash}|{index.size}",
                  f"name:{hints.get('filename')}|{hints.get('videoSize')}" if hints.get("filename") else None} - {None}:
            self.store.alias(a, file_key)
        tracks = index.text_tracks()
        images = index.image_tracks()
        self.store.file_read(file_key, index.size, oshash, any(not t["forced"] and t["cues"] for t in tracks),
                             {"tracks": [{k: t[k] for k in ("number", "language", "codec", "forced")} | {"cues": len(t["cues"])}
                                         for t in tracks + images], "bytes": reader.bytes,
                              "image_check": pipeline.image_check(pipeline.text_references(index), images), "rule": self.s.rule_id})
        per_lang = {lang: [c for c in candidates if c["lang"] == lang] for lang in self.s.languages}
        if self.s.audio_route and not self.audio_allowed(job) and not self._has_reference(index):
            self._await_pick(job, per_lang, hints.get("filename"), oshash)
            log.info("done %s file=%s bytes=%d requests=%d", job.label, file_key, reader.bytes, reader.requests)
            return
        self._sync_all(job, per_lang, lambda lang, cands: self._sync_language(job, index, file_key, lang, cands, oshash,
                                                                              hints.get("filename")))
        log.info("done %s file=%s bytes=%d requests=%d", job.label, file_key, reader.bytes, reader.requests)

    LIST_TTL_S = 600       # a text pass's subtitle list and downloads, reused by the audio job that follows a pick

    def _candidates(self, job, hints):
        """The file's subtitle list (AIOStreams and the extra sources). An audio job reuses the list its text pass got
        within LIST_TTL_S (asking again took 0.7 to 1.8 s of a pick's 14 s)."""
        now = time.time()
        with self.cv:
            got = self.lists.get(job.alias)
        if job.audio and got and now - got[0] < self.LIST_TTL_S:
            return [dict(c) for c in got[1]]
        candidates = self.aios.subtitles(job.type, job.video_id, hints)
        if self.extra is not None:
            seen = {c["url"] for c in candidates}
            try:
                candidates += [c for c in self.extra.search(job.type, job.video_id, hints=hints) if c["url"] not in seen]
            except Exception as e:  # never let the extra sources stop the file
                log.info("extra sources failed %s: %s", job.label, type(e).__name__)
        if candidates:
            with self.cv:
                self.lists[job.alias] = (now, [dict(c) for c in candidates])
                while len(self.lists) > 200:
                    self.lists.pop(next(iter(self.lists)))
        return candidates

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
        return pipeline.choose_reference(index, 0, self.s.languages[0] if self.s.languages else "eng", self.s.image_ref,
                                         self.s.prefer_plain, self.s.fallback_reference) is not None

    def _await_pick(self, job, per_lang, filename, oshash):
        """A file with no subtitle track of its own: nothing can be judged until the viewer picks subsync's entry and
        the audio route starts. Its text pass does only what a pick serves meanwhile (the best guess, and with
        SUBSYNC_ANCHORED_GUESS the other languages lined up with it, from the first SUBSYNC_GATE_TRIES downloads), then
        hands over at once: 12 Angry Men's audio route started 15 s after the pick, once ~40 downloads had been judged
        for nothing. When the pick has come already, the languages are left open, so it keeps waiting (up to the pick
        wait) for the audio route's first pass instead of being served the original as this pass ends."""
        if self.s.anchored_guess and len(per_lang) > 1:
            main = self.s.languages[0]
            guess = ((self.originals.get(job.alias) or {}).get(main) or {}).get("id")
            want = {}
            for lang, cands in per_lang.items():
                if lang == main:
                    want[lang] = [(r, c) for r, c in enumerate(cands) if c["id"] == guess][:1]
                else:
                    order = choose.order(cands, filename, oshash, skip=self.s.skip_ai) if self.s.rank_by_release \
                        else list(enumerate(cands))
                    want[lang] = order[: self.s.gate_tries]
            for lang, tries in want.items():
                got = self._fetch_all(job, lang, tries)
                job.fetched[lang] = [(cand, decode(raw, lang)) for _, cand, raw in got]
            self._set_provisional(job)
        log.info("no text or image subtitle track %s: the audio route waits for the viewer to pick the Subsync entry",
                 job.label)
        self._needs_pick(job)
        if self._audio_queued(job.alias):
            return   # picked already: the audio job publishes; a waiting pick gets its first pass
        for lang in per_lang:
            self._publish(job.alias, lang, None, final=True)

    def _sync_all(self, job, per_lang, sync):
        """Judge every language (sync(lang, candidates) -> result key) and publish each as final. A language that waits
        for the audio route is published once the anchored guesses are set, also when another language raised (the
        first error is raised after)."""
        deferred, errors = {}, []
        try:
            if self.s.parallel_languages and len(per_lang) > 1:
                with ThreadPoolExecutor(max_workers=len(per_lang), thread_name_prefix="subsync-lang") as pool:
                    futures = {pool.submit(sync, lang, cands): lang for lang, cands in per_lang.items()}
                    for fut in as_completed(futures):
                        try:
                            self._finish(job, futures[fut], fut.result(), deferred)
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
        if errors:
            raise errors[0]

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
        a pick after a restart still finds it): with SUBSYNC_RANK_BY_RELEASE the likeliest to fit the file as it is
        (18g: Detectorists' first download was the viewer's first, a DVD rip 41.7 s off), else the viewer's first."""
        fallbacks = {}
        for lang in self.s.languages:
            mine = [c for c in candidates if c["lang"] == lang]
            guess = choose.best_guess(mine, filename, file_hash, skip=self.s.skip_ai) if self.s.rank_by_release else None
            pick = guess or (mine[0] if mine else None)
            fallbacks[lang] = {"url": pick["url"], "id": pick["id"]} if pick else None
        with self.cv:
            self.originals[alias] = fallbacks
            self.cv.notify_all()
        self.store.originals_put(alias, fallbacks)

    def _sync_language(self, job, index, file_key, lang, candidates, oshash=None, filename=None):
        """The viewer's first subtitle, then subtitles made for this release, then the rest, until one
        passes or `max_tries` were tried. Returns the last_good key to offer, if any. `rank` stays the viewer's order.
        With SUBSYNC_LIST_IN_SYNC on, an original further down the list that is in sync as it is gets listed as an
        unchanged "(synced)" copy, so the viewer can tell which one fits this file; the first one needs no copy.
        With SUBSYNC_FAST_PATH on, every try is downloaded at once and its original checked first (a gate, no
        alass), so an in-sync original is listed within seconds; re-timing runs only when none is in sync. With the audio
        route off, a file with no text or image subtitle track stores nothing, so turning the route on later syncs it."""
        best_fit = self.s.best_fit
        ref = pipeline.choose_reference(index, 0, lang, False, self.s.prefer_plain, self.s.fallback_reference) \
            if hasattr(index, "text_tracks") else None
        ref_cues = len(ref["cues"]) if ref else None
        if self.s.rank_by_release:
            tries = choose.order(candidates, filename, oshash, skip=self.s.skip_ai, ref_cues=ref_cues)
        else:
            tries = [(r, c) for r, c in enumerate(candidates) if not (self.s.skip_ai and choose.unwanted(c))]
        # best fit with the fast path: the originals of up to SUBSYNC_GATE_TRIES are checked (cheap); alass runs on at
        # most SUBSYNC_MAX_TRIES of them
        tries = tries[: max(self.s.max_tries, self.s.gate_tries) if best_fit and self.s.fast_path else self.s.max_tries]
        passed = []        # best fit: the keys of passing results
        best_so_far = []   # best fit: (fit rank, key) of the best pass published so far
        audio = self.audio_allowed(job)
        def entry(rank, cand, raw):
            sha = hashlib.sha256(raw).hexdigest()
            return {"rank": rank, "cand": cand, "raw": raw, "sha": sha, "got": self.fresh(result_key(file_key, sha))}

        streamed = best_fit and self.s.fast_path   # each download judged as it ends (below)
        if streamed:
            entries = []
        elif self.s.fast_path:   # every try at once, so the originals can be checked first
            entries = [entry(*f) for f in self._fetch_all(job, lang, tries)]
        else:                  # as in v3: one at a time, stopping at the first that passes
            entries = (entry(*f) for f in (self._fetch_one(job, lang, rank, cand) for rank, cand in tries) if f)

        def listed_cached(e):
            """A stored outcome (with an in-sync original further down listed now if it wasn't): passes?"""
            got = e["got"]
            outcome = got["outcome"]
            if outcome == "in_sync" and (e["rank"] > 0 or best_fit) and self.s.list_in_sync:
                text = decode(e["raw"], lang)   # stored before the switch was on: list it now, unchanged
                self.store.put(file_key, e["sha"], lang, e["rank"], "synced",
                               f"in sync as it is (viewer's #{e['rank'] + 1}): listed unchanged", got.get("route"),
                               dict(got.get("detail") or {}, unchanged=True,
                                    stacked=round(pipeline.stacked_share(parse(text)), 3)), "ass" if is_ass(text) else "srt", text,
                               e["cand"]["id"], offer=not best_fit)
                outcome = "synced"
            ok = outcome in ("synced", "in_sync")
            if ok and best_fit and outcome == "synced":
                offer(result_key(file_key, e["sha"]))
            return ok

        def offer(key):
            """Best fit: a passing result; a pick waiting for this language gets the best one so far."""
            passed.append(key)
            rank = self.fit_rank(self.store.get(key) or {})
            if not best_so_far or rank < best_so_far[0][0]:
                best_so_far[:] = [(rank, key)]
                self._publish(job.alias, lang, key)

        def record(e, r, t0):
            detail = {k: r[k] for k in ("route", "reference", "gate_original", "gate_synced", "gate_snapped", "refined",
                                        "unchanged", "magnet", "shift", "model", "windows", "audio_s", "lines", "dialogue",
                                        "alass_error", "fast", "better", "audio_track", "shift_fit", "cross", "aligned",
                                        "recentred_ms") if k in r}
            detail["seconds"] = round(time.time() - t0, 2)
            detail["rule"] = self.s.rule_id
            detail["release"] = (e["cand"].get("release") or "")[:120]
            detail["stacked"] = round(pipeline.stacked_share(parse(e.get("text") or decode(e["raw"], lang))), 3)
            key = self.store.put(file_key, e["sha"], lang, e["rank"], r["outcome"], r.get("reason"), r.get("route"), detail,
                                 e["ext"], r.get("body"), e["cand"]["id"], offer=not best_fit)
            log.info("sync %s %s rank %d: %s (%s)", job.label, lang, e["rank"], r["outcome"], r.get("reason"))
            if best_fit and r["outcome"] == "synced":
                offer(key)
            return r["outcome"] in ("synced", "in_sync")

        def in_sync_result(e, base):
            """An original in sync as it is: listed (when listing is on) as its magnet copy, else unchanged."""
            r = dict(base, outcome="in_sync", reason="original passes against the release track")
            if not ((e["rank"] > 0 or best_fit) and self.s.list_in_sync):
                return {k: v for k, v in r.items() if k not in ("body", "magnet")}
            listed = f"in sync as it is (viewer's #{e['rank'] + 1})"
            if r.get("cross"):
                listed += ", aligned to another language's track"
            if r.get("body") is not None:
                return dict(r, outcome="synced", reason=f"{listed}: lines snapped to the release's own")
            return dict(r, outcome="synced", reason=f"{listed}: listed unchanged", body=e["text"], unchanged=True)

        def check_one(e):
            """The fast path for one download: its original against the release track (a gate, no alass). In sync?"""
            t0 = time.time()
            e["text"] = decode(e["raw"], lang)
            e["ext"] = "ass" if is_ass(e["text"]) else "srt"
            chk = pipeline.check_original(index, e["text"], lang, splits=self.s.gate_splits, image_ref=self.s.image_ref,
                                          prefer_plain=self.s.prefer_plain,
                                          snap_within=self.s.snap_within if self.s.snap else None,
                                          better=self.s.better, fallback=self.s.fallback_reference)
            e["checked"] = chk is not None
            if chk and chk["in_sync"]:
                record(e, in_sync_result(e, dict(chk, fast=True)), t0)
                e["got"] = self.fresh(result_key(file_key, e["sha"]))
                return True
            return False

        def shift_one(e):
            """The shift path for one download whose original was judged and not in sync: one steady offset, in
            milliseconds. A pass by the main rule against its own language (no alass needed)?"""
            t0 = time.time()
            r = pipeline.shift_sync(index, e["text"], e["ext"], lang, snap=self.s.snap, snap_within=self.s.snap_within,
                                    splits=self.s.gate_splits, image_ref=self.s.image_ref, better=self.s.better,
                                    prefer_plain=self.s.prefer_plain, fallback=self.s.fallback_reference)
            if not (r and r["outcome"] == "synced"):
                return False
            record(e, dict(r, fast=True), t0)
            e["got"] = self.fresh(result_key(file_key, e["sha"]))
            return r["reason"].startswith("passes")

        done = False
        if streamed:
            # best fit: each download judged as it ends, its original and then its one shift, so a waiting pick is
            # served a pass within seconds (Kingdom of Heaven's first passes came 18 s after the request when all 20
            # downloads per language were waited for); alass below, in try order
            position = {rank: i for i, (rank, _) in enumerate(tries)}
            for f in self._fetch_stream(job, lang, tries):
                e = entry(*f)
                entries.append(e)
                if e["got"]:
                    done = listed_cached(e) or done
                elif check_one(e):
                    done = True
                elif e["checked"] and self.s.shift_path:
                    done = shift_one(e) or done
            entries.sort(key=lambda x: position[x["rank"]])
        elif best_fit:
            entries = list(entries)
        elif self.s.fast_path:   # the viewer's order: originals first, stopping at the first that passes
            for e in entries:
                if e["got"]:
                    if listed_cached(e):
                        done = True
                        break
                    continue
                if check_one(e):
                    done = True
                    break
            if self.s.shift_path and not done:
                for e in entries:
                    if not e["got"] and e.get("checked") and shift_one(e):
                        done = True
                        break
        if not done:
            fitted, shas = 0, set()
            for e in entries:
                if e["got"]:
                    if not self.s.fast_path and listed_cached(e) and not best_fit:
                        break
                    continue
                if best_fit:   # the same subtitle from two sources is fitted once; alass on SUBSYNC_MAX_TRIES at most
                    if e["sha"] in shas or fitted >= self.s.max_tries:
                        continue
                    shas.add(e["sha"])
                    fitted += 1
                t0 = time.time()
                e.setdefault("text", decode(e["raw"], lang))
                e.setdefault("ext", "ass" if is_ass(e["text"]) else "srt")
                r = pipeline.sync_subtitle(index, e["text"], e["ext"], lang, alass=self.s.alass, audio=audio,
                                           audio_lead=self.s.audio_lead, vad=self.vad() if audio else None,
                                           ffmpeg=self.s.ffmpeg, snap=self.s.snap, snap_within=self.s.snap_within,
                                           splits=self.s.gate_splits, image_ref=self.s.image_ref,
                                           audio_lock=self.audio_lock, better=self.s.better,
                                           audio_better=self.s.audio_better, audio_budget=self.s.audio_budget,
                                           prefer_plain=self.s.prefer_plain, fallback=self.s.fallback_reference)
                if not audio and r.get("route") is None and r["outcome"] == "keep_original" \
                        and "audio route is off" in (r.get("reason") or ""):
                    if self.s.audio_route:
                        log.info("sync %s %s rank %d: no text or image subtitle track; the audio route waits for the "
                                 "viewer to pick the Subsync entry", job.label, lang, e["rank"])
                        if isinstance(entries, list) and self.s.anchored_guess:   # for an anchored guess (_set_provisional)
                            job.fetched[lang] = [(x["cand"], x.get("text") or decode(x["raw"], lang)) for x in entries]
                        self._needs_pick(job)
                    else:
                        log.info("sync %s %s rank %d: no text or image subtitle track, and the audio route is off",
                                 job.label, lang, e["rank"])
                    break   # nothing stored
                if r["outcome"] == "in_sync":
                    r = in_sync_result(e, r)
                if record(e, r, t0) and not best_fit:
                    break
        if best_fit:
            # every candidate judged: the best fit first, up to SUBSYNC_MAIN_ALTERNATIVES offered
            ranked = sorted(dict.fromkeys(passed), key=lambda k: self.fit_rank(self.store.get(k) or {}))
            self.store.set_offers(file_key, lang, ranked[: self.s.alternatives])
            return ranked[0] if ranked else None
        lg = self.store.last_good(file_key, lang)
        return lg["key"] if lg else None

    def _fetch_one(self, job, lang, rank, cand):
        url, now = cand["url"], time.time()
        with self.cv:
            got = self.fetched_subs.get(url)
        if got and now - got[0] < self.LIST_TTL_S:
            return rank, cand, got[1]
        try:
            raw = fetch_subtitle(url)
            with self.cv:
                self.fetched_subs[url] = (now, raw)
                while len(self.fetched_subs) > 1 and sum(len(r) for _, r in self.fetched_subs.values()) > FETCHED_CAP:
                    self.fetched_subs.pop(next(iter(self.fetched_subs)))
            return rank, cand, raw
        except Exception as e:
            log.info("subtitle fetch failed %s %s rank %d: %s", job.label, lang, rank, type(e).__name__)
            return None

    def _fetch_stream(self, job, lang, tries):
        """Best fit's downloads: up to 4 at once, each yielded as it ends; failed ones are left out."""
        pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="subsync-fetch")
        try:
            for fut in as_completed([pool.submit(self._fetch_one, job, lang, rank, cand) for rank, cand in tries]):
                got = fut.result()
                if got is not None:
                    yield got
        finally:   # closed early (judging raised): downloads not started are dropped, running ones end on their own
            pool.shutdown(wait=False, cancel_futures=True)

    def _fetch_all(self, job, lang, tries):
        """The fast path's downloads: up to 4 at once, results in try order; failed ones are left out."""
        if len(tries) <= 1:
            got = [self._fetch_one(job, lang, rank, cand) for rank, cand in tries]
        else:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="subsync-fetch") as pool:
                got = list(pool.map(lambda rc: self._fetch_one(job, lang, *rc), tries))
        return [g for g in got if g is not None]


def lang_list(text):
    return [lang3(x) for x in (text or "").split(",") if x.strip()]
