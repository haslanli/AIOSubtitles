"""The audio route in public mode, pick-only: it starts only after a person picks the "(AIOSubtitles)" entry of a
file with no usable subtitle track, through that person's own sync config, within the per-file audio limit and the
person's and the service's data for the day; one audio sync at a time per two places. Plain Python except where marked;
no network beyond 127.0.0.1; every run bounded (ulimit -t)."""
import os
import threading
import time
import unittest

from helpers import (closed_after, dialogue_srt, FakeDownloads, FIXTURES, HAVE_CRYPTOGRAPHY, have, KEY,
                     Limits as CountedLimits, Listing, local_network_only, NEEDS, RangeServer, start_server,
                     stop_server, TempDir, wait_until)
from subsync import audiofit  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.store import Store  # noqa: E402
from subsync.worker import Job, OverLimit, Safety, Turn, Worker, audio_places  # noqa: E402



class Limits(CountedLimits):
    """A person's limits, with today's audio syncs not counted: these tests are about the places and the day's data."""

    def may_start_audio(self, opened=False):
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)
        self.store = closed_after(self, Store(self.dir))
        self.workers = []
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        for w in self.workers:
            w.stop()

    def settings(self, **env):
        return Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC_BASE": "http://x.test", "SUBSYNC_PUBLIC": "on",
                              "SUBSYNC_AUDIO_ROUTE": "on", "SUBSYNC_LANGUAGES": "eng", "SUBSYNC_EXTRA_SOURCES": ""},
                             **env))

    def worker(self, limits=None, aios=None, **kw):
        w = Worker(kw.pop("settings", None) or self.settings(), self.store, aios or Listing(), Safety(), limits=limits,
                   **kw)
        self.workers.append(w)
        return w


# --------------------------------------------------------------------------------------------- the start-up check

class StartTest(unittest.TestCase):
    def test_public_mode_starts_with_the_audio_route_on(self):
        self.assertEqual(Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_AUDIO_ROUTE": "on"}).conflicts(), [])

    def test_the_instances_on_pick_setting_no_longer_stops_public_mode(self):
        # Each person's own choice replaces it (had refused it off); test_audio_choice has the rest
        self.assertEqual(Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_AUDIO_ROUTE": "on",
                                   "SUBSYNC_AUDIO_ON_PICK": "off"}).conflicts(), [])
        self.assertEqual(Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_AUDIO_ON_PICK": "off"}).conflicts(), [])
        self.assertEqual(Settings({"SUBSYNC_AUDIO_ROUTE": "on", "SUBSYNC_AUDIO_ON_PICK": "off"}).conflicts(), [])


# ------------------------------------------------------------------------------------- the audio lane and places

class LaneTest(Base):
    def test_audio_places_are_half_the_places_at_least_one(self):
        self.assertEqual([audio_places(n) for n in (1, 2, 3, 4, 8)], [1, 1, 1, 2, 4])
        self.assertEqual(audio_places(None), 1)

    def test_public_mode_has_an_audio_lane_only_with_the_route_on(self):
        for route, expect in (("on", True), ("off", False)):
            with self.subTest(route=route):
                w = self.worker(settings=self.settings(SUBSYNC_AUDIO_ROUTE=route))
                before = set(threading.enumerate())
                w.start()
                self.assertEqual(getattr(w, "audio_worker", None) is not None and w.audio_worker.is_alive(), expect)
                if not expect:      # test_public's copy of this case, moved here (R3-4)
                    started = set(threading.enumerate()) - before    # the threads: no leftover's name hides one
                    self.assertNotIn("subsync-audio", {t.name for t in started})
                    self.assertIsNone(getattr(w, "audio_worker", None))

    def test_an_audio_sync_takes_an_audio_place_and_other_files_go_on(self):
        """Two places, one of them for audio: two people's picks run one after the other, a third person's ordinary
        file runs beside the first pick."""
        turn, audio_turn, spans, lock = Turn(2), Turn(1), [], threading.Lock()

        class Recording(Worker):
            def process(self, job):
                t0 = time.time()
                time.sleep(0.3)
                with lock:
                    spans.append((job.audio, t0, time.time()))
        for name, audio in (("a", True), ("b", True), ("c", False)):
            w = Recording(self.settings(), self.store, Listing(), Safety(), turn=turn, audio_turn=audio_turn)
            self.workers.append(w)
            job = Job("movie", "tt" + name, {"filename": name + ".mkv", "videoSize": 1})
            job.audio = audio
            w.submit(job)
        for w in self.workers:
            w.start()
            time.sleep(0.05)
        self.assertTrue(wait_until(lambda: len(spans) == 3))
        audio = sorted((t0, t1) for a, t0, t1 in spans if a)
        self.assertLessEqual(audio[0][1], audio[1][0])                       # never two audio syncs at once
        other = [(t0, t1) for a, t0, t1 in spans if not a][0]
        self.assertTrue(other[0] < audio[0][1] and audio[0][0] < other[1])   # the ordinary file went on beside it


# ------------------------------------------------------ each person's own waiting files and original subtitles

ALIAS = "name:b.mp4|5"
JOB = ("movie", "tt9", {"filename": "b.mp4", "videoSize": 5})
ORIGINALS = {"eng": {"url": "https://a-personal-addon.test/A-SECRET/1.srt", "id": "a-1"}}


def job(audio=False):
    j = Job(*JOB)
    j.audio = audio
    return j


class OwnRowsTest(Base):
    def waiting(self, w):
        w.originals[ALIAS] = dict(ORIGINALS)
        w._needs_pick(job())

    def test_another_persons_waiting_file_and_its_originals_never_reach_you(self):
        a = self.worker(Limits(who="a" * 16))
        a._set_originals(ALIAS, [{"id": "a-1", "url": ORIGINALS["eng"]["url"], "lang": "eng"}], "b.mp4", None)
        self.waiting(a)
        b = self.worker(Limits(who="b" * 16))
        self.assertFalse(b.waits_for_pick(ALIAS))
        self.assertIsNone(b.known_originals(ALIAS))
        self.assertFalse(b.pick(ALIAS))
        again = self.worker(Limits(who="a" * 16))        # the same person after a restart finds their own
        self.assertTrue(again.waits_for_pick(ALIAS))
        self.assertEqual(again.known_originals(ALIAS), ORIGINALS)

    def test_a_self_hosted_install_keeps_its_rows_by_file(self):
        w = self.worker(settings=self.settings(SUBSYNC_PUBLIC="off"))
        self.waiting(w)
        self.assertIsNotNone(self.store.pickable_get(ALIAS))
        self.assertEqual(self.store.originals_get(ALIAS), ORIGINALS)

    def test_public_mode_without_a_persons_key_keeps_nothing_in_the_store(self):
        w = self.worker()                                 # no limits, so no key: memory only
        self.waiting(w)
        w._set_originals(ALIAS, [{"id": "a-1", "url": "u", "lang": "eng"}], "b.mp4", None)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM pickable").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM originals").fetchone()[0], 0)

    def test_the_shared_rows_of_earlier_versions_are_dropped(self):
        self.store.pickable_put(ALIAS, *JOB)
        self.store.originals_put(ALIAS, ORIGINALS)
        a = self.worker(Limits(who="a" * 16))
        self.waiting(a)
        self.assertEqual(self.store.drop_shared_waiting(), 2)
        self.assertIsNone(self.store.pickable_get(ALIAS))
        self.assertIsNone(self.store.originals_get(ALIAS))
        self.assertTrue(self.worker(Limits(who="a" * 16)).waits_for_pick(ALIAS))   # the person's own rows stay


    def own_rows(self, rows_key):
        """How many rows of the person whose rows key this is the store holds: waiting files, originals, refusals."""
        mine = f"p:{rows_key}:%"
        return sum(self.store.db.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} LIKE ?", (mine,)).fetchone()[0]
                   for table, column in (("pickable", "alias"), ("originals", "alias"), ("files", "file_key")))

    def test_a_persons_rows_go_at_once_by_their_rows_key(self):
        # Delete left a person's waiting files and original subtitles in the store for up to 7 days: their
        # keys couldn't be found by person; now each is under the person's rows key, and anyone else's stay
        from subsync.store import REFUSED_BACKOFF_S
        a, b = self.worker(Limits(who="a" * 16)), self.worker(Limits(who="b" * 16))
        for w in (a, b):
            self.waiting(w)
            self.store.file_failed(w._refusal_key(ALIAS), REFUSED_BACKOFF_S)
        self.assertTrue(a._row_key(ALIAS).startswith("p:" + "a" * 16 + ":"))
        self.assertEqual((self.own_rows("a" * 16), self.own_rows("b" * 16)), (3, 3))
        self.assertEqual(self.store.drop_rows_of("a" * 16), 3)
        self.assertEqual((self.own_rows("a" * 16), self.own_rows("b" * 16)), (0, 3))
        again = self.worker(Limits(who="a" * 16))
        self.assertFalse(again.waits_for_pick(ALIAS))
        self.assertIsNone(again.known_originals(ALIAS))
        self.assertFalse(again._file_blocked(ALIAS))
        other = self.worker(Limits(who="b" * 16))
        self.assertTrue(other.waits_for_pick(ALIAS))
        self.assertEqual(other.known_originals(ALIAS), ORIGINALS)
        self.assertTrue(other._file_blocked(ALIAS))

    def test_once_the_persons_data_is_deleted_their_worker_writes_nothing_more(self):
        # Delete stops the person's worker with forget: a pick still queued and a job still running leave no
        # row of theirs after it (a plain stop keeps the queued pick waiting, as before)
        from subsync.reader import Refused
        kept, gone = self.worker(Limits(who="b" * 16)), self.worker(Limits(who="a" * 16))
        for w in (kept, gone):
            w.submit(job(audio=True))
        kept.stop()
        gone.stop(forget=True)
        self.assertEqual((self.own_rows("b" * 16), self.own_rows("a" * 16)), (1, 0))
        a = self.worker(Limits(who="a" * 16))

        def running(j):
            a.stop(forget=True)                              # their data is deleted while this job runs
            a._set_originals(ALIAS, [{"id": "a-1", "url": ORIGINALS["eng"]["url"], "lang": "eng"}], "b.mp4", None)
            a._needs_pick(job())
            raise Refused("HTTP 429")
        a.process = running
        a.submit(Job("movie", "tt8", {"filename": "c.mp4", "videoSize": 5}))
        a._loop("main")
        self.assertEqual(self.own_rows("a" * 16), 0)
        self.assertEqual(a.originals[ALIAS], ORIGINALS)              # in memory only, for this worker's last moments
        self.assertTrue(a.waits_for_pick(ALIAS))


class OwnFirstPassTest(Base):
    """A file someone else read lately and that has no subtitle track: your own first pass (an index read) is queued,
    so you can pick it; it isn't, once you wait for your pick or the file's languages are judged."""
    FILE = "f" * 16 + "-5"

    def app(self, w, **env):
        from subsync.addon import App
        return App(w.s, self.store, w, Safety())

    def read_lately(self, reference):
        self.store.file_read(self.FILE, 5, "f" * 16, False, {"rule": self.settings().rule_id, "reference": reference})
        self.store.alias(ALIAS, self.FILE)

    def test_a_no_track_file_read_for_someone_else_gets_your_own_first_pass(self):
        self.read_lately(False)
        b = self.worker(Limits(who="b" * 16))
        self.app(b).subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual([(j.alias, j.audio) for j in b.jobs.values()], [(ALIAS, False)])

    def test_also_when_its_languages_were_judged_without_the_audio_route(self):
        # a pass with the route off marks its languages judged: no copy came of it
        self.read_lately(False)
        b = self.worker(Limits(who="b" * 16))
        self.store.judged_put(self.FILE, ["eng"], b.s.rule_id)
        self.app(b).subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual([(j.alias, j.audio) for j in b.jobs.values()], [(ALIAS, False)])
        b.jobs.clear()
        b.queue.clear()
        b.s.audio_when = "off"                  # someone whose choice is off can't pick or open it: no pass
        self.app(b).subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual(len(b.jobs), 0)

    def test_also_a_file_whose_only_track_is_too_small_for_your_languages_subtitles(self):
        # A first pass marks judged the languages it left to the audio route (the file's only track too small
        # for their subtitles): until the audio route has judged them, everyone with one of those languages gets their
        # own first pass, as for a file with no track; nobody whose choice is off, nor for other languages
        self.store.file_read(self.FILE, 5, "f" * 16, True, {"rule": self.settings().rule_id, "reference": True,
                                                            "audio_only": ["eng"]})
        self.store.alias(ALIAS, self.FILE)
        self.store.judged_put(self.FILE, ["eng", "rus"], self.settings().rule_id)
        b = self.worker(Limits(who="b" * 16))
        self.app(b).subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual([(j.alias, j.audio) for j in b.jobs.values()], [(ALIAS, False)])
        c = self.worker(Limits(who="c" * 16), settings=self.settings(SUBSYNC_LANGUAGES="rus"))
        self.app(c).subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual(len(c.jobs), 0)
        d = self.worker(Limits(who="d" * 16))
        d.s.audio_when = "off"
        self.app(d).subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual(len(d.jobs), 0)
        self.store.put(self.FILE, "a" * 64, "eng", 0, "keep_original", "below the bar", route="audio")
        e = self.worker(Limits(who="e" * 16))
        self.app(e).subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual(len(e.jobs), 0)

    def test_not_when_the_file_has_its_own_track_or_you_wait_or_it_is_judged_or_the_route_is_off(self):
        self.read_lately(True)
        b = self.worker(Limits(who="b" * 16))
        self.app(b).subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual(len(b.jobs), 0)
        self.read_lately(False)
        b.originals[ALIAS] = dict(ORIGINALS)
        b._needs_pick(job())
        self.app(b).subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual(len(b.jobs), 0)
        c = self.worker(Limits(who="c" * 16))
        self.store.put(self.FILE, "a" * 64, "eng", 0, "keep_original", "below the bar", route="audio")   # judged by audio
        self.store.judged_put(self.FILE, ["eng"], c.s.rule_id)
        self.app(c).subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual(len(c.jobs), 0)
        self.store.db.execute("DELETE FROM judged")
        self.store.db.execute("DELETE FROM results")
        d = self.worker(Limits(who="d" * 16), settings=self.settings(SUBSYNC_AUDIO_ROUTE="off"))
        self.app(d).subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual(len(d.jobs), 0)


# --------------------------------------------------------------- a pick starts only when it can, and has room

class RoomTest(Base):
    FILE = "f" * 16 + "-5"

    def facts(self, rate=300_000, tracks=1, index_bytes=1_000_000):
        self.store.file_read(self.FILE, 5, "f" * 16, False, {"audio": {"rate": rate, "tracks": tracks},
                                                             "bytes": index_bytes, "reference": False})
        self.store.alias(ALIAS, self.FILE)

    def waiting(self, limits, **env):
        w = self.worker(limits, settings=self.settings(**env) if env else None)
        w.originals[ALIAS] = dict(ORIGINALS)
        w._needs_pick(job())
        return w

    def test_the_most_an_audio_sync_could_read(self):
        # 8 fit, 4 held-out and 4 planned windows of 30 s, 12 more on a close call, at 1.25 x the file's byte rate
        self.assertEqual(audiofit.most_read(300_000, 10 ** 9, 12), 315_000_000)
        self.assertEqual(audiofit.most_read(300_000, 10 ** 9, 12, tracks=2), 630_000_000)
        self.assertEqual(audiofit.most_read(4_000_000, 10 ** 9, 12), 10 ** 9)   # shorter windows, the limit decides
        self.assertEqual(audiofit.most_read(6_000_000, 10 ** 9, 12), 0)         # too high a bitrate: no audio read
        self.assertEqual(audiofit.most_read(None, 10 ** 9, 12), 10 ** 9)        # not known: the whole limit
        self.assertEqual(audiofit.most_read(300_000, None, 0), 180_000_000)     # no limit set: all 16 windows
        self.assertEqual(audiofit.planned_window(10 ** 9, 4_000_000), 12.5)
        self.assertEqual(audiofit.planned_window(10 ** 9, 300_000), 30.0)
        self.assertIsNone(audiofit.planned_window(10 ** 9, 6_000_000))

    def test_a_pick_that_cant_be_queued_leaves_the_file_waiting(self):
        self.facts()
        w = self.waiting(Limits(queue=0))
        self.assertFalse(w.pick(ALIAS))
        self.assertTrue(w.waits_for_pick(ALIAS))
        self.assertTrue(self.worker(Limits(queue=0)).waits_for_pick(ALIAS))   # its row stays in the store too
        self.assertEqual(len(w.jobs), 0)

    def test_the_room_counts_a_margin_over_the_planned_windows(self):
        # a test run read a file up to 2.44 times its planned windows (each subtitle places its own), so the
        # limit is 2.5 times, at most the per-file limit, plus the index
        self.facts()
        w = self.waiting(Limits())
        self.assertEqual(w._audio_need(ALIAS), int(2.5 * 315_000_000) + 1_000_000)
        self.facts(rate=4_000_000)                         # the limit decides
        self.assertEqual(w._audio_need(ALIAS), 10 ** 9 + 1_000_000)

    def test_a_pick_starts_only_with_room_for_the_most_it_could_read(self):
        self.facts()                                       # 2.5 x 315 MB of audio, plus the index's 1 MB
        w = self.waiting(Limits(left=788_000_000))
        self.assertFalse(w.pick(ALIAS))
        self.assertTrue(w.waits_for_pick(ALIAS))
        w.limits.left = 788_500_000
        self.assertTrue(w.pick(ALIAS))
        self.assertEqual([(j.alias, j.audio) for j in w.jobs.values()], [(ALIAS, True)])

    def test_the_services_day_must_have_room_too(self):
        self.facts()
        w = self.waiting(Limits())
        self.store.day_add("read", w.s.daily_budget - 100_000_000)
        self.assertFalse(w.pick(ALIAS))
        self.assertTrue(w.waits_for_pick(ALIAS))

    def test_a_file_whose_rate_isnt_known_needs_room_for_the_whole_limit(self):
        w = self.waiting(Limits(left=900_000_000))         # no facts stored: 1 GB plus the index cap
        self.assertFalse(w.pick(ALIAS))
        w.limits.left = 10 ** 9 + w.s.index_cap
        self.assertTrue(w.pick(ALIAS))

    def test_an_audio_job_without_room_when_it_starts_reads_nothing_and_waits_again(self):
        # several picks can be queued against the same data left: each job checks again before it reads
        self.facts()
        w = self.waiting(Limits(left=800_000_000))
        self.assertTrue(w.pick(ALIAS))
        w.limits.left = 200_000_000                      # another audio sync of this person's ran first
        w.process(w.jobs.pop(ALIAS))
        self.assertEqual(w.limits.read, 0)
        self.assertTrue(w.waits_for_pick(ALIAS))

    def test_a_day_that_runs_out_during_an_audio_sync_leaves_the_file_waiting(self):
        # Rarely, a sync reads more than the margin; the person's limit stops it, and the file waits again
        class Short(Worker):
            def process(self, job):
                raise OverLimit("bytes per day")
        self.facts()
        w = Short(self.settings(), self.store, Listing(), Safety(), limits=Limits())
        self.workers.append(w)
        w.originals[ALIAS] = dict(ORIGINALS)
        w._needs_pick(job())
        self.assertTrue(w.pick(ALIAS))
        self.assertFalse(w.waits_for_pick(ALIAS))
        w.start()
        self.assertTrue(wait_until(lambda: w.waits_for_pick(ALIAS)))
        plain = Short(self.settings(), self.store, Listing(), Safety(), limits=Limits(who="c" * 16))
        self.workers.append(plain)
        plain.submit(Job("movie", "tt8", {"filename": "c.mkv", "videoSize": 1}))   # not an audio job: dropped as before
        plain.start()
        self.assertTrue(wait_until(lambda: not plain.jobs and plain.current is None))
        self.assertFalse(plain.waits_for_pick("name:c.mkv|1"))

    def test_an_early_pick_without_room_waits_for_another_pick(self):
        self.facts()
        w = self.worker(Limits(left=1))
        w.picked_early[ALIAS] = time.time()
        w.originals[ALIAS] = dict(ORIGINALS)
        self.assertFalse(w._needs_pick(job()))
        self.assertTrue(w.waits_for_pick(ALIAS))
        self.assertEqual(len(w.jobs), 0)


# ------------------------------------------------------------ the first pass of a file with no track, and its audio job

class FirstPassTest(Base):
    SUBS = [{"id": "os-1", "lang": "eng", "release": "", "hash": "", "hash_match": False, "ai": False}]

    def setUp(self):
        super().setUp()
        self.srv = RangeServer(FIXTURES)
        self.addCleanup(self.srv.close)
        self.size = os.path.getsize(os.path.join(FIXTURES, "tiny-nosub.mp4"))
        self.hints = {"filename": "tiny-nosub.mp4", "videoSize": self.size}
        subs = [dict(self.SUBS[0], url=self.srv.url("tiny.srt"))]
        self.aios = Listing({"url": self.srv.url("tiny-nosub.mp4")}, subs)

    def test_the_first_pass_keeps_the_files_rate_and_waits_for_the_persons_pick(self):
        limits = Limits()
        w = self.worker(limits, aios=self.aios)
        w.process(Job("movie", "tt1", self.hints))
        alias = Job("movie", "tt1", self.hints).alias
        self.assertTrue(w.waits_for_pick(alias))
        detail = self.store.file_detail(self.store.resolve(alias))
        self.assertIs(detail["reference"], False)
        self.assertGreater(detail["audio"]["rate"], 0)
        self.assertEqual(detail["audio"]["tracks"], 1)
        self.assertEqual(limits.started, 1)

    def test_an_audio_job_is_not_a_new_file(self):
        limits = Limits()
        w = self.worker(limits, aios=self.aios)
        w._sync_all = lambda *a: None                      # the audio fit itself runs in the image's tests
        picked = Job("movie", "tt1", self.hints)
        picked.audio = True
        w.process(picked)
        self.assertEqual(limits.started, 0)
        self.assertGreater(limits.read, 0)                 # its reads still count in the person's data


# ------------------------------------------------------------------ a copy for someone whose list came from the store

SRT = "1\n00:00:01,000 --> 00:00:02,000\nHello there, Kenobi.\n\n2\n00:00:03,000 --> 00:00:04,000\nGeneral.\n"
SYNCED = "1\n00:00:01,500 --> 00:00:02,500\nHello there, Kenobi.\n\n2\n00:00:03,500 --> 00:00:04,500\nGeneral.\n"


class CopyFromTheStoreTest(Base):
    """A person whose request is answered entirely from copies another person's sync made,
    and who never had a job for that file, can download them: the copy is rebuilt from their own list, fetched then."""
    FILE = "0123456789abcdef-1000"

    def test_a_copy_listed_from_the_store_can_be_downloaded(self):
        import hashlib
        import tempfile
        from subsync.addon import App
        folder = tempfile.mkdtemp(dir=self.dir)
        with open(os.path.join(folder, "orig.srt"), "w", encoding="utf-8") as f:
            f.write(SRT)
        srv = RangeServer(folder)
        self.addCleanup(srv.close)
        store = closed_after(self, Store(os.path.join(self.dir, "public"), keep_texts=False))
        key = store.put(self.FILE, hashlib.sha256(SRT.encode()).hexdigest(), "eng", 0, "synced", body=SYNCED,
                        fmt="srt", candidate_id="c1", original=SRT)
        store.set_offers(self.FILE, "eng", [key])
        store.alias("name:a.mkv|1000", self.FILE)
        listing = Listing(subtitles=[{"id": "c1", "url": srv.url("orig.srt"), "lang": "eng"}])
        w = Worker(self.settings(), store, listing, Safety(), limits=Limits(who="b" * 16))
        self.workers.append(w)
        app = App(w.s, store, w, Safety())
        subs = app.subtitles("movie", "tt1", "filename=a.mkv&videoSize=1000")
        self.assertEqual([s["label"] for s in subs], ["English (synced)"])
        self.assertEqual(len(w.jobs), 0)                    # answered from the store: no job, nothing read
        self.assertEqual(app.text_of(key), SYNCED)


class OriginalEntryPublicTest(Base):
    """The "(original)" entry in public mode: its address names only the file; its download
    serves the subtitle from the downloading person's own list, never another person's address or text."""
    FILE = "f" * 16 + "-5"
    A_URL, B_URL, B_OTHER = "https://a.test/c1.srt", "https://b.test/c1.srt", "https://b.test/c9.srt"

    def setUp(self):
        super().setUp()
        self.downloads = FakeDownloads({self.A_URL: dialogue_srt("eng"), self.B_URL: dialogue_srt("eng", start=31.0),
                                        self.B_OTHER: dialogue_srt("eng", start=32.0)})
        for patcher in (self.downloads.patch(), local_network_only()):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = closed_after(self, Store(os.path.join(self.dir, "public"), keep_texts=False))
        a = self.worker(Limits(who="a" * 16), Listing(subtitles=[{"id": "c1", "url": self.A_URL, "lang": "eng"}]))
        a._candidates(job(), dict(job().hints))           # person A's list, and A's judging kept c1
        a._set_originals(ALIAS, [{"id": "c1", "url": self.A_URL, "lang": "eng"}], "b.mp4", None)
        rule = a.s.rule_id
        self.store.file_read(self.FILE, 5, "f" * 16, True, {"rule": rule, "reference": True})
        self.store.alias(ALIAS, self.FILE)
        self.store.judged_put(self.FILE, ["eng"], rule)
        self.store.kept_original_put(self.FILE, "eng", "c1", 0.6)

    def download(self, app, url):
        import urllib.request
        from subsync.addon import serve
        server = start_server(serve(app, 0))
        try:
            path = url.replace("http://x.test", f"http://127.0.0.1:{server.server_address[1]}")
            with urllib.request.urlopen(path, timeout=10) as r:
                return r.read().decode()
        finally:
            stop_server(server)

    def person_b(self, *subs):
        from subsync.addon import App
        b = self.worker(Limits(who="b" * 16), Listing(subtitles=list(subs)))
        return b, App(b.s, self.store, b, Safety())

    def test_your_entry_serves_the_subtitle_from_your_own_list(self):
        import json
        b, app = self.person_b({"id": "c1", "url": self.B_URL, "lang": "eng"})
        subs = app.subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual([(s["label"], s["variantOf"]) for s in subs], [("English (original)", "c1")])
        self.assertNotIn("a.test", json.dumps(subs))
        self.assertEqual(len(b.jobs), 0)                                    # judged lately: nothing queued
        self.assertEqual(self.download(app, subs[0]["url"]), dialogue_srt("eng", start=31.0))
        self.assertNotIn(self.A_URL, self.downloads.asked)
        self.assertEqual(len(b.jobs), 0)

    def test_without_that_subtitle_in_your_list_your_own_likeliest_one(self):
        b, app = self.person_b({"id": "c9", "url": self.B_OTHER, "lang": "eng"})
        subs = app.subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual(self.download(app, subs[0]["url"]), dialogue_srt("eng", start=32.0))
        self.assertEqual(self.downloads.asked, [self.B_OTHER])


# --------------------------------------------------------------------------------------------------- the texts

# The audio route as each person chooses it, Usenet included (drafts shown again with the rest (6))
T_A1 = ("Your answer to ./setup's audio question, on or off: whether files with no subtitle track of their own are synced "
        "from their speech, reading up to SUBSYNC_AUDIO_BUDGET_GB of audio each, after someone picks their "
        "\"(Subsync)\" entry or, with SUBSYNC_AUDIO_WHEN=open, as soon as they are opened; on a public instance each "
        "person chooses. Recommended: off on your own install unless you often watch such files; on a public instance, "
        "on if the server has two or more processors. Off: they keep their original subtitles and subsync reads about 1 "
        "MB per file. On a public instance each person's audio reads go through their own debrid service or Usenet and "
        "count in their daily data, and one sync serves everyone who plays the same file.")
T_A2 = ("The setting SUBSYNC_AUDIO_WHEN replaced, still read while that one isn't set: on means pick, off means open. "
        "Recommended: leave it out and set SUBSYNC_AUDIO_WHEN instead. A public instance ignores both: each person "
        "chooses on their settings page.")
# the two public-instance additions kept short, so each explanation stays within 125 words
T_A3 = "lets each person choose their audio syncs,"
T_A4 = "On a public instance it counts in each person's daily data (SUBSYNC_USER_DAILY_MB)."
T_A5 = ("Megabytes read per person per day (UTC) through their own sync config. Reading a file's subtitle track takes "
        "about 1 MB, a few MB when the track's lines are pictures; 10,000 is 200 files at the most one file's index "
        "may take (32 MB) with room to spare, so those reads never stop someone before the file limit. A file the "
        "audio route syncs from its sound takes about 60–350 MB, so 10,000 MB covers at least 28 a day; such a sync "
        "starts only when what is left of the day likely covers it, so it rarely stops half-way; if it does, the file "
        "waits for another pick. Past the limit, that person gets only copies already in the store until the next day.")
# Each person chooses (off, on their pick, as soon as a file is opened), Usenet files too, the instance's number
# of audio syncs at once
T_A6 = ("With the audio route on, a file with no usable subtitle track can be synced from its sound, as you choose on "
        "your settings page: off; after you pick its \"<Language> (AIOSubtitles)\" entry (the default); or, where the "
        "instance offers it, as soon as your app asks for its subtitles, if no synced copy exists and a subtitle exists "
        "to fit. Never through Real-Debrid. AIOSubtitles reads short parts of the sound through your own debrid service "
        "or Usenet, about 60–350 MB, at most the instance's audio limit, and works one to three minutes; your app gets "
        "the original at once, and the synced copy is there next time, for everyone who plays the file. It "
        "starts only when your day's data and audio syncs, and the service's data, likely have room; otherwise, or if it "
        "stops half-way, the file waits for another pick. The service runs a set number of these at once "
        "(SUBSYNC_AUDIO_AT_ONCE), so other files aren't held up.")
T_A9 = ("A file with no subtitle track of its own can be synced from its sound, when you choose: by default only after "
        "you pick its \"(AIOSubtitles)\" entry. That reads about 60–350 MB of it through your debrid service or Usenet, "
        "and the copy is there the next time you open the file.")
T_A10 = ("; for a file with no subtitle track of its own, as you choose on your settings page (by default only after you "
         "pick its \"(AIOSubtitles)\" entry), short parts of its sound (about 60–350 MB, at most 1 GB) through your own "
         "debrid service or Usenet, used only to time the subtitles and not kept;")
T_A11 = ("A file with no subtitle track of its own takes one to three minutes once its sync from the sound starts (by "
         "default after you pick its \"(AIOSubtitles)\" entry; you choose on your settings page); its copy is there the "
         "next time you open it.")


class TextsTest(unittest.TestCase):
    def setUp(self):
        from subsync import explain
        self.explain = explain
        self.settings = {name: entry[-1] for name, entry in explain.SETTINGS.items()}

    def test_the_settings_say_how_the_audio_route_works_on_a_public_instance(self):
        self.assertEqual(self.settings["SUBSYNC_AUDIO_ROUTE"], T_A1)
        self.assertEqual(self.settings["SUBSYNC_AUDIO_ON_PICK"], T_A2)
        self.assertNotIn("keeps the audio route off", self.settings["SUBSYNC_PUBLIC"])
        self.assertIn("SUBSYNC_PUBLIC_BASE, " + T_A3 + " and shares", self.settings["SUBSYNC_PUBLIC"])
        self.assertTrue(self.settings["SUBSYNC_AUDIO_BUDGET_GB"].endswith(" " + T_A4))
        self.assertEqual(self.settings["SUBSYNC_USER_DAILY_MB"], T_A5)

    def test_the_rule_for_files_without_a_subtitle_track(self):
        self.assertEqual(dict(self.explain.RULES)["Public mode: files without a subtitle track"], T_A6)

    def pages_settings(self, **env):
        return Settings(dict({"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://x.test", "SUBSYNC_SECRET_KEY": KEY,
                              "SUBSYNC_AUDIO_ROUTE": "on"}, **env))

    def test_the_pages_say_it_only_when_the_instance_has_the_audio_route_on(self):
        import html
        import re
        from subsync import pages

        def text(page):
            return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
        on, off = self.pages_settings(), self.pages_settings(SUBSYNC_AUDIO_ROUTE="off")
        self.assertIn(T_A9, text(pages.signup_page(on)))
        self.assertNotIn("from its sound", text(pages.signup_page(off)))
        privacy = text(pages.privacy_page(on))
        self.assertIn("doesn't keep)" + T_A10 + " and the subtitles your AIOStreams lists", privacy)
        self.assertNotIn("short parts of its sound", text(pages.privacy_page(off)))
        half = text(pages.privacy_page(self.pages_settings(SUBSYNC_AUDIO_BUDGET_GB="0.5")))
        self.assertIn("(about 60–350 MB, at most 0.5 GB)", half)

    def test_the_signed_up_page_says_what_a_file_without_a_track_takes(self):
        from types import SimpleNamespace
        from subsync import pages
        # every field of signup.SignupResult
        result = SimpleNamespace(added=True, manual=False, changes=[], warning=None, languages=["eng"],
                                 manifest_url="https://x.test/u/" + "t" * 43 + "/manifest.json", token="t" * 43,
                                 store_only=False, to_store_only=False, usenet=False, usenet_why=None, sync_made=True)
        import html
        self.assertIn(T_A11, html.unescape(pages.result_page(result, self.pages_settings())))
        self.assertNotIn("one to three minutes", pages.result_page(result, self.pages_settings(SUBSYNC_AUDIO_ROUTE="off")))
        self.assertNotIn("one to three minutes", pages.result_page(result))


# ----------------------------------------------------------------------------------------------- the audio decoder

class DecoderTest(unittest.TestCase):
    def test_the_decoder_opens_only_the_pipe_and_the_one_demuxer_it_names(self):
        from subsync.mkv import decoder_cmd
        cmd = decoder_cmd("ffmpeg", "matroska", ["-ss", "1.000"], linux=False)
        self.assertEqual(cmd[0], "ffmpeg")
        i = cmd.index("-i")
        self.assertEqual(cmd[i + 1], "pipe:0")
        self.assertEqual(cmd[cmd.index("-protocol_whitelist") + 1], "pipe")
        self.assertEqual(cmd[cmd.index("-format_whitelist") + 1], "matroska")
        self.assertEqual(cmd[cmd.index("-f") + 1], "matroska")
        self.assertLess(cmd.index("-format_whitelist"), i)
        self.assertEqual(cmd[-2:], ["-ss", "1.000"])

    def test_on_linux_the_decoder_has_its_own_memory_and_processor_limits(self):
        from subsync.mkv import FFMPEG_CPU_S, FFMPEG_MEMORY_KB, decoder_cmd
        cmd = decoder_cmd("/usr/bin/ffmpeg", "aac", [], linux=True)
        self.assertEqual(cmd[:2], ["sh", "-c"])
        # the limits where the shell can set them; ffmpeg runs either way, so a shell that can't never stops decoding
        self.assertEqual(cmd[2], f'ulimit -v {FFMPEG_MEMORY_KB} 2>/dev/null; ulimit -t {FFMPEG_CPU_S} 2>/dev/null; '
                                 'exec "$@"')
        self.assertEqual(cmd[3:5], ["ffmpeg", "/usr/bin/ffmpeg"])           # $0, then the decoder itself
        self.assertEqual(cmd[5:], decoder_cmd("/usr/bin/ffmpeg", "aac", [], linux=False)[1:])
        self.assertEqual((FFMPEG_MEMORY_KB, FFMPEG_CPU_S), (1024 * 1024, 120))

    @unittest.skipUnless(have("ffmpeg"), "ffmpeg not installed")
    def test_both_audio_readers_decode_through_it(self):
        from unittest import mock
        from subsync import mkv, mp4
        from subsync.reader import RangeReader
        srv = RangeServer(FIXTURES)
        self.addCleanup(srv.close)
        made = []
        real = mkv.decoder_cmd

        def spy(*a, **k):
            made.append(a[1])
            return real(*a, **k)
        with mock.patch.object(mkv, "decoder_cmd", spy), mock.patch.object(mp4, "decoder_cmd", spy):
            idx = mkv.MkvIndex(RangeReader(srv.url("tiny.mkv"), "t")).read_head().read_cues()
            self.assertAlmostEqual(len(idx.window_pcm(30.0, 10.0)) / 2 / 16000, 10.0, delta=0.1)
            m = mp4.open_index(RangeReader(srv.url("tiny-nosub.mp4"), "t"))
            self.assertAlmostEqual(len(m.window_pcm(20.0, 10.0)) / 2 / 16000, 10.0, delta=0.1)
        self.assertEqual(made, ["matroska", "aac"])


# ---------------------------------------------------------------------------------------- the public service's start

class PublicStartTest(Base):
    def test_the_start_drops_the_shared_rows_of_earlier_versions_and_keeps_no_texts(self):
        from subsync.__main__ import public_store
        self.store.pickable_put(ALIAS, *JOB)
        self.store.originals_put(ALIAS, ORIGINALS)
        store = public_store(self.settings())
        self.assertFalse(store.keep_texts)
        self.assertIsNone(store.pickable_get(ALIAS))
        self.assertIsNone(store.originals_get(ALIAS))

    def test_the_start_line_says_whether_and_how_the_audio_route_runs(self):
        from subsync.__main__ import public_start_line
        on = public_start_line(self.settings(SUBSYNC_TRUSTED_PROXY="gateway, 127.0.0.1"), 2, 1)
        self.assertIn("2 files at once; audio route allowed: each person off / on pick / when opened, up to 1 GB a file, "
                      "1 at a time, 20 a person a day (5 opened);", on)
        half = public_start_line(self.settings(SUBSYNC_AUDIO_BUDGET_GB="0.5"), 4, 2)
        self.assertIn("up to 0.5 GB a file, 2 at a time", half)
        self.assertIn("audio route off", public_start_line(self.settings(SUBSYNC_AUDIO_ROUTE="off"), 1, 1))
        self.assertIn("1 file at once", public_start_line(self.settings(SUBSYNC_AUDIO_ROUTE="off"), 1, 1))


# ------------------------------------------------------------------------------ each person's settings, the pool

def record(link="https://aios.test/stremio/u1/E1/manifest.json", languages=("eng",)):
    return {"instance": "https://aios.test", "sync_uuid": "u1", "sync_link": link, "sync_password": "p1",
            "languages": list(languages), "version": 1}


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class PoolTest(unittest.TestCase):
    def setUp(self):
        from subsync.users import Users, new_token
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)
        self.env = {"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                    "SUBSYNC_PUBLIC_BASE": "https://subs.test", "SUBSYNC_EXTRA_SOURCES": "", "SUBSYNC_ORIGINAL_LANGUAGE": "off", "SUBSYNC_SYNCS_AT_ONCE": "2",
                    "SUBSYNC_AUDIO_ROUTE": "on", "SUBSYNC_AUDIO_BUDGET_GB": "0.5"}
        self.store = closed_after(self, Store(self.dir))
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), Settings(self.env).secret_bytes())
        self.addCleanup(self.users.close)
        self.tokens = [new_token(), new_token()]
        for t in self.tokens:
            self.users.create(t, record())

    def pool(self, **env):
        from subsync.public import Pool
        e = dict(self.env, **env)
        pool = Pool(Settings(e), e, self.store, self.users, start_workers=False)
        self.addCleanup(pool.close)
        return pool

    def test_each_person_gets_the_instances_audio_route_after_a_pick_unless_they_chose_otherwise(self):
        from subsync.public import user_settings
        s = user_settings(dict(self.env, SUBSYNC_AUDIO_ON_PICK="off"), record(), self.tokens[0])
        self.assertTrue(s.audio_route)
        self.assertEqual(s.audio_choice, "pick")             # their own choice, never the environment's
        self.assertEqual(s.audio_budget, 500_000_000)
        self.assertFalse(user_settings(dict(self.env, SUBSYNC_AUDIO_ROUTE="off"), record(), self.tokens[0]).audio_route)

    def test_everyone_shares_one_audio_place_per_two_places(self):
        pool = self.pool()
        a, b = pool.app(self.tokens[0]), pool.app(self.tokens[1])
        self.assertEqual((pool.turn.places, pool.audio_turn.places), (2, 1))
        self.assertIs(a.worker.audio_turn, pool.audio_turn)
        self.assertIs(b.worker.audio_turn, pool.audio_turn)
        self.assertEqual(self.pool(SUBSYNC_SYNCS_AT_ONCE="4").audio_turn.places, 2)

    def test_one_speech_detector_for_everyone_made_on_first_use(self):
        from unittest import mock
        from subsync import vad
        made = []

        class FakeVad:
            def __init__(self):
                made.append(self)
        pool = self.pool()
        a, b = pool.app(self.tokens[0]), pool.app(self.tokens[1])
        self.assertEqual(made, [])
        with mock.patch.object(vad, "SileroVad", FakeVad):
            self.assertIs(a.worker.vad(), b.worker.vad())
        self.assertEqual(len(made), 1)

    def test_room_is_what_is_left_of_the_persons_day(self):
        from subsync.public import StoreOnly, UserLimits
        limits = UserLimits(Settings(self.env), self.users, self.tokens[0])
        day = Settings(self.env).user_daily_bytes
        self.assertTrue(limits.has_room(day))
        limits.after_read(day - 300_000_000)
        self.assertTrue(limits.has_room(300_000_000))
        self.assertFalse(limits.has_room(300_000_001))
        self.assertFalse(StoreOnly().has_room(1))            # stored copies only: nothing is ever read

    def test_a_worker_running_an_audio_sync_is_not_stopped_as_idle(self):
        pool = self.pool()
        a = pool.app(self.tokens[0])
        job = Job("movie", "tt1", {"filename": "x.mkv", "videoSize": 1})
        job.audio = True
        a.worker.current_audio = job
        pool.clock = lambda: time.time() + pool.s.worker_idle_s + 10
        self.assertEqual(pool.reap(), 0)
        a.worker.current_audio = None
        self.assertEqual(pool.reap(), 1)


if __name__ == "__main__":
    unittest.main()
