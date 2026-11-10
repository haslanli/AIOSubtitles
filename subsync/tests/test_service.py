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
from subsync.sources import Aios, extras, parse_extras, unreadable  # noqa: E402
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

        def list_arrives():
            time.sleep(0.4)
            with self.worker.cv:
                self.worker.originals[self.ALIAS] = {"eng": {"url": "http://opensubs/x.srt", "id": "os-9"}, "rus": None}
                self.worker.cv.notify_all()
        threading.Thread(target=list_arrives, daemon=True).start()
        t0 = time.time()
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers["Location"]), (302, "http://opensubs/x.srt"))
        self.assertLess(time.time() - t0, 2.5)
        self.assertEqual(self.get("/pick/" + token(self.ALIAS) + "/rus.srt")[0], 404)   # no Russian subtitle exists
        self.assertIn(self.ALIAS, self.worker.picked_early)                              # remembered for the index read

    def test_downloading_the_entry_serves_the_original_and_starts_the_audio_route_once(self):
        self.waiting()
        code, _, headers = self.get("/pick/" + token(self.ALIAS) + "/eng.srt")
        self.assertEqual((code, headers["Location"]), (302, "http://opensubs/x.srt"))
        self.assertNotIn(self.ALIAS, self.worker.pickable)
        self.assertEqual([(j.alias, j.audio) for j in self.worker.jobs.values()], [(self.ALIAS, True)])
        self.get("/pick/" + token(self.ALIAS) + "/eng.srt")           # again: nothing more is queued
        self.assertEqual(len(self.worker.jobs), 1)

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

    def test_an_unknown_link_starts_nothing(self):
        self.assertEqual(self.get("/pick/" + token("name:other.mp4|9") + "/eng.srt")[0], 404)
        self.assertEqual(self.get("/pick/!!!/eng.srt")[0], 404)
        self.assertEqual(len(self.worker.jobs), 0)


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
        self.assertEqual(self.app.subtitles("movie", "tt1", "filename=b.mkv"), [])
        self.assertEqual(self.app.subtitles("movie", "tt1", ""), [])


class StreamDataTest(unittest.TestCase):
    def test_listing_user_agent_gets_stream_data_from_stock_aios(self):
        # Stock AIOStreams sends streamData only to clients whose User-Agent starts with "AIO"
        from subsync import sources
        self.assertTrue(sources.USER_AGENT.startswith("AIO"))
