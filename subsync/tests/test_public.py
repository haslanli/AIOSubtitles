"""Public mode: its settings, the pool of users' workers, the shared store's memory per language, and the worker's turn
and limits."""
import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from subsync.settings import Settings  # noqa: E402

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


class PublicSettingsTest(unittest.TestCase):
    def test_defaults(self):
        s = Settings({})
        self.assertFalse(s.public)
        self.assertEqual(s.secret_key, "")
        self.assertEqual(s.invite, "")
        self.assertEqual(s.max_users, 200)
        self.assertEqual(s.signups_per_hour, 5)
        self.assertEqual(s.user_files_per_day, 40)
        self.assertEqual(s.user_daily_bytes, 500 * 1024 * 1024)
        self.assertEqual(s.user_queue, 3)
        self.assertEqual(s.active_workers, 30)
        self.assertEqual(s.worker_idle_s, 1800)
        self.assertEqual(s.user_keep_days, 180)
        self.assertEqual(s.trusted_proxy, ("127.0.0.1",))
        self.assertEqual(s.max_connections, 64)

    def test_trusted_proxy_is_a_list(self):
        self.assertEqual(Settings({"SUBSYNC_TRUSTED_PROXY": "127.0.0.1, ::1"}).trusted_proxy, ("127.0.0.1", "::1"))

    def test_missing_in_public_mode(self):
        self.assertEqual(Settings({"SUBSYNC_PUBLIC": "on"}).missing(), ["SUBSYNC_SECRET_KEY", "SUBSYNC_PUBLIC_BASE"])
        short = base64.urlsafe_b64encode(bytes(31)).decode()
        self.assertIn("SUBSYNC_SECRET_KEY", Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": short}).missing())
        self.assertIn("SUBSYNC_SECRET_KEY", Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": "not base64!"}).missing())
        ok = Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY, "SUBSYNC_PUBLIC_BASE": "https://x.test"})
        self.assertEqual(ok.missing(), [])
        self.assertEqual(ok.secret_bytes(), bytes(range(32)))

    def test_secret_bytes_refuses_a_short_key(self):
        with self.assertRaises(ValueError):
            Settings({"SUBSYNC_SECRET_KEY": base64.urlsafe_b64encode(bytes(31)).decode()}).secret_bytes()

    def test_self_hosted_missing_unchanged(self):
        self.assertEqual(Settings({}).missing(),
                         ["SUBSYNC_SOURCE_MANIFEST", "SUBSYNC_SOURCE_PASSWORD", "SUBSYNC_PUBLIC_BASE"])

    def test_audio_route_conflicts_in_public_mode(self):
        self.assertEqual(Settings({"SUBSYNC_PUBLIC": "on"}).conflicts(), [])
        self.assertEqual(len(Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_AUDIO_ROUTE": "on"}).conflicts()), 1)
        self.assertEqual(Settings({"SUBSYNC_AUDIO_ROUTE": "on"}).conflicts(), [])


PRESETS = [{"type": "torrentio", "instanceId": "tio", "enabled": True, "options": {"name": "Torrentio"}},
           {"type": "opensubtitles", "instanceId": "ost", "enabled": True,
            "options": {"name": "OpenSubtitles", "resources": ["subtitles"]}},
           {"type": "custom", "instanceId": "ssy", "enabled": True,
            "options": {"name": "subsync", "manifestUrl": "http://own:8790/K/manifest.json", "resources": ["subtitles"]}}]
VIEWER = {"services": [{"id": "torbox", "enabled": True, "credentials": {"apiKey": "k"}}], "presets": PRESETS}
LINK = "https://aiosubtitles.test/u/" + "t" * 43 + "/manifest.json"


class SharedRulesTest(unittest.TestCase):
    def test_public_addon_is_named_and_placed_before_the_other_subtitles(self):
        from subsync import aiosconfig
        viewer = dict(VIEWER, presets=PRESETS[:2])
        new, changes = aiosconfig.viewer_change(viewer, LINK, name="AIOSubtitles")
        names = [p["options"].get("name") for p in new["presets"]]
        self.assertEqual(names, ["Torrentio", "AIOSubtitles", "OpenSubtitles"])
        self.assertEqual(new["presets"][1]["options"]["manifestUrl"], LINK)
        self.assertEqual(aiosconfig.viewer_change(new, LINK, name="AIOSubtitles")[1], [])   # already in place

    def test_the_sync_config_leaves_out_both_addons(self):
        from subsync import aiosconfig
        viewer, _ = aiosconfig.viewer_change(VIEWER, LINK, name="AIOSubtitles")
        sync = aiosconfig.sync_config(viewer, LINK, names=("aiosubtitles", "subsync"))
        self.assertEqual([p["options"].get("name") for p in sync["presets"]], ["Torrentio", "OpenSubtitles"])
        self.assertFalse(sync["failover"]["enabled"])


HASH = "ab" * 20
FILE = "0123456789abcdef-1000"


class ListingAios:
    """A listing with one row and a fixed subtitle list; counts the subtitle-list requests."""

    def __init__(self, row=None, subtitles=()):
        self.row, self.subs, self.subtitle_requests = row, list(subtitles), 0
        self.base = ""

    def find_row(self, *a):
        return self.row

    def subtitles(self, *a):
        self.subtitle_requests += 1
        return [dict(c) for c in self.subs]


class SharedStoreTest(unittest.TestCase):
    def setUp(self):
        from helpers import TempDir
        from subsync.store import Store
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.store = Store(self.dir)

    def tearDown(self):
        self.tmp.__exit__(None, None, None)

    def app(self, languages, aios=None):
        from subsync.addon import App
        from subsync.worker import Safety, Worker
        s = Settings({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC_BASE": "http://x.test", "SUBSYNC_LANGUAGES": languages})
        worker = Worker(s, self.store, aios or ListingAios(), Safety())   # not started: jobs stay queued
        return App(s, self.store, worker, Safety())

    def test_bt_alias(self):
        from subsync.sources import bt_alias
        row = lambda h, i: {"streamData": {"torrent": {"infoHash": h, "fileIdx": i}}}   # noqa: E731
        self.assertEqual(bt_alias(row(HASH.upper(), 3)), f"bt:{HASH}:3")
        self.assertEqual(bt_alias(row(HASH, 0)), f"bt:{HASH}:0")
        for bad in (row(HASH, None), row(HASH, -1), row(HASH, True), row("abc", 0), row("g" * 40, 0), {}, {"url": "x"}):
            with self.subTest(row=bad):
                self.assertIsNone(bt_alias(bad))

    def test_judged_all(self):
        self.assertIsNone(self.store.judged_all(FILE, ["eng"], "r1", 0))      # stored before this memory existed
        self.store.judged_put(FILE, ["eng"], "r1")
        self.assertTrue(self.store.judged_all(FILE, ["eng"], "r1", 0))
        self.assertFalse(self.store.judged_all(FILE, ["eng", "rus"], "r1", 0))
        self.assertFalse(self.store.judged_all(FILE, ["eng"], "r2", 0))       # judged under another rule
        self.assertFalse(self.store.judged_all(FILE, ["eng"], "r1", 4e9))     # too long ago

    def _known_file(self, langs, rule):
        self.store.alias("name:a.mkv|1000", FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {"rule": rule})
        self.store.judged_put(FILE, langs, rule)

    def test_another_language_gets_the_file_read_again(self):
        app = self.app("eng,rus")
        self._known_file(["eng"], app.s.rule_id)
        app.subtitles("movie", "tt1", "filename=a.mkv&videoSize=1000")
        self.assertEqual(len(app.worker.jobs), 1)

    def test_all_languages_judged_queue_nothing(self):
        app = self.app("eng,rus")
        self._known_file(["eng", "rus"], app.s.rule_id)
        app.subtitles("movie", "tt1", "filename=a.mkv&videoSize=1000")
        self.assertEqual(len(app.worker.jobs), 0)

    def test_a_known_torrent_file_is_not_read_again(self):
        from subsync.worker import Job
        aios = ListingAios({"url": "http://127.0.0.1:9/never", "streamData": {"torrent": {"infoHash": HASH, "fileIdx": 0}}},
                           [{"id": "os-1", "url": "http://127.0.0.1:9/sub.srt", "lang": "eng", "release": "", "hash": "",
                             "hash_match": False, "ai": False}])
        app = self.app("eng", aios)
        self.store.alias(f"bt:{HASH}:0", FILE)
        self.store.file_read(FILE, 1000, FILE[:16], True, {"rule": app.s.rule_id})
        self.store.judged_put(FILE, ["eng"], app.s.rule_id)
        job = Job("movie", "tt1", {"filename": "other-name.mkv", "videoSize": 1000})
        app.worker.process(job)
        self.assertEqual(self.store.resolve(job.alias), FILE)
        self.assertEqual(aios.subtitle_requests, 0)
        self.assertEqual(self.store.bytes_today("read"), 0)

    def test_a_read_records_the_torrent_and_the_languages(self):
        from helpers import FIXTURES, RangeServer
        from subsync.worker import Job
        srv = RangeServer(FIXTURES)
        try:
            aios = ListingAios({"url": srv.url("tiny.mkv"), "streamData": {"torrent": {"infoHash": HASH, "fileIdx": 2}}},
                               [{"id": "os-1", "url": srv.url("tiny.srt"), "lang": "eng", "release": "", "hash": "",
                                 "hash_match": False, "ai": False}])
            app = self.app("eng", aios)
            size = os.path.getsize(os.path.join(FIXTURES, "tiny.mkv"))
            app.worker.process(Job("movie", "tt1", {"filename": "tiny.mkv", "videoSize": size}))
            file_key = self.store.resolve(f"bt:{HASH}:2")
            self.assertIsNotNone(file_key)
            self.assertTrue(self.store.judged_all(file_key, ["eng"], app.s.rule_id, 0))
        finally:
            srv.close()


class StubLimits:
    def __init__(self, queue=10, files=True, fail_read=False):
        self.queue, self.files, self.fail_read = queue, files, fail_read
        self.reads, self.received, self.started = [], [], 0

    def may_queue(self, queued):
        return queued < self.queue

    def may_start_file(self):
        return self.files

    def file_started(self):
        self.started += 1

    def before_read(self, length):
        from subsync.worker import OverLimit
        self.reads.append(length)
        if self.fail_read:
            raise OverLimit("bytes per day")

    def after_read(self, n):
        self.received.append(n)


def wait_until(check, timeout=3.0):
    import time
    t0 = time.time()
    while not check() and time.time() - t0 < timeout:
        time.sleep(0.02)
    return check()


class WorkerHooksTest(unittest.TestCase):
    def setUp(self):
        from helpers import TempDir
        from subsync.store import Store
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.store = Store(self.dir)
        self.workers = []

    def tearDown(self):
        for w in self.workers:
            w.stop()
        self.tmp.__exit__(None, None, None)

    def settings(self, **env):
        return Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC_BASE": "http://x.test"}, **env))

    def test_turn_is_first_come_first_served(self):
        import threading
        import time
        from subsync.worker import Turn
        turn, order, threads = Turn(), [], []
        with turn:
            for i in range(3):
                t = threading.Thread(target=lambda i=i: [turn.__enter__(), order.append(i), turn.__exit__(None, None, None)])
                t.start()
                threads.append(t)
                time.sleep(0.05)           # each asks after the one before
            self.assertEqual(order, [])    # nobody runs while the turn is held
        for t in threads:
            t.join(2)
        self.assertEqual(order, [0, 1, 2])

    def test_two_workers_sharing_a_turn_never_overlap(self):
        import threading
        import time
        from subsync.worker import Job, Safety, Turn, Worker
        turn, spans, lock = Turn(), [], threading.Lock()

        class Recording(Worker):
            def process(self, job):
                t0 = time.time()
                time.sleep(0.05)
                with lock:
                    spans.append((t0, time.time()))
        for n in range(2):
            w = Recording(self.settings(), self.store, ListingAios(), Safety(), turn=turn)
            self.workers.append(w)
            for i in range(2):
                w.submit(Job("movie", f"tt{n}{i}", {"filename": f"{n}{i}.mkv", "videoSize": 1}))
        for w in self.workers:
            w.start()
        self.assertTrue(wait_until(lambda: len(spans) == 4))
        spans.sort()
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            self.assertLessEqual(a1, b0)

    def test_a_full_queue_queues_nothing(self):
        from subsync.worker import Job, Safety, Worker
        w = Worker(self.settings(), self.store, ListingAios(), Safety(), limits=StubLimits(queue=1))
        first = Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1})
        self.assertIs(w.submit(first), first)
        self.assertIs(w.submit(Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1})), first)   # already queued
        self.assertIsNone(w.submit(Job("movie", "tt2", {"filename": "b.mkv", "videoSize": 1})))
        self.assertEqual(len(w.jobs), 1)

    def test_over_limit_drops_the_job(self):
        from subsync.worker import Job, OverLimit, Safety, Worker
        aios = ListingAios({"url": "http://127.0.0.1:9/never"},
                           [{"id": "os-1", "url": "http://127.0.0.1:9/s.srt", "lang": "eng", "release": "", "hash": "",
                             "hash_match": False, "ai": False}])
        limits = StubLimits(files=False)
        w = Worker(self.settings(), self.store, aios, Safety(), limits=limits)
        with self.assertRaises(OverLimit):
            w.process(Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1}))
        self.assertEqual(aios.subtitle_requests, 0)     # stopped before anything is asked
        self.workers.append(w)
        w.submit(Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1}))
        w.start()
        self.assertTrue(wait_until(lambda: not w.jobs and w.current is None))
        import time
        time.sleep(0.1)
        self.assertEqual(len(w.jobs), 0)                # not queued again for later
        self.assertFalse(self.store.file_blocked("name:a.mkv|1"))

    def test_guard_asks_the_limits(self):
        from subsync.worker import JobGuard, OverLimit, Safety
        limits = StubLimits()
        g = JobGuard(Safety(), self.store, 10 ** 9, limits=limits)
        g.before_read(5)
        g.after_read(7)
        self.assertEqual((limits.reads, limits.received), ([5], [7]))
        with self.assertRaises(OverLimit):
            JobGuard(Safety(), self.store, 10 ** 9, limits=StubLimits(fail_read=True)).before_read(5)

    def test_files_are_counted_when_their_read_starts(self):
        from helpers import FIXTURES, RangeServer
        from subsync.worker import Job, Safety, Worker
        srv = RangeServer(FIXTURES)
        try:
            aios = ListingAios({"url": srv.url("tiny.mkv")},
                               [{"id": "os-1", "url": srv.url("tiny.srt"), "lang": "eng", "release": "", "hash": "",
                                 "hash_match": False, "ai": False}])
            limits = StubLimits()
            w = Worker(self.settings(), self.store, aios, Safety(), limits=limits)
            size = os.path.getsize(os.path.join(FIXTURES, "tiny.mkv"))
            w.process(Job("movie", "tt1", {"filename": "tiny.mkv", "videoSize": size}))
            self.assertEqual(limits.started, 1)
            self.assertGreater(sum(limits.received), 0)
        finally:
            srv.close()

    def test_no_audio_lane_in_public_mode(self):
        import threading
        from subsync.worker import Safety, Worker
        w = Worker(self.settings(SUBSYNC_PUBLIC="on"), self.store, ListingAios(), Safety())
        self.workers.append(w)
        before = {t.name for t in threading.enumerate()}
        w.start()
        self.assertNotIn("subsync-audio", {t.name for t in threading.enumerate()} - before)

    def test_stop_ends_the_loop(self):
        from subsync.worker import Safety, Worker
        w = Worker(self.settings(SUBSYNC_PUBLIC="on"), self.store, ListingAios(), Safety())
        w.start()
        w.stop()
        w.join(2)
        self.assertFalse(w.is_alive())


try:
    import cryptography  # noqa: F401
    HAVE_CRYPTOGRAPHY = True
except ImportError:
    HAVE_CRYPTOGRAPHY = False


def record(link="https://aios.test/stremio/u1/E1/manifest.json", languages=("eng",)):
    return {"instance": "https://aios.test", "sync_uuid": "u1", "sync_link": link, "sync_password": "p1",
            "languages": list(languages), "version": 1}


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, "cryptography is not installed (the image has it; tests run it in a venv)")
class PoolTest(unittest.TestCase):
    def setUp(self):
        from helpers import TempDir
        from subsync.store import Store
        from subsync.users import Users, new_token
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.env = {"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                    "SUBSYNC_PUBLIC_BASE": "https://subs.test", "SUBSYNC_ACTIVE_WORKERS": "2",
                    "SUBSYNC_EXTRA_SOURCES": ""}
        self.settings = Settings(self.env)
        self.clock = Clock()
        self.store = Store(self.dir)
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.settings.secret_bytes(), clock=self.clock)
        self.tokens = [new_token() for _ in range(3)]
        self.users.create(self.tokens[0], record())
        self.users.create(self.tokens[1], record("https://aios.test/stremio/u2/E2/manifest.json", ("rus", "eng")))
        self.users.create(self.tokens[2], record())
        self.pool = self.new_pool()

    def new_pool(self):
        from subsync.public import Pool
        return Pool(self.settings, self.env, self.store, self.users, clock=self.clock)

    def tearDown(self):
        self.pool.close()
        self.users.close()
        self.tmp.__exit__(None, None, None)

    def test_unknown_token_has_no_app(self):
        from subsync.users import new_token
        self.assertIsNone(self.pool.app(new_token()))
        self.assertIsNone(self.pool.app("../etc/passwd"))
        self.assertEqual(self.pool.active(), 0)

    def test_each_user_gets_their_own_languages_and_link(self):
        a, b = self.pool.app(self.tokens[0]), self.pool.app(self.tokens[1])
        self.assertEqual(a.s.languages, ["eng"])
        self.assertEqual(b.s.languages, ["rus", "eng"])
        self.assertEqual(a.s.public_base, "https://subs.test/u/" + self.tokens[0])
        self.assertEqual(a.worker.aios.base, "https://aios.test/stremio/u1/E1")
        self.assertEqual(b.worker.aios.base, "https://aios.test/stremio/u2/E2")
        self.assertEqual(a.worker.aios.password, "p1")
        self.assertIs(a.store, b.store)
        self.assertFalse(a.s.audio_route)
        self.assertIs(self.pool.app(self.tokens[0]), a)          # the same person keeps their app
        self.assertEqual(self.pool.active(), 2)

    def test_workers_share_one_turn(self):
        a, b = self.pool.app(self.tokens[0]), self.pool.app(self.tokens[1])
        self.assertIs(a.worker.turn, b.worker.turn)
        self.assertIsNotNone(a.worker.limits)

    def test_idle_workers_stop_and_restart(self):
        a = self.pool.app(self.tokens[0])
        self.clock.t += self.settings.worker_idle_s - 1
        self.assertEqual(self.pool.reap(), 0)
        self.clock.t += 2
        self.assertEqual(self.pool.reap(), 1)
        a.worker.join(2)
        self.assertFalse(a.worker.is_alive())
        self.assertEqual(self.pool.active(), 0)
        again = self.pool.app(self.tokens[0])
        self.assertIsNot(again, a)
        self.assertTrue(again.worker.is_alive())

    def test_a_busy_worker_is_not_reaped(self):
        from subsync.worker import Job
        a = self.pool.app(self.tokens[0])
        a.worker.jobs["x"] = Job("movie", "tt1", {"filename": "x.mkv"})   # something waiting
        self.clock.t += self.settings.worker_idle_s + 10
        self.assertEqual(self.pool.reap(), 0)
        a.worker.jobs.clear()

    def test_past_the_active_cap_answers_from_the_store(self):
        from subsync.worker import Job
        self.pool.app(self.tokens[0])
        self.pool.app(self.tokens[1])
        third = self.pool.app(self.tokens[2])
        self.assertIsNotNone(third)
        self.assertEqual(self.pool.active(), 2)
        self.assertFalse(third.worker.is_alive())
        self.assertIsNone(third.worker.submit(Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1})))

    def test_limits_per_user(self):
        from subsync.public import UserLimits
        from subsync.worker import OverLimit
        limits = UserLimits(self.settings, self.users, self.tokens[0])
        for _ in range(40):
            self.assertTrue(limits.may_start_file())
            limits.file_started()
        self.assertFalse(limits.may_start_file())                 # the 41st file of the day
        limits.after_read(500 * 1024 * 1024 - 10)
        limits.before_read(10)
        with self.assertRaises(OverLimit):
            limits.before_read(11)                                 # past 500 MB
        self.assertTrue(limits.may_queue(2))
        self.assertFalse(limits.may_queue(3))
        other = UserLimits(self.settings, self.users, self.tokens[1])
        self.assertTrue(other.may_start_file())                   # someone else is not affected
        self.clock.t += 86400
        self.assertTrue(limits.may_start_file())                  # the next UTC day
        limits.before_read(11)

    def test_records_survive_a_restart(self):
        from subsync.users import Users
        self.pool.close()
        self.users.close()
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.settings.secret_bytes(), clock=self.clock)
        self.pool = self.new_pool()
        self.assertEqual(self.pool.app(self.tokens[1]).s.languages, ["rus", "eng"])

    def test_refresh_restarts_the_worker(self):
        a = self.pool.app(self.tokens[0])
        self.users.update(self.tokens[0], record("https://aios.test/stremio/u9/E9/manifest.json"))
        self.pool.stop(self.tokens[0])
        a.worker.join(2)
        self.assertFalse(a.worker.is_alive())
        self.assertEqual(self.pool.app(self.tokens[0]).worker.aios.base, "https://aios.test/stremio/u9/E9")

    def test_housekeeping_deletes_long_unused_records(self):
        self.clock.t += 181 * 86400
        self.pool.housekeeping()
        self.assertIsNone(self.users.open(self.tokens[0]))


class StartupTest(unittest.TestCase):
    def run_main(self, env):
        from unittest import mock
        from subsync import __main__ as entry
        with mock.patch.dict(os.environ, env, clear=True), self.assertLogs("subsync", level="ERROR") as logs:
            with self.assertRaises(SystemExit) as e:
                entry.main()
        self.assertEqual(e.exception.code, 2)
        return "\n".join(logs.output)

    def test_main_refuses_public_mode_without_a_secret(self):
        out = self.run_main({"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://x.test"})
        self.assertIn("SUBSYNC_SECRET_KEY", out)

    def test_main_refuses_public_mode_with_the_audio_route(self):
        out = self.run_main({"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://x.test", "SUBSYNC_SECRET_KEY": KEY,
                             "SUBSYNC_AUDIO_ROUTE": "on"})
        self.assertIn("audio route", out)
        self.assertNotIn(KEY, out)

    def test_requirements_pin_both_platforms(self):
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "requirements.txt")
        if not os.path.exists(path):
            self.skipTest("requirements.txt is only in the source folder (the image installs from it with --require-hashes)")
        text = open(path).read()
        for name, version, wheels in (("cryptography", "50.0.2", 2), ("cffi", "2.1.1", 2), ("pycparser", "3.12", 1)):
            with self.subTest(name=name):
                m = re.search(rf"^{name}=={re.escape(version)}((?:\s*\\\s*--hash=sha256:[0-9a-f]{{64}})+)", text, re.M)
                self.assertIsNotNone(m, name)
                self.assertEqual(len(re.findall("sha256:", m.group(1))), wheels)


class PublicTextsTest(unittest.TestCase):
    def test_each_field_has_one_short_plain_explanation(self):
        import re
        from subsync import explain
        note = re.compile(r"\b(?:AD\d{1,3}|S4-\d+|P-S4-[A-Z0-9-]+)\b")
        for name, text in list(explain.PUBLIC_FIELDS.items()) + list(explain.PUBLIC_TEXTS.items()):
            with self.subTest(name=name):
                self.assertNotIn("\n", text)
                self.assertTrue(text.endswith((".", ")")), text[-30:])
                self.assertIsNone(note.search(text))
                if name in explain.PUBLIC_FIELDS:
                    self.assertLessEqual(len(text.split()), 125)

    def test_every_page_field_has_its_explanation(self):
        import re
        from subsync import explain, pages
        source = open(pages.__file__).read()
        used = set(re.findall(r'_(?:field|check)\("(\w+)"', source)) | set(re.findall(r'help="(\w+)"', source))
        used.discard("password")   # its explanation is named by help=
        self.assertEqual(used - set(explain.PUBLIC_FIELDS), set())

    def test_public_rules_are_listed(self):
        from subsync import explain
        names = [name for name, _ in explain.RULES]
        for rule in ("Public mode: your AIOStreams password is used once", "Public mode: Real-Debrid is left out",
                     "Public mode: one file at a time", "Public mode: limits per person",
                     "Public mode: what is kept, and for how long"):
            with self.subTest(rule=rule):
                self.assertIn(rule, names)

    def test_the_settings_page_shows_small_reads_with_one_decimal(self):
        # A subtitle-track sync reads a few hundred KB: "0 MB read" after a sync would look like nothing was read.
        from subsync import pages
        s = Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY, "SUBSYNC_PUBLIC_BASE": "https://x.test"})
        record = {"languages": ["eng"]}
        for nbytes, shown in ((0, " 0 MB of 500 MB read"), (20 * 1024, " 0.1 MB of"), (351 * 1024, " 0.3 MB of"),
                              (5 * 1024 * 1024, " 5.0 MB of"), (12 * 1024 * 1024, " 12 MB of")):
            with self.subTest(nbytes=nbytes):
                html = pages.settings_page(s, record, {"files": 1, "bytes": nbytes}, "https://x.test/u/t/manifest.json")
                self.assertIn(shown, html)

    def test_the_privacy_text_names_cloudflare_and_the_backups_before_how_long(self):
        from subsync import explain
        text = explain.PUBLIC_TEXTS["privacy"]
        added = ("AIOSubtitles runs behind Cloudflare, which passes every request on and so sees your internet address "
                 "and the links you open, your personal link included. AIOSubtitles itself keeps no log of requests. "
                 "Backups of the stored data are kept for up to 30 days.")
        self.assertIn("Logs name the titles synced, never who asked. " + added + " How long:", text)


if __name__ == "__main__":
    unittest.main()
