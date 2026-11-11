"""Store, safety, sources filters and the addon's answers."""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import TempDir  # noqa: E402
from subsync import ALGO_VERSION  # noqa: E402
from subsync.addon import App, serve, token  # noqa: E402
from subsync.settings import Settings  # noqa: E402
from subsync.sources import Aios, expected_size, extras, parse_extras, same_hash, unreadable  # noqa: E402
from subsync.store import Store, result_key  # noqa: E402
from subsync.reader import Stopped  # noqa: E402
from subsync.worker import Job, JobGuard, Safety, Worker, rewrite  # noqa: E402

SHA_A, SHA_B, SHA_C = "a" * 64, "b" * 64, "c" * 64
FILE = "0123456789abcdef-1000"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class StoreTest(unittest.TestCase):
    def test_synced_is_offered_and_rank_rules(self):
        with TempDir() as d:
            st = Store(d)
            k1 = st.put(FILE, SHA_A, "eng", 1, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nx\n", fmt="srt")
            self.assertEqual([r["key"] for r in st.offers(FILE, ["eng", "rus"])], [k1])
            st.put(FILE, SHA_B, "eng", 2, "synced", body="b", fmt="srt")      # worse rank: keeps the last good
            self.assertEqual(st.last_good(FILE, "eng")["key"], k1)
            k3 = st.put(FILE, SHA_C, "eng", 0, "synced", body="c", fmt="srt")  # better rank replaces it
            self.assertEqual(st.last_good(FILE, "eng")["key"], k3)
            st.put(FILE, SHA_A[::-1].replace("a", "d"), "eng", 0, "keep_original")  # a failing new one: last good stays
            self.assertEqual(st.last_good(FILE, "eng")["key"], k3)
            st.put(FILE, "e" * 64, "eng", 0, "in_sync")                       # first subtitle fine as it is: nothing offered
            self.assertEqual(st.offers(FILE, ["eng"]), [])
            self.assertEqual(st.body(k3), "c")

    def test_cap_evicts_least_recently_served(self):
        with TempDir() as d:
            clock = Clock()
            st = Store(d, cap_bytes=10 ** 9, clock=clock)
            body = os.urandom(200_000).hex()   # incompressible-ish
            keys = []
            for i, sha in enumerate((SHA_A, SHA_B, SHA_C)):
                clock.t += 10
                keys.append(st.put(f"{i:016x}-1", sha, "eng", 0, "synced", body=body, fmt="srt"))
            clock.t += 10
            st.served(keys[0])
            st.cap = st.total_bytes() - 1      # one body must go: the least recently served is keys[1]
            st.enforce_cap()
            self.assertIsNotNone(st.get(keys[0]))
            self.assertIsNone(st.get(keys[1]))
            self.assertIsNotNone(st.get(keys[2]))

    def test_lifetimes_and_read_failure_backoff(self):
        with TempDir() as d:
            clock = Clock()
            st = Store(d, clock=clock)
            k = st.put(FILE, SHA_A, "eng", 0, "keep_original")
            self.assertIsNotNone(st.fresh(k))
            clock.t += 31 * 86400
            self.assertIsNone(st.fresh(k))                 # retried after 30 days
            waits = []
            for _ in range(4):
                r = st.file_failed("name:x|1")
                waits.append(None if r is None else round(r - clock.t))
            self.assertEqual(waits, [3600, 21600, 86400, None])
            self.assertTrue(st.file_blocked("name:x|1"))
            ks = st.put(FILE, SHA_B, "eng", 0, "synced", body="x", fmt="srt")
            clock.t += 181 * 86400
            st.expire()
            self.assertIsNone(st.get(ks))                  # 180 days after last served

    def test_budget_per_day_and_kind(self):
        with TempDir() as d:
            clock = Clock()
            st = Store(d, clock=clock)
            st.add_bytes("read", 100)
            st.add_bytes("read", 50)
            st.add_bytes("other", 7)
            self.assertEqual(st.bytes_today("read"), 150)
            clock.t += 86400
            self.assertEqual(st.bytes_today("read"), 0)

    def test_key_format(self):
        self.assertEqual(result_key(FILE, SHA_A), f"subsync:{ALGO_VERSION}:{FILE}:{'a' * 16}")


class SafetyTest(unittest.TestCase):
    def test_guard_rules(self):
        with TempDir() as d:
            clock = Clock()
            st = Store(d, clock=clock)
            safety = Safety(backoff_s=900, clock=clock)
            g = JobGuard(safety, st, daily_budget=1000)
            g.before_read(500)
            g.after_read(500)
            with self.assertRaises(Stopped):
                g.before_read(600)                         # the day's budget, counted as bytes received
            other = JobGuard(safety, st, daily_budget=10 ** 12)
            other.before_read(1)
            self.assertEqual(safety.refused(), 900)
            self.assertEqual(safety.refused(), 1800)
            with self.assertRaises(Stopped):
                other.before_read(1)                       # a refusal stops every read
            clock.t += 1801
            other.before_read(1)
            other.after_read(1)
            self.assertEqual(safety.refused(), 900)        # a good read resets the doubling

    def test_rewrite(self):
        self.assertEqual(rewrite("https://jf.example/api/v1/proxy/x", "https://jf.example=http://aiostreams:3000"),
                         "http://aiostreams:3000/api/v1/proxy/x")
        self.assertEqual(rewrite("http://other/x", "https://jf.example=http://aiostreams:3000"), "http://other/x")


class SourcesTest(unittest.TestCase):
    def row(self, **kw):
        d = {"type": "debrid", "service": {"id": "torbox", "cached": True}, "addon": {"name": "Torrentio"},
             "filename": "a.mkv", "size": 10}
        d.update(kw)
        return {"url": "http://x/y", "streamData": d, "behaviorHints": {}}

    def test_unreadable_rows(self):
        self.assertIsNone(unreadable(self.row()))
        self.assertEqual(unreadable(self.row(type="usenet")), "type usenet")
        self.assertEqual(unreadable(self.row(service={"id": "torbox", "cached": False})), "not cached")
        self.assertEqual(unreadable(self.row(library=True)), "library row")
        self.assertIsNone(unreadable(self.row(addon={"name": "Private Map"})))      # nothing skipped by default
        self.assertEqual(unreadable(self.row(addon={"name": " Private Map "}), ("private map",)), "addon Private Map")
        self.assertEqual(Aios("", skip_addons=[" Private Map", ""]).skip, ("private map",))
        self.assertEqual(Settings({"SUBSYNC_SKIP_ADDONS": "Private Map, Other"}).skip_addons, ("Private Map", "Other"))

    def test_extras_round_trip(self):
        h = {"videoHash": "0123456789abcdef", "videoSize": 123, "filename": "A B [x].mkv"}
        self.assertEqual(parse_extras(extras(h)), h)


class FindRowTest(unittest.TestCase):
    """Checklist 18a: the played file is found when its listed size is rounded or wrong. Stremio sends the real
    size, listings round it to 0.01 GiB (up to 5.4 MB off), Torrentio listed 1,679 bytes for a 3 GB file; a row whose
    listing names the file's hash is that file. The file's own hash, read with its index, settles it (process())."""
    REAL = 1_438_814_044
    NAME = "Show.S01E01.WEB-DL.mkv"

    def aios(self, *rows):
        a = Aios("http://aios", size_slack=5_767_168)
        a.streams = lambda *_: list(rows)
        return a

    @staticmethod
    def row(name, size, url="http://x/1", video_hash=None):
        return {"url": url, "streamData": {"type": "debrid", "service": {"id": "torbox", "cached": True},
                                           "filename": name, "size": size},
                "behaviorHints": {"videoHash": video_hash} if video_hash else {}}

    def find(self, hints, *rows):
        got = self.aios(*rows).find_row("series", "tt1:1:1", hints)
        return got["url"] if got else None

    def test_the_real_size_finds_a_rounded_listing(self):
        self.assertEqual(self.find({"filename": self.NAME, "videoSize": self.REAL},
                                   self.row(self.NAME, self.REAL + 3_000_000)), "http://x/1")

    def test_an_exact_size_wins_over_a_close_one(self):
        self.assertEqual(self.find({"filename": self.NAME, "videoSize": self.REAL},
                                   self.row(self.NAME, self.REAL + 3_000_000, "http://x/1"),
                                   self.row(self.NAME, self.REAL, "http://x/2")), "http://x/2")

    def test_a_listed_size_no_video_can_have_is_found_by_its_name(self):
        self.assertEqual(self.find({"filename": self.NAME, "videoSize": self.REAL}, self.row(self.NAME, 1679)), "http://x/1")

    def test_another_size_of_the_same_name_is_another_file(self):
        self.assertIsNone(self.find({"filename": self.NAME, "videoSize": self.REAL}, self.row(self.NAME, 2 * self.REAL)))

    def test_a_row_listing_the_files_hash_is_that_file(self):
        self.assertEqual(self.find({"videoHash": "AB12", "videoSize": self.REAL},
                                   self.row("other name.mkv", self.REAL, "http://x/1"),
                                   self.row("renamed.mkv", self.REAL, "http://x/2", video_hash="ab12")), "http://x/2")

    def test_same_name_rows_with_sizes_no_video_has_take_the_first(self):
        self.assertEqual(self.find({"filename": self.NAME, "videoSize": 1679},
                                   self.row(self.NAME, 1679, "http://x/1"), self.row(self.NAME, 1679, "http://x/2")),
                         "http://x/1")

    def test_hashes_compare_by_value(self):
        self.assertTrue(same_hash("0f3a000000000001", "f3a000000000001"))
        self.assertTrue(same_hash("AB12", "ab12"))
        self.assertFalse(same_hash("ab12", "ab13"))
        self.assertFalse(same_hash("not hex", "ab12"))

    def test_expected_size_for_the_reader(self):
        self.assertEqual(expected_size({"videoSize": self.REAL}, self.row(self.NAME, 1679)), self.REAL)
        self.assertEqual(expected_size({"videoSize": 1679}, self.row(self.NAME, self.REAL)), self.REAL)
        self.assertIsNone(expected_size({"videoSize": 1679}, self.row(self.NAME, 1679)))   # neither can be a video


class FakeAios(Aios):
    def __init__(self):
        super().__init__("")
        self.row = None

    def find_row(self, *a):
        return self.row

    def subtitles(self, *a):
        return []


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class AddonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDir()
        d = self.tmp.__enter__()
        self.s = Settings({"SUBSYNC_DATA": d, "SUBSYNC_PUBLIC_BASE": "http://subsync:8790", "SUBSYNC_PENDING_ENTRY": "on",
                           "SUBSYNC_PENDING_WAIT_MS": "200"})
        self.store = Store(d)
        self.safety = Safety()
        self.worker = Worker(self.s, self.store, FakeAios(), self.safety)   # not started: jobs stay queued
        self.server = serve(App(self.s, self.store, self.worker, self.safety), 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.__exit__(None, None, None)

    def test_manifest(self):
        code, body, _ = http(self.base + "/manifest.json")
        m = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(m["resources"], ["subtitles"])

    def test_known_file_lists_labelled_entries_and_serves_them(self):
        ass = ("[Script Info]\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
               "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hi\n")
        key = self.store.put(FILE, SHA_A, "eng", 0, "synced", body=ass, fmt="ass", candidate_id="os-123")
        self.store.alias("name:a.mkv|1000", FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {})   # read just now: no new job, no pending entry
        code, body, _ = http(self.base + "/subtitles/movie/tt1/filename=a.mkv&videoSize=1000.json")
        subs = json.loads(body)["subtitles"]
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["lang"], "eng")
        self.assertEqual(subs[0]["label"], "English (synced)")
        self.assertEqual(subs[0]["variantOf"], "os-123")      # marked as a variant of the original
        self.assertTrue(subs[0]["url"].startswith(f"http://subsync:8790/file/{ALGO_VERSION}_"))
        path = subs[0]["url"].replace("http://subsync:8790", "")
        code, body, headers = http(self.base + path)
        self.assertEqual(code, 200)
        self.assertIn(b"Dialogue: 0,0:00:01.00", body)
        code, body, _ = http(self.base + path.replace(".ass", ".srt"))
        self.assertIn(b"00:00:01,000 --> 00:00:02,000", body)
        self.assertGreaterEqual(self.store.get(key)["last_served"], self.store.get(key)["created"])

    def test_unknown_file_queues_a_job_and_lists_pending_entries(self):
        # subtitle list not known yet: nothing to list (no entry for a language that may have no subtitle at all)
        code, body, _ = http(self.base + "/subtitles/series/tt2%3A1%3A1/filename=b.mkv&videoSize=5.json")
        self.assertEqual(json.loads(body)["subtitles"], [])
        self.assertEqual(len(self.worker.jobs), 1)
        job = next(iter(self.worker.jobs.values()))
        self.assertEqual((job.type, job.video_id), ("series", "tt2:1:1"))
        # the job has its subtitle list (English only): a pending English entry, none for Russian
        self.worker.originals[job.alias] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        t0 = time.time()
        code, body, _ = http(self.base + "/subtitles/series/tt2%3A1%3A1/filename=b.mkv&videoSize=5.json")
        subs = json.loads(body)["subtitles"]
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual([(s["lang"], s["label"], s["variantOf"]) for s in subs], [("eng", "English (synced)", "os-9")])
        self.assertIn("/pending/", subs[0]["url"])
        del self.worker.originals[job.alias]
        # no result and no original known yet: 503 so the player retries
        t0 = time.time()
        code, _, headers = http(self.base + "/pending/" + token(job.alias) + "/eng.srt")
        self.assertEqual(code, 503)
        self.assertLess(time.time() - t0, 2.0)
        # the original known: a redirect to it
        self.worker.originals[job.alias] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}}
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(self.base + "/pending/" + token(job.alias) + "/eng.srt", timeout=10)
            self.fail("expected a redirect")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 302)
            self.assertEqual(e.headers["Location"], "http://opensubs/x.srt")

    def test_no_hints_no_job(self):
        code, body, _ = http(self.base + "/subtitles/movie/tt3.json")
        self.assertEqual(json.loads(body)["subtitles"], [])
        self.assertEqual(len(self.worker.jobs), 0)

    def test_unlisted_file_is_not_looked_for_again(self):
        self.worker.unlisted["name:e.mkv|9"] = time.time()
        http(self.base + "/subtitles/movie/tt6/filename=e.mkv&videoSize=9.json")
        self.assertEqual(len(self.worker.jobs), 0)
        http(self.base + "/subtitles/movie/tt6/filename=other.mkv&videoSize=9.json")
        self.assertEqual(len(self.worker.jobs), 1)                       # another file of the title is looked for

    def test_no_subtitle_to_sync_reads_nothing(self):
        self.worker.aios.row = {"url": "http://127.0.0.1:9/never"}     # the file is listed, but no subtitle is
        self.worker.process(Job("movie", "tt7", {"filename": "f.mkv", "videoSize": 9}))
        self.assertEqual(self.store.bytes_today("read"), 0)


class PickTest(AddonTest):
    """Audio route on: it starts only when the viewer downloads subsync's own entry for the file."""
    # AddonTest's own cases describe the audio route being off; they are not repeated here
    test_manifest = test_known_file_lists_labelled_entries_and_serves_them = None
    test_unknown_file_queues_a_job_and_lists_pending_entries = test_no_hints_no_job = None
    test_unlisted_file_is_not_looked_for_again = test_no_subtitle_to_sync_reads_nothing = None
    URL = "/subtitles/movie/tt9/filename=b.mp4&videoSize=5.json"
    ALIAS = "name:b.mp4|5"

    def setUp(self):
        super().setUp()
        self.s.audio_route = True
        self.assertTrue(self.s.audio_on_pick)
        self.job = Job("movie", "tt9", {"filename": "b.mp4", "videoSize": 5})
        self.assertEqual(self.job.alias, self.ALIAS)

    def waiting(self):
        self.worker.pickable[self.ALIAS] = self.job
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}

    def get(self, path):
        try:
            r = urllib.request.build_opener(NoRedirect).open(self.base + path, timeout=10)
            return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_a_plain_request_may_not_use_the_audio_route(self):
        self.assertFalse(self.worker.audio_allowed(self.job))
        picked = Job("movie", "tt9", self.job.hints)
        picked.audio = True
        self.assertTrue(self.worker.audio_allowed(picked))
        self.s.audio_on_pick = False                       # the old behaviour stays available
        self.assertTrue(self.worker.audio_allowed(self.job))
        self.s.audio_route = False
        self.assertFalse(self.worker.audio_allowed(picked))

    def test_a_file_waiting_for_a_pick_lists_one_subsync_entry_per_language_that_has_a_subtitle(self):
        self.waiting()
        self.store.file_read("f" * 16 + "-5", 5, "f" * 16, False, {})   # read a moment ago: no new job
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]
        self.assertEqual([(s["lang"], s["label"], s["variantOf"]) for s in subs], [("eng", "English (Subsync)", "os-9")])
        self.assertTrue(subs[0]["url"].endswith("/pick/" + token(self.ALIAS) + "/eng.srt"))
        self.s.audio_route = False
        self.assertEqual(json.loads(http(self.base + self.URL)[1])["subtitles"], [])

    def test_the_first_answer_is_not_held_back(self):
        self.s.pending_entry = False
        self.s.pending_wait = 3.0
        t0 = time.time()
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]
        self.assertLess(time.time() - t0, 1.0)             # AIOStreams orders addons by who answers first
        self.assertEqual([s["label"] for s in subs], ["English (Subsync)"])

    def test_the_first_answer_already_has_the_entry_for_every_language_and_the_download_waits_for_the_list(self):
        self.s.pick_wait = 3.0
        self.s.languages = type(self.s.languages)(["eng", "rus"])
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]     # unknown file: a job is queued
        self.assertEqual([(s["lang"], s["label"], s["variantOf"]) for s in subs],
                         [("eng", "English (Subsync)", "original"), ("rus", "Russian (Subsync)", "original")])
        self.assertEqual([j.audio for j in self.worker.jobs.values()], [False])

        def list_arrives():   # the job gets the list, reads the index: no subtitle track (the file needs the audio route)
            time.sleep(0.4)
            with self.worker.cv:
                self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
                self.worker.jobs.pop(self.ALIAS)
                self.worker.queue.clear()
                self.worker.done[self.ALIAS] = {"eng": None, "rus": None}
                self.worker.cv.notify_all()
        threading.Thread(target=list_arrives, daemon=True).start()
        t0 = time.time()
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers["Location"]), (302, "http://opensubs/x.srt"))
        self.assertLess(time.time() - t0, 2.5)
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/rus.srt")   # no Russian subtitle exists: a note
        self.assertEqual(code, 200)
        self.assertIn(b"No Russian subtitle was found for this file", body)
        self.assertIn(self.ALIAS, self.worker.picked_early)                              # remembered for the index read

    def test_downloading_the_entry_serves_the_original_and_starts_the_audio_route_once(self):
        self.waiting()
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers["Location"]), (302, "http://opensubs/x.srt"))
        self.assertNotIn(self.ALIAS, self.worker.pickable)
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, True)])
        self.get("/pick/" + token(self.ALIAS) + "/eng.srt")           # again: nothing more is queued
        self.assertEqual(len(self.worker.jobs), 1)

    def test_a_waiting_file_serves_the_anchored_guess_of_another_language(self):
        self.waiting()
        self.worker.originals[self.ALIAS]["rus"] = {"url": "http://opensubs/r.srt", "id": "os-r"}
        self.worker.provisional[self.ALIAS] = {"rus": "1\n00:00:01,000 --> 00:00:02,000\nanchored\n"}
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/rus.srt")
        self.assertEqual((code, b"anchored" in body), (200, True))
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")   # the main language: its best guess
        self.assertEqual((code, headers["Location"]), (302, "http://opensubs/x.srt"))

    def test_the_audio_verdict_replaces_the_anchored_guess(self):
        self.waiting()
        self.worker.provisional[self.ALIAS] = {"rus": "anchored", "eng": "x"}
        job = Job(self.job.type, self.job.video_id, self.job.hints)
        job.audio = True
        self.worker._finish(job, "rus", "k-rus", {})
        self.assertEqual(self.worker.provisional[self.ALIAS], {"eng": "x"})
        self.assertEqual(self.worker.published(self.ALIAS, "rus"), "k-rus")

    def test_anchored_guesses_can_be_switched_off(self):
        self.assertTrue(self.s.anchored_guess)
        self.assertFalse(Settings({"SUBSYNC_ANCHORED_GUESS": "off"}).anchored_guess)

    def test_a_waiting_language_is_published_with_its_anchored_guess_when_another_raises(self):
        job = Job(self.job.type, self.job.video_id, self.job.hints)
        job.fetched = {"eng": []}
        calls = []
        self.worker._set_provisional = lambda j: calls.append(j)

        def sync(lang, cands):
            if lang == "rus":
                raise ValueError("bad subtitle")
            return "k-" + lang
        with self.assertRaises(ValueError):
            self.worker._sync_all(job, {"eng": [], "rus": []}, sync)
        self.assertEqual(calls, [job])
        self.assertEqual(self.worker.published(self.ALIAS, "eng"), "k-eng")
        self.assertIn("eng", self.worker.judged[self.ALIAS])

    def cands(self):
        return {"eng": [{"id": "os-9", "url": "http://opensubs/x.srt", "lang": "eng"},
                        {"id": "os-8", "url": "http://opensubs/y.srt", "lang": "eng"}],
                "rus": [{"id": "os-r", "url": "http://opensubs/r.srt", "lang": "rus"}]}

    def test_a_no_track_file_picked_already_hands_over_at_once_and_its_pick_keeps_waiting(self):
        # 12 Angry Men (YIFY MP4): the audio route started 15 s after the pick, once ~40 downloads had been judged for
        # nothing, and the pick was released with the original as that pass ended
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        self.worker.picked_early[self.ALIAS] = time.time()
        fetched = []
        self.worker._fetch_all = lambda job, lang, tries: (fetched.append((lang, [c["id"] for _, c in tries])),
                                                           [(r, c, b"1\n00:00:01,000 --> 00:00:02,000\nx\n") for r, c in tries])[1]
        provisional = []
        self.worker._set_provisional = lambda job: provisional.append(sorted(job.fetched))
        self.worker._await_pick(self.job, self.cands(), "b.mp4", None)
        self.assertEqual(sorted(fetched), [("eng", ["os-9"]), ("rus", ["os-r"])])   # the guess and the other's first tries
        self.assertEqual(provisional, [["eng", "rus"]])
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, True)])
        self.assertNotIn(self.ALIAS, self.worker.judged)                              # the pick keeps waiting

    def test_a_no_track_file_not_picked_yet_waits_for_its_pick(self):
        self.s.anchored_guess = False
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        self.worker._fetch_all = lambda *a: self.fail("nothing to download without anchored guesses")
        self.worker._await_pick(self.job, self.cands(), "b.mp4", None)
        self.assertIn(self.ALIAS, self.worker.pickable)
        self.assertEqual(len(self.worker.jobs), 0)
        self.assertEqual(self.worker.judged[self.ALIAS], {"eng", "rus"})

    def test_a_picked_files_audio_job_waits_for_its_text_pass(self):
        text = Job(self.job.type, self.job.video_id, self.job.hints)
        self.worker.current = text                                                # the text pass still runs
        picked = Job(self.job.type, self.job.video_id, self.job.hints)
        picked.audio = True
        self.worker.submit(picked)
        got = []
        t = threading.Thread(target=lambda: got.append(self.worker._next("audio")), daemon=True)
        t.start()
        t.join(0.3)
        self.assertEqual(got, [])                                                # never both at once on one file
        with self.worker.cv:
            self.worker.current = None                                            # the text pass ends
            self.worker.cv.notify_all()
        t.join(6.0)
        self.assertEqual(got, [picked])
        self.worker.stop_flag = True

    def test_a_language_the_audio_route_judged_gets_no_anchored_guess(self):
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}}
        job = Job(self.job.type, self.job.video_id, self.job.hints)
        main = "1\n00:00:01,000 --> 00:00:02,000\nx\n"
        job.fetched = {"eng": [({"id": "os-9"}, main)], "rus": [({"id": "os-r"}, main)]}
        self.worker.judged[self.ALIAS] = {"rus"}                                 # the audio route's verdict came first
        import subsync.pipeline as pl
        real = pl.anchored_guess
        pl.anchored_guess = lambda main_text, texts: {"index": 0, "body": "anchored", "share": 0.9}
        try:
            self.worker._set_provisional(job)
        finally:
            pl.anchored_guess = real
        self.assertEqual(self.worker.provisional[self.ALIAS], {})

    def test_the_audio_job_reuses_its_text_pass_list_and_downloads(self):
        calls = []

        class Listing(FakeAios):
            def subtitles(self, *a):
                calls.append(a)
                return [{"id": "os-9", "url": "http://opensubs/x.srt", "lang": "eng"}]
        self.worker.aios = Listing()
        self.assertEqual(len(self.worker._candidates(self.job, {})), 1)
        audio = Job(self.job.type, self.job.video_id, self.job.hints)
        audio.audio = True
        self.assertEqual(self.worker._candidates(audio, {})[0]["id"], "os-9")
        self.assertEqual(len(calls), 1)                                    # not asked again
        self.worker._candidates(self.job, {})                              # a text pass asks again
        self.assertEqual(len(calls), 2)
        import subsync.worker as w
        got, real = [], w.fetch_subtitle
        w.fetch_subtitle = lambda url: (got.append(url), b"sub")[1]
        try:
            cand = {"id": "os-9", "url": "http://opensubs/x.srt", "lang": "eng"}
            self.assertEqual(self.worker._fetch_one(self.job, "eng", 0, cand)[2], b"sub")
            self.assertEqual(self.worker._fetch_one(audio, "eng", 0, cand)[2], b"sub")
        finally:
            w.fetch_subtitle = real
        self.assertEqual(got, ["http://opensubs/x.srt"])

    def test_a_download_before_the_file_is_known_to_need_audio_is_remembered(self):
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}}
        self.assertEqual(self.get("/pick/" + token(self.ALIAS) + "/eng.srt")[0], 302)
        self.assertEqual(len(self.worker.jobs), 0)
        self.assertIn(self.ALIAS, self.worker.picked_early)
        self.worker._needs_pick(self.job)                              # the index read ends: no subtitle track
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, True)])
        self.assertNotIn(self.ALIAS, self.worker.pickable)

    def test_a_synced_copy_is_served_from_the_same_link(self):
        self.waiting()
        key = self.store.put("f" * 16 + "-5", SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nx\n", fmt="srt")
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual(code, 200)
        self.assertIn(b"00:00:01,000", body)
        self.assertIsNotNone(key)

    def test_a_language_without_any_subtitle_gets_a_one_line_note_not_an_error(self):
        # Stock AIOMetadata turned subsync's 404 into an HTTP 502 for the viewer
        self.waiting()
        code, body, headers = self.get("/pick/" + token(self.ALIAS) + "/rus.srt")
        self.assertEqual(code, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/x-subrip"))
        text = body.decode("utf-8")
        self.assertIn("-->", text)
        self.assertIn("No Russian subtitle was found for this file.", text)
        self.assertEqual(len(self.worker.jobs), 1)                     # the English pick is unaffected

    def test_a_list_still_unknown_after_the_wait_asks_to_retry(self):
        self.s.pick_wait = 0.3
        http(self.base + self.URL)                                     # unknown file: a job is queued, list unknown
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers.get("Retry-After")), (503, "1"))   # retriable, unlike a note kept an hour

    def test_a_pick_waits_the_pick_wait_in_total_never_more(self):
        # The result wait and the list wait each took the whole wait (up to 28 s), past AIOMetadata's 15 s
        # subtitle timeout, and the viewer got HTTP 502
        self.s.pick_wait = 1.0
        http(self.base + self.URL)                                     # unknown file: an index-only job, nothing answers
        t0 = time.time()
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers.get("Retry-After")), (503, "1"))
        self.assertLess(time.time() - t0, 1.5)

    def test_a_download_while_best_fit_judges_gets_the_best_copy_so_far(self):
        # Passes are published before every subtitle is judged; the final best slot is written at the end.
        # Sherlock S01E01's first download was the first pass (Russian 62 % within 200 ms) where a better one (82 %) came
        # seconds later: the download waits for the language's final result within the pick wait, and is served the best
        # pass published by then
        self.s.pick_wait = 3.0
        http(self.base + self.URL)                                     # unknown file: an index-only job is queued
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        keys = [self.store.put("f" * 16 + "-5", sha, "eng", rank, "synced", fmt="srt", offer=False,
                               body=f"1\n00:00:01,000 --> 00:00:02,000\n{label}\n")
                for sha, rank, label in ((SHA_A, 3, "first pass"), (SHA_B, 5, "better pass"))]

        def judging():
            time.sleep(0.2)
            self.worker._publish(self.ALIAS, "eng", keys[0])
            time.sleep(0.3)
            self.worker._publish(self.ALIAS, "eng", keys[1])
        threading.Thread(target=judging, daemon=True).start()
        t0 = time.time()
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual(code, 200)
        self.assertIn(b"better pass", body)
        self.assertGreaterEqual(time.time() - t0, 2.5)                 # still judging: it waited the pick wait out

    def test_a_new_job_for_the_file_is_waited_for_not_the_last_ones_result(self):
        # review: a file judged before (a rule change, a list asked again) is queued anew; a pick must wait for the new
        # job's final result, not return at once with the old one
        self.worker._publish(self.ALIAS, "eng", None, final=True)       # the earlier job kept the original
        self.worker.submit(Job("movie", "tt9", {"filename": "b.mp4", "videoSize": 5}))
        self.assertNotIn("eng", self.worker.judged.get(self.ALIAS, set()))

    def test_a_download_is_answered_once_the_language_is_judged(self):
        self.s.pick_wait = 5.0
        http(self.base + self.URL)
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        key = self.store.put("f" * 16 + "-5", SHA_A, "eng", 1, "synced", fmt="srt", offer=False,
                             body="1\n00:00:01,000 --> 00:00:02,000\nthe best\n")

        def judged():
            time.sleep(0.3)
            self.worker._publish(self.ALIAS, "eng", key, final=True)
        threading.Thread(target=judged, daemon=True).start()
        t0 = time.time()
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, b"the best" in body), (200, True))
        self.assertLess(time.time() - t0, 2.0)

    def test_a_download_moves_its_files_job_to_the_front(self):
        self.s.pick_wait = 0.3
        self.worker.submit(Job("movie", "tt1", {"filename": "other.mkv", "videoSize": 9}))   # another file asked first
        http(self.base + self.URL)                                     # this file's job, behind it
        self.assertEqual([j.alias for j in self.worker.queue][-1], self.ALIAS)
        self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual(self.worker.queue[0].alias, self.ALIAS)

    def test_a_file_waiting_for_a_pick_is_remembered_after_a_restart(self):
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        self.worker._needs_pick(self.job)
        self.store.file_read("f" * 16 + "-5", 5, "f" * 16, False, {})   # read a moment ago: no new job
        self.store.alias(self.ALIAS, "f" * 16 + "-5")
        restarted = Worker(self.s, self.store, FakeAios(), Safety())   # a new process: nothing in memory
        self.assertTrue(restarted.waits_for_pick(self.ALIAS))
        app = App(self.s, self.store, restarted, Safety())
        subs = app.subtitles("movie", "tt9", "filename=b.mp4&videoSize=5")
        self.assertEqual([(s["lang"], s["label"], s["variantOf"]) for s in subs], [("eng", "English (Subsync)", "os-9")])
        self.assertTrue(restarted.pick(self.ALIAS))
        self.assertEqual([(j.alias, j.audio, j.type, j.video_id, j.hints) for j in restarted.jobs.values()],
                         [(self.ALIAS, True, "movie", "tt9", {"filename": "b.mp4", "videoSize": 5})])
        self.assertFalse(Worker(self.s, self.store, FakeAios(), Safety()).waits_for_pick(self.ALIAS))   # picked once

    def test_an_unknown_link_starts_nothing(self):
        self.assertEqual(self.get("/pick/" + token("name:other.mp4|9") + "/eng.srt")[0], 404)
        self.assertEqual(self.get("/pick/!!!/eng.srt")[0], 404)
        self.assertEqual(len(self.worker.jobs), 0)


class LanesTest(unittest.TestCase):
    """The audio route (about a minute per file) runs on its own worker, so it never holds another file's job."""

    def test_a_plain_job_finishes_while_an_audio_job_runs(self):
        with TempDir() as d:
            s = Settings({"SUBSYNC_DATA": d})
            release, order = threading.Event(), []

            class W(Worker):
                def process(self, job):
                    if job.audio:
                        release.wait(10)
                    order.append(job.alias)
            w = W(s, Store(d), FakeAios(), Safety())
            audio = Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1})
            audio.audio = True
            w.submit(audio)
            w.start()
            time.sleep(0.3)                                    # the audio job is running
            w.submit(Job("movie", "tt2", {"filename": "b.mkv", "videoSize": 2}))
            t0 = time.time()
            while "name:b.mkv|2" not in order and time.time() - t0 < 3:
                time.sleep(0.05)
            self.assertEqual(order, ["name:b.mkv|2"])           # done while the audio job still runs
            self.assertTrue(w.busy_with("name:a.mkv|1"))
            release.set()
            t0 = time.time()
            while len(order) < 2 and time.time() - t0 < 3:
                time.sleep(0.05)
            self.assertEqual(order, ["name:b.mkv|2", "name:a.mkv|1"])
            w.stop_flag = True
            with w.cv:
                w.cv.notify_all()


class AddonKeyTest(unittest.TestCase):
    """The routes that start work or show status answer only under the private key; files and /health don't
    need it; a base path a reverse proxy forwards as is is removed first."""

    def setUp(self):
        self.tmp = TempDir()
        d = self.tmp.__enter__()
        self.s = Settings({"SUBSYNC_DATA": d, "SUBSYNC_PUBLIC_BASE": "https://example.org/subsync",
                           "SUBSYNC_ADDON_KEY": "/k3y/", "SUBSYNC_BASE_PATH": "subsync/"})
        self.store = Store(d)
        self.worker = Worker(self.s, self.store, FakeAios(), Safety())   # not started
        self.app = App(self.s, self.store, self.worker, Safety())
        self.server = serve(self.app, 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.__exit__(None, None, None)

    def test_settings_normalised(self):
        self.assertEqual((self.s.addon_key, self.s.base_path), ("k3y", "/subsync"))
        self.assertEqual((Settings({}).addon_key, Settings({}).base_path), ("", ""))

    def test_work_routes_need_the_key(self):
        for path in ("/manifest.json", "/status", "/subtitles/movie/tt1/filename=a.mkv&videoSize=5.json",
                     "/subsync/manifest.json", "/wrong/manifest.json", "/k3yy/manifest.json"):
            self.assertEqual(http(self.base + path)[0], 404, path)
        self.assertEqual(len(self.worker.jobs), 0)
        self.assertEqual(self.app.subtitle_requests, 0)
        for prefix in ("/k3y", "/subsync/k3y"):
            code, body, _ = http(self.base + prefix + "/manifest.json")
            self.assertEqual((code, json.loads(body)["resources"]), (200, ["subtitles"]))
        code, body, _ = http(self.base + "/k3y/subtitles/movie/tt1.json")
        self.assertEqual((code, json.loads(body)["subtitles"]), (200, []))
        code, body, _ = http(self.base + "/subsync/k3y/status")
        status = json.loads(body)
        self.assertEqual((code, status["subtitle_requests"]), (200, 1))
        self.assertIsInstance(status["last_subtitle_request"], int)
        self.assertEqual((status["busy"], status["busy_audio"]), (False, False))
        self.worker.current_audio = Job("movie", "tt1", {"filename": "a.mkv"})   # the audio worker on a file
        status = json.loads(http(self.base + "/subsync/k3y/status")[1])
        self.assertEqual((status["busy"], status["busy_audio"]), (True, True))

    def test_files_and_health_are_open(self):
        self.store.put(FILE, SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n", fmt="srt")
        self.store.alias("name:a.mkv|1000", FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {})
        code, body, _ = http(self.base + "/k3y/subtitles/movie/tt1/filename=a.mkv&videoSize=1000.json")
        url = json.loads(body)["subtitles"][0]["url"]
        self.assertTrue(url.startswith(f"https://example.org/subsync/file/{ALGO_VERSION}_"))
        path = url[len("https://example.org"):]
        for p in (path, path[len("/subsync"):], "/k3y" + path[len("/subsync"):]):
            self.assertEqual(http(self.base + p)[0], 200, p)
        for p in ("/health", "/subsync/health", "/k3y/health"):
            self.assertEqual(http(self.base + p)[:2], (200, b"ok"), p)

    def test_no_key_keeps_the_old_routes(self):
        app = App(Settings({"SUBSYNC_DATA": self.s.data}), self.store, self.worker, Safety())
        self.assertEqual(app.route("/manifest.json"), ("/manifest.json", True))
        self.assertEqual(app.route("/k3y/manifest.json"), ("/k3y/manifest.json", True))


class NameOnlyTest(unittest.TestCase):
    """A request with a file name but no size names that file, never just the title."""

    def setUp(self):
        self.tmp = TempDir()
        d = self.tmp.__enter__()
        self.s = Settings({"SUBSYNC_DATA": d, "SUBSYNC_PUBLIC_BASE": "http://subsync:8790"})
        self.store = Store(d)
        self.worker = Worker(self.s, self.store, FakeAios(), Safety())   # not started
        self.app = App(self.s, self.store, self.worker, Safety())

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_alias(self):
        self.assertEqual(Job("movie", "tt1", {"filename": "a.mkv"}).alias, "name:a.mkv|None")
        self.assertEqual(Job("movie", "tt1", {"videoHash": "ab", "videoSize": 3}).alias, "hash:ab|3")
        self.assertEqual(Job("movie", "tt1").alias, "title:movie/tt1")

    def test_another_release_of_the_title_gets_nothing_of_this_file(self):
        self.store.put(FILE, SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n", fmt="srt")
        self.store.alias(Job("movie", "tt1", {"filename": "a.mkv"}).alias, FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {})
        self.assertEqual(len(self.app.subtitles("movie", "tt1", "filename=a.mkv")), 1)
        other = self.app.subtitles("movie", "tt1", "filename=b.mkv")
        # b.mkv is queued for its own sync: only its own "(Subsync)" entries, nothing made for a.mkv
        self.assertEqual([s["label"] for s in other], ["English (Subsync)"])
        self.assertTrue(other[0]["url"].endswith("/pick/" + token("name:b.mkv|None") + "/eng.srt"))
        self.assertEqual(self.app.subtitles("movie", "tt1", ""), [])


class StreamDataTest(unittest.TestCase):
    def test_listing_user_agent_gets_stream_data_from_stock_aios(self):
        # Stock AIOStreams sends streamData only to clients whose User-Agent starts with "AIO"
        from subsync import sources
        self.assertTrue(sources.USER_AGENT.startswith("AIO"))


class AudioOffEntryTest(unittest.TestCase):
    """With the audio option off, subsync answers at once; while it works on a file it lists one
    "<Language> (Subsync)" entry per language, whose download serves the synced copy when ready, else the original."""
    URL = "/subtitles/movie/tt9/filename=b.mkv&videoSize=5.json"
    ALIAS = "name:b.mkv|5"

    def setUp(self):
        self.tmp = TempDir()
        d = self.tmp.__enter__()
        self.s = Settings({"SUBSYNC_DATA": d, "SUBSYNC_PUBLIC_BASE": "http://subsync:8790", "SUBSYNC_LANGUAGES": "eng,rus",
                           "SUBSYNC_PENDING_WAIT_MS": "3000", "SUBSYNC_PICK_WAIT_MS": "3000"})
        self.assertFalse(self.s.audio_route or self.s.pending_entry)    # the stock defaults
        self.store = Store(d)
        self.worker = Worker(self.s, self.store, FakeAios(), Safety())  # not started: jobs stay queued
        self.server = serve(App(self.s, self.store, self.worker, Safety()), 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.__exit__(None, None, None)

    def get(self, path):
        try:
            r = urllib.request.build_opener(NoRedirect).open(self.base + path, timeout=10)
            return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def test_the_first_answer_comes_at_once_with_an_entry_per_language(self):
        t0 = time.time()
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]
        self.assertLess(time.time() - t0, 1.0)                 # AIOStreams orders addons by who answers first
        self.assertEqual([(s["lang"], s["label"], s["variantOf"]) for s in subs],
                         [("eng", "English (Subsync)", "original"), ("rus", "Russian (Subsync)", "original")])
        self.assertTrue(subs[0]["url"].endswith("/pick/" + token(self.ALIAS) + "/eng.srt"))
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, False)])

    def test_picking_the_entry_serves_the_copy_the_running_job_makes(self):
        http(self.base + self.URL)                                       # the job is queued
        with self.worker.cv:
            self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}

        def copy_ready():
            time.sleep(0.4)
            key = self.store.put(FILE, SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n", fmt="srt")
            self.store.alias(self.ALIAS, FILE)
            self.worker._publish(self.ALIAS, "eng", key, final=True)     # the job's result for the language
        threading.Thread(target=copy_ready, daemon=True).start()
        t0 = time.time()
        code, body, _ = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual(code, 200)
        self.assertIn(b"00:00:01,000 --> 00:00:02,000", body)
        self.assertLess(time.time() - t0, 2.5)

    def test_picking_after_the_original_was_kept_serves_the_original_and_reads_nothing_more(self):
        self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers["Location"]), (302, "http://opensubs/x.srt"))
        self.assertEqual((len(self.worker.jobs), len(self.worker.picked_early)), (0, 0))   # no audio read started

    def test_a_finished_copy_is_listed_as_synced_without_a_subsync_entry(self):
        self.store.put(FILE, SHA_A, "eng", 0, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n", fmt="srt")
        self.store.alias(self.ALIAS, FILE)
        self.store.file_read(FILE, 5, FILE[:16], True, {})               # read just now: no new job
        subs = json.loads(http(self.base + self.URL)[1])["subtitles"]
        self.assertEqual([s["label"] for s in subs], ["English (synced)"])


class EmptyListTest(unittest.TestCase):
    """A subtitle list that comes back empty (a slow subtitle addon left out of AIOStreams' answer) is not a
    "none found": nothing is remembered, and a pick asks for the list again instead of serving the note."""
    ALIAS = "name:b.mkv|5"

    def setUp(self):
        self.tmp = TempDir()
        d = self.tmp.__enter__()
        self.s = Settings({"SUBSYNC_DATA": d, "SUBSYNC_PUBLIC_BASE": "http://subsync:8790", "SUBSYNC_LANGUAGES": "eng,rus",
                           "SUBSYNC_PICK_WAIT_MS": "300"})
        self.store = Store(d)
        self.aios = FakeAios()
        self.aios.row = {"url": "http://127.0.0.1:9/never"}
        self.worker = Worker(self.s, self.store, self.aios, Safety())   # not started
        self.server = serve(App(self.s, self.store, self.worker, Safety()), 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.__exit__(None, None, None)

    def test_an_empty_list_is_not_remembered_as_no_subtitle(self):
        job = Job("movie", "tt9", {"filename": "b.mkv", "videoSize": 5})
        self.worker.submit(job)
        self.worker.jobs.clear()
        self.worker.queue.clear()
        self.worker.process(job)                                        # the list comes back empty
        self.assertIsNone(self.worker.known_originals(self.ALIAS))
        self.assertIsNone(self.store.originals_get(self.ALIAS))
        self.assertEqual(self.store.bytes_today("read"), 0)

    def test_a_pick_after_an_empty_list_asks_for_the_list_again(self):
        job = Job("movie", "tt9", {"filename": "b.mkv", "videoSize": 5})
        self.worker.submit(job)
        self.worker.jobs.clear()
        self.worker.queue.clear()
        self.worker.process(job)
        try:
            urllib.request.build_opener(NoRedirect).open(self.base + "/pick/" + token(self.ALIAS) + "/eng.srt", timeout=10)
            self.fail("expected 503")
        except urllib.error.HTTPError as e:
            self.assertEqual((e.code, e.headers.get("Retry-After")), (503, "1"))   # not the "none found" note
        self.assertEqual([(j.alias, j.type, j.video_id) for j in self.worker.jobs.values()], [(self.ALIAS, "movie", "tt9")])


class BestGuessFallbackTest(EmptyListTest):
    """While there is no copy, a download of subsync's entry serves the subtitle likeliest to fit the file
    as it is (a hash match, the release group, the kind of source), not the viewer's first: on Detectorists S01E01 that was
    a DVD rip 41.7 s off, where AutoSync served the WEB subtitle that fits by its name."""
    test_an_empty_list_is_not_remembered_as_no_subtitle = test_a_pick_after_an_empty_list_asks_for_the_list_again = None
    NAME = "Show.S01E01.WEBRip.x264-GRP.mkv"

    def setUp(self):
        super().setUp()
        cands = [{"id": "os-dvd", "url": "http://subs/dvd.srt", "lang": "eng", "release": "Show S01E01 DVDRip NonHI"},
                 {"id": "os-web", "url": "http://subs/web.srt", "lang": "eng", "release": "Show.S01E01.WEB"},
                 {"id": "os-rus", "url": "http://subs/rus.srt", "lang": "rus", "release": "Show"},
                 {"id": "os-hash", "url": "http://subs/hash.srt", "lang": "rus", "release": "Show", "hash": "ab12"}]
        self.aios.subtitles = lambda *a: [dict(c) for c in cands]

    def originals(self, hints, **env):
        for k, v in env.items():
            setattr(self.s, k, v)
        job = Job("series", "tt9:1:1", dict({"filename": self.NAME, "videoSize": 5}, **hints))
        self.worker.process(job)                                       # the list is known; the read then fails
        return {lang: (o or {}).get("id") for lang, o in self.worker.known_originals(job.alias).items()}

    def test_the_likeliest_fit_is_served_while_there_is_no_copy(self):
        self.assertEqual(self.originals({}), {"eng": "os-web", "rus": "os-rus"})
        self.assertEqual(self.originals({"videoHash": "AB12"}), {"eng": "os-web", "rus": "os-hash"})

    def test_ranking_off_keeps_the_viewers_first(self):
        self.assertEqual(self.originals({}, rank_by_release=False), {"eng": "os-dvd", "rus": "os-rus"})


class BestFitOffersTest(unittest.TestCase):
    """(2), (3), (6): the passing results of a file and language are offered best fit first; the main language
    lists up to 3 of them, the others 1; a pick (the "(Subsync)" entry) waits up to 14 s for the result."""

    def setUp(self):
        self.tmp = TempDir()
        d = self.tmp.__enter__()
        self.s = Settings({"SUBSYNC_DATA": d, "SUBSYNC_PUBLIC_BASE": "http://subsync:8790", "SUBSYNC_LANGUAGES": "eng,rus"})
        self.store = Store(d)
        self.worker = Worker(self.s, self.store, FakeAios(), Safety())
        self.app = App(self.s, self.store, self.worker, Safety())

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def put(self, sha, lang, rank):
        return self.store.put(FILE, sha * 64, lang, rank, "synced", body=f"1\n00:00:0{rank + 1},000 --> 00:00:09,000\n{sha}\n",
                              fmt="srt", offer=False)

    def test_defaults(self):
        self.assertEqual(self.s.pick_wait, 14.0)                # AIOMetadata gives up on a subtitle after 15 s
        self.assertTrue(self.s.best_fit and self.s.skip_ai and self.s.prefer_plain)
        self.assertEqual(self.s.alternatives, 3)

    def test_offers_follow_the_given_order_and_the_first_is_the_one_a_pick_serves(self):
        a, b, c = self.put("a", "eng", 0), self.put("b", "eng", 1), self.put("c", "eng", 2)
        self.assertIsNone(self.store.last_good(FILE, "eng"))   # offer=False: nothing offered until the ranking is set
        self.store.set_offers(FILE, "eng", [c, a, b])
        self.assertEqual(self.store.last_good(FILE, "eng")["key"], c)
        self.assertEqual([r["key"] for r in self.store.offers(FILE, ["eng"], {"eng": 3})], [c, a, b])
        self.assertEqual([r["key"] for r in self.store.offers(FILE, ["eng"])], [c])   # one per language by default
        self.store.set_offers(FILE, "eng", [])
        self.assertIsNone(self.store.last_good(FILE, "eng"))

    def test_the_main_language_lists_up_to_three_the_others_one(self):
        keys = [self.put(x, "eng", i) for i, x in enumerate("abcd")]
        self.store.set_offers(FILE, "eng", keys)
        rus = [self.put(x, "rus", i) for i, x in enumerate("ef")]
        self.store.set_offers(FILE, "rus", rus)
        self.store.alias("name:a.mkv|1000", FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {"rule": self.s.rule_id})   # read just now: no new job
        subs = self.app.subtitles("movie", "tt1", "filename=a.mkv&videoSize=1000")
        self.assertEqual([s["label"] for s in subs],
                         ["English (synced)", "English (synced 2)", "English (synced 3)", "Russian (synced)"])
        self.assertEqual(len({s["id"] for s in subs}), 4)

    def test_fit_ranking(self):
        exact = {"outcome": "synced", "reason": "in sync as it is (viewer's #3): listed unchanged",
                 "detail": {"gate_original": {"median_ms": 0.0, "within200": 1.0}}}
        snapped = {"outcome": "synced", "reason": "passes against the release track (snapped)",
                   "detail": {"gate_synced": {"median_ms": 60.0, "within200": 0.86}}}
        snapped_worse = {"outcome": "synced", "reason": "passes against the release track",
                         "detail": {"gate_synced": {"median_ms": 90.0, "within200": 0.91}}}
        better = {"outcome": "synced", "reason": "clearly better than the original",
                  "detail": {"gate_synced": {"median_ms": 150.0, "within200": 0.6}}}
        speech = {"outcome": "synced", "reason": "clearly better than the original (speech)",
                  "detail": {"gate_synced": {"median_ms": 107.0, "within200": 0.64}}}
        speech_best = dict(speech, detail={"gate_synced": {"median_ms": 95.0, "within200": 0.64}})
        ranked = sorted([better, snapped_worse, exact, snapped], key=Worker.fit_rank)
        self.assertEqual(ranked, [exact, snapped_worse, snapped, better])   # unchanged first; then share, then median
        self.assertEqual(sorted([speech, speech_best], key=Worker.fit_rank), [speech_best, speech])

    def test_a_subtitle_moved_by_one_shift_ranks_with_the_unchanged_ones(self):
        # Game of Thrones S01E01: an in-sync original (91.6 % within 200 ms) kept a BluRay rip, 1 s off and exact after one
        # shift, from being tried; a whole-subtitle shift keeps its author's timing, so it ranks by fit with the originals
        original = {"outcome": "synced", "reason": "in sync as it is (viewer's #8): lines snapped to the release's own",
                    "detail": {"gate_original": {"median_ms": 56.0, "within200": 0.916}, "magnet": {}}}
        rip = {"outcome": "synced", "reason": "passes against the release track (one shift) (snapped)",
               "detail": {"gate_synced": {"median_ms": 1.0, "within200": 1.0}, "shift_fit": {"offset_s": -0.999}}}
        weak_shift = {"outcome": "synced", "reason": "clearly better than the original (one shift)",
                      "detail": {"gate_synced": {"median_ms": 150.0, "within200": 0.7}, "shift_fit": {"offset_s": 3.0}}}
        alass = {"outcome": "synced", "reason": "passes against the release track (snapped)",
                 "detail": {"gate_synced": {"median_ms": 0.0, "within200": 1.0}}}
        self.assertEqual(sorted([alass, weak_shift, original, rip], key=Worker.fit_rank), [rip, original, alass, weak_shift])

    def test_a_stacked_subtitle_ranks_after_plain_ones_of_its_class(self):
        # The Fellowship of the Ring: a fan subtitle with Elvish transcriptions laid over the English lines (7 % of its
        # lines start while another is on screen) fit best by a hair and was the first download
        layered = {"outcome": "synced", "reason": "in sync as it is (viewer's #6): lines snapped to the release's own",
                   "detail": {"gate_original": {"median_ms": 0.0, "within200": 0.997}, "magnet": {}, "stacked": 0.074}}
        plain = {"outcome": "synced", "reason": "passes against the release track (one shift) (snapped)",
                 "detail": {"gate_synced": {"median_ms": 0.0, "within200": 0.99}, "shift_fit": {"offset_s": -0.1}, "stacked": 0.0}}
        alass = {"outcome": "synced", "reason": "passes against the release track (snapped)",
                 "detail": {"gate_synced": {"median_ms": 0.0, "within200": 1.0}}}
        self.assertEqual(sorted([alass, layered, plain], key=Worker.fit_rank), [plain, layered, alass])
        far_worse = dict(plain, detail=dict(plain["detail"], gate_synced={"median_ms": 0.0, "within200": 0.91}))
        self.assertEqual(sorted([far_worse, layered], key=Worker.fit_rank), [layered, far_worse])   # not after a far worse one


class RuleChangeTest(unittest.TestCase):
    """A verdict judged under another pass rule (or other rule settings) is judged again."""

    def setUp(self):
        self.tmp = TempDir()
        d = self.tmp.__enter__()
        self.s = Settings({"SUBSYNC_DATA": d, "SUBSYNC_PUBLIC_BASE": "http://subsync:8790"})
        self.store = Store(d)
        self.worker = Worker(self.s, self.store, FakeAios(), Safety())
        self.app = App(self.s, self.store, self.worker, Safety())

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def test_the_rule_id_follows_the_pass_rule_settings(self):
        base = Settings({}).rule_id
        self.assertEqual(base, Settings({}).rule_id)
        for env in ({"SUBSYNC_BETTER_MEDIAN_MS": "150"}, {"SUBSYNC_BETTER": "off"}, {"SUBSYNC_SNAP_WITHIN": "0.9"},
                    {"SUBSYNC_AUDIO_BETTER": "off"}, {"SUBSYNC_AUDIO_BETTER_MEDIAN_MS": "120"},
                    {"SUBSYNC_AUDIO_BUDGET_GB": "2"}):
            self.assertNotEqual(Settings(env).rule_id, base, env)
        self.assertEqual(Settings({"SUBSYNC_LANGUAGES": "eng,rus"}).rule_id, base)   # not a pass rule setting

    def test_a_kept_original_judged_under_another_rule_is_judged_again(self):
        key = self.store.put(FILE, SHA_A, "eng", 0, "keep_original", "below the bar", detail={"rule": "older"})
        self.assertIsNone(self.worker.fresh(key))
        self.store.put(FILE, SHA_A, "eng", 0, "keep_original", "below the bar", detail={"rule": self.s.rule_id})
        self.assertIsNotNone(self.worker.fresh(key))
        synced = self.store.put(FILE, SHA_B, "eng", 1, "synced", body="1\n00:00:01,000 --> 00:00:02,000\nHi\n",
                                fmt="srt", route="audio", detail={"rule": "older"})
        self.assertIsNotNone(self.worker.fresh(synced))                  # an audio-route copy that passed stays offered

    def test_a_result_from_the_files_own_track_judged_under_another_rule_is_judged_again(self):
        # the index is read anyway: a copy made against a typesetting track before that track was refused, or an in-sync
        # original listed before the magnet, is made again under the rule in force
        for outcome, route in (("synced", "cues"), ("in_sync", "cues"), ("synced", "image")):
            key = self.store.put(FILE, SHA_C, "eng", 1, outcome, body="1\n00:00:01,000 --> 00:00:02,000\nHi\n",
                                 fmt="srt", route=route, detail={"rule": "older"})
            self.assertIsNone(self.worker.fresh(key), (outcome, route))
            self.store.put(FILE, SHA_C, "eng", 1, outcome, body="1\n00:00:01,000 --> 00:00:02,000\nHi\n",
                           fmt="srt", route=route, detail={"rule": self.s.rule_id})
            self.assertIsNotNone(self.worker.fresh(key), (outcome, route))

    def test_a_file_read_under_another_rule_is_queued_again_at_once(self):
        self.store.alias("name:a.mkv|1000", FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {"rule": "older"})
        self.app.subtitles("movie", "tt1", "filename=a.mkv&videoSize=1000")
        self.assertEqual(len(self.worker.jobs), 1)
        self.worker.jobs.clear()
        self.worker.queue.clear()
        self.store.file_read(FILE, 1000, FILE[:16], True, {"rule": self.s.rule_id})
        self.app.subtitles("movie", "tt1", "filename=a.mkv&videoSize=1000")
        self.assertEqual(len(self.worker.jobs), 0)                       # read under this rule within 24 h
