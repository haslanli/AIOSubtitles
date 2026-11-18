"""Public mode: its settings, the pool of users' workers, the shared store's memory per language, and the worker's turn
and limits."""
import base64
import re
import os
import unittest

from helpers import Clock, closed_after, HAVE_CRYPTOGRAPHY, NEEDS, wait_until  # noqa: E402
from subsync.settings import Settings, available_cpus  # noqa: E402

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


class PublicSettingsTest(unittest.TestCase):
    def test_defaults(self):
        s = Settings({})
        self.assertFalse(s.public)
        self.assertEqual(s.secret_key, "")
        self.assertEqual(s.invite, "")
        self.assertEqual(s.max_users, 200)
        self.assertEqual(s.signups_per_hour, 5)
        self.assertEqual(s.user_files_per_day, 200)
        self.assertEqual(s.user_daily_bytes, 10000 * 1024 * 1024)
        self.assertEqual(s.user_queue, 3)
        self.assertEqual(s.active_workers, 100)
        self.assertEqual(s.worker_idle_s, 1800)
        self.assertEqual(s.user_keep_days, 180)
        self.assertEqual(s.trusted_proxy, ("127.0.0.1",))
        self.assertEqual(s.max_connections, 256)
        self.assertEqual(s.syncs_at_once, available_cpus())   # auto

    def test_trusted_proxy_is_a_list(self):
        self.assertEqual(Settings({"SUBSYNC_TRUSTED_PROXY": "127.0.0.1, ::1"}).trusted_proxy, ("127.0.0.1", "::1"))

    def route_file(self, text):
        from helpers import TempDir
        tmp = TempDir()
        folder = tmp.__enter__()
        self.addCleanup(tmp.__exit__, None, None, None)
        path = os.path.join(folder, "route")
        with open(path, "w") as f:
            f.write(text)
        return path

    ROUTES = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
              "eth0\t0000FEA9\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
              "eth0\t00000000\t013D1EAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
              "eth0\t003D1EAC\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0\n")

    def test_trusted_proxy_gateway_is_found_at_start(self):
        """`gateway`: the address the container's own network reaches it from (its default gateway), so a web server
        reaching subsync through a port Docker publishes is trusted without a fixed network."""
        from unittest import mock
        from subsync import settings as settings_module
        with mock.patch.object(settings_module, "ROUTE_FILE", self.route_file(self.ROUTES)):
            self.assertEqual(Settings({"SUBSYNC_TRUSTED_PROXY": "gateway"}).trusted_proxy, ("172.30.61.1",))
            both = Settings({"SUBSYNC_TRUSTED_PROXY": "gateway, 127.0.0.1", "SUBSYNC_PUBLIC": "on"})
            self.assertEqual(both.trusted_proxy, ("172.30.61.1", "127.0.0.1"))
            self.assertEqual(both.conflicts(), [])

    def test_trusted_proxy_gateway_without_a_default_route_stops_public_mode(self):
        from unittest import mock
        from subsync import settings as settings_module
        no_default = self.ROUTES.splitlines()[0] + "\n" + self.ROUTES.splitlines()[1] + "\n"
        for path in (self.route_file(no_default), "/nonexistent/route"):
            with self.subTest(path=path), mock.patch.object(settings_module, "ROUTE_FILE", path):
                s = Settings({"SUBSYNC_TRUSTED_PROXY": "gateway", "SUBSYNC_PUBLIC": "on"})
                self.assertEqual(s.trusted_proxy, ())
                problems = s.conflicts()
                self.assertEqual(len(problems), 1)
                self.assertIn("SUBSYNC_TRUSTED_PROXY", problems[0])
                self.assertEqual(Settings({"SUBSYNC_TRUSTED_PROXY": "gateway"}).conflicts(), [])   # self-hosted: unused

    def test_syncs_at_once(self):
        from unittest import mock
        from subsync import settings as settings_module
        self.assertEqual(Settings({"SUBSYNC_SYNCS_AT_ONCE": "2"}).syncs_at_once, 2)
        with mock.patch.object(settings_module, "available_cpus", return_value=3):
            self.assertEqual(Settings({"SUBSYNC_SYNCS_AT_ONCE": "auto"}).syncs_at_once, 3)
        for wrong in ("0", "-1", "two", "1.5", ""):
            with self.subTest(value=wrong):
                s = Settings({"SUBSYNC_SYNCS_AT_ONCE": wrong, "SUBSYNC_PUBLIC": "on"})
                self.assertIsNone(s.syncs_at_once)
                self.assertTrue(any("SUBSYNC_SYNCS_AT_ONCE" in p for p in s.conflicts()))

    def test_a_number_int_cannot_read_is_a_conflict_not_a_crash(self):
        # Isdigit takes superscripts ("2²") that int refuses, and int reads no more than 4,300 digits;
        # either was a traceback at start instead of the setting's sentence
        for name, field in (("SUBSYNC_SYNCS_AT_ONCE", "syncs_at_once"), ("SUBSYNC_AUDIO_AT_ONCE", "audio_at_once"),
                            ("SUBSYNC_MAX_LANGUAGES", "max_languages")):
            for wrong in ("²", "2²", "1" * 5000):
                with self.subTest(name=name, value=wrong[:8]):
                    s = Settings({name: wrong, "SUBSYNC_PUBLIC": "on"})
                    self.assertIsNone(getattr(s, field))
                    self.assertTrue(any(p.startswith(name + " must be a whole number") for p in s.conflicts()))
        self.assertEqual(Settings({"SUBSYNC_MAX_LANGUAGES": "٣"}).max_languages, 3)   # a decimal digit int() reads

    def test_available_cpus_follows_the_containers_limit(self):
        from subsync import settings as settings_module
        cpus = settings_module.available_cpus
        self.assertEqual(cpus(cpu_max=self.route_file("max 100000\n"), processors=lambda: 2), 2)
        self.assertEqual(cpus(cpu_max=self.route_file("150000 100000\n"), processors=lambda: 4), 2)
        self.assertEqual(cpus(cpu_max=self.route_file("50000 100000\n"), processors=lambda: 4), 1)
        self.assertEqual(cpus(cpu_max="/nonexistent/cpu.max", cfs_dir="/nonexistent", processors=lambda: 6), 6)
        self.assertEqual(cpus(cpu_max="/nonexistent/cpu.max", cfs_dir="/nonexistent", processors=lambda: None), 1)

    def test_available_cpus_follows_a_cgroup_v1_limit(self):
        # A cgroup v1 host has no cpu.max; its limit is in cpu.cfs_quota_us and cpu.cfs_period_us
        from unittest import mock
        from subsync import settings as settings_module

        def cfs(quota, period="100000\n"):
            folder = os.path.dirname(self.route_file(quota))
            os.replace(os.path.join(folder, "route"), os.path.join(folder, "cpu.cfs_quota_us"))
            with open(os.path.join(folder, "cpu.cfs_period_us"), "w") as f:
                f.write(period)
            return folder
        cpus = settings_module.available_cpus
        self.assertEqual(cpus(cpu_max="/nonexistent/cpu.max", cfs_dir=cfs("150000\n"), processors=lambda: 8), 2)
        self.assertEqual(cpus(cpu_max="/nonexistent/cpu.max", cfs_dir=cfs("50000\n"), processors=lambda: 8), 1)
        self.assertEqual(cpus(cpu_max="/nonexistent/cpu.max", cfs_dir=cfs("-1\n"), processors=lambda: 8), 8)   # none
        self.assertEqual(cpus(cpu_max="/nonexistent/cpu.max", cfs_dir=cfs("garbled\n"), processors=lambda: 8), 8)
        # cgroup v2's file, when there is one, is the limit
        self.assertEqual(cpus(cpu_max=self.route_file("max 100000\n"), cfs_dir=cfs("150000\n"), processors=lambda: 8),
                         8)
        with mock.patch.object(settings_module, "CPU_MAX_FILE", "/nonexistent/cpu.max"), \
                mock.patch.object(settings_module, "CFS_DIR", cfs("300000\n")):
            self.assertEqual(Settings({"SUBSYNC_SYNCS_AT_ONCE": "auto"}).syncs_at_once,
                             min(3, os.process_cpu_count() or 1))

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

    def test_the_audio_route_in_public_mode_as_each_person_chooses(self):
        # public mode kept the audio route to a pick; each person's own choice now replaces the
        # instance's, so the old setting no longer stops a public instance (test_audio_choice has the rest)
        self.assertEqual(Settings({"SUBSYNC_PUBLIC": "on"}).conflicts(), [])
        self.assertEqual(Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_AUDIO_ROUTE": "on"}).conflicts(), [])
        self.assertEqual(Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_AUDIO_ROUTE": "on",
                                   "SUBSYNC_AUDIO_ON_PICK": "off"}).conflicts(), [])
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
        self.store = closed_after(self, Store(self.dir))

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
        self.assertEqual(self.store.day_count("read"), 0)

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
    who = None

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

    def has_room(self, n):
        return True

    def after_read(self, n):
        self.received.append(n)

    def may_start_audio(self, opened=False):
        return True

    def audio_started(self, opened=False):
        pass


def held(turn):
    """A Turn's (tickets handed out, holders inside), read under its lock: a thread that asked has its ticket, and is
    either inside or waiting."""
    with turn._cv:
        return turn._next, turn._inside


class WorkerHooksTest(unittest.TestCase):
    def setUp(self):
        from helpers import TempDir
        from subsync.store import Store
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.store = closed_after(self, Store(self.dir))
        self.workers = []

    def tearDown(self):
        for w in self.workers:
            w.stop()
        self.tmp.__exit__(None, None, None)

    def settings(self, **env):
        return Settings(dict({"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC_BASE": "http://x.test"}, **env))

    def test_turn_is_first_come_first_served(self):
        import threading
        from subsync.worker import Turn
        turn, order, threads = Turn(), [], []
        with turn:
            for i in range(3):
                t = threading.Thread(target=lambda i=i: [turn.__enter__(), order.append(i), turn.__exit__(None, None, None)])
                t.start()
                threads.append(t)
                self.assertTrue(wait_until(lambda: held(turn)[0] == i + 2))   # it has asked: the next asks after it
            self.assertEqual(held(turn), (4, 1))   # nobody runs while the turn is held
            self.assertEqual(order, [])
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
        self.assertTrue(wait_until(lambda: len(spans) == 4, 3.0))
        spans.sort()
        for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
            self.assertLessEqual(a1, b0)

    def test_a_turn_with_two_places_lets_two_in_and_keeps_the_order(self):
        import threading
        from subsync.worker import Turn
        turn, order, threads, release = Turn(2), [], [], threading.Event()
        self.assertEqual(turn.places, 2)

        def hold(i):
            with turn:
                order.append(i)
                release.wait(2)
        for i in range(4):
            t = threading.Thread(target=hold, args=(i,))
            t.start()
            threads.append(t)
            self.assertTrue(wait_until(lambda: held(turn)[0] == i + 1))   # it has asked: the next asks after it
        self.assertEqual(held(turn), (4, 2))     # two places: the third and fourth wait
        self.assertTrue(wait_until(lambda: len(order) == 2))
        self.assertEqual(order, [0, 1])
        release.set()
        for t in threads:
            t.join(2)
        self.assertEqual(order, [0, 1, 2, 3])    # in the order they asked
        self.assertEqual(Turn(0).places, 1)      # never less than one place

    def test_a_claim_keeps_one_file_to_one_worker(self):
        import threading
        import time
        from subsync.worker import Turn
        turn, spans, lock = Turn(2), {}, threading.Lock()

        def work(key, i):
            with turn.claim(key):
                t0 = time.time()
                time.sleep(0.1)
                with lock:
                    spans.setdefault(key, []).append((t0, time.time()))
        threads = [threading.Thread(target=work, args=(key, i)) for i, key in enumerate(("a", "a", "b"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(2)
        (a0, a1), (b0, b1) = sorted(spans["a"])
        self.assertLessEqual(a1, b0)             # the same file one after the other
        self.assertEqual(turn._claims, set())    # nothing left claimed

    def test_two_places_work_on_two_files_at_once_but_never_the_same_file(self):
        import threading
        import time
        from subsync.worker import Job, Safety, Turn, Worker
        turn, spans, lock = Turn(2), [], threading.Lock()

        class Recording(Worker):
            def process(self, job):
                t0 = time.time()
                time.sleep(0.15)
                with lock:
                    spans.append((job.alias, t0, time.time()))
        jobs = (("tt1", "same.mkv"), ("tt2", "other.mkv"), ("tt1", "same.mkv"))
        for video_id, name in jobs:      # three people: two play the same file
            w = Recording(self.settings(), self.store, ListingAios(), Safety(), turn=turn)
            self.workers.append(w)
            w.submit(Job("movie", video_id, {"filename": name, "videoSize": 1}))
        for w in self.workers:
            w.start()
        self.assertTrue(wait_until(lambda: len(spans) == 3, 3.0))
        same = sorted((t0, t1) for alias, t0, t1 in spans if "same" in alias)
        self.assertLessEqual(same[0][1], same[1][0])
        other = [(t0, t1) for alias, t0, t1 in spans if "other" in alias][0]
        self.assertTrue(other[0] < same[0][1] and same[0][0] < other[1])   # two different files overlapped

    def test_two_people_on_one_torrent_file_read_it_once(self):
        """Different file names, the same torrent file: the second waits for the first and then reads nothing."""
        from helpers import FIXTURES, RangeServer, TempDir
        from subsync.store import Store
        from subsync.worker import Job, Safety, Turn, Worker
        size = os.path.getsize(os.path.join(FIXTURES, "tiny.mkv"))

        def run(names):
            """The requests the file server saw and the bytes read, with one person per file name."""
            srv, tmp = RangeServer(FIXTURES), TempDir()
            folder = tmp.__enter__()
            workers = []
            try:
                store = closed_after(self, Store(folder))
                row = {"url": srv.url("tiny.mkv"), "streamData": {"torrent": {"infoHash": HASH, "fileIdx": 2}}}
                subs = [{"id": "os-1", "url": srv.url("tiny.srt"), "lang": "eng", "release": "", "hash": "",
                         "hash_match": False, "ai": False}]
                turn = Turn(2)
                settings = Settings({"SUBSYNC_DATA": folder, "SUBSYNC_PUBLIC_BASE": "http://x.test",
                                     "SUBSYNC_LANGUAGES": "eng"})
                for name in names:
                    w = Worker(settings, store, ListingAios(row, subs), Safety(), turn=turn)
                    workers.append(w)
                    w.submit(Job("movie", "tt1", {"filename": name, "videoSize": size}))
                for w in workers:
                    w.start()
                self.assertTrue(wait_until(lambda: all(not w.jobs and w.current is None for w in workers), 20))
                self.assertIsNotNone(store.resolve(f"bt:{HASH}:2"))
                return len(srv.log), store.day_count("read")
            finally:
                for w in workers:
                    w.stop()
                srv.close()
                tmp.__exit__(None, None, None)
        one = run(["tiny.mkv"])
        self.assertGreater(one[1], 0)
        self.assertEqual(run(["tiny.mkv", "tiny-renamed.mkv"]), one)

    def test_a_wait_for_the_same_torrent_file_holds_no_place(self):
        """A person whose file another person works on under another name (the same torrent file) waits for
        it with their place given back, so a third person's file is worked on meanwhile (Turn's rule)."""
        import threading
        import time
        from subsync.worker import Job, Safety, Turn, Worker
        turn, spans, lock = Turn(2), {}, threading.Lock()
        torrent = {"url": "http://127.0.0.1:9/never", "streamData": {"torrent": {"infoHash": HASH, "fileIdx": 2}}}

        def process_row(job, hints, row, bt):
            t0 = time.time()
            time.sleep(0.6)
            with lock:
                spans[job.alias] = (t0, time.time())
        for name, row in (("same.mkv", torrent), ("same-renamed.mkv", torrent),
                          ("other.mkv", {"url": "http://127.0.0.1:9/other"})):
            w = Worker(self.settings(), self.store, ListingAios(row), Safety(), turn=turn)
            w._process_row = process_row
            self.workers.append(w)
            w.submit(Job("movie", "tt1", {"filename": name, "videoSize": 1}))
            w.start()
            time.sleep(0.1)                                  # each asks after the one before
        self.assertTrue(wait_until(lambda: len(spans) == 3, 3.0))
        first, second, other = (spans[f"name:{name}|1"] for name in ("same.mkv", "same-renamed.mkv", "other.mkv"))
        self.assertLessEqual(first[1], second[0])            # the same torrent file one after the other
        self.assertLess(other[0], first[1])                  # the other file had a place while the second waited
        self.assertEqual((turn._inside, turn._claims), (0, set()))

    def test_one_persons_refused_read_backs_the_file_off_for_them_only(self):
        """A refused read (HTTP 403/429) is most likely the person's own account's limit: the file backs off
        for that person, never for another person who plays it."""
        from subsync.reader import Refused
        from subsync.worker import Job, Safety, Worker
        row = {"url": "http://127.0.0.1:9/never"}

        def person(who):
            limits = StubLimits()
            limits.who = who
            w = Worker(self.settings(SUBSYNC_PUBLIC="on"), self.store, ListingAios(row), Safety(), limits=limits)
            self.workers.append(w)
            return w
        a, b = person("a" * 16), person("b" * 16)
        job = Job("movie", "tt1", {"filename": "a.mkv", "videoSize": 1})

        def refused(j):
            a.stop_flag = True                               # one turn of the loop
            raise Refused("HTTP 429")
        a.process = refused
        a.submit(job)
        a._loop("main")
        self.assertTrue(a._file_blocked(job.alias))          # 1 h, then 6 h, 24 h, for this person
        self.assertFalse(self.store.file_blocked(job.alias))
        b.process(Job("movie", "tt1", dict(job.hints)))
        self.assertEqual(b.aios.subtitle_requests, 1)        # not backing off: the other person's job goes on

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
        self.assertTrue(wait_until(lambda: not w.jobs and w.current is None, 3.0))
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

    def test_no_audio_lane_in_public_mode_while_the_audio_route_is_off(self):
        # With the route on, public mode has an audio lane (test_public_audio.LaneTest has both)
        import threading
        from subsync.worker import Safety, Worker
        w = Worker(self.settings(SUBSYNC_PUBLIC="on", SUBSYNC_AUDIO_ROUTE="off"), self.store, ListingAios(), Safety())
        self.workers.append(w)
        before = {t.name for t in threading.enumerate()}
        w.start()
        self.assertNotIn("subsync-audio", {t.name for t in threading.enumerate()} - before)
        self.assertIsNone(getattr(w, "audio_worker", None))   # the worker's own: no leftover thread's name hides it

    def test_stop_ends_the_loop(self):
        from subsync.worker import Safety, Worker
        w = Worker(self.settings(SUBSYNC_PUBLIC="on"), self.store, ListingAios(), Safety())
        w.start()
        w.stop()
        w.join(2)
        self.assertFalse(w.is_alive())


def record(link="https://aios.test/stremio/u1/E1/manifest.json", languages=("eng",)):
    return {"instance": "https://aios.test", "sync_uuid": "u1", "sync_link": link, "sync_password": "p1",
            "languages": list(languages), "version": 1}


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class PoolTest(unittest.TestCase):
    def setUp(self):
        from helpers import TempDir
        from subsync.store import Store
        from subsync.users import Users, new_token
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.env = {"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                    "SUBSYNC_PUBLIC_BASE": "https://subs.test", "SUBSYNC_ACTIVE_WORKERS": "2",
                    "SUBSYNC_EXTRA_SOURCES": "", "SUBSYNC_ORIGINAL_LANGUAGE": "off"}
        self.settings = Settings(self.env)
        self.clock = Clock()
        self.store = closed_after(self, Store(self.dir))
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
        self.assertEqual(a.worker.turn.places, self.settings.syncs_at_once)
        self.assertIsNotNone(a.worker.limits)

    def test_the_turn_has_as_many_places_as_files_at_once(self):
        from subsync.public import Pool
        env = dict(self.env, SUBSYNC_SYNCS_AT_ONCE="2")
        pool = Pool(Settings(env), env, self.store, self.users, clock=self.clock)
        try:
            self.assertEqual(pool.turn.places, 2)
            self.assertIs(pool.app(self.tokens[0]).worker.turn, pool.turn)
        finally:
            pool.close()

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
        for _ in range(200):
            self.assertTrue(limits.may_start_file())
            limits.file_started()
        self.assertFalse(limits.may_start_file())                 # the 201st file of the day
        limits.after_read(10000 * 1024 * 1024 - 10)
        limits.before_read(10)
        with self.assertRaises(OverLimit):
            limits.before_read(11)                                 # past 10,000 MB
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

    def test_a_new_app_parses_the_environment_once(self):
        # Twice more, for the instance's own settings, which the Pool has
        from unittest import mock
        made, real = [], Settings.__init__

        def counted(settings, *a, **k):
            made.append(settings)
            real(settings, *a, **k)
        with mock.patch.object(Settings, "__init__", counted):
            app = self.pool.app(self.tokens[1])
        self.assertEqual(made, [app.s])
        self.assertEqual(app.s.public_base, "https://subs.test/u/" + self.tokens[1])
        self.assertEqual(app.s.languages, ["rus", "eng"])


SRT_EN = ("1\n00:00:01,000 --> 00:00:02,000\nHello there my friend\n\n2\n00:00:03,000 --> 00:00:04,000\nGeneral Kenobi\n\n"
          "3\n00:00:05,000 --> 00:00:06,000\nYou are a bold one\n")
SYNCED_EN = SRT_EN.replace("00:00:01,000 --> 00:00:02,000", "00:00:01,500 --> 00:00:02,500")
SRT_FR = ("1\n00:00:01,000 --> 00:00:02,000\nBonjour mon ami\n\n2\n00:00:03,000 --> 00:00:04,000\nGénéral Kenobi\n\n"
          "3\n00:00:05,000 --> 00:00:06,000\nVous êtes bien audacieux\n")
FILE_KEY, ALIAS = "0123456789abcdef-1000", "hash:0123456789abcdef|1000"
FILE = "videoHash=0123456789abcdef&videoSize=1000"


class Listing:
    """The built-in sources' stand-in: every file's list has one English and one French subtitle, served on this
    machine."""
    ledger = None

    def __init__(self, base):
        self.base = base

    def search(self, type_, video_id, langs=None, hints=None, skip=None):
        return [{"id": "os-en", "url": f"{self.base}/en.srt", "lang": "eng", "source": "stub"},
                {"id": "os-fr", "url": f"{self.base}/fr.srt", "lang": "fre", "source": "stub"}]

    def headers_for(self, cand):
        return None


@unittest.skipUnless(HAVE_CRYPTOGRAPHY, NEEDS)
class KeptAppTest(unittest.TestCase):
    """A person's App is kept between their requests, a worker or not, so the entries of a list download (public mode
    rebuilds a copy, or gets an original, from the person's own list); their next App starts from what the last
    one had: the files they asked about and their refusal back-off."""

    def setUp(self):
        import functools
        import http.server
        from helpers import start_server, stop_server, TempDir
        from subsync.public import Pool
        from subsync.store import Store
        from subsync.users import Users
        self.tmp = TempDir()
        self.dir = self.tmp.__enter__()
        self.addCleanup(self.tmp.__exit__, None, None, None)
        subs = os.path.join(self.dir, "subs")
        os.makedirs(subs)
        for name, text in (("en.srt", SRT_EN), ("fr.srt", SRT_FR)):
            with open(os.path.join(subs, name), "w", encoding="utf-8") as f:
                f.write(text)

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        server = start_server(http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                              functools.partial(Quiet, directory=subs)))
        self.addCleanup(stop_server, server)
        self.env = {"SUBSYNC_DATA": self.dir, "SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY,
                    "SUBSYNC_PUBLIC_BASE": "https://subs.test", "SUBSYNC_ACTIVE_WORKERS": "1",
                    "SUBSYNC_EXTRA_SOURCES": "", "SUBSYNC_ORIGINAL_LANGUAGE": "off"}
        self.settings = Settings(self.env)
        self.clock = Clock()
        # as public mode keeps it (__main__.public_store)
        self.store = closed_after(self, Store(self.dir, keep_texts=False))
        self.users = Users(os.path.join(self.dir, "users.sqlite3"), self.settings.secret_bytes(), clock=self.clock)
        self.addCleanup(self.users.close)
        self.pool = Pool(self.settings, self.env, self.store, self.users, clock=self.clock, start_workers=False)
        self.addCleanup(self.pool.close)
        self.pool.extra = Listing(f"http://127.0.0.1:{server.server_address[1]}")
        import hashlib
        self.key = self.store.put(FILE_KEY, hashlib.sha256(SRT_EN.encode()).hexdigest(), "eng", 0, "synced",
                                  body=SYNCED_EN, fmt="srt", candidate_id="os-en", original=SRT_EN)
        self.store.alias(ALIAS, FILE_KEY)

    def person(self, languages=("eng",), **fields):
        """A signed-up person with nothing to read through (their list is the built-in sources' alone)."""
        from subsync.users import new_token
        token = new_token()
        self.users.create(token, dict({"instance": "https://aios.test", "main_uuid": "0" * 8 + "-0000-0000-0000-" + "0" * 12,
                                       "languages": list(languages), "version": 1}, **fields))
        return token

    def listed(self, app):
        return [e["label"] for e in app.subtitles("movie", "tt1", FILE)]

    def test_stored_copies_only_download_what_they_were_listed(self):
        # Their App was made again for each request, so the download (the player's next request) reached one
        # that knew nothing of the file: 404
        token = self.person(store_only=True)
        app = self.pool.app(token)
        self.assertEqual(self.listed(app), ["English (synced)"])
        again = self.pool.app(token)
        self.assertIs(again, app)
        self.assertEqual(again.text_of(self.key), SYNCED_EN)
        self.assertEqual(self.pool.active(), 0)                   # still no worker of their own

    def test_past_the_cap_their_original_downloads(self):
        # An "(original)" entry's download got the "No French subtitle was found" note
        import time
        self.pool.app(self.person())                              # the only worker
        token = self.person(languages=("fre",))
        self.store.kept_original_put(FILE_KEY, "fre", "os-fr", 0.9)   # someone else's judging kept it
        app = self.pool.app(token)
        self.assertEqual(self.listed(app), ["French (original)"])
        again = self.pool.app(token)
        self.assertIs(again, app)
        text, why, _ = again.worker.original_for(ALIAS, "fre", time.time() + 10, "os-fr")
        self.assertEqual((text, why), (SRT_FR, None))
        self.assertEqual(self.pool.active(), 1)

    def test_past_the_cap_a_worker_once_one_is_free_with_what_it_knew(self):
        first = self.person()
        self.pool.app(first)
        token = self.person()
        app = self.pool.app(token)
        self.assertIsNone(app.worker.who)                         # answered from the store only
        self.assertEqual(self.listed(app), ["English (synced)"])
        self.pool.stop(first)
        again = self.pool.app(token)
        self.assertIsNot(again, app)
        self.assertIsNotNone(again.worker.who)                    # their own worker now
        self.assertEqual(self.pool.active(), 1)
        self.assertEqual(again.text_of(self.key), SYNCED_EN)

    def test_the_next_app_still_downloads_what_was_listed(self):
        # An App stopped as idle (or by a settings save, the daily check, a restart of theirs) took the files
        # it knew with it, while players keep the list an hour
        from subsync.public import SEEN_KEPT_S
        token = self.person()
        app = self.pool.app(token)
        self.assertEqual(self.listed(app), ["English (synced)"])
        self.clock.t += self.settings.worker_idle_s + 1
        self.assertEqual(self.pool.reap(), 1)
        again = self.pool.app(token)
        self.assertIsNot(again, app)
        self.assertEqual(again.text_of(self.key), SYNCED_EN)
        self.pool.stop(token)                                     # a settings save
        self.assertEqual(self.pool.app(token).text_of(self.key), SYNCED_EN)
        self.pool.stop(token)
        self.clock.t += SEEN_KEPT_S + 1                           # not for longer than a player keeps a list
        self.assertIsNone(self.pool.app(token).text_of(self.key))
        app = self.pool.app(token)
        self.listed(app)
        self.pool.stop(token, forget=True)                        # a delete: nothing of theirs is kept
        self.assertIsNone(self.pool.app(token).text_of(self.key))

    def test_the_refusal_back_off_outlives_a_stopped_app(self):
        # A new App started it again at 15 minutes, so it never doubled past the first steps
        token = self.person()
        app = self.pool.app(token)
        self.assertEqual(app.safety.refused(), 900)
        self.assertEqual(app.safety.refused(), 1800)              # doubling with each refusal in a row
        self.clock.t += self.settings.worker_idle_s + 1
        self.assertEqual(self.pool.reap(), 1)
        again = self.pool.app(token)
        self.assertIs(again.safety, app.safety)
        self.assertEqual(again.safety.refused(), 3600)
        self.pool.stop(token)                                     # a settings save keeps it
        self.assertIs(self.pool.app(token).safety, app.safety)
        self.pool.stop(token, reset=True)                         # a refresh: they may have changed their debrid key
        fresh = self.pool.app(token).safety
        self.assertIsNot(fresh, app.safety)
        self.assertEqual((fresh.refusals, fresh.backing_off()), (0, False))
        fresh.refused()
        fresh.read_ok()                                           # the run ended: nothing to keep once it stops
        self.clock.t += self.settings.worker_idle_s + 1
        self.assertEqual(self.pool.reap(), 1)
        self.assertNotIn(self.users.user_id(token), self.pool.safeties)

    def test_a_record_deleted_while_it_was_opened_is_not_served(self):
        # A request that opened the record as a delete ended kept serving the deleted link
        from unittest import mock
        token = self.person()
        real_open = self.users.open

        def open_as_the_delete_ends(t):
            record = real_open(t)
            if record is not None:
                self.users.delete(t)
                self.pool.stop(t, forget=True)                    # the delete's stop, once the record is gone
            return record
        with mock.patch.object(self.users, "open", open_as_the_delete_ends):
            self.assertIsNone(self.pool.app(token))
        self.assertIsNone(self.pool.live_app(token))
        self.assertEqual(self.pool.active(), 0)


class HousekeepingTest(unittest.TestCase):
    """Public mode's housekeeping round: idle workers stop every round, the shared store's old rows go every
    EXPIRE_EVERY_S, and a failure of one skips neither the other nor the next round."""

    def test_each_part_runs_on_its_own(self):
        import sqlite3
        from subsync.__main__ import housekeeping_round
        from subsync.worker import EXPIRE_EVERY_S
        calls, fail = [], set()

        class Part:
            def __init__(self, name):
                self.name = name

            def __call__(self):
                calls.append(self.name)
                if self.name in fail:
                    raise sqlite3.OperationalError("database is locked")

        pool, store = type("P", (), {})(), type("S", (), {})()
        pool.housekeeping, store.expire = Part("reap"), Part("expire")
        t = 1_800_000_000.0
        with self.assertLogs("subsync", "WARNING") as logs:
            fail.add("reap")
            self.assertEqual(housekeeping_round(pool, store, 0.0, t), t)        # a failed reap: the store expires
            self.assertEqual(calls, ["reap", "expire"])
            fail.clear()
            self.assertEqual(housekeeping_round(pool, store, t, t + 60), t)     # not due: the workers only
            self.assertEqual(calls[2:], ["reap"])
            fail.add("expire")
            later = t + EXPIRE_EVERY_S + 60
            self.assertEqual(housekeeping_round(pool, store, t, later), later)  # tried again an hour later, as before
            self.assertEqual(calls[3:], ["reap", "expire"])
        self.assertEqual([line.split(":", 2)[2] for line in logs.output],
                         ["housekeeping failed: OperationalError", "store expiry failed: OperationalError"])


class StartupTest(unittest.TestCase):
    def run_main(self, env):
        """subsync's entry point with `env` as the whole environment: its logs at ERROR, once it has stopped as it must.
        Its logging set-up (an INFO handler on stdout) is left out, so later tests' logs stay theirs, and a
        main that went past its checks fails at once, never serves (it would serve until stopped)."""
        from unittest import mock
        from subsync import __main__ as entry
        went_on = AssertionError("main went on past its checks")
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("logging.basicConfig"), \
                mock.patch.object(entry, "main_public", side_effect=went_on), \
                mock.patch.object(entry, "Store", side_effect=went_on), \
                self.assertLogs("subsync", level="ERROR") as logs:
            with self.assertRaises(SystemExit) as e:
                entry.main()
        self.assertEqual(e.exception.code, 2)
        return "\n".join(logs.output)

    def test_main_refuses_public_mode_without_a_secret(self):
        out = self.run_main({"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://x.test"})
        self.assertIn("SUBSYNC_SECRET_KEY", out)

    def test_a_main_past_its_checks_fails_the_test_at_once(self):
        for env in ({"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://x.test", "SUBSYNC_SECRET_KEY": KEY},
                    {"SUBSYNC_SOURCE_MANIFEST": "https://aios.test/stremio/u/e/manifest.json",
                     "SUBSYNC_SOURCE_PASSWORD": "p", "SUBSYNC_PUBLIC_BASE": "http://subsync.test:8790"}):
            with self.subTest(public="SUBSYNC_PUBLIC" in env), self.assertRaisesRegex(AssertionError, "went on"):
                self.run_main(env)

    def test_main_leaves_logging_as_it_found_it(self):
        import logging
        from unittest import mock
        root = logging.getLogger()
        level = root.level
        with mock.patch.object(root, "handlers", []):          # as in a run where nothing set logging up before
            self.run_main({"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://x.test"})
            self.assertEqual(root.handlers, [])
        self.assertEqual(root.level, level)

    def test_main_refuses_more_audio_places_than_places(self):
        out = self.run_main({"SUBSYNC_PUBLIC": "on", "SUBSYNC_PUBLIC_BASE": "https://x.test", "SUBSYNC_SECRET_KEY": KEY,
                             "SUBSYNC_AUDIO_ROUTE": "on", "SUBSYNC_SYNCS_AT_ONCE": "2", "SUBSYNC_AUDIO_AT_ONCE": "3"})
        self.assertIn("SUBSYNC_AUDIO_AT_ONCE can't be more than SUBSYNC_SYNCS_AT_ONCE (2)", out)
        self.assertNotIn(KEY, out)

    def test_requirements_pin_both_platforms(self):
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "requirements.txt")
        if not os.path.exists(path):
            self.skipTest("requirements.txt is only in the source folder (the image installs from it with --require-hashes)")
        with open(path, encoding="utf-8") as f:
            text = f.read()
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
                if name in explain.PUBLIC_FIELDS:   # the Usenet box's explanation, approved as written at 136
                    self.assertLessEqual(len(text.split()), 140 if name == "usenet" else 125)

    def test_every_page_field_has_its_explanation(self):
        import re
        from subsync import explain, pages
        with open(pages.__file__, encoding="utf-8") as f:
            source = f.read()
        used = set(re.findall(r'_(?:field|check|chips)\("(\w+)"', source)) | set(re.findall(r'help="(\w+)"', source))
        used.discard("password")   # its explanation is named by help=
        self.assertEqual(used - set(explain.PUBLIC_FIELDS), set())

    def test_each_check_state_has_a_title_and_one_plain_text(self):
        import re
        from subsync import explain
        note = re.compile(r"\b(?:AD\d{1,3}|S4-\d+|P-S4-[A-Z0-9-]+)\b")
        self.assertEqual(set(explain.PUBLIC_STATES), {"sync_gone", "main_gone", "password_refused", "cannot_rebuild",
                                                      "reads_refused", "unreachable"})
        for state, (title, text, action) in explain.PUBLIC_STATES.items():
            with self.subTest(state=state):
                self.assertFalse(title.endswith("."))
                self.assertNotIn("\n", text)
                self.assertTrue(text.endswith("."), text[-30:])
                self.assertIsNone(note.search(title + text))
                self.assertLessEqual(len(text.split()), 60)
                self.assertIsInstance(action, bool)
        self.assertFalse(explain.PUBLIC_STATES["unreachable"][2])      # a note only: nothing for the person to do

    def test_no_public_text_is_written_for_one_small_server(self):
        from subsync import explain
        public = [text for name, text in explain.RULES if name.startswith("Public mode")]
        public += [text for name, (_, text) in explain.SETTINGS.items() if text.startswith("Public mode only")]
        public += list(explain.PUBLIC_FIELDS.values()) + list(explain.PUBLIC_TEXTS.values())
        for text in public:
            with self.subTest(text=text[:40]):
                self.assertNotIn("small server", text)
                self.assertNotIn("one file at a time", text)

    def test_public_rules_are_listed(self):
        from subsync import explain
        names = [name for name, _ in explain.RULES]
        for rule in ("Public mode: your AIOStreams password is used once", "Public mode: Real-Debrid is left out",
                     "Public mode: a few files at once", "Public mode: limits per person",
                     "Public mode: what is kept, and for how long"):
            with self.subTest(rule=rule):
                self.assertIn(rule, names)

    def test_the_settings_page_shows_small_reads_with_one_decimal(self):
        # A subtitle-track sync reads a few hundred KB: "0 MB read" after a sync would look like nothing was read.
        from subsync import pages
        s = Settings({"SUBSYNC_PUBLIC": "on", "SUBSYNC_SECRET_KEY": KEY, "SUBSYNC_PUBLIC_BASE": "https://x.test"})
        record = {"languages": ["eng"]}
        for nbytes, shown in ((0, " 0 MB of 10000 MB read"), (20 * 1024, " 0.1 MB of"), (351 * 1024, " 0.3 MB of"),
                              (5 * 1024 * 1024, " 5.0 MB of"), (12 * 1024 * 1024, " 12 MB of")):
            with self.subTest(nbytes=nbytes):
                html = pages.settings_page(s, record, {"files": 1, "bytes": nbytes}, "https://x.test/u/t/manifest.json")
                text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))      # the value and its unit are two spans
                self.assertRegex(text, r"(?<![\d.])" + re.escape(shown.strip()))   # the number whole, not a tail

    def test_the_privacy_text_names_cloudflare_and_the_backups_before_how_long(self):
        from subsync import explain
        parts = explain.privacy_parts(Settings({}))
        text = " ".join(f"{head}: {text}" for head, text in parts)       # the parts in order, under their headings
        added = ("AIOSubtitles runs behind Cloudflare, which passes every request on and so sees your internet address "
                 "and the links you open, your personal link included. AIOSubtitles itself keeps no log of requests. "
                 "Backups of the stored data are kept for up to 30 days.")
        self.assertIn("Logs name the titles synced, never who asked. Who else sees your requests: " + added
                      + " How long it is kept:", text)

    def test_the_privacy_text_says_what_reading_a_track_of_pictures_means(self):
        # Image subtitle tracks' words are read on the server, and such a track can take a few MB
        from subsync import explain
        self.assertIn("for each file you play, its subtitle track (about 1 MB; a few MB when the track's lines are "
                      "pictures, whose words AIOSubtitles reads on its own server and doesn't keep) and the subtitles "
                      "your AIOStreams lists", dict(explain.privacy_parts(Settings({})))["What is read"])

    def test_the_other_read_sizes_say_the_same_with_the_same_words(self):
        # The landing intro and the README's texts that give "about 1 MB" per file take the privacy sentence's
        # approved words, nothing more
        from subsync import explain
        words = ("about 1 MB; a few MB when the track's lines are pictures, whose words AIOSubtitles reads on its own "
                 "server and doesn't keep")
        self.assertIn(f"that file's own subtitle track ({words}) through a separate config",
                      explain.PUBLIC_TEXTS["intro"])
        notes = dict(explain.RULES)
        self.assertIn(f"subtitle track is {words.replace('; ', ' (', 1)}) and is synced in seconds",
                      notes["Public mode: a few files at once"])
        settings = {name: entry[-1] for name, entry in explain.SETTINGS.items()}
        self.assertIn("(about 1 MB per file, a few MB when the track's lines are pictures; up to 1 GB per file with "
                      "the audio route)", settings["SUBSYNC_DAILY_BUDGET_GB"])
        self.assertIn("Reading a file's subtitle track takes about 1 MB, a few MB when the track's lines are "
                      "pictures; 10,000 is 200 files", settings["SUBSYNC_USER_DAILY_MB"])


if __name__ == "__main__":
    unittest.main()
