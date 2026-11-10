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
from .sources import fetch_subtitle
from .store import result_key
from .subs import decode, is_ass

log = logging.getLogger("subsync.worker")
UNLISTED_S = 3600


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

    def before_read(self, length):
        if self.safety.backing_off():
            raise Stopped("backing off after a refusal")
        if self.store.bytes_today("read") + length > self.daily_budget:
            raise Stopped("daily budget spent")
        if self.file_cap and self.bytes + length > self.file_cap:
            raise Stopped("file byte cap")

    def after_read(self, n):
        self.bytes += n
        self.store.add_bytes("read", n)
        if n:
            self.safety.read_ok()


class Job:
    def __init__(self, type_, video_id, hints=None):
        self.type, self.video_id = type_, video_id
        self.hints = {k: v for k, v in (hints or {}).items() if v}
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
    def __init__(self, settings, store, aios, safety, vad_factory=None):
        super().__init__(daemon=True, name="subsync-worker")
        self.s, self.store, self.aios, self.safety = settings, store, aios, safety
        self.vad_factory = vad_factory
        self._vad = None
        self.queue, self.jobs = [], {}   # jobs in arrival order; alias -> queued job
        self.cv = threading.Condition()
        self.current = None
        self.done = {}            # alias -> {lang: result key | None}; for pending entries
        self.originals = {}       # alias -> {lang: {"url", "id"} of the original subtitle}, the pending entries' fallback
        self.unlisted = {}        # alias -> when a file was last not found in the listing
        self.pickable = {}        # alias -> job of a file that needs the audio route and waits for the viewer's pick
        self.picked_early = {}    # alias -> when its entry was downloaded before the file was known to need audio
        self.stop_flag = False
        self.last_expire = 0.0
        self.vad_lock = threading.Lock()
        self.audio_lock = threading.Lock()     # one audio fit at a time (shared reader)

    # -- queue -------------------------------------------------------------------------------------------------
    def submit(self, job):
        with self.cv:
            old = self.jobs.get(job.alias)
            if old is not None:
                old.audio = old.audio or job.audio
                return old
            if self.current is not None and self.current.alias == job.alias and (self.current.audio or not job.audio):
                return self.current
            self.jobs[job.alias] = job
            self.queue.append(job)
            self.cv.notify_all()
            return job

    def audio_allowed(self, job):
        return self.s.audio_route and (job.audio or not self.s.audio_on_pick)

    def waits_for_pick(self, alias):
        with self.cv:
            return alias in self.pickable

    def pick(self, alias):
        """The viewer downloaded subsync's entry for this file: start its audio route, once. Returns True if started."""
        with self.cv:
            waiting = self.pickable.pop(alias, None)
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
        """A file with no usable subtitle track of its own, read index-only: it waits for the viewer's pick."""
        with self.cv:
            early = self.picked_early.pop(job.alias, None)
            if early is None or time.time() - early > 3600:
                self.pickable[job.alias] = job
                while len(self.pickable) > 2000:
                    self.pickable.pop(next(iter(self.pickable)))
                return
        again = Job(job.type, job.video_id, job.hints)   # its entry was already downloaded while this read ran
        again.audio = True
        log.info("picked %s: the audio route starts", job.label)
        self.submit(again)

    def recently_unlisted(self, alias, within=UNLISTED_S):
        """A file the listing didn't have (Usenet, uncached) is not looked for again for an hour."""
        t = self.unlisted.get(alias)
        return t is not None and time.time() - t < within

    def busy_with(self, alias):
        with self.cv:
            return alias in self.jobs or (self.current is not None and self.current.alias == alias)

    def wait_for(self, alias, lang, timeout):
        """Wait up to timeout for a language's result on a queued file: (result key or None, original URL or None)."""
        end = time.time() + timeout
        with self.cv:
            while True:
                got = self.done.get(alias, {})
                original = (self.originals.get(alias, {}).get(lang) or {}).get("url")
                if lang in got:
                    return got[lang], original
                if not (alias in self.jobs or (self.current is not None and self.current.alias == alias)):
                    return None, original
                left = end - time.time()
                if left <= 0:
                    return None, original
                self.cv.wait(left)

    def wait_for_file(self, alias, timeout):
        """Wait up to timeout for a queued file's languages; returns {lang: {"url", "id"} of the original} once its
        subtitle list is known (None while it isn't)."""
        end = time.time() + timeout
        with self.cv:
            while True:
                busy = alias in self.jobs or (self.current is not None and self.current.alias == alias)
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
                busy = alias in self.jobs or (self.current is not None and self.current.alias == alias)
                left = end - time.time()
                if not busy or left <= 0:
                    break
                self.cv.wait(left)
            return self.originals.get(alias)

    def _next(self):
        with self.cv:
            while not self.stop_flag:
                now = time.time()
                for i, job in enumerate(self.queue):
                    if job.not_before <= now:
                        del self.queue[i]
                        del self.jobs[job.alias]
                        self.current = job
                        return job
                self.cv.wait(5.0)
        return None

    # -- the loop ----------------------------------------------------------------------------------------------
    def run(self):
        while not self.stop_flag:
            job = self._next()
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
                    self.current = None
                    self.done.setdefault(job.alias, {})
                    for lang in self.s.languages:
                        self.done[job.alias].setdefault(lang, None)
                    self.cv.notify_all()
                    while len(self.done) > 500:
                        self.done.pop(next(iter(self.done)))
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

    def _publish(self, alias, lang, key):
        with self.cv:
            self.done.setdefault(alias, {})[lang] = key
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
        candidates = self.aios.subtitles(job.type, job.video_id, hints)
        with self.cv:
            self.originals[job.alias] = {lang: next(({"url": c["url"], "id": c["id"]} for c in candidates if c["lang"] == lang), None)
                                         for lang in self.s.languages}
            self.cv.notify_all()
        if not any(c["lang"] in self.s.languages for c in candidates):
            log.info("no subtitles to sync %s", job.label)   # nothing is read
            return
        guard = JobGuard(self.safety, self.store, self.s.daily_budget,
                         self.s.file_cap if self.audio_allowed(job) else self.s.index_cap)
        reader = RangeReader(rewrite(url, self.s.url_rewrite), job.label, guard, timeout=self.s.read_timeout,
                             size=hints.get("videoSize"), size_tolerance=0.0005,
                             size_slack=self.s.size_slack)
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
        file_key = f"{oshash}-{index.size}"
        for a in {job.alias, f"hash:{oshash}|{index.size}",
                  f"name:{hints.get('filename')}|{hints.get('videoSize')}" if hints.get("filename") else None} - {None}:
            self.store.alias(a, file_key)
        tracks = index.text_tracks()
        images = index.image_tracks()
        self.store.file_read(file_key, index.size, oshash, any(not t["forced"] and t["cues"] for t in tracks),
                             {"tracks": [{k: t[k] for k in ("number", "language", "codec", "forced")} | {"cues": len(t["cues"])}
                                         for t in tracks + images], "bytes": reader.bytes,
                              "image_check": pipeline.image_check(tracks, images)})
        per_lang = {lang: [c for c in candidates if c["lang"] == lang] for lang in self.s.languages}
        if self.s.parallel_languages and len(per_lang) > 1:
            with ThreadPoolExecutor(max_workers=len(per_lang), thread_name_prefix="subsync-lang") as pool:
                futures = {pool.submit(self._sync_language, job, index, file_key, lang, cands, oshash,
                                       hints.get("filename")): lang for lang, cands in per_lang.items()}
                errors = []
                for fut in as_completed(futures):
                    try:
                        self._publish(job.alias, futures[fut], fut.result())
                    except Exception as e:   # the other language still finishes; the first error is raised after
                        errors.append(e)
                if errors:
                    raise errors[0]
        else:
            for lang, cands in per_lang.items():
                self._publish(job.alias, lang, self._sync_language(job, index, file_key, lang, cands, oshash,
                                                                    hints.get("filename")))
        log.info("done %s file=%s bytes=%d requests=%d", job.label, file_key, reader.bytes, reader.requests)

    def _sync_language(self, job, index, file_key, lang, candidates, oshash=None, filename=None):
        """The viewer's first subtitle, then subtitles made for this release, then the rest, until one
        passes or `max_tries` were tried. Returns the last_good key to offer, if any. `rank` stays the viewer's order.
        With SUBSYNC_LIST_IN_SYNC on, an original further down the list that is in sync as it is gets listed as an
        unchanged "(synced)" copy, so the viewer can tell which one fits this file; the first one needs no copy.
        With SUBSYNC_FAST_PATH on, every try is downloaded at once and its original checked first (a gate, no
        alass), so an in-sync original is listed within seconds; re-timing runs only when none is in sync. With the audio
        route off, a file with no text or image subtitle track stores nothing, so turning the route on later syncs it."""
        tries = choose.order(candidates, filename, oshash) if self.s.rank_by_release else list(enumerate(candidates))
        tries = tries[: self.s.max_tries]
        audio = self.audio_allowed(job)
        def entry(rank, cand, raw):
            sha = hashlib.sha256(raw).hexdigest()
            return {"rank": rank, "cand": cand, "raw": raw, "sha": sha, "got": self.store.fresh(result_key(file_key, sha))}

        if self.s.fast_path:   # every try at once, so the originals can be checked first
            entries = [entry(*f) for f in self._fetch_all(job, lang, tries)]
        else:                  # as in v3: one at a time, stopping at the first that passes
            entries = (entry(*f) for f in (self._fetch_one(job, lang, rank, cand) for rank, cand in tries) if f)

        def listed_cached(e):
            """A stored outcome (with an in-sync original further down listed now if it wasn't): passes?"""
            got = e["got"]
            outcome = got["outcome"]
            if outcome == "in_sync" and e["rank"] > 0 and self.s.list_in_sync:
                text = decode(e["raw"], lang)   # stored before the switch was on: list it now, unchanged
                self.store.put(file_key, e["sha"], lang, e["rank"], "synced",
                               f"in sync as it is (viewer's #{e['rank'] + 1}): listed unchanged", got.get("route"),
                               dict(got.get("detail") or {}, unchanged=True), "ass" if is_ass(text) else "srt", text,
                               e["cand"]["id"])
                outcome = "synced"
            return outcome in ("synced", "in_sync")

        def record(e, r, t0):
            detail = {k: r[k] for k in ("route", "reference", "gate_original", "gate_synced", "gate_snapped", "refined",
                                        "unchanged", "shift", "model", "windows", "audio_s", "lines", "dialogue",
                                        "alass_error", "fast") if k in r}
            detail["seconds"] = round(time.time() - t0, 2)
            detail["release"] = (e["cand"].get("release") or "")[:120]
            self.store.put(file_key, e["sha"], lang, e["rank"], r["outcome"], r.get("reason"), r.get("route"), detail,
                           e["ext"], r.get("body"), e["cand"]["id"])
            log.info("sync %s %s rank %d: %s (%s)", job.label, lang, e["rank"], r["outcome"], r.get("reason"))
            return r["outcome"] in ("synced", "in_sync")

        def in_sync_result(e, base):
            r = dict(base, outcome="in_sync", reason="original passes against the release track")
            if e["rank"] > 0 and self.s.list_in_sync:
                r = dict(r, outcome="synced", reason=f"in sync as it is (viewer's #{e['rank'] + 1}): listed unchanged",
                         body=e["text"], unchanged=True)
            return r

        done = False
        if self.s.fast_path:
            for e in entries:
                if e["got"]:
                    if listed_cached(e):
                        done = True
                        break
                    continue
                t0 = time.time()
                e["text"] = decode(e["raw"], lang)
                e["ext"] = "ass" if is_ass(e["text"]) else "srt"
                chk = pipeline.check_original(index, e["text"], lang, splits=self.s.gate_splits, image_ref=self.s.image_ref)
                if chk and chk["in_sync"]:
                    record(e, in_sync_result(e, dict(chk, fast=True)), t0)
                    done = True
                    break
        if not done:
            for e in entries:
                if e["got"]:
                    if not self.s.fast_path and listed_cached(e):
                        break
                    continue
                t0 = time.time()
                e.setdefault("text", decode(e["raw"], lang))
                e.setdefault("ext", "ass" if is_ass(e["text"]) else "srt")
                r = pipeline.sync_subtitle(index, e["text"], e["ext"], lang, alass=self.s.alass, audio=audio,
                                           audio_lead=self.s.audio_lead, vad=self.vad() if audio else None,
                                           ffmpeg=self.s.ffmpeg, snap=self.s.snap, snap_within=self.s.snap_within,
                                           splits=self.s.gate_splits, image_ref=self.s.image_ref,
                                           audio_lock=self.audio_lock, better=self.s.better)
                if not audio and r.get("route") is None and r["outcome"] == "keep_original" \
                        and "audio route is off" in (r.get("reason") or ""):
                    if self.s.audio_route:
                        log.info("sync %s %s rank %d: no text or image subtitle track; the audio route waits for the "
                                 "viewer to pick the Subsync entry", job.label, lang, e["rank"])
                        self._needs_pick(job)
                    else:
                        log.info("sync %s %s rank %d: no text or image subtitle track, and the audio route is off",
                                 job.label, lang, e["rank"])
                    break   # nothing stored
                if r["outcome"] == "in_sync":
                    r = in_sync_result(e, r)
                if record(e, r, t0):
                    break
        lg = self.store.last_good(file_key, lang)
        return lg["key"] if lg else None

    def _fetch_one(self, job, lang, rank, cand):
        try:
            return rank, cand, fetch_subtitle(cand["url"])
        except Exception as e:
            log.info("subtitle fetch failed %s %s rank %d: %s", job.label, lang, rank, type(e).__name__)
            return None

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
